# Building optional components

```eval_rst
.. toctree::
   :hidden:
```

Omnistat includes optional components that can be built and installed to
provide additional data collectors.

## Hardware Counters

The ROCprofiler extension provides access to low-level GPU hardware counters
for in-depth performance analysis. There are different ways to build and
install this extension depending on how Omnistat is installed.

### Install with setuptools

This method builds the extension in-place, without installing an Omnistat package.
```bash
# Install build dependencies
pip install cmake-build-extension nanobind

# Build and install extension in place
BUILD_ROCPROFILER_SDK_EXTENSION=1 python setup.py build_ext --inplace
```

### Install with pip

This method builds the extension and installs Omnistat as a package.
```bash
BUILD_ROCPROFILER_SDK_EXTENSION=1 pip install .[query]
```

With a **`venv`** virtual environment:
```bash
python -m venv ~/venv/omnistat
BUILD_ROCPROFILER_SDK_EXTENSION=1 ~/venv/omnistat/bin/python -m pip install .[query]
```

## Kernel Tracing

The kernel tracing extension is a standalone C++ shared library
(`libomnistat_trace.so`) that intercepts GPU kernel dispatches at runtime to
collect per-kernel timing and execution metrics. Unlike the ROCprofiler
extension above, it does not require a Python build step.

### Requirements

- ROCm 6.4+
- libcurl
- C++20 compiler
- CMake 3.15+

```{note}
The build automatically fetches the [fmt](https://github.com/fmtlib/fmt) library via CMake's `FetchContent`.
```

### Build

```bash
cmake -S rocprofiler-sdk/ -B build-trace/ -DBUILD_KERNEL_TRACE_LIB=ON
cmake --build build-trace/
```

The resulting library is located at `build-trace/libomnistat_trace.so`. See
[Kernel Tracing metrics](../metrics.md#kernel-tracing) for usage instructions.
