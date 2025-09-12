# Building extensions

```eval_rst
.. toctree::
   :hidden:
```

Omnistat includes optional extensions that can be built and installed to
provide additional data collectors.

## ROCprofiler

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
