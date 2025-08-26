# This setup.py script is only meant to build the optional ROCProfiler-SDK
# extension and is automatically called by pip. To extension is built only
# when BUILD_ROCPROFILER_SDK_EXTENSION=1 is set in the environment.

import os
import sys
from pathlib import Path

from cmake_build_extension import BuildExtension, CMakeExtension
from setuptools import setup

ext_modules = []

build_rocprofiler_sdk = os.environ.get("BUILD_ROCPROFILER_SDK_EXTENSION", "0")
if build_rocprofiler_sdk == "1":
    rocprofiler_sdk = CMakeExtension(
        name="rocprofiler_sdk_extension",
        source_dir="rocprofiler-sdk",
        cmake_configure_options=[
            # Point CMake to the right Python interpreter
            f"-DPython_ROOT_DIR={Path(sys.prefix)}",
        ],
    )
    ext_modules.append(rocprofiler_sdk)

setup(
    name="omnistat",
    ext_modules=ext_modules,
    cmdclass=dict(
        # Enable the CMakeExtension entries defined above
        build_ext=BuildExtension,
    ),
)
