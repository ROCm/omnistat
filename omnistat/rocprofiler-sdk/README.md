# Omnistat ROCProfiler-SDK Integration

- [Performance Counter Sampling (Python Extension)](#performance-counter-sampling-python-extension)
- [Counter Enablement Library](#counter-enablement-library)
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
documentation to [build the ROCprofiler extensions](https://rocm.github.io/omnistat/installation/extensions.html#collector-extension).

For development and custom builds, the extension can be installed with CMake:
```bash
# Install build dependencies
pip install nanobind

# Build and install in place
cmake -S omnistat/rocprofiler-sdk/ -B build/
cmake --build build/
cmake --install build/ --prefix .
```

With a **`venv`** virtual environment:
```bash
python3 -m venv ~/venv/omnistat
~/venv/omnistat/bin/pip install nanobind
cmake -S omnistat/rocprofiler-sdk/ -B build/ -DPython_EXECUTABLE=~/venv/omnistat/bin/python
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
collector](https://rocm.github.io/omnistat/metrics.html#hardware-counters) for more
advanced usage using Omnistat.

---

## Counter Enablement Library

A standalone C++ shared library (`libomnistat_count.so`) that enables hardware
counter collection for the queues of the application it is loaded into, so that
a separate Omnistat instance can sample those counters. It is only needed in
user mode; in system mode, running Omnistat with the `CAP_PERFMON` capability is
enough.

The library registers a ROCProfiler-SDK device counting service and never starts
it: registration alone is what makes the application's queues visible to counter
collection. This reproduces what ROCProfiler v1 provided through
`HSA_TOOLS_LIB`, which is no longer available in ROCm 10.

### Requirements

- ROCm with ROCProfiler-SDK
- CMake 3.15+

### Building

```bash
cmake -S rocprofiler-sdk/ -B build-count/ -DBUILD_COUNT_LIB=ON
cmake --build build-count/
```

This produces `build-count/libomnistat_count.so`.

### Usage

Load the library into the application being monitored, not into Omnistat:

```bash
export ROCP_TOOL_LIBRARIES=/path/to/libomnistat_count.so
```

On startup the library prints `Omnistat: counters enabled` to the application's
standard error, or `Omnistat: counters disabled (...)` with a reason. An
application that never loads the library collects no counters and reports
nothing, so these messages are the only confirmation that it is active.

---

## Kernel Tracing Library

A standalone C++ shared library (`libomnistat_trace.so`) that traces GPU
kernel dispatches and streams the data to omnistat-standalone via HTTP.

### Requirements

- ROCm 6.4+ with ROCProfiler-SDK
- C++20 compiler (GCC 13+ or Clang 16+)
- CMake 3.15+

CMake automatically fetches the header-only `cpp-httplib` library (used to send
trace data over HTTP), and the `fmt` library if the compiler lacks
`std::format`.

### Building

```bash
cmake -S omnistat/rocprofiler-sdk/ -B build-trace/ -DBUILD_KERNEL_TRACE_LIB=ON
cmake --build build-trace/
```

This produces `build-trace/libomnistat_trace.so`.

### Usage

The library is loaded via rocprofiler-sdk's tool loading mechanism. Point the
`ROCP_TOOL_LIBRARIES` environment variable at the built shared library, then
run any application and kernel dispatches are traced automatically.

```bash
export ROCP_TOOL_LIBRARIES=/path/to/libomnistat_trace.so
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
| `OMNISTAT_TRACE_LOG` | `0` | Set to `1` to print a trace summary to stdout on exit |

### Exit Summary

When `OMNISTAT_TRACE_LOG=1` is set, the library prints a summary line on
application exit:

```
[hostname][12345][omnistat] Trace summary: 1234/1234 processed records (12/12 successful flushes)
```
