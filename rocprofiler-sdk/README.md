# ROCProfiler-SDK Python Extension

Python bindings for ROCProfiler-SDK that enables GPU performance counter
sampling for AMD GPUs. This extension provides a simple interface to collect
hardware counters from AMD GPUs directly in Python applications.

## Requirements

- ROCm 6.4+ with ROCProfiler-SDK
- Python 3.8+ with development headers
- CMake
- [cmake-build-extension](https://github.com/diegoferigo/cmake-build-extension)
- [nanobind](https://github.com/wjakob/nanobind)

## Install with pip

The extension is an **optional** Omnistat module and is disabled by default.
It can be automatically built and installed as part of a standard Omnistat
installation with `pip`. It can be enabled with an environment variable:
```bash
BUILD_ROCPROFILER_SDK_EXTENSION=1 pip install .
```

With a **`venv`** virtual environment:
```bash
python3 -m venv ~/venv/omnistat
BUILD_ROCPROFILER_SDK_EXTENSION=1 ~/venv/omnistat/bin/python -m pip install .
```

## Install with CMake

For development or custom builds:
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

## Example

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
