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
      buffer_size_bytes_(parse_env_uint("OMNISTAT_TRACE_BUFFER_SIZE", DEFAULT_BUFFER_SIZE_BYTES)),
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

    client_ = std::make_unique<httplib::Client>("127.0.0.1", static_cast<int>(endpoint_port_));
    if (!client_) {
        std::cerr << "Omnistat: failed to initialize HTTP client" << std::endl;
        return -1;
    }
    client_->set_keep_alive(true);
    client_->set_tcp_nodelay(true);
    client_->set_connection_timeout(HTTP_TIMEOUT_SECONDS);
    client_->set_read_timeout(HTTP_TIMEOUT_SECONDS);
    client_->set_write_timeout(HTTP_TIMEOUT_SECONDS);

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

        const auto buffer_watermark_bytes = buffer_size_bytes_ - (buffer_size_bytes_ / 8);
        ROCPROFILER_CALL(
            rocprofiler_create_buffer(context_, buffer_size_bytes_, buffer_watermark_bytes,
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
    // thread (no kernel-dispatch join), POSTed by the flush thread on its
    // interval or when the accumulator fills.
    if (rccl_enabled_) {
        // Capture the rocprofiler half of the gpu-id mapping now. The HIP half is
        // deferred to first use in the RCCL callback: calling HIP APIs during
        // tool_init, while HIP is still starting up, silently breaks
        // kernel-dispatch tracing.
        for (const auto& agent : rocp_agents) {
            gpu_id_by_pci[pci_key_from_location(agent.domain, agent.location_id)] =
                agent.logical_node_type_id;
        }

        const auto status = rocprofiler_configure_callback_tracing_service(
            context_, ROCPROFILER_CALLBACK_TRACING_RCCL_API, nullptr, 0, rccl_api_callback, this);
        if (status != ROCPROFILER_STATUS_SUCCESS) {
            std::cerr << "Omnistat: RCCL tracing disabled ("
                      << rocprofiler_get_status_string(status) << ")" << std::endl;
            rccl_enabled_ = false;
        }
    }

    if (!kernel_enabled_ && !rccl_enabled_) {
        return -1;
    }

    int valid = 0;
    ROCPROFILER_CALL(rocprofiler_context_is_valid(context_, &valid), "check context validity");
    if (valid == 0) {
        return -1;
    }

    ROCPROFILER_CALL(rocprofiler_start_context(context_), "start context");

    record_kernel_flush_time();
    flush_thread_ = std::thread(&Tracer::flush_loop, this);

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
        std::lock_guard<std::mutex> lock(flush_mutex_);
        stop_requested_.store(true);
    }
    flush_cv_.notify_one();

    if (flush_thread_.joinable()) {
        flush_thread_.join();
    }

    // Final RCCL drain: post anything accumulated since the last flush.
    // Note this lands late in process teardown -- measurements show communicator
    // teardown rows reach a collector on the order of tens of seconds after the
    // application's last output, dominated by runtime finalization rather than
    // anything here (an RCCL-only run, with no kernel buffer to flush, behaves
    // the same). A collector must outlive the application accordingly.
    if (rccl_enabled_) {
        std::string rccl_data;
        size_t rccl_records = 0;
        rccl_drain(rccl_data, rccl_records);
        if (rccl_records > 0 && !rccl_flush(rccl_data, rccl_records)) {
            std::cerr << "Omnistat: failed to post final RCCL trace data" << std::endl;
        }
    }

    // Summary last, so it accounts for the final drain above. One line per
    // stream: a combined rate would let kernel volume mask an RCCL outage.
    if (log_enabled_) {
        log_stream_summary("kernel", kernel_stats_);
        log_stream_summary("rccl", rccl_stats_);
    }
}

bool Tracer::kernel_flush(std::string_view data, size_t num_records) {
    record_kernel_flush_time();
    return post_batch(kernel_path_, data, num_records, kernel_stats_);
}

bool Tracer::post_batch(const std::string& path, std::string_view data, size_t num_records,
                        Stats& stats) {
    const auto start = std::chrono::steady_clock::now();

    bool success = false;
    try {
        auto res = client_->Post(path, data.data(), data.size(), "application/json");
        success = res && res->status < 400;
    } catch (...) {
        if (log_enabled_) {
            std::cout << "Omnistat: exception in post_batch; trace data lost" << std::endl;
        }
    }

    stats.record_flush(num_records, success ? FlushStatus::Success : FlushStatus::Failure,
                       std::chrono::duration_cast<std::chrono::microseconds>(
                           std::chrono::steady_clock::now() - start));
    return success;
}

void Tracer::flush_loop() {
    while (true) {
        std::unique_lock<std::mutex> lock(flush_mutex_);

        // Wake on the timer, a stop, or an RCCL size request.
        flush_cv_.wait_for(lock, periodic_flush_interval_, [this] {
            return stop_requested_.load() || rccl_flush_requested_.load();
        });
        if (stop_requested_.load()) {
            break;
        }

        rccl_flush_requested_.store(false);

        auto now = std::chrono::steady_clock::now();
        auto last = std::chrono::steady_clock::time_point(
            std::chrono::steady_clock::duration(kernel_last_flush_time_.load()));
        const bool flushed_recently = (now - last) < periodic_flush_interval_;

        // The kernel buffer also flushes on its own watermark, so skip this
        // one when a flush happened recently.
        if (kernel_enabled_ && !flushed_recently) {
            auto flush_status = rocprofiler_flush_buffer(kernel_buffer_);

            // Ignore BUFFER_BUSY errors as the buffer might be in use
            if (flush_status != ROCPROFILER_STATUS_SUCCESS &&
                flush_status != ROCPROFILER_STATUS_ERROR_BUFFER_BUSY) {
                std::cerr << "Omnistat: kernel buffer flush failed with status "
                          << flush_status << std::endl;
            }
        }

        // Drained on every wake, timer or size trigger, and never debounced.
        if (rccl_enabled_) {
            std::string rccl_data;
            size_t rccl_records = 0;
            rccl_drain(rccl_data, rccl_records);
            if (rccl_records > 0 && !rccl_flush(rccl_data, rccl_records)) {
                std::cerr << "Omnistat: failed to post RCCL trace data" << std::endl;
            }
        }
    }
}

