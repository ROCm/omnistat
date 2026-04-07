// ---------------------------------------------------------------------------
// MIT License
//
// Copyright (c) 2026 Advanced Micro Devices, Inc. All Rights Reserved.
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

// A minimal rocprofiler-sdk v2 tool loaded via ROCP_TOOL_LIBRARIES that
// intercepts hsa_queue_create and submits a PM4 COMPUTE_PERFCOUNT_ENABLE
// packet on every new HSA queue.

#include <rocprofiler-sdk/intercept_table.h>
#include <rocprofiler-sdk/registration.h>
#include <rocprofiler-sdk/rocprofiler.h>
#include <hsa/hsa_api_trace.h>
#include <hsa/hsa_ven_amd_aqlprofile.h>

#include <cstring>
#include <iostream>
#include <mutex>
#include <unordered_map>

// Saved original hsa_queue_create — set once in hsa_intercept_cb.
static decltype(::hsa_queue_create)* orig_hsa_queue_create = nullptr;

// aqlprofile extension function table — initialised once.
static hsa_ven_amd_aqlprofile_pfn_t g_aqlprofile = {};

// Fine-grained system memory pool from the CPU agent — found once.
// Allocations use HSA_AMD_MEMORY_POOL_EXECUTABLE_FLAG so the CP firmware
// can read PM4 commands from the buffer.
static hsa_amd_memory_pool_t g_sys_pool = {0};

// Per-agent cache of the pre-built PM4 enable packet.
// The pm4_command bytes reference cmd_buf via an embedded GPU address, so
// cmd_buf must stay allocated for the lifetime of the library. Never freed,
// matching exactly what librocprofiler64.so does in GetGpuCommand().
struct CachedPacket {
    hsa_ext_amd_aql_pm4_packet_t pkt = {}; // pm4_command filled by get_info
    void* cmd_buf = nullptr;
};
static std::unordered_map<uint32_t, CachedPacket> g_packet_cache;
static std::mutex g_cache_mutex;

// ---------------------------------------------------------------------------
// One-time initialisation helpers
// ---------------------------------------------------------------------------

static hsa_status_t find_system_pool_cb(hsa_amd_memory_pool_t pool, void* data) {
    hsa_amd_segment_t seg;
    hsa_amd_memory_pool_get_info(pool, HSA_AMD_MEMORY_POOL_INFO_SEGMENT, &seg);
    if (seg != HSA_AMD_SEGMENT_GLOBAL)
        return HSA_STATUS_SUCCESS;

    uint32_t flags = 0;
    hsa_amd_memory_pool_get_info(pool, HSA_AMD_MEMORY_POOL_INFO_GLOBAL_FLAGS, &flags);
    if (!(flags & HSA_AMD_MEMORY_POOL_GLOBAL_FLAG_FINE_GRAINED))
        return HSA_STATUS_SUCCESS;

    bool alloc_ok = false;
    hsa_amd_memory_pool_get_info(pool, HSA_AMD_MEMORY_POOL_INFO_RUNTIME_ALLOC_ALLOWED, &alloc_ok);
    if (!alloc_ok)
        return HSA_STATUS_SUCCESS;

    *static_cast<hsa_amd_memory_pool_t*>(data) = pool;
    return HSA_STATUS_INFO_BREAK;
}

static hsa_status_t find_cpu_agent_cb(hsa_agent_t agent, void* data) {
    hsa_device_type_t t;
    hsa_agent_get_info(agent, HSA_AGENT_INFO_DEVICE, &t);
    if (t == HSA_DEVICE_TYPE_CPU) {
        *static_cast<hsa_agent_t*>(data) = agent;
        return HSA_STATUS_INFO_BREAK;
    }
    return HSA_STATUS_SUCCESS;
}

// ---------------------------------------------------------------------------
// PM4 enable packet — build once per agent, reuse for every queue
// ---------------------------------------------------------------------------

