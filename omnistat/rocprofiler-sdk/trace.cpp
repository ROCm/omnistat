// ---------------------------------------------------------------------------
// MIT License
//
// Copyright (c) 2025 Advanced Micro Devices, Inc. All Rights Reserved.
//
// Permission is hereby granted, free of charge, to any person obtaining a
// copy of this software and associated documentation files (the "Software"),
// to deal in the Software without restriction, including without limitation
// the rights to use, copy, modify, merge, publish, distribute, sublicense,
// and/or sell copies of the Software, and to permit persons to whom the
// Software is furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
// FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
// DEALINGS IN THE SOFTWARE.
// ---------------------------------------------------------------------------

#include "trace.hpp"
#include "common.hpp"

#include <rocprofiler-sdk/registration.h>
#include <rocprofiler-sdk/version.h>

#ifndef ROCPROFILER_SDK_VERSION
#define ROCPROFILER_SDK_VERSION ROCPROFILER_VERSION
#endif

#include <chrono>
#include <memory>
#include <stdexcept>
#include <thread>
#include <unistd.h>

namespace omnistat {

Tracer::Tracer()
    : periodic_flush_interval_(std::chrono::seconds(
          parse_env_uint("OMNISTAT_TRACE_MAX_INTERVAL", DEFAULT_FLUSH_INTERVAL_SECONDS))),
      kernel_buffer_size_bytes_(parse_env_uint("OMNISTAT_TRACE_BUFFER_SIZE", DEFAULT_BUFFER_SIZE_BYTES)),
      endpoint_port_(parse_env_uint("OMNISTAT_TRACE_ENDPOINT_PORT", DEFAULT_TRACE_ENDPOINT_PORT)),
      log_enabled_(parse_env_uint("OMNISTAT_TRACE_LOG", 0) != 0) {
    kernel_enabled_ = parse_env_bool("OMNISTAT_KERNEL_TRACE", true);
    rccl_enabled_ = parse_env_bool("OMNISTAT_RCCL_TRACE", kernel_enabled_);
}

int Tracer::initialize() {
    // Nothing to do if both streams are disabled.
    if (!kernel_enabled_ && !rccl_enabled_) {
        return 0;
    }

    client_ = std::make_unique<httplib::Client>("localhost", static_cast<int>(endpoint_port_));
    if (!client_) {
        std::cerr << "Omnistat: failed to initialize HTTP client" << std::endl;
        return -1;
    }
    client_->set_keep_alive(true);

    ROCPROFILER_CALL(rocprofiler_create_context(&context_), "create context");

    // One agent enumeration feeds both streams. They cannot share a lookup:
    // kernel dispatch records carry an agent handle, while RCCL API records
    // carry no device at all and must be resolved via HIP.
    const auto rocp_agents = omnistat::get_rocprofiler_agents();

    // Kernel-dispatch tracing: code-object tracking (kernel names), the agent
    // map (gpu attribution), and the LOSSLESS dispatch buffer. Only set up when
    // enabled — an RCCL-only job pays none of this.
    if (kernel_enabled_) {
        for (const auto& agent : rocp_agents) {
            gpu_id_by_agent[agent.id.handle] = agent.logical_node_type_id;
        }

        auto code_object_ops = std::vector<rocprofiler_tracing_operation_t>{
            ROCPROFILER_CODE_OBJECT_DEVICE_KERNEL_SYMBOL_REGISTER};
        ROCPROFILER_CALL(
            rocprofiler_configure_callback_tracing_service(
                context_, ROCPROFILER_CALLBACK_TRACING_CODE_OBJECT, code_object_ops.data(),
                code_object_ops.size(), kernel_code_object_callback, this),
            "configure code object tracing service");

        const auto buffer_watermark_bytes = kernel_buffer_size_bytes_ - (kernel_buffer_size_bytes_ / 8);
        ROCPROFILER_CALL(
            rocprofiler_create_buffer(context_, kernel_buffer_size_bytes_, buffer_watermark_bytes,
                                      ROCPROFILER_BUFFER_POLICY_LOSSLESS, kernel_dispatch_callback, this,
                                      &kernel_buffer_),
            "create buffer");

        ROCPROFILER_CALL(rocprofiler_configure_buffer_tracing_service(
                             context_, ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH, nullptr, 0,
                             kernel_buffer_),
                         "configure buffer tracing service for kernel dispatches");

        auto thread = rocprofiler_callback_thread_t{};
        ROCPROFILER_CALL(rocprofiler_create_callback_thread(&thread), "create thread");
        ROCPROFILER_CALL(rocprofiler_assign_callback_thread(kernel_buffer_, thread),
                         "assign thread for buffer");
    }

    // RCCL API tracing: enumerates collectives + comm lifecycle on the app
    // thread (no kernel-dispatch join), POSTed on the periodic cadence.
    if (rccl_enabled_) {
        // Capture the rocprofiler half of the gpu-id mapping now. The HIP half is
        // deferred to first use in the RCCL callback: calling HIP APIs during
        // tool_init, while HIP is still starting up, silently breaks
        // kernel-dispatch tracing.
        for (const auto& agent : rocp_agents) {
            gpu_id_by_pci[pci_key_from_location(agent.domain, agent.location_id)] =
                agent.logical_node_type_id;
        }

        ROCPROFILER_CALL(
            rocprofiler_configure_callback_tracing_service(
                context_, ROCPROFILER_CALLBACK_TRACING_RCCL_API, nullptr, 0, rccl_api_callback,
                this),
            "configure RCCL API tracing service");
    }

    int valid = 0;
    ROCPROFILER_CALL(rocprofiler_context_is_valid(context_, &valid), "check context validity");
    if (valid == 0) {
        return -1;
    }

    ROCPROFILER_CALL(rocprofiler_start_context(context_), "start context");

    record_flush_time();
    periodic_thread_ = std::thread(&Tracer::periodic_flush, this);

    return 0;
}

Tracer::~Tracer() {
    // Flush -> stop -> flush, mirroring rocprofv3's finalization sequence. The
    // buffer only exists when kernel tracing was enabled; the context only when
    // at least one stream was enabled. Guard accordingly.
    if (kernel_enabled_) {
        rocprofiler_flush_buffer(kernel_buffer_);
    }
    if (kernel_enabled_ || rccl_enabled_) {
        rocprofiler_stop_context(context_);
    }

    if (kernel_enabled_) {
        rocprofiler_flush_buffer(kernel_buffer_);
    }

    {
        std::lock_guard<std::mutex> lock(periodic_mutex_);
        stop_requested_.store(true);
    }
    periodic_cv_.notify_one();

    if (periodic_thread_.joinable()) {
        periodic_thread_.join();

        if (log_enabled_) {
            char hostname[256];
            gethostname(hostname, sizeof(hostname));

            auto successful_records = total_records_ - failed_records_;
            auto successful_flushes = total_flushes_ - failed_flushes_;
            std::cout << "[" << hostname << "][" << getpid()
                      << "][omnistat] Trace summary: " << successful_records << "/"
                      << total_records_ << " processed records (" << successful_flushes << "/"
                      << total_flushes_ << " successful flushes)" << std::endl;
        }
    }

    // Final RCCL drain: post anything accumulated since the last periodic flush.
    // Note this lands late in process teardown -- measurements show communicator
    // teardown rows reach a collector on the order of tens of seconds after the
    // application's last output, dominated by runtime finalization rather than
    // anything here (an RCCL-only run, with no kernel buffer to flush, behaves
    // the same). A collector must outlive the application accordingly.
    if (rccl_enabled_) {
        std::string rccl_data;
        size_t rccl_n = 0;
        rccl_drain(rccl_data, rccl_n);
        if (rccl_n > 0) {
            rccl_flush(rccl_data, rccl_n);
        }
    }
}

bool Tracer::kernel_flush(std::string_view data, size_t num_records) {
    record_flush_time();

    auto res = client_->Post(kernel_path_, std::string(data), "application/json");
    bool success = res && res->status < 400;

    record_flush_stats(num_records, !success);
    return success;
}

void Tracer::periodic_flush() {
    while (true) {
        std::unique_lock<std::mutex> lock(periodic_mutex_);

        // wait_for returns false on timeout, true if predicate returns true
        bool stop_signaled = periodic_cv_.wait_for(lock, periodic_flush_interval_,
                                                   [this] { return stop_requested_.load(); });
        if (stop_signaled) {
            break;
        }

        auto now = std::chrono::steady_clock::now();
        auto last = std::chrono::steady_clock::time_point(
            std::chrono::steady_clock::duration(last_flush_time_.load()));
        if ((now - last) < periodic_flush_interval_) {
            continue;
        }

        // Timeout occurred, perform periodic flush. The kernel-dispatch buffer
        // only exists when kernel tracing is enabled.
        if (kernel_enabled_) {
            auto flush_status = rocprofiler_flush_buffer(kernel_buffer_);

            // Ignore BUFFER_BUSY errors as the buffer might be in use
            if (flush_status != ROCPROFILER_STATUS_SUCCESS &&
                flush_status != ROCPROFILER_STATUS_ERROR_BUFFER_BUSY) {
                std::cerr << "Warning: periodic buffer flush failed with status " << flush_status
                          << std::endl;
            }
        }

        // Drain + POST the accumulated RCCL streams on the same cadence. The RCCL
        // callback fills these synchronously on the app threads; this thread owns
        // their flush (there is no rocprofiler buffer backing them).
        if (rccl_enabled_) {
            std::string rccl_data;
            size_t rccl_n = 0;
            rccl_drain(rccl_data, rccl_n);
            if (rccl_n > 0 && !rccl_flush(rccl_data, rccl_n)) {
                std::cerr << "Omnistat: failed to post RCCL trace data" << std::endl;
            }
        }
    }
}

void Tracer::rccl_add_collective(std::string_view element) {
    std::lock_guard<std::mutex> lock(rccl_mutex_);
    if (rccl_collectives_count_ > 0) {
        rccl_collectives_buffer_.push_back(',');
    }
    rccl_collectives_buffer_.append(element);
    ++rccl_collectives_count_;
}

void Tracer::rccl_add_comm(std::string_view element) {
    std::lock_guard<std::mutex> lock(rccl_mutex_);
    if (rccl_comms_count_ > 0) {
        rccl_comms_buffer_.push_back(',');
    }
    rccl_comms_buffer_.append(element);
    ++rccl_comms_count_;
}

void Tracer::rccl_drain(std::string& out, size_t& num_records) {
    std::lock_guard<std::mutex> lock(rccl_mutex_);
    num_records = rccl_collectives_count_ + rccl_comms_count_;
    if (num_records == 0) {
        return;
    }
    // Build: {"collectives":[...],"comms":[...]}
    out.reserve(rccl_collectives_buffer_.size() + rccl_comms_buffer_.size() + 64);
    out.append("{\"collectives\":[");
    out.append(rccl_collectives_buffer_);
    out.append("],\"comms\":[");
    out.append(rccl_comms_buffer_);
    out.append("]}");
    rccl_collectives_buffer_.clear();
    rccl_comms_buffer_.clear();
    rccl_collectives_count_ = 0;
    rccl_comms_count_ = 0;
}

bool Tracer::rccl_flush(std::string_view data, size_t num_records) {
    // Deliberately does NOT call record_flush_time(). That timestamp debounces
    // the periodic tick so it won't redundantly flush the kernel buffer right
    // after a watermark-triggered flush. RCCL has no watermark trigger and the
    // periodic thread is its only flusher, so stamping the time here would make
    // the next tick's "flushed recently?" test skip -- draining RCCL every other
    // tick instead of every one.
    auto res = client_->Post(rccl_path_, std::string(data), "application/json");
    bool success = res && res->status < 400;
    record_flush_stats(num_records, !success);
    return success;
}

void Tracer::report_callback_error(const char* where, const std::exception& error) {
    if (log_enabled_) {
        std::cerr << "Omnistat: exception in " << where << " (" << error.what()
                  << "); trace data lost" << std::endl;
    }
}

void Tracer::record_flush_time() {
    last_flush_time_.store(std::chrono::steady_clock::now().time_since_epoch().count());
}

void Tracer::record_flush_stats(size_t num_records, bool failed) {
    total_flushes_.fetch_add(1, std::memory_order_relaxed);
    total_records_.fetch_add(num_records, std::memory_order_relaxed);
    if (failed) {
        failed_flushes_.fetch_add(1, std::memory_order_relaxed);
        failed_records_.fetch_add(num_records, std::memory_order_relaxed);
    }
}

} // namespace omnistat