void Tracer::rccl_add_collective(std::string_view element) {
    bool over_threshold = false;
    {
        std::lock_guard<std::mutex> lock(rccl_mutex_);
        if (rccl_collectives_count_ > 0) {
            rccl_collectives_buffer_.push_back(',');
        }
        rccl_collectives_buffer_.append(element);
        ++rccl_collectives_count_;
        over_threshold = rccl_pending_bytes() >= buffer_size_bytes_;
    }
    if (over_threshold) {
        request_rccl_flush();
    }
}

void Tracer::rccl_add_comm(std::string_view element) {
    bool over_threshold = false;
    {
        std::lock_guard<std::mutex> lock(rccl_mutex_);
        if (rccl_comms_count_ > 0) {
            rccl_comms_buffer_.push_back(',');
        }
        rccl_comms_buffer_.append(element);
        ++rccl_comms_count_;
        over_threshold = rccl_pending_bytes() >= buffer_size_bytes_;
    }
    if (over_threshold) {
        request_rccl_flush();
    }
}

size_t Tracer::rccl_pending_bytes() const {
    return rccl_collectives_buffer_.size() + rccl_comms_buffer_.size();
}

void Tracer::request_rccl_flush() {
    // Only the thread that raises the flag notifies, so a burst of appends past
    // the threshold does not repeat the wake. A notify lost between the waiter's
    // predicate check and its block just defers the drain to the next tick,
    // which is the pre-existing behaviour.
    if (!rccl_flush_requested_.exchange(true)) {
        flush_cv_.notify_one();
    }
}

void Tracer::rccl_drain(std::string& out, size_t& num_records) {
    std::string collectives, comms;
    {
        // Swap under the lock, build outside: the RCCL callback runs on the
        // application's own collective threads.
        std::lock_guard<std::mutex> lock(rccl_mutex_);
        num_records = rccl_collectives_count_ + rccl_comms_count_;
        if (num_records == 0) {
            return;
        }
        collectives.swap(rccl_collectives_buffer_);
        comms.swap(rccl_comms_buffer_);
        rccl_collectives_buffer_.clear();
        rccl_comms_buffer_.clear();
        rccl_collectives_count_ = 0;
        rccl_comms_count_ = 0;
    }

    // Build: {"collectives":[...],"comms":[...]}
    out.reserve(collectives.size() + comms.size() + 64);
    out.append("{\"collectives\":[");
    out.append(collectives);
    out.append("],\"comms\":[");
    out.append(comms);
    out.append("]}");
}

bool Tracer::rccl_flush(std::string_view data, size_t num_records) {
    return post_batch(rccl_path_, data, num_records, rccl_stats_);
}

void Tracer::report_callback_error(const char* where, const std::exception& error) {
    if (log_enabled_) {
        std::cerr << "Omnistat: exception in " << where << " (" << error.what()
                  << "); trace data lost" << std::endl;
    }
}

void Tracer::record_kernel_flush_time() {
    kernel_last_flush_time_.store(std::chrono::steady_clock::now().time_since_epoch().count());
}

void Tracer::Stats::record_flush(size_t num_records, FlushStatus status,
                                 std::chrono::microseconds latency) {
    const uint64_t latency_us = latency.count();

    total_flushes.fetch_add(1, std::memory_order_relaxed);
    total_records.fetch_add(num_records, std::memory_order_relaxed);
    total_latency_us.fetch_add(latency_us, std::memory_order_relaxed);

    auto observed_max = max_latency_us.load(std::memory_order_relaxed);
    while (latency_us > observed_max &&
           !max_latency_us.compare_exchange_weak(observed_max, latency_us,
                                                 std::memory_order_relaxed)) {
    }

    if (status == FlushStatus::Failure) {
        failed_flushes.fetch_add(1, std::memory_order_relaxed);
        failed_records.fetch_add(num_records, std::memory_order_relaxed);
    }
}

void Tracer::log_stream_summary(const char* stream, const Stats& stats) const {
    // Snapshot once: the callback thread may still be flushing, so reading a
    // counter twice would print inconsistent totals.
    const uint64_t total_flushes = stats.total_flushes.load();
    if (total_flushes == 0) {
        return;
    }
    const uint64_t total_records = stats.total_records.load();
    const uint64_t failed_flushes = stats.failed_flushes.load();
    const uint64_t failed_records = stats.failed_records.load();
    const uint64_t total_latency_us = stats.total_latency_us.load();
    const uint64_t max_latency_us = stats.max_latency_us.load();

    char hostname[256];
    gethostname(hostname, sizeof(hostname));

    std::cout << "[" << hostname << "][" << getpid() << "][omnistat] Trace summary (" << stream
              << "): " << (total_records - failed_records) << "/" << total_records << " records, "
              << (total_flushes - failed_flushes) << "/" << total_flushes << " flushes, POST avg "
              << (total_latency_us / total_flushes) / 1000.0 << "ms max " << max_latency_us / 1000.0
              << "ms" << std::endl;
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
