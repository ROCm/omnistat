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

#include <curl/curl.h>

#include <atomic>
#include <condition_variable>
#include <string>
#include <unordered_map>

namespace omnistat {

// Default periodic flush interval in seconds
constexpr unsigned int DEFAULT_FLUSH_INTERVAL_SECONDS = 30;

class KernelTracer {
  public:
    KernelTracer();

    // Methods called during rocprofiler-sdk's tool initialization and finalization
    int initialize(void* tool_data);
    void finalize();

    // Records the current timestamp whenever the flush callback is called
    void record_flush_time();

    // Members used directly by the rocprofiler-sdk tool API
    rocprofiler_context_id_t context_ = {.handle = 0};
    rocprofiler_buffer_id_t buffer_ = {};
    std::unordered_map<rocprofiler_kernel_id_t,
                       rocprofiler_callback_tracing_code_object_kernel_symbol_register_data_t>
        kernels_ = {};

  private:
    // Thread for periodic record flushing, which happens in addition to the
    // flushing triggered by full buffers
    void periodic_flush();

    const std::chrono::seconds periodic_flush_interval_;

    std::thread periodic_thread_;
    std::mutex periodic_mutex_;
    std::condition_variable periodic_cv_;
    std::atomic<bool> stop_requested_{false};
    std::atomic<std::chrono::steady_clock::time_point> last_flush_time_;
};

} // namespace omnistat