// Returns a pointer to the cached CachedPacket for this agent, building it on
// first call.  Returns nullptr on failure.
static const CachedPacket* get_cached_packet(hsa_agent_t agent) {
    uint32_t node_id = 0;
    hsa_agent_get_info(agent, static_cast<hsa_agent_info_t>(HSA_AGENT_INFO_NODE), &node_id);

    std::lock_guard<std::mutex> lock(g_cache_mutex);

    auto it = g_packet_cache.find(node_id);
    if (it != g_packet_cache.end())
        return &it->second;

    // Build the packet for this agent for the first time.
    hsa_ven_amd_aqlprofile_profile_t profile = {};
    profile.agent = agent;
    profile.type = HSA_VEN_AMD_AQLPROFILE_EVENT_TYPE_PMC;

    // Step 1: get_info(ENABLE_CMD, NULL) fills profile.command_buffer.size.
    hsa_status_t s = g_aqlprofile.hsa_ven_amd_aqlprofile_get_info(
        &profile, HSA_VEN_AMD_AQLPROFILE_INFO_ENABLE_CMD, nullptr);
    if (s != HSA_STATUS_SUCCESS || profile.command_buffer.size == 0) {
        std::cerr << "[omnistat] ERROR: ENABLE_CMD size query failed (status="
                  << static_cast<int>(s) << ")\n";
        return nullptr;
    }

    // Step 2: allocate command buffer with EXECUTABLE flag; grant GPU access.
    // Never freed — matches librocprofiler64.so behaviour; pm4_command embeds
    // this pointer as a GPU address.
    void* cmd_buf = nullptr;
    if (hsa_amd_memory_pool_allocate(g_sys_pool, profile.command_buffer.size,
                                     HSA_AMD_MEMORY_POOL_EXECUTABLE_FLAG,
                                     &cmd_buf) != HSA_STATUS_SUCCESS) {
        std::cerr << "[omnistat] ERROR: command buffer allocation failed\n";
        return nullptr;
    }
    hsa_amd_agents_allow_access(1, &agent, nullptr, cmd_buf);
    profile.command_buffer.ptr = cmd_buf;

    // Step 3: get_info(ENABLE_CMD, &pkt) fills pkt.pm4_command.
    CachedPacket entry;
    entry.cmd_buf = cmd_buf;
    s = g_aqlprofile.hsa_ven_amd_aqlprofile_get_info(
        &profile, HSA_VEN_AMD_AQLPROFILE_INFO_ENABLE_CMD, &entry.pkt);
    if (s != HSA_STATUS_SUCCESS) {
        std::cerr << "[omnistat] ERROR: ENABLE_CMD packet fill failed (status="
                  << static_cast<int>(s) << ")\n";
        hsa_amd_memory_pool_free(cmd_buf);
        return nullptr;
    }

    auto [ins_it, ok] = g_packet_cache.emplace(node_id, entry);
    return &ins_it->second;
}

// Submits the pre-built PM4 enable packet to queue and waits for completion.
static void submit_enable(hsa_queue_t* queue, const CachedPacket& cached) {
    hsa_signal_t done_signal;
    if (hsa_signal_create(1, 0, nullptr, &done_signal) != HSA_STATUS_SUCCESS) {
        std::cerr << "[omnistat] ERROR: hsa_signal_create failed\n";
        return;
    }

    uint64_t write_idx = hsa_queue_add_write_index_scacq_screl(queue, 1);

    while (write_idx - hsa_queue_load_read_index_scacquire(queue) >= queue->size)
        ;

    auto* slot = reinterpret_cast<hsa_ext_amd_aql_pm4_packet_t*>(queue->base_address) +
                 (write_idx & (queue->size - 1));

    memcpy(slot->pm4_command, cached.pkt.pm4_command, sizeof(cached.pkt.pm4_command));
    slot->completion_signal = done_signal;

    uint16_t header =
        static_cast<uint16_t>(HSA_PACKET_TYPE_VENDOR_SPECIFIC << HSA_PACKET_HEADER_TYPE);
    __atomic_store_n(&slot->header, header, __ATOMIC_RELEASE);

    hsa_signal_store_relaxed(queue->doorbell_signal, static_cast<hsa_signal_value_t>(write_idx));

    hsa_signal_wait_scacquire(done_signal, HSA_SIGNAL_CONDITION_LT, 1, UINT64_MAX,
                              HSA_WAIT_STATE_BLOCKED);
    hsa_signal_destroy(done_signal);
}

