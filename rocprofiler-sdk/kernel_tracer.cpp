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

#include "kernel_tracer.hpp"
#include "common.hpp"

#include <chrono>
#include <cxxabi.h>
#include <fstream>
#include <iterator>
#include <memory>
#include <thread>

#if defined(HAS_STD_FORMAT)
#include <format>
namespace fmt = std;
#else
#include <fmt/core.h>
#endif

namespace omnistat {

// Demangle kernel names
static std::string demangle(const char* mangled_name) {
    int status = -1;
    std::unique_ptr<char, void (*)(void*)> result(
        abi::__cxa_demangle(mangled_name, nullptr, nullptr, &status), std::free);
    return (status == 0) ? result.get() : mangled_name;
}

// Callback used to register kernels when loading code objects. Forces a flush
// on every kernel unload; the expectation is that only happens at the end of
// the application and it's only triggered once for the first kernel unload.
void code_object_callback(rocprofiler_callback_tracing_record_t record,
                          rocprofiler_user_data_t* user_data [[maybe_unused]],
                          void* tool_data) {
    auto* tracer = static_cast<KernelTracer*>(tool_data);

    if (record.kind == ROCPROFILER_CALLBACK_TRACING_CODE_OBJECT &&
        record.operation == ROCPROFILER_CODE_OBJECT_LOAD) {
        if (record.phase == ROCPROFILER_CALLBACK_PHASE_UNLOAD) {
            // Never reached when using the tool with the ROCP_TOOL_LIBRARIES
            // environment variable, hence the need to flush on kernel unload.
            auto flush_status = rocprofiler_flush_buffer(tracer->buffer);
            if (flush_status != ROCPROFILER_STATUS_ERROR_BUFFER_BUSY)
                ROCPROFILER_CALL(flush_status, "flush buffer");
        }
    } else if (record.kind == ROCPROFILER_CALLBACK_TRACING_CODE_OBJECT &&
               record.operation == ROCPROFILER_CODE_OBJECT_DEVICE_KERNEL_SYMBOL_REGISTER) {
        auto* data =
            static_cast<rocprofiler_callback_tracing_code_object_kernel_symbol_register_data_t*>(
                record.payload);
        if (record.phase == ROCPROFILER_CALLBACK_PHASE_LOAD) {
            tracer->kernels.emplace(data->kernel_id, demangle(data->kernel_name));
        } else if (record.phase == ROCPROFILER_CALLBACK_PHASE_UNLOAD) {
            ROCPROFILER_CALL(rocprofiler_flush_buffer(tracer->buffer), "flush buffer");
            tracer->kernels.erase(data->kernel_id);
        }
    }
}

void full_buffer_callback(rocprofiler_context_id_t context [[maybe_unused]],
                          rocprofiler_buffer_id_t buffer_id [[maybe_unused]],
                          rocprofiler_record_header_t** headers, size_t num_headers,
                          void* tool_data, uint64_t drop_count [[maybe_unused]]) {
    auto* tracer = static_cast<KernelTracer*>(tool_data);

    if (num_headers == 0) {
        throw std::runtime_error{
            "rocprofiler invoked a buffer callback with no headers. this should never happen"};
    } else if (headers == nullptr) {
        throw std::runtime_error{"rocprofiler invoked a buffer callback with a null pointer to the "
                                 "array of headers. this should never happen"};
    }

    // Estimate bytes per record to reserve memory upfront. Likely
    // overestimating, but some kernel names can be very long (>700 bytes).
    constexpr size_t max_bytes_per_record = 1024;

    std::string data;
    data.reserve(num_headers * max_bytes_per_record);
    for (size_t i = 0; i < num_headers; ++i) {
        auto* header = headers[i];

        if (header->category == ROCPROFILER_BUFFER_CATEGORY_TRACING &&
            header->kind == ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH) {
            auto* record =
                static_cast<rocprofiler_buffer_tracing_kernel_dispatch_record_t*>(header->payload);
            fmt::format_to(std::back_inserter(data), "{},\"{}\",{},{}\n",
                           tracer->agents.at(record->dispatch_info.agent_id.handle),
                           tracer->kernels.at(record->dispatch_info.kernel_id),
                           record->start_timestamp, record->end_timestamp);
        } else {
            throw std::runtime_error{
                fmt::format("unexpected rocprofiler_record_header_t category + kind: ({} + {})",
                            header->category, header->kind)};
        }
    }

    if (!tracer->flush(data, num_headers)) {
        std::cerr << "Omnistat: failed to post kernel trace data" << std::endl;
    }
}

KernelTracer::KernelTracer()
    : periodic_flush_interval_(std::chrono::seconds(
          parse_env_uint("OMNISTAT_TRACE_MAX_INTERVAL", DEFAULT_FLUSH_INTERVAL_SECONDS))),
      buffer_size_bytes_(parse_env_uint("OMNISTAT_TRACE_BUFFER_SIZE", DEFAULT_BUFFER_SIZE_BYTES)) {
}

int KernelTracer::initialize() {
    http_client_ = std::make_unique<httplib::Client>("localhost", DEFAULT_TRACE_ENDPOINT_PORT);
    http_client_->set_connection_timeout(5, 0);
    http_client_->set_read_timeout(5, 0);
    http_client_->set_write_timeout(5, 0);

    agents = omnistat::build_agent_map();

    ROCPROFILER_CALL(rocprofiler_create_context(&context_), "create context");

    auto code_object_ops = std::vector<rocprofiler_tracing_operation_t>{
        ROCPROFILER_CODE_OBJECT_DEVICE_KERNEL_SYMBOL_REGISTER};

    ROCPROFILER_CALL(rocprofiler_configure_callback_tracing_service(
                         context_, ROCPROFILER_CALLBACK_TRACING_CODE_OBJECT, code_object_ops.data(),
                         code_object_ops.size(), code_object_callback, this),
                     "configure code object tracing service");

    const auto buffer_watermark_bytes = buffer_size_bytes_ - (buffer_size_bytes_ / 8);

    ROCPROFILER_CALL(rocprofiler_create_buffer(context_, buffer_size_bytes_, buffer_watermark_bytes,
                                               ROCPROFILER_BUFFER_POLICY_LOSSLESS,
                                               full_buffer_callback, this, &buffer),
                     "create buffer");

    ROCPROFILER_CALL(rocprofiler_configure_buffer_tracing_service(
                         context_, ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH, nullptr, 0, buffer),
                     "configure buffer tracing service for kernel dispatches");

    auto thread = rocprofiler_callback_thread_t{};
    ROCPROFILER_CALL(rocprofiler_create_callback_thread(&thread), "create thread");

    ROCPROFILER_CALL(rocprofiler_assign_callback_thread(buffer, thread),
                     "assign thread for buffer");

    int valid = 0;
    ROCPROFILER_CALL(rocprofiler_context_is_valid(context_, &valid), "check context validity");
    if (valid == 0) {
        return -1;
    }

    ROCPROFILER_CALL(rocprofiler_start_context(context_), "start context");

    record_flush_time();
    periodic_thread_ = std::thread(&KernelTracer::periodic_flush, this);

    return 0;
}

KernelTracer::~KernelTracer() {
    {
        std::lock_guard<std::mutex> lock(periodic_mutex_);
        stop_requested_.store(true);
    }
    periodic_cv_.notify_one();

    if (periodic_thread_.joinable()) {
        periodic_thread_.join();

        auto successful_records = total_records_ - failed_records_;
        auto successful_flushes = total_flushes_ - failed_flushes_;
        std::cout << "Omnistat trace summary: " << successful_records << "/" << total_records_
                  << " processed records (" << successful_flushes << "/" << total_flushes_
                  << " successful flushes)" << std::endl;
    }

    http_client_.reset();
}

bool KernelTracer::flush(std::string_view data, size_t num_records) {
    record_flush_time();
    auto res = http_client_->Post("/kernel_trace", data.data(), data.size(), "text/plain");
    bool success = res && res->status < 400;
    record_flush_stats(num_records, !success);
    return success;
}

void KernelTracer::periodic_flush() {
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

        // Timeout occurred, perform periodic flush
        auto flush_status = rocprofiler_flush_buffer(buffer);

        // Ignore BUFFER_BUSY errors as the buffer might be in use
        if (flush_status != ROCPROFILER_STATUS_SUCCESS &&
            flush_status != ROCPROFILER_STATUS_ERROR_BUFFER_BUSY) {
            std::cerr << "Warning: periodic buffer flush failed with status " << flush_status
                      << std::endl;
        }
    }
}

