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

#pragma once

#include <rocprofiler-sdk/rocprofiler.h>

#include <httplib.h>

#include <atomic>
#include <condition_variable>
#include <memory>
#include <string>
#include <string_view>
#include <unordered_map>

namespace omnistat {

// Default periodic flush interval in seconds
constexpr uint64_t DEFAULT_FLUSH_INTERVAL_SECONDS = 30;

// Default buffer size in bytes
constexpr uint64_t DEFAULT_BUFFER_SIZE_BYTES = 262144;

// Endpoint port for sending kernel trace data
constexpr uint64_t DEFAULT_TRACE_ENDPOINT_PORT = 8001;

class KernelTracer {
  public:
    KernelTracer();
    ~KernelTracer();

    // Method called during rocprofiler-sdk's tool initialization
    int initialize();

    // Sends kernel trace data to the HTTP endpoint and records flush stats.
    bool flush(std::string_view data, size_t num_records);

    // Members used directly by the rocprofiler-sdk tool callbacks
    rocprofiler_buffer_id_t buffer = {};
    std::unordered_map<rocprofiler_kernel_id_t, std::string> kernels = {};
    std::unordered_map<uint64_t, uint32_t> agents = {};

  private:
    // Thread for periodic record flushing, which happens in addition to the
    // flushing triggered by full buffers
    void periodic_flush();

    // Internal helpers for flush()
    void record_flush_time();
    void record_flush_stats(size_t num_records, bool failed);

    rocprofiler_context_id_t context_ = {.handle = 0};

    const std::chrono::seconds periodic_flush_interval_;
    const uint64_t buffer_size_bytes_;

    std::thread periodic_thread_;
    std::mutex periodic_mutex_;
    std::condition_variable periodic_cv_;
    std::atomic<bool> stop_requested_{false};
    std::atomic<std::chrono::steady_clock::rep> last_flush_time_;

    // Counters for summary statistics
    std::atomic<uint64_t> total_flushes_{0};
    std::atomic<uint64_t> total_records_{0};
    std::atomic<uint64_t> failed_flushes_{0};
    std::atomic<uint64_t> failed_records_{0};

    // HTTP client for sending trace data
    std::unique_ptr<httplib::Client> http_client_;
};

} // namespace omnistat