// ---------------------------------------------------------------------------
// hsa_queue_create wrapper
// ---------------------------------------------------------------------------

static hsa_status_t wrapped_hsa_queue_create(hsa_agent_t agent, uint32_t size,
                                             hsa_queue_type32_t type,
                                             void (*callback)(hsa_status_t, hsa_queue_t*, void*),
                                             void* data, uint32_t private_segment_size,
                                             uint32_t group_segment_size, hsa_queue_t** queue) {
    hsa_status_t status = orig_hsa_queue_create(agent, size, type, callback, data,
                                                private_segment_size, group_segment_size, queue);
    if (status != HSA_STATUS_SUCCESS || !queue || !*queue)
        return status;

    const CachedPacket* cached = get_cached_packet(agent);
    if (cached)
        submit_enable(*queue, *cached);

    return status;
}

// ---------------------------------------------------------------------------
// rocprofiler-sdk intercept registration
// ---------------------------------------------------------------------------

static void hsa_intercept_cb(rocprofiler_intercept_table_t type,
                             [[maybe_unused]] uint64_t lib_version,
                             [[maybe_unused]] uint64_t lib_instance,
                             void** tables,
                             [[maybe_unused]] uint64_t num_tables,
                             [[maybe_unused]] void* user_data) {

    if (type != ROCPROFILER_HSA_TABLE)
        return;

    // HSA is fully initialized by the time this callback fires — safe to query
    // the aqlprofile extension and memory pools here.
    hsa_status_t s = hsa_system_get_major_extension_table(HSA_EXTENSION_AMD_AQLPROFILE, 1,
                                                          sizeof(g_aqlprofile), &g_aqlprofile);
    if (s != HSA_STATUS_SUCCESS || !g_aqlprofile.hsa_ven_amd_aqlprofile_get_info) {
        std::cerr << "[omnistat] ERROR: aqlprofile unavailable (status=" << static_cast<int>(s)
                  << ")\n";
        return;
    }

    hsa_agent_t cpu = {0};
    hsa_iterate_agents(find_cpu_agent_cb, &cpu);
    hsa_amd_agent_iterate_memory_pools(cpu, find_system_pool_cb, &g_sys_pool);
    if (g_sys_pool.handle == 0) {
        std::cerr << "[omnistat] ERROR: no fine-grained system pool\n";
        return;
    }

    auto* hsa_table = static_cast<HsaApiTable*>(tables[0]);
    orig_hsa_queue_create = hsa_table->core_->hsa_queue_create_fn;

    if (!orig_hsa_queue_create) {
        std::cerr << "[omnistat] ERROR: hsa_queue_create_fn is null\n";
        return;
    }

    hsa_table->core_->hsa_queue_create_fn = wrapped_hsa_queue_create;
    std::cerr << "[omnistat] hsa_queue_create intercept installed\n";
}

extern "C" rocprofiler_tool_configure_result_t*
rocprofiler_configure([[maybe_unused]] uint32_t version,
                      [[maybe_unused]] const char* runtime_version,
                      [[maybe_unused]] uint32_t priority,
                      rocprofiler_client_id_t* id) {

    id->name = "omnistat-counter-enabler";

    rocprofiler_status_t status = rocprofiler_at_intercept_table_registration(
        hsa_intercept_cb, ROCPROFILER_HSA_TABLE, nullptr);
    if (status != ROCPROFILER_STATUS_SUCCESS)
        std::cerr << "[omnistat] ERROR: intercept registration failed (status="
                  << static_cast<int>(status) << ")\n";

    return nullptr;
}
