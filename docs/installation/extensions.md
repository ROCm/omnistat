# Building optional components

```eval_rst
.. toctree::
   :hidden:
```

Omnistat includes two optional components that can be built and installed to
provide additional data collector capabilities.

1. [Hardware counter support](#hardware-counter-support)
2. [Kernel tracing support](#kernel-tracing-support)

Both rely on C++ compilations via `cmake` and additional instructions for each optional component
are outlined below.

---

## Hardware counter support

Collecting GPU hardware counters relies on two separate pieces:

1. The [collector extension](#collector-extension), built into Omnistat, which
   samples counters from the GPUs. Always required.
2. The [counter enablement library](#counter-enablement-library), loaded into
   the application being monitored. Only required when running Omnistat in user
   mode, as described in [Hardware Counters
   metrics](../metrics.md#hardware-counters).

### Collector extension

The ROCprofiler extension provides access to low-level GPU hardware counters
for in-depth performance analysis. There are different ways to build and
install this extension depending on how Omnistat is installed.

#### Install with setuptools

This method builds the extension in-place, without installing an Omnistat package.
```bash
# Install build dependencies
pip install cmake-build-extension nanobind

# Build and install extension in place
BUILD_ROCPROFILER_SDK_EXTENSION=1 python setup.py build_ext --inplace
```

#### Install with pip

This method builds the extension and installs Omnistat as a package.
```bash
BUILD_ROCPROFILER_SDK_EXTENSION=1 pip install .
```

With a **`venv`** virtual environment:
```bash
python -m venv ~/venv/omnistat
BUILD_ROCPROFILER_SDK_EXTENSION=1 ~/venv/omnistat/bin/python -m pip install .
```

### Counter enablement library

`libomnistat_count.so` is a standalone C++ shared library loaded into the
application being monitored. It enables counter collection for the queues of
that process, so that a separate Omnistat instance can sample those counters
without additional privileges. Unlike the collector extension, it does not
require a Python build step.

(counter-enablement-requirements)=
#### Requirements

- ROCm with ROCProfiler-SDK
- CMake 3.15+

(counter-enablement-build)=
#### Build

```bash
cmake -S omnistat/rocprofiler-sdk/ -B build-count/ -DBUILD_COUNT_LIB=ON
cmake --build build-count/
```

The resulting library is located at `build-count/libomnistat_count.so`. See
[Hardware Counters metrics](../metrics.md#hardware-counters) for usage
instructions.

## Kernel tracing support

### Kernel tracing library

`libomnistat_trace.so` is a standalone C++ shared library that intercepts GPU
kernel dispatches at runtime to collect per-kernel timing and execution
metrics. Like the counter enablement library, it does not require a Python
build step.

(kernel-tracing-requirements)=
#### Requirements

- ROCm 6.4+
- C++20 compiler
- CMake 3.15+

```{note}
The build automatically fetches the header-only
[cpp-httplib](https://github.com/yhirose/cpp-httplib) library (used to send
trace data over HTTP) via CMake's `FetchContent`, along with the
[fmt](https://github.com/fmtlib/fmt) library if the compiler does not support
`std::format`. For offline builds, download the source trees ahead of time and
point CMake at them:

    cmake -S omnistat/rocprofiler-sdk/ -B build-trace/ -DBUILD_KERNEL_TRACE_LIB=ON \
      -DFETCHCONTENT_SOURCE_DIR_HTTPLIB=/path/to/cpp-httplib \
      -DFETCHCONTENT_SOURCE_DIR_FMT=/path/to/fmt
```

(kernel-tracing-build)=
#### Build

```bash
cmake -S omnistat/rocprofiler-sdk/ -B build-trace/ -DBUILD_KERNEL_TRACE_LIB=ON
cmake --build build-trace/
```

The resulting library is located at `build-trace/libomnistat_trace.so`. See
[Kernel Tracing metrics](../metrics.md#kernel-tracing) for usage instructions.
