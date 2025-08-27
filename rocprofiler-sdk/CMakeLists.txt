cmake_minimum_required(VERSION 3.15)
project(omnistat_rocprofiler_sdk_extension LANGUAGES CXX)

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
