# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2023 - 2025 Advanced Micro Devices, Inc. All Rights Reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# -------------------------------------------------------------------------------

"""Hardware counter collection using device mode with rocprofiler

Implements collection of user-requested hardware counters. The ROCm
runtime must be pre-installed to use this data collector. This data
collector gathers counters on a per GPU basis and exposes them using the
"omnistat_rocprofiler" metric, with individual cards and counter names
denoted by labels. The following sample highlights example counters
under the "omnistat_rocprofiler" counter for card 0:

omnistat_rocprofiler{card="0",counter="SQ_WAVES"} 0.0
omnistat_rocprofiler{card="0",counter="SQ_INSTS"} 0.0
omnistat_rocprofiler{card="0",counter="TOTAL_64_OPS"} 0.0
omnistat_rocprofiler{card="0",counter="SQ_INSTS_VALU"} 0.0
omnistat_rocprofiler{card="0",counter="TA_BUSY_avr"} 0.0
"""

import ctypes
import logging
import os
import sys

from prometheus_client import Gauge, generate_latest

from omnistat.collector_base import Collector


class rocprofiler_counter_value_t(ctypes.Structure):
    _fields_ = [
        ("value", ctypes.c_double),
    ]


class rocprofiler_device_profile_metric_t(ctypes.Structure):
    _fields_ = [
        ("metric_name", ctypes.c_char * 64),
        ("value", rocprofiler_counter_value_t),
    ]


class rocprofiler_session_id_t(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_uint64),
    ]


class rocprofiler(Collector):
    def __init__(self, config):
        logging.debug("Initializing rocprofiler data collector")

        rocm_path = "/opt/rocm"
        if config.has_section("omnistat.collectors"):
            rocm_path = section.get("rocm_path", rocm_path)

        if not config.has_section("omnistat.collectors.rocprofiler"):
            logging.error("ERROR: Missing rocprofiler collector configuration.")
            sys.exit(4)

        metric_names = config["omnistat.collectors.rocprofiler"].get("metrics", None)
        if metric_names == None:
            logging.error("ERROR: Undefined list of rocprofiler metrics.")
            sys.exit(4)

        metric_names = metric_names.split(",")
        if len(metric_names) == 0:
            logging.error(f"ERROR: Empty or unexpected list of rocprofiler metrics: {metric_names}")
            sys.exit(4)

        hip_lib = rocm_path + "/lib/libamdhip64.so"
        rocprofiler_lib = rocm_path + "/lib/librocprofiler64v2.so"

        if not os.path.isfile(hip_lib):
            logging.error("ERROR: Unable to load HIP library.")
            logging.error("--> looking for %s" % hip_lib)
            sys.exit(4)

        if not os.path.isfile(rocprofiler_lib):
            logging.error("ERROR: Unable to load rocprofiler library.")
            logging.error("--> looking for %s" % rocprofiler_lib)
            sys.exit(4)

        self.__libhip = ctypes.CDLL(hip_lib)
        self.__librocprofiler = ctypes.CDLL(rocprofiler_lib)

        logging.info("Runtime library loaded from %s" % hip_lib)
        logging.info("Runtime library loaded from %s" % rocprofiler_lib)

        num_gpus = (ctypes.c_int)()
        status = self.__libhip.hipGetDeviceCount(ctypes.byref(num_gpus))
        assert status == 0
        status = self.__librocprofiler.rocprofiler_initialize()
        assert status == 0
        logging.info("rocprofiler library API initialized")

        self.__num_gpus = num_gpus.value
        self.__names = metric_names

        self.__metric = None

        # Lists indexed by GPU ID:
        #  __sessions: stores rocprofiler device mode sessions
        #  __values: stores arrays of values returned by rocprofiler
        self.__sessions = []
        self.__values = []

        for i in range(self.__num_gpus):
            self.__sessions.append((rocprofiler_session_id_t)())
            self.__values.append((rocprofiler_device_profile_metric_t * len(self.__names))())

        logging.info(f"--> rocprofiler: number of GPUs detected = {self.__num_gpus}")
        logging.info(f"--> rocprofiler: metrics = {self.__names}")

        # Convert list of metrics to pass with ctypes
        names_bytes = [bytes(i, "utf-8") for i in self.__names]
        names_array = (ctypes.c_char_p * len(names_bytes))()
        names_array[:] = names_bytes

        # Create rocprofiler sessions for each GPU
        for i in range(self.__num_gpus):
            self.__librocprofiler.rocprofiler_device_profiling_session_create(
                names_array, len(names_array), ctypes.byref(self.__sessions[i]), 0, i
            )

        logging.info("--> rocprofiler initialized")

    def registerMetrics(self):
        metric_name = f"omnistat_rocprofiler"
        self.__metric = Gauge(metric_name, "Performance counter data from rocprofiler", labelnames=["card", "counter"])
        logging.info("--> [registered] %s (gauge)" % (metric_name))

        for i in range(self.__num_gpus):
            self.__librocprofiler.rocprofiler_device_profiling_session_start(self.__sessions[i])

    def updateMetrics(self):
        for i in range(self.__num_gpus):
            self.__librocprofiler.rocprofiler_device_profiling_session_poll(self.__sessions[i], self.__values[i])

            # Reset sessions to address issues with values (SWDEV-468600)
            self.__librocprofiler.rocprofiler_device_profiling_session_stop(self.__sessions[i])
            self.__librocprofiler.rocprofiler_device_profiling_session_start(self.__sessions[i])

        for i in range(self.__num_gpus):
            for j, name in enumerate(self.__names):
                array = self.__values[i]
                value = array[j].value.value
                self.__metric.labels(card=i, counter=name).set(value)

        return