void KernelTracer::record_flush_time() {
    last_flush_time_.store(std::chrono::steady_clock::now().time_since_epoch().count());
}

void KernelTracer::record_flush_stats(size_t num_headers, bool failed) {
    total_flushes_.fetch_add(1, std::memory_order_relaxed);
    total_records_.fetch_add(num_headers, std::memory_order_relaxed);
    if (failed) {
        failed_flushes_.fetch_add(1, std::memory_order_relaxed);
        failed_records_.fetch_add(num_headers, std::memory_order_relaxed);
    }
}

} // namespace omnistat

// ------------------------------------------------------------------------------------------------
// ROCProfiler SDK tool initialization
// ------------------------------------------------------------------------------------------------

int tool_init(rocprofiler_client_finalize_t fini_func [[maybe_unused]], void* tool_data) {
    auto* tracer = static_cast<omnistat::KernelTracer*>(tool_data);
    return tracer->initialize();
}

void tool_fini(void* tool_data) {
    auto* tracer = static_cast<omnistat::KernelTracer*>(tool_data);
    delete tracer;
}

extern "C" rocprofiler_tool_configure_result_t*
rocprofiler_configure(uint32_t version [[maybe_unused]], const char* runtime_version [[maybe_unused]],
                      uint32_t priority [[maybe_unused]], rocprofiler_client_id_t* id) {
    id->name = "omnistat-kernel-trace";

    auto* tracer = new omnistat::KernelTracer();

    static auto cfg = rocprofiler_tool_configure_result_t{
        sizeof(rocprofiler_tool_configure_result_t), &tool_init, &tool_fini, tracer};

    return &cfg;
}
