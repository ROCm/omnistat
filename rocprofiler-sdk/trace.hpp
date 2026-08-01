// ---------------------------------------------------------------------------
// MIT License
//
// Copyright (c) 2025 - 2026 Advanced Micro Devices, Inc. All Rights Reserved.
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

#pragma once

#include <rocprofiler-sdk/rocprofiler.h>

#include <httplib.h>

#include <atomic>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <unordered_map>

namespace omnistat {

// Default periodic flush interval in seconds, shared by both streams
constexpr uint64_t DEFAULT_FLUSH_INTERVAL_SECONDS = 13;

// Default size in bytes of the kernel-dispatch buffer
constexpr uint64_t DEFAULT_BUFFER_SIZE_BYTES = 262144;

// Endpoint port for sending trace data (both streams)
constexpr uint64_t DEFAULT_TRACE_ENDPOINT_PORT = 8001;

class Tracer {
  public:
    Tracer();
    ~Tracer();

    // Method called during rocprofiler-sdk's tool initialization
    int initialize();

    // Kernel-dispatch stream: the code-object callback fills the kernel-name
    // map; the dispatch callback reads it plus the agent map (gpu attribution)
    // to format records, then POSTs them via kernel_flush().
    std::unordered_map<rocprofiler_kernel_id_t, std::string> kernel_names = {};
    std::unordered_map<uint64_t, uint32_t> agents = {};

    bool kernel_flush(std::string_view data, size_t num_records);

    // RCCL stream: append one positional-array element to the collectives /
    // comms accumulators. Called by the RCCL-API callback on the app's threads
    // and drained by the periodic flush thread, so a single mutex guards both.
    void rccl_add_collective(std::string_view element);
    void rccl_add_comm(std::string_view element);

  private:
    // Thread for periodic record flushing, which happens in addition to the
    // flushing triggered by full buffers
    void periodic_flush();

    // Swap out the accumulated RCCL streams into a ready-to-POST JSON body and
    // reset. Returns total element count via num_records.
    void rccl_drain(std::string& out, size_t& num_records);

    // Sends RCCL trace data (collectives + comm lifecycle) to /rccl_trace.
    bool rccl_flush(std::string_view data, size_t num_records);

    // Internal helpers shared by both flush paths
    void record_flush_time();
    void record_flush_stats(size_t num_records, bool failed);

    // HTTP client and endpoint paths for sending trace data. The same client
    // (localhost:port, keep-alive) serves both the kernel-dispatch stream and
    // the RCCL stream, which target different endpoint paths.
    std::unique_ptr<httplib::Client> client_;
    const uint64_t endpoint_port_;
    const bool log_enabled_;

    std::string kernel_path_ = "/kernel_trace";
    std::string rccl_path_ = "/rccl_trace";

    bool kernel_enabled_ = true;
    bool rccl_enabled_ = true;

    rocprofiler_context_id_t context_ = {.handle = 0};

    // Kernel-dispatch state
    rocprofiler_buffer_id_t kernel_buffer_ = {};
    const uint64_t kernel_buffer_size_bytes_;

    // RCCL accumulators (guarded by rccl_mutex_)
    std::mutex rccl_mutex_;
    std::string rccl_collectives_buffer_;  // comma-joined [gpu,op,count,dtype,comm,ts]
    std::string rccl_comms_buffer_;        // comma-joined [gpu,op,comm,nranks,h_start,h_end]
    size_t rccl_collectives_count_ = 0;
    size_t rccl_comms_count_ = 0;

    // Periodic flush: drains both streams on a timer. Backstops the kernel
    // buffer watermark; for RCCL it is the only trigger. last_flush_time_
    // debounces, so a watermark-driven flush skips the next scheduled drain.
    const std::chrono::seconds periodic_flush_interval_;
    std::thread periodic_thread_;
    std::mutex periodic_mutex_;
    std::condition_variable periodic_cv_;
    std::atomic<bool> stop_requested_{false};
    std::atomic<std::chrono::steady_clock::rep> last_flush_time_;

    // Flush statistics for the exit summary, aggregated across both streams
    std::atomic<uint64_t> total_flushes_{0};
    std::atomic<uint64_t> total_records_{0};
    std::atomic<uint64_t> failed_flushes_{0};
    std::atomic<uint64_t> failed_records_{0};
};

// Free-standing rocprofiler-sdk callbacks, declared here so initialize() can
// reference them across translation units.
void kernel_code_object_callback(rocprofiler_callback_tracing_record_t record,
                                 rocprofiler_user_data_t* user_data, void* tool_data);
void kernel_dispatch_callback(rocprofiler_context_id_t context, rocprofiler_buffer_id_t buffer_id,
                              rocprofiler_record_header_t** headers, size_t num_headers,
                              void* tool_data, uint64_t drop_count);
void rccl_api_callback(rocprofiler_callback_tracing_record_t record,
                       rocprofiler_user_data_t* user_data, void* tool_data);

} // namespace omnistat
