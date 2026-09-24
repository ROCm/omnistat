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

// Enables hardware counter collection for the queues of the application this
// library is loaded into.
//
// ROCProfiler-SDK only makes an application's queues visible to hardware counter
// collection when a counting service is registered in that process: queue setup
// consults the registered contexts and, when any of them requests counters, emits
// a packet enabling performance counting on the queue. Registration alone is
// enough; the context is never started, so no counters are collected here.
//
// This reproduces what ROCProfiler v1 provided through HSA_TOOLS_LIB, and lets a
// separate Omnistat exporter sample counters for this application without
// requiring CAP_PERFMON. Load it into the application with:
//
//   ROCP_TOOL_LIBRARIES=/path/to/libomnistat_count.so
//
// Note that only queues of applications loading this library are counted; work
// from other processes is not reflected in the values reported by the exporter.

#include <rocprofiler-sdk/agent.h>
#include <rocprofiler-sdk/device_counting_service.h>
#include <rocprofiler-sdk/registration.h>
#include <rocprofiler-sdk/rocprofiler.h>
#include <rocprofiler-sdk/version.h>

#include <iostream>

#ifndef ROCPROFILER_SDK_VERSION
#define ROCPROFILER_SDK_VERSION ROCPROFILER_VERSION
#endif

namespace {

rocprofiler_context_id_t context = {};
rocprofiler_agent_id_t agent = {};

// A device counting service is used rather than a dispatch counting service:
// both enable counters, but registering a dispatch service also makes
// ROCProfiler-SDK replace the application's queues, which silently discards the
// CU masks and priorities they were created with.
rocprofiler_status_t find_agent(rocprofiler_agent_version_t, const void** agents, size_t count,
                                void*) {
    for (size_t i = 0; i < count; i++) {
        const auto* candidate = static_cast<const rocprofiler_agent_v0_t*>(agents[i]);
        if (candidate->type == ROCPROFILER_AGENT_TYPE_GPU && agent.handle == 0) {
            agent = candidate->id;
        }
    }
    return ROCPROFILER_STATUS_SUCCESS;
}

// Required to register the service, but never invoked: the context is left inactive.
void device_callback(rocprofiler_context_id_t, rocprofiler_agent_id_t,
                     rocprofiler_device_counting_agent_cb_t, void*) {
}

// Failures are reported but never raised: this library is loaded into a user's
// application, and being unable to enable counters is not a reason to disrupt it.
int tool_init(rocprofiler_client_finalize_t, void*) {
    rocprofiler_status_t status = rocprofiler_create_context(&context);
    if (status != ROCPROFILER_STATUS_SUCCESS) {
        std::cerr << "Omnistat: counters disabled (unable to create context: "
                  << rocprofiler_get_status_string(status) << ")" << std::endl;
        return -1;
    }

    rocprofiler_query_available_agents(ROCPROFILER_AGENT_INFO_VERSION_0, find_agent,
                                       sizeof(rocprofiler_agent_v0_t), nullptr);
    if (agent.handle == 0) {
        std::cerr << "Omnistat: counters disabled (no GPU agent found)" << std::endl;
        return -1;
    }

    status = rocprofiler_configure_device_counting_service(context, rocprofiler_buffer_id_t{0},
                                                           agent, device_callback, nullptr);
    if (status != ROCPROFILER_STATUS_SUCCESS) {
        std::cerr << "Omnistat: counters disabled (unable to configure counting service: "
                  << rocprofiler_get_status_string(status) << ")" << std::endl;
        return -1;
    }

    // Reported unconditionally: an application that fails to load this library
    // collects no counters and reports no error, so this is the only indication
    // that counters are being collected for this process.
    std::cerr << "Omnistat: counters enabled" << std::endl;
    return 0;
}

void tool_fini(void*) {
}

} // namespace

extern "C" rocprofiler_tool_configure_result_t*
rocprofiler_configure(uint32_t version, const char* runtime_version,
                      uint32_t priority [[maybe_unused]], rocprofiler_client_id_t* id) {
    constexpr uint32_t compiled_version = ROCPROFILER_SDK_VERSION;

    if (version / 10000 != compiled_version / 10000) {
        std::cerr << "Omnistat: counters disabled (version mismatch, compiled against "
                  << compiled_version / 10000 << "." << (compiled_version % 10000) / 100 << "."
                  << compiled_version % 100 << " but runtime is "
                  << (runtime_version ? runtime_version : "unknown") << ")" << std::endl;
        return nullptr;
    }

    id->name = "omnistat-counters";

    static auto cfg = rocprofiler_tool_configure_result_t{
        sizeof(rocprofiler_tool_configure_result_t), &tool_init, &tool_fini, nullptr};

    return &cfg;
}
