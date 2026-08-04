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

// Kernel-dispatch tracing callbacks. Registered by Tracer::initialize() when
// OMNISTAT_KERNEL_TRACE is enabled: code-object loads populate the kernel-name
// map, and the LOSSLESS dispatch buffer is drained here and POSTed.

#include "trace.hpp"

#include <cxxabi.h>
#include <iostream>
#include <iterator>
#include <memory>
#include <string>

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
void kernel_code_object_callback(rocprofiler_callback_tracing_record_t record,
                          rocprofiler_user_data_t* user_data [[maybe_unused]], void* tool_data) {
    auto* tracer = static_cast<Tracer*>(tool_data);

    if (record.kind == ROCPROFILER_CALLBACK_TRACING_CODE_OBJECT &&
        record.operation == ROCPROFILER_CODE_OBJECT_DEVICE_KERNEL_SYMBOL_REGISTER) {
        auto* data =
            static_cast<rocprofiler_callback_tracing_code_object_kernel_symbol_register_data_t*>(
                record.payload);
        if (record.phase == ROCPROFILER_CALLBACK_PHASE_LOAD) {
            tracer->kernel_names.emplace(data->kernel_id, demangle(data->kernel_name));
        }
    }
}

void kernel_dispatch_callback(rocprofiler_context_id_t context [[maybe_unused]],
                          rocprofiler_buffer_id_t buffer_id [[maybe_unused]],
                          rocprofiler_record_header_t** headers, size_t num_headers,
                          void* tool_data, uint64_t drop_count [[maybe_unused]]) {
    auto* tracer = static_cast<Tracer*>(tool_data);

    if (num_headers == 0 || headers == nullptr) {
        return;
    }

    // Estimate bytes per record to reserve memory upfront. Likely
    // overestimating, but some kernel names can be very long (>700 bytes).
    constexpr size_t max_bytes_per_record = 1024;

    std::string data;
    data.reserve(num_headers * max_bytes_per_record);

    // Start JSON array
    data.push_back('[');

    size_t num_records = 0;
    for (size_t i = 0; i < num_headers; ++i) {
        auto* header = headers[i];
        if (header->category == ROCPROFILER_BUFFER_CATEGORY_TRACING &&
            header->kind == ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH) {
            auto* record =
                static_cast<rocprofiler_buffer_tracing_kernel_dispatch_record_t*>(header->payload);

            // Look up rather than .at(): a dispatch naming an agent or kernel we
            // never saw registered would otherwise throw out_of_range from a
            // rocprofiler callback thread, terminating the application. Skip the
            // record instead -- tracing must never be able to kill the app.
            auto agent = tracer->gpu_id_by_agent.find(record->dispatch_info.agent_id.handle);
            auto name = tracer->kernel_names.find(record->dispatch_info.kernel_id);
            if (agent == tracer->gpu_id_by_agent.end() || name == tracer->kernel_names.end()) {
                continue;
            }

            // Build array element: [gpu_id, "kernel_name", start_ns, end_ns]
            fmt::format_to(std::back_inserter(data), "[{},\"{}\",{},{}],",
                           agent->second, name->second,
                           record->start_timestamp, record->end_timestamp);
            ++num_records;
        }
    }

    if (num_records == 0) {
        return;
    }

    // Replace trailing comma with closing bracket
    data.back() = ']';

    if (!tracer->kernel_flush(data, num_records)) {
        std::cerr << "Omnistat: failed to post kernel trace data" << std::endl;
    }
}

} // namespace omnistat
