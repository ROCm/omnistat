# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2025 Advanced Micro Devices, Inc. All Rights Reserved.
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

"""Performance counter collection using rocprofiler-sdk device counting

Implements collection of hardware counters. The ROCm runtime must be
pre-installed to use this data collector, and Omnistat must be built and
installed with the ROCProfiler-SDK extension. This data collector gathers
counters on a per GPU basis and exposes them using the
"omnistat_gpu_performance_counter" metric, with individual cards and counter
names denoted by labels. Example:

omnistat_gpu_performance_counter{card="0",name="SQ_WAVES"} 0.0
omnistat_gpu_performance_counter{card="0",name="SQ_INSTS"} 0.0
omnistat_gpu_performance_counter{card="0",name="TOTAL_64_OPS"} 0.0
omnistat_gpu_performance_counter{card="0",name="SQ_INSTS_VALU"} 0.0
omnistat_gpu_performance_counter{card="0",name="TA_BUSY_avr"} 0.0
"""

import json
import logging
import os
import sys

from prometheus_client import Gauge, generate_latest

from omnistat.collector_base import Collector
from omnistat.rocprofiler_sdk_extension import get_samplers, initialize

initialize()


class rocprofiler_sdk(Collector):
    def __init__(self, counters='[["GRBM_COUNT"]]'):
        logging.debug("Initializing rocprofiler_sdk collector")

        try:
            counters = json.loads(counters)
        except json.JSONDecodeError as e:
            logging.error(f"ERROR: Decoding list of counters as JSON: {e}")
            sys.exit(4)

        if not isinstance(counters, list) or len(counters) == 0:
            logging.error("ERROR: Unexpected list of counters.")
            sys.exit(4)

        # If counters is a flat list, convert to list of lists.
        if not all(isinstance(i, list) for i in counters):
            counters = [counters]

        self.__names = []
        self.__samplers = get_samplers()
        self.__metric = None

        # Assign sets of counters to each sampler, cycling through counters if
        # there are more samplers than sets of counters.
        for i in range(len(self.__samplers)):
            self.__names.append(counters[i % len(counters)])

        try:
            for i, sampler in enumerate(self.__samplers):
                sampler.start(self.__names[i])
        except Exception as e:
            logging.error(f"ERROR: {e}")
            sys.exit(4)

        logging.info(f"--> rocprofiler-sdk: number of GPUs = {len(self.__samplers)}")
        for i, names in enumerate(self.__names):
            logging.info(f"--> rocprofiler-sdk: GPU ID {i} counter names = {names}")

    def registerMetrics(self):
        metric_name = f"omnistat_gpu_performance_counter"
        self.__metric = Gauge(metric_name, "Performance counter data from rocprofiler-sdk", labelnames=["card", "name"])
        logging.info("--> [registered] %s (gauge)" % (metric_name))

    def updateMetrics(self):
        for i, sampler in enumerate(self.__samplers):
            values = sampler.sample()
            for j, name in enumerate(self.__names[i]):
                self.__metric.labels(card=i, name=name).set(values[j])
        return