// ------------------------------------------------------------------------------------------------
// ROCProfiler SDK tool initialization
// ------------------------------------------------------------------------------------------------

int tool_init(rocprofiler_client_finalize_t fini_func [[maybe_unused]], void* tool_data) {
    try {
        auto* tracer = static_cast<omnistat::Tracer*>(tool_data);
        return tracer->initialize();
    } catch (const std::exception& e) {
        std::cerr << "Omnistat: tracing disabled (initialization failure)" << std::endl;
        return -1;
    }
}

void tool_fini(void* tool_data) {
    auto* tracer = static_cast<omnistat::Tracer*>(tool_data);
    delete tracer;
}

extern "C" rocprofiler_tool_configure_result_t*
rocprofiler_configure(uint32_t version, const char* runtime_version,
                      uint32_t priority [[maybe_unused]], rocprofiler_client_id_t* id) {
    constexpr uint32_t compiled_version = ROCPROFILER_SDK_VERSION;

    if (version / 10000 != compiled_version / 10000) {
        std::cerr << "Omnistat: tracing disabled (version mismatch, compiled against "
                  << compiled_version / 10000 << "." << (compiled_version % 10000) / 100 << "."
                  << compiled_version % 100 << " but runtime is "
                  << (runtime_version ? runtime_version : "unknown") << ")" << std::endl;
        return nullptr;
    }

    id->name = "omnistat-trace";

    auto* tracer = new omnistat::Tracer();

    static auto cfg = rocprofiler_tool_configure_result_t{
        sizeof(rocprofiler_tool_configure_result_t), &tool_init, &tool_fini, tracer};

    return &cfg;
}
