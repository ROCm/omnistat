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
