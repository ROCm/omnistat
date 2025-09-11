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

#include <nanobind/nanobind.h>
#include <nanobind/stl/bind_vector.h>
#include <nanobind/stl/shared_ptr.h>
#include <nanobind/stl/string.h>

namespace nb = nanobind;
using namespace nb::literals;

NB_MODULE(rocprofiler_sdk_extension, m) {
    m.doc() = "Omnistat's ROCProfiler-SDK device sampling module";

    m.def("initialize", &omnistat::initialize);

    nb::bind_vector<std::vector<std::string>>(m, "StringList");
    nb::bind_vector<std::vector<double>>(m, "DoubleList");

    nb::class_<omnistat::DeviceSampler>(m, "DeviceSampler")
        .def("start", &omnistat::DeviceSampler::start, "counters"_a)
        .def("sample", &omnistat::DeviceSampler::sample)
        .def("stop", &omnistat::DeviceSampler::stop);

    nb::bind_vector<std::vector<std::shared_ptr<omnistat::DeviceSampler>>>(m, "DeviceSamplerList");

    m.def("get_samplers", &omnistat::get_samplers);
}
