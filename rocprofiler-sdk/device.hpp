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

#include <map>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

namespace omnistat {

class DeviceSampler {
  public:
    DeviceSampler(rocprofiler_agent_id_t agent);

    void start(const std::vector<std::string>& counters);
    void stop();

    std::vector<double> sample();

  private:
    rocprofiler_agent_id_t agent_ = {};

    rocprofiler_context_id_t ctx_ = {.handle = 0};
    rocprofiler_profile_config_id_t profile_ = {.handle = 0};

    std::map<std::vector<std::string>, rocprofiler_profile_config_id_t> cached_profiles_;

    // Store sizes and counter IDs for each profile. Sizes for each profile ID
    // are used to update the record buffer when the profile changes. The list of
    // counter IDs is used to aggregate and return counter values in the same
    // order they are requested.
    std::unordered_map<uint64_t, uint64_t> profile_sizes_;
    std::unordered_map<uint64_t, std::vector<rocprofiler_counter_id_t>> profile_counter_ids_;

    // Buffer records from last sample; its size changes every time a new
    // profile is started
    std::vector<rocprofiler_record_counter_t> records_;

    void set_profile(rocprofiler_context_id_t ctx,
                     rocprofiler_agent_set_profile_callback_t cb) const;
    std::unordered_map<std::string, rocprofiler_counter_id_t> get_supported_counters() const;
};

void initialize();

const std::vector<std::shared_ptr<DeviceSampler>>& get_samplers();

} // namespace omnistat
