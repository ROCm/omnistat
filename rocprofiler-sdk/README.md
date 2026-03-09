# Omnistat ROCProfiler-SDK Integration

- [Performance Counter Sampling (Python Extension)](#performance-counter-sampling-python-extension)
- [Kernel Tracing Library](#kernel-tracing-library)

---

## Performance Counter Sampling (Python Extension)

Python bindings for ROCProfiler-SDK that enables GPU performance counter
sampling for AMD GPUs. This extension provides a simple interface to collect
hardware counters from AMD GPUs directly in Python applications.

### Requirements

- ROCm 6.4+ with ROCProfiler-SDK
- Python 3.8+ with development headers
- CMake
- [cmake-build-extension](https://github.com/diegoferigo/cmake-build-extension)
- [nanobind](https://github.com/wjakob/nanobind)

### Installation

For standard installations of the extension as part of Omnistat, refer to the
documentation to [build the ROCprofiler extensions](https://rocm.github.io/omnistat/installation/building-extensions.html#rocprofiler).

For development and custom builds, the extension can be installed with CMake:
```bash
# Install build dependencies
pip install nanobind

# Build and install in place
cmake -S rocprofiler-sdk/ -B build/
cmake --build build/
cmake --install build/ --prefix .
```

With a **`venv`** virtual environment:
```bash
python3 -m venv ~/venv/omnistat
~/venv/omnistat/bin/pip install nanobind
cmake -S rocprofiler-sdk/ -B build/ -DPython_EXECUTABLE=~/venv/omnistat/bin/python
cmake --build build/
cmake --install build/ --prefix .
```

### Example

The following example shows how to use and test the extension, loading it
directly without Omnistat's collector.

```python
import time
import rocprofiler_sdk_extension

# Initialize the extension
rocprofiler_sdk_extension.initialize()

# Get GPU device samplers
samplers = rocprofiler_sdk_extension.get_samplers()

# Start counter collection
counters = ["GRBM_COUNT"]
for sampler in samplers:
    sampler.start(counters)

# Collect 3x samples, one every second
for i in range(3):
    for j, sampler in enumerate(samplers):
        values = sampler.sample()
        print(f"[{i}] GPU {j} number of cycles: {values[0]}")
    time.sleep(1)

# Stop counter collection
for sampler in samplers:
    sampler.stop()
```

Refer to the documentation of the [ROCprofiler
collector](https://rocm.github.io/omnistat/metrics.html#rocprofiler) for more
advanced usage using Omnistat.

---

## Kernel Tracing Library

A standalone C++ shared library (`librsdk_kernel_trace.so`) that traces GPU
kernel dispatches and streams the data to omnistat-standalone via HTTP.

### Requirements

- ROCm 6.4+ with ROCProfiler-SDK
- libcurl
- C++20 compiler (GCC 13+ or Clang 16+)
- CMake 3.15+

CMake automatically fetches the `fmt` library if the compiler lacks
`std::format`.

### Building

```bash
cmake -S rocprofiler-sdk/ -B build-trace/ -DBUILD_KERNEL_TRACE_LIB=ON
cmake --build build-trace/
```

This produces `build-trace/librsdk_kernel_trace.so`.

### Usage

The library is loaded via rocprofiler-sdk's tool loading mechanism. Point the
`ROCP_TOOL_LIBRARIES` environment variable at the built shared library, then
run any application and kernel dispatches are traced automatically.

```bash
export ROCP_TOOL_LIBRARIES=/path/to/librsdk_kernel_trace.so
```

Dispatch records are JSON-encoded and sent via HTTP POST to
`localhost:<port>/kernel_trace` (default port 8001, configurable via
`OMNISTAT_TRACE_ENDPOINT_PORT`). This requires Omnistat to be running with the
kernel tracing collector enabled.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `OMNISTAT_TRACE_MAX_INTERVAL` | `13` (seconds) | Max time between periodic buffer flushes |
| `OMNISTAT_TRACE_BUFFER_SIZE` | `262144` (bytes) | rocprofiler-sdk buffer size for dispatch records |
| `OMNISTAT_TRACE_ENDPOINT_PORT` | `8001` | Port for the HTTP endpoint receiving kernel trace data |

### Exit Summary

On application exit the library prints a summary line:

```
Omnistat trace summary: 12345/12345 processed records (42/42 successful flushes)
```
