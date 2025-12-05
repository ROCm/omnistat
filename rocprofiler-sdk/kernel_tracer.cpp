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

#include <fstream>

namespace omnistat {

// Global variable to keep track of kernel tracing, necessary so that all
// callbacks have access to tracing data.
static KernelTracer tracer;

// Map per-process agent IDs to GPU node IDs.
static std::unordered_map<uint64_t, uint32_t> agent_map = {};

// Callback used to register kernels when loading code objects. Forces a flush
// on every kernel unload; the expectation is that only happens at the end of
// the application and it's only triggered once for the first kernel unload.
void code_object_callback(rocprofiler_callback_tracing_record_t record,
                          rocprofiler_user_data_t* user_data, void* callback_data) {
    if (record.kind == ROCPROFILER_CALLBACK_TRACING_CODE_OBJECT &&
        record.operation == ROCPROFILER_CODE_OBJECT_LOAD) {
        if (record.phase == ROCPROFILER_CALLBACK_PHASE_UNLOAD) {
            // Never reached when using the tool with the ROCP_TOOL_LIBRARIES
            // environment variable, hence the need to flush on kernel unload.
            auto flush_status = rocprofiler_flush_buffer(tracer.buffer);
            if (flush_status != ROCPROFILER_STATUS_ERROR_BUFFER_BUSY)
                ROCPROFILER_CALL(flush_status, "flush buffer");
        }
    } else if (record.kind == ROCPROFILER_CALLBACK_TRACING_CODE_OBJECT &&
               record.operation == ROCPROFILER_CODE_OBJECT_DEVICE_KERNEL_SYMBOL_REGISTER) {
        auto* data =
            static_cast<rocprofiler_callback_tracing_code_object_kernel_symbol_register_data_t*>(
                record.payload);
        if (record.phase == ROCPROFILER_CALLBACK_PHASE_LOAD) {
            tracer.kernels.emplace(data->kernel_id, *data);
        } else if (record.phase == ROCPROFILER_CALLBACK_PHASE_UNLOAD) {
            ROCPROFILER_CALL(rocprofiler_flush_buffer(tracer.buffer), "flush buffer");
            tracer.kernels.erase(data->kernel_id);
        }
    }
}

static size_t write_callback(char* ptr, size_t size, size_t nmemb, void* userdata) {
    return size * nmemb;
}

void full_buffer_callback(rocprofiler_context_id_t context [[maybe_unused]],
                          rocprofiler_buffer_id_t buffer_id [[maybe_unused]],
                          rocprofiler_record_header_t** headers, size_t num_headers,
                          void* tool_data, uint64_t drop_count [[maybe_unused]]) {
    auto* curl = static_cast<CURL*>(tool_data);

    std::ostringstream data_stream;

    if (num_headers == 0) {
        throw std::runtime_error{
            "rocprofiler invoked a buffer callback with no headers. this should never happen"};
    } else if (headers == nullptr) {
        throw std::runtime_error{"rocprofiler invoked a buffer callback with a null pointer to the "
                                 "array of headers. this should never happen"};
    }

    for (size_t i = 0; i < num_headers; ++i) {
        auto* header = headers[i];

        if (header->category == ROCPROFILER_BUFFER_CATEGORY_TRACING &&
            header->kind == ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH) {
            auto* record =
                static_cast<rocprofiler_buffer_tracing_kernel_dispatch_record_t*>(header->payload);
            data_stream << agent_map[record->dispatch_info.agent_id.handle] << ","
                        << tracer.kernels.at(record->dispatch_info.kernel_id).kernel_name << ","
                        << record->start_timestamp << "," << record->end_timestamp << "\n";
        } else {
            auto msg = std::stringstream{};
            msg << "unexpected rocprofiler_record_header_t category + kind: (" << header->category
                << " + " << header->kind << ")";
            throw std::runtime_error{msg.str()};
        }
    }

    std::string data = data_stream.str();
    curl_easy_setopt(curl, CURLOPT_POST, 1L);
    curl_easy_setopt(curl, CURLOPT_POSTFIELDS, data.c_str());
    curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE, data.length());

    std::string response_buffer;
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &response_buffer);

    auto res = curl_easy_perform(curl);
    if (res != CURLE_OK) {
        std::cerr << curl_easy_strerror(res) << std::endl;
    }
}

int KernelTracer::initialize(void* tool_data) {
    ROCPROFILER_CALL(rocprofiler_create_context(&context), "create context");

    auto code_object_ops = std::vector<rocprofiler_tracing_operation_t>{
        ROCPROFILER_CODE_OBJECT_DEVICE_KERNEL_SYMBOL_REGISTER};

    ROCPROFILER_CALL(rocprofiler_configure_callback_tracing_service(
                         context, ROCPROFILER_CALLBACK_TRACING_CODE_OBJECT, code_object_ops.data(),
                         code_object_ops.size(), code_object_callback, nullptr),
                     "configure code object tracing service");

    constexpr auto buffer_size_bytes = 262144;
    constexpr auto buffer_watermark_bytes = buffer_size_bytes - (buffer_size_bytes / 8);

    ROCPROFILER_CALL(rocprofiler_create_buffer(context, buffer_size_bytes, buffer_watermark_bytes,
                                               ROCPROFILER_BUFFER_POLICY_LOSSLESS,
                                               full_buffer_callback, tool_data, &buffer),
                     "create buffer");

    ROCPROFILER_CALL(rocprofiler_configure_buffer_tracing_service(
                         context, ROCPROFILER_BUFFER_TRACING_KERNEL_DISPATCH, nullptr, 0, buffer),
                     "configure buffer tracing service for kernel dispatches");

    auto thread = rocprofiler_callback_thread_t{};
    ROCPROFILER_CALL(rocprofiler_create_callback_thread(&thread), "create thread");

    ROCPROFILER_CALL(rocprofiler_assign_callback_thread(buffer, thread),
                     "assign thread for buffer");

    int valid = 0;
    ROCPROFILER_CALL(rocprofiler_context_is_valid(context, &valid), "check context validity");
    if (valid == 0) {
        return -1;
    }

    ROCPROFILER_CALL(rocprofiler_start_context(context), "start context");
    return 0;
}

} // namespace omnistat

// ------------------------------------------------------------------------------------------------
// ROCProfiler SDK tool initialization
// ------------------------------------------------------------------------------------------------

int tool_init(rocprofiler_client_finalize_t fini_func, void* tool_data) {
    omnistat::agent_map = omnistat::build_agent_map();
    return omnistat::tracer.initialize(tool_data);
}

void tool_fini(void* tool_data) {
    auto* curl = static_cast<CURL*>(tool_data);
    curl_easy_cleanup(curl);
    curl_global_cleanup();
}

extern "C" rocprofiler_tool_configure_result_t* rocprofiler_configure(uint32_t version,
                                                                      const char* runtime_version,
                                                                      uint32_t priority,
                                                                      rocprofiler_client_id_t* id) {
    id->name = "omnistat-kernel-trace";

    curl_global_init(CURL_GLOBAL_ALL);

    CURL* curl = curl_easy_init();
    if (curl) {
        curl_easy_setopt(curl, CURLOPT_URL, "http://localhost:8001/kernel_trace");
    }

    struct curl_slist* http_headers = NULL;
    http_headers = curl_slist_append(http_headers, "Content-Type: text/plain");
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, http_headers);

    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, &omnistat::write_callback);

    static auto cfg =
        rocprofiler_tool_configure_result_t{sizeof(rocprofiler_tool_configure_result_t), &tool_init,
                                            &tool_fini, static_cast<void*>(curl)};

    return &cfg;
}
