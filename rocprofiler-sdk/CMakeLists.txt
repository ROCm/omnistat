# ----------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2025 Advanced Micro Devices, Inc. All Rights Reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
# ----------------------------------------------------------------------------

cmake_minimum_required(VERSION 3.15)
project(omnistat_rocprofiler_sdk_extension LANGUAGES CXX)

# The kernel tracing library is meant to be built separately from the Omnistat
# package and extensions. When it's enabled, compile only the library and
# nothing else.
option(BUILD_KERNEL_TRACE_LIB "Build the kernel tracing library" OFF)

if (NOT BUILD_KERNEL_TRACE_LIB)
    find_package(Python COMPONENTS Interpreter Development REQUIRED)

    set(CMAKE_EXECUTE_PROCESS_COMMAND_ECHO STDOUT)
    execute_process(
        COMMAND "${Python_EXECUTABLE}" -m nanobind --cmake_dir
        OUTPUT_STRIP_TRAILING_WHITESPACE OUTPUT_VARIABLE nanobind_ROOT)

    find_package(nanobind CONFIG REQUIRED)

    find_package(rocprofiler-sdk REQUIRED)

    nanobind_add_module(rocprofiler_sdk_extension device.cpp binding.cpp)
    target_link_libraries(rocprofiler_sdk_extension PRIVATE rocprofiler-sdk::rocprofiler-sdk)

    # Install the module to location within the Python package
    install(TARGETS rocprofiler_sdk_extension LIBRARY DESTINATION omnistat)
else()
    find_package(rocprofiler-sdk REQUIRED)

    include(FetchContent)
    include(CheckCXXSourceCompiles)

    # Download cpp-httplib header-only library from GitHub
    FetchContent_Declare(
        httplib
        URL https://github.com/yhirose/cpp-httplib/archive/refs/tags/v0.18.3.tar.gz
        DOWNLOAD_EXTRACT_TIMESTAMP TRUE
    )
    FetchContent_MakeAvailable(httplib)

    add_library(rsdk_kernel_trace SHARED kernel_tracer.cpp)
    target_compile_features(rsdk_kernel_trace PRIVATE cxx_std_20)
    target_link_libraries(rsdk_kernel_trace PRIVATE rocprofiler-sdk::rocprofiler-sdk)
    target_include_directories(rsdk_kernel_trace PRIVATE ${httplib_SOURCE_DIR})

    # Check if the compiler supports std::format
    set(CMAKE_REQUIRED_FLAGS "-std=c++20")
    check_cxx_source_compiles("
        #include <format>
        #include <string>
        int main() {
            std::string s = std::format(\"{}\", 42);
            return 0;
        }
    " HAS_STD_FORMAT)

    if (HAS_STD_FORMAT)
        target_compile_definitions(rsdk_kernel_trace PRIVATE HAS_STD_FORMAT)
    else()
        message(STATUS "std::format not available, using fmt library")
        FetchContent_Declare(
            fmt
            URL https://github.com/fmtlib/fmt/archive/refs/tags/11.0.2.tar.gz
            DOWNLOAD_EXTRACT_TIMESTAMP TRUE
        )
        FetchContent_MakeAvailable(fmt)
        target_compile_definitions(rsdk_kernel_trace PRIVATE FMT_HEADER_ONLY)
        target_include_directories(rsdk_kernel_trace PRIVATE ${fmt_SOURCE_DIR}/include)
    endif()
endif()
