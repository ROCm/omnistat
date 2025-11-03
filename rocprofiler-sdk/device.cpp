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

#include "device.hpp"
#include "common.hpp"

#include <hsa/hsa.h>

#include <iostream>
#include <sstream>

namespace omnistat {

// Global variable to keep track of samplers, necessary because it needs to be initialized and used
// by different entities. Samplers and rocprofiler contexts are initialized while rocprofiler is
// being configured, which isn't directly under our control.
static std::vector<std::shared_ptr<DeviceSampler>> samplers = {};

const std::vector<std::shared_ptr<DeviceSampler>> &get_samplers() {
    return samplers;
}

// Initialization required to use the extension. ROCProfiler-SDK normally expects to be loaded as
// part of an application with GPU code, e.g. HIP. This Python extension doesn't execute anything in
// the GPU, so we need to force its initialization. HSA also needs to be initialized to set up the
// right queues for device profiling.
void initialize() {
    ROCPROFILER_CALL(rocprofiler_force_configure(&rocprofiler_configure), "configure rocprofiler");
    hsa_init();
}

// Calculate the size of a given counter based on its dimensions. GPU counters aren't simple
// scalars: counters might exist per SE, CU, etc. and are reported as separate records by
// ROCProfiler-SDK.
size_t get_counter_size(rocprofiler_counter_id_t counter) {
    size_t size = 1;
    rocprofiler_iterate_counter_dimensions(
        counter,
        [](rocprofiler_counter_id_t, const rocprofiler_record_dimension_info_t *dim_info,
           size_t num_dims, void *user_data) {
            size_t *s = static_cast<size_t *>(user_data);
            for (size_t i = 0; i < num_dims; i++) {
                *s *= dim_info[i].instance_size;
            }
            return ROCPROFILER_STATUS_SUCCESS;
        },
        static_cast<void *>(&size));
    return size;
}

DeviceSampler::DeviceSampler(rocprofiler_agent_id_t agent) : agent_(agent) {
    ROCPROFILER_CALL(rocprofiler_create_context(&ctx_), "create context");

    ROCPROFILER_CALL(rocprofiler_configure_device_counting_service(
                         ctx_, rocprofiler_buffer_id_t{.handle = 0}, agent,
                         [](rocprofiler_context_id_t context_id, rocprofiler_agent_id_t,
                            rocprofiler_agent_set_profile_callback_t set_config, void *user_data) {
                             if (user_data) {
                                 auto *sampler = static_cast<DeviceSampler *>(user_data);
                                 sampler->set_profile(context_id, set_config);
                             }
                         },
                         this),
                     "device counting service");
}

void DeviceSampler::set_profile(rocprofiler_context_id_t ctx,
                                rocprofiler_agent_set_profile_callback_t cb) const {
    if (profile_.handle != 0) {
        ROCPROFILER_CALL(cb(ctx, profile_), "set profile callback");
    }
}

std::unordered_map<std::string, rocprofiler_counter_id_t>
DeviceSampler::get_supported_counters() const {
    std::unordered_map<std::string, rocprofiler_counter_id_t> out;
    std::vector<rocprofiler_counter_id_t> gpu_counters;

    ROCPROFILER_CALL(rocprofiler_iterate_agent_supported_counters(
                         agent_,
                         [](rocprofiler_agent_id_t, rocprofiler_counter_id_t *counters,
                            size_t num_counters, void *user_data) {
                             std::vector<rocprofiler_counter_id_t> *vec =
                                 static_cast<std::vector<rocprofiler_counter_id_t> *>(user_data);
                             for (size_t i = 0; i < num_counters; i++) {
                                 vec->push_back(counters[i]);
                             }
                             return ROCPROFILER_STATUS_SUCCESS;
                         },
                         static_cast<void *>(&gpu_counters)),
                     "iterate supported counters");
    for (auto &counter : gpu_counters) {
        rocprofiler_counter_info_v0_t version;
        ROCPROFILER_CALL(rocprofiler_query_counter_info(counter, ROCPROFILER_COUNTER_INFO_VERSION_0,
                                                        static_cast<void *>(&version)),
                         "query counter");
        out.emplace(version.name, counter);
    }
    return out;
}

void DeviceSampler::start(const std::vector<std::string> &counters) {
    rocprofiler_profile_config_id_t profile = {};
    std::size_t profile_size = 0;

    auto cached_profile = cached_profiles_.find(counters);
    if (cached_profile == cached_profiles_.end()) {
        std::vector<rocprofiler_counter_id_t> counter_ids;

        auto supported_counters = get_supported_counters();
        for (const auto &counter : counters) {
            auto it = supported_counters.find(counter);
            if (it == supported_counters.end()) {
                throw std::runtime_error("Unsupported counter: " + counter);
            }

            profile_size += get_counter_size(it->second);
            counter_ids.push_back(it->second);
        }

        ROCPROFILER_CALL(rocprofiler_create_profile_config(agent_, counter_ids.data(),
                                                           counter_ids.size(), &profile),
                         "create profile");

        cached_profiles_.emplace(counters, profile);
        profile_sizes_.emplace(profile.handle, profile_size);
        profile_counter_ids_.emplace(profile.handle, counter_ids);
    } else {
        profile = cached_profile->second;
        profile_size = profile_sizes_[profile.handle];
    }

    profile_ = profile;
    records_.resize(profile_size);
    ROCPROFILER_CALL(rocprofiler_start_context(ctx_), "start context");
}

void DeviceSampler::stop() {
    ROCPROFILER_CALL(rocprofiler_stop_context(ctx_), "stop context");
}

std::vector<double> DeviceSampler::sample() {
    std::vector<double> result;
    std::unordered_map<rocprofiler_counter_instance_id_t, double> aggregate;

    size_t size = records_.size();
    rocprofiler_sample_device_counting_service(ctx_, {}, ROCPROFILER_COUNTER_FLAG_NONE,
                                               records_.data(), &size);

    // Aggregate counter records: sums all records from each counter in an
    // attempt to return a value that represents total activity.
    rocprofiler_counter_id_t counter_id = {.handle = 0};
    for (const auto &record : records_) {
        rocprofiler_query_record_counter_id(record.id, &counter_id);
        aggregate[counter_id.handle] += record.counter_value;
    }

    const auto counter_ids = profile_counter_ids_[profile_.handle];
    for (const auto &counter_id : counter_ids) {
        result.push_back(aggregate[counter_id.handle]);
    }

    return result;
}

} // namespace omnistat

// ------------------------------------------------------------------------------------------------
// ROCProfiler SDK tool initialization
// ------------------------------------------------------------------------------------------------

int tool_init(rocprofiler_client_finalize_t fini_func, void *) {
    auto agents = omnistat::get_rocprofiler_agents();
    if (agents.empty()) {
        std::cerr << "No agents found\n";
        return -1;
    }

    for (auto agent : agents) {
        omnistat::samplers.push_back(std::make_shared<omnistat::DeviceSampler>(agent.id));
    }

    return 0;
}

void tool_fini(void *user_data) {
    omnistat::samplers.clear();
}

extern "C" rocprofiler_tool_configure_result_t *rocprofiler_configure(uint32_t version,
                                                                      const char *runtime_version,
                                                                      uint32_t priority,
                                                                      rocprofiler_client_id_t *id) {
    id->name = "omnistat-rocprofiler-sdk-extension";

    std::ostream *output_stream = &std::cout;
    static auto cfg =
        rocprofiler_tool_configure_result_t{sizeof(rocprofiler_tool_configure_result_t), &tool_init,
                                            &tool_fini, static_cast<void *>(output_stream)};

    return &cfg;
}
