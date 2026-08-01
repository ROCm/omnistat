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

// RCCL-API tracing callback. Registered by Tracer::initialize() when
// OMNISTAT_RCCL_TRACE is enabled: enumerates collectives (count/datatype) and
// communicator lifecycle (init duration, nranks) on the app's calling thread,
// accumulating into the Tracer's RCCL streams for the periodic flush to POST.

#include "trace.hpp"

#include <rocprofiler-sdk/rccl.h>

#include <hip/hip_runtime.h>

#include <dlfcn.h>
#include <iterator>
#include <string>
#include <string_view>

#if defined(HAS_STD_FORMAT)
#include <format>
namespace fmt = std;
#else
#include <fmt/core.h>
#endif

namespace omnistat {

// Extract the communicator handle + nranks from a comm-lifecycle op's args.
// Create ops (Init/Split) expose the comm via an OUTPUT pointer that is only
// populated at EXIT, so this must be read on the EXIT phase. Destroy ops take
// the comm by value (valid at either phase). nranks is -1 when not applicable.
static void rccl_comm_args(uint32_t op, rocprofiler_callback_tracing_rccl_api_data_t* data,
                           uintptr_t* comm, int* nranks) {
    *comm = 0;
    *nranks = -1;
    switch (op) {
        case ROCPROFILER_RCCL_API_ID_ncclCommInitRank:
            if (data->args.ncclCommInitRank.newcomm)
                *comm = reinterpret_cast<uintptr_t>(*data->args.ncclCommInitRank.newcomm);
            *nranks = data->args.ncclCommInitRank.nranks;
            break;
        case ROCPROFILER_RCCL_API_ID_ncclCommInitRankConfig:
            if (data->args.ncclCommInitRankConfig.comm)
                *comm = reinterpret_cast<uintptr_t>(*data->args.ncclCommInitRankConfig.comm);
            *nranks = data->args.ncclCommInitRankConfig.nranks;
            break;
        case ROCPROFILER_RCCL_API_ID_ncclCommSplit:
            if (data->args.ncclCommSplit.newcomm)
                *comm = reinterpret_cast<uintptr_t>(*data->args.ncclCommSplit.newcomm);
            // nranks of a split comm is not in the args; left as -1 (unknown).
            break;
        case ROCPROFILER_RCCL_API_ID_ncclCommDestroy:
            *comm = reinterpret_cast<uintptr_t>(data->args.ncclCommDestroy.comm);
            break;
        case ROCPROFILER_RCCL_API_ID_ncclCommAbort:
            *comm = reinterpret_cast<uintptr_t>(data->args.ncclCommAbort.comm);
            break;
        case ROCPROFILER_RCCL_API_ID_ncclCommFinalize:
            *comm = reinterpret_cast<uintptr_t>(data->args.ncclCommFinalize.comm);
            break;
        default:
            break;
    }
}

static bool is_comm_op(uint32_t op) {
    switch (op) {
        case ROCPROFILER_RCCL_API_ID_ncclCommInitRank:
        case ROCPROFILER_RCCL_API_ID_ncclCommInitAll:
        case ROCPROFILER_RCCL_API_ID_ncclCommInitRankConfig:
        case ROCPROFILER_RCCL_API_ID_ncclCommSplit:
        case ROCPROFILER_RCCL_API_ID_ncclCommDestroy:
        case ROCPROFILER_RCCL_API_ID_ncclCommAbort:
        case ROCPROFILER_RCCL_API_ID_ncclCommFinalize:
            return true;
        default:
            return false;
    }
}

// RCCL-API callback. Collectives are enumerated on ENTER; comm lifecycle ops are
// handled across ENTER/EXIT (init duration = EXIT-ENTER, and create ops' output
// comm handle is only valid at EXIT). Fires synchronously on the app's calling
// thread; kept minimal. No correlation-id join / GPU timing here — this is the
// enumeration + comm-lifecycle stream only.
void rccl_api_callback(rocprofiler_callback_tracing_record_t record,
                       rocprofiler_user_data_t* user_data, void* tool_data) {
    auto* tracer = static_cast<Tracer*>(tool_data);
    auto* data = static_cast<rocprofiler_callback_tracing_rccl_api_data_t*>(record.payload);

    // Comm lifecycle: stash the issue timestamp at ENTER, emit at EXIT with the
    // (now-valid) comm handle + measured init duration.
    if (is_comm_op(record.operation)) {
        if (record.phase == ROCPROFILER_CALLBACK_PHASE_ENTER) {
            rocprofiler_timestamp_t ts = 0;
            rocprofiler_get_timestamp(&ts);
            user_data->value = ts;  // survives to the EXIT callback for this call
            return;
        }
        if (record.phase != ROCPROFILER_CALLBACK_PHASE_EXIT) {
            return;
        }
        rocprofiler_timestamp_t h_end = 0;
        rocprofiler_get_timestamp(&h_end);
        uint64_t h_start = user_data->value;

        const char* op_name = nullptr;
        uint64_t op_len = 0;
        rocprofiler_query_callback_tracing_kind_operation_name(
            ROCPROFILER_CALLBACK_TRACING_RCCL_API, record.operation, &op_name, &op_len);
        std::string_view op_sv =
            op_name ? std::string_view(op_name, op_len) : std::string_view("?");

        // ncclCommInitAll is single-process multi-GPU: one call creates ndev
        // comms across ndev devices. Emit one row per device, attributed to that
        // device's gpu_id (agent id order == device order), nranks = ndev.
        if (record.operation == ROCPROFILER_RCCL_API_ID_ncclCommInitAll) {
            int ndev = data->args.ncclCommInitAll.ndev;
            const int* devlist = data->args.ncclCommInitAll.devlist;
            ncclComm_t* comms = data->args.ncclCommInitAll.comms;
            for (int i = 0; i < ndev; ++i) {
                int gpu_id = devlist ? devlist[i] : i;
                uintptr_t comm = comms ? reinterpret_cast<uintptr_t>(comms[i]) : 0;
                std::string element;
                fmt::format_to(std::back_inserter(element), "[{},\"{}\",{},{},{},{}]",
                               gpu_id, op_sv, comm, ndev, h_start, h_end);
                tracer->rccl_add_comm(element);
            }
            return;
        }

        uintptr_t comm = 0;
        int nranks = -1;
        rccl_comm_args(record.operation, data, &comm, &nranks);

        // ncclCommSplit (and CommShrink) don't expose the new comm's rank count
        // in the API args, so rccl_comm_args leaves nranks=-1 -> "unknown".
        // The newcomm handle IS valid at EXIT, so query the true size via
        // ncclCommCount (O(1): returns the stored comm->nRanks, no sync).
        // Resolved with dlsym so the trace lib gains NO link-time RCCL
        // dependency — it runs in-process where librccl is already loaded.
        if (nranks < 0 && comm != 0) {
            using comm_count_fn = int (*)(void*, int*);
            static comm_count_fn comm_count =
                reinterpret_cast<comm_count_fn>(dlsym(RTLD_DEFAULT, "ncclCommCount"));
            if (comm_count) {
                int n = -1;
                if (comm_count(reinterpret_cast<void*>(comm), &n) == 0 && n > 0) {
                    nranks = n;
                }
            }
        }

        int gpu_id = 0;
        hipGetDevice(&gpu_id);

        // Element: [gpu_id, "op", comm, nranks, h_start, h_end]
        std::string element;
        fmt::format_to(std::back_inserter(element), "[{},\"{}\",{},{},{},{}]",
                       gpu_id, op_sv, comm, nranks, h_start, h_end);
        tracer->rccl_add_comm(element);
        return;
    }

    // Collectives are enumerated on ENTER. GroupStart/End are ignored (they carry
    // no count/size). Only collectives carry a (count, datatype) worth recording;
    // extract the count field (name varies per op) and datatype from the args.
    if (record.phase != ROCPROFILER_CALLBACK_PHASE_ENTER) {
        return;
    }

    size_t count = 0;
    int dtype = -1;
    const void* comm = nullptr;
    bool is_collective = true;
    switch (record.operation) {
        case ROCPROFILER_RCCL_API_ID_ncclAllReduce:
            count = data->args.ncclAllReduce.count;
            dtype = data->args.ncclAllReduce.datatype;
            comm = data->args.ncclAllReduce.comm;
            break;
        case ROCPROFILER_RCCL_API_ID_ncclBroadcast:
            count = data->args.ncclBroadcast.count;
            dtype = data->args.ncclBroadcast.datatype;
            comm = data->args.ncclBroadcast.comm;
            break;
        case ROCPROFILER_RCCL_API_ID_ncclReduce:
            count = data->args.ncclReduce.count;
            dtype = data->args.ncclReduce.datatype;
            comm = data->args.ncclReduce.comm;
            break;
        case ROCPROFILER_RCCL_API_ID_ncclAllGather:
            count = data->args.ncclAllGather.sendcount;
            dtype = data->args.ncclAllGather.datatype;
            comm = data->args.ncclAllGather.comm;
            break;
        case ROCPROFILER_RCCL_API_ID_ncclReduceScatter:
            count = data->args.ncclReduceScatter.recvcount;
            dtype = data->args.ncclReduceScatter.datatype;
            comm = data->args.ncclReduceScatter.comm;
            break;
        case ROCPROFILER_RCCL_API_ID_ncclSend:
            count = data->args.ncclSend.count;
            dtype = data->args.ncclSend.datatype;
            comm = data->args.ncclSend.comm;
            break;
        case ROCPROFILER_RCCL_API_ID_ncclRecv:
            count = data->args.ncclRecv.count;
            dtype = data->args.ncclRecv.datatype;
            comm = data->args.ncclRecv.comm;
            break;
        default:
            is_collective = false;
            break;
    }
    if (!is_collective) {
        return;
    }

    const char* op_name = nullptr;
    uint64_t op_len = 0;
    rocprofiler_query_callback_tracing_kind_operation_name(ROCPROFILER_CALLBACK_TRACING_RCCL_API,
                                                           record.operation, &op_name, &op_len);

    int gpu_id = 0;
    hipGetDevice(&gpu_id);

    rocprofiler_timestamp_t ts = 0;
    rocprofiler_get_timestamp(&ts);

    // Element: [gpu_id, "op", count, dtype, comm, ts]
    std::string element;
    fmt::format_to(std::back_inserter(element), "[{},\"{}\",{},{},{},{}]", gpu_id,
                   op_name ? std::string(op_name, op_len) : std::string("?"), count, dtype,
                   reinterpret_cast<uintptr_t>(comm), ts);
    tracer->rccl_add_collective(element);
}

} // namespace omnistat
