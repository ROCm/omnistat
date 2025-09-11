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
"omnistat_hardware_counter" metric, with individual cards and counter names
denoted by labels. Example:

omnistat_hardware_counter{source="gpu",card="0",name="SQ_WAVES"} 0.0
omnistat_hardware_counter{source="gpu",card="0",name="SQ_INSTS"} 0.0
omnistat_hardware_counter{source="gpu",card="0",name="TOTAL_64_OPS"} 0.0
omnistat_hardware_counter{source="gpu",card="0",name="SQ_INSTS_VALU"} 0.0
omnistat_hardware_counter{source="gpu",card="0",name="TA_BUSY_avr"} 0.0
"""

import json
import logging
import os
import sys

from prometheus_client import Gauge, generate_latest

from omnistat.collector_base import Collector

try:
    from omnistat.rocprofiler_sdk_extension import get_samplers, initialize
except ModuleNotFoundError as e:
    logging.error(f"Missing ROCProfiler-SDK extension: build with installation required")
    sys.exit(4)

STRATEGIES = ["gpu-id", "periodic"]

initialize()


class rocprofiler_sdk(Collector):
    def __init__(self, config):
        logging.debug("Initializing rocprofiler_sdk collector")

        counters = '[["GRBM_COUNT"]]'
        strategy = "gpu-id"

        section = "omnistat.collectors.rocprofiler_sdk"
        if config.has_section(section):
            counters = config[section].get("counters", counters)
            strategy = config[section].get("distribution_strategy", strategy)

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

        if strategy not in STRATEGIES:
            logging.error(f"ERROR: Invalid distribution strategy: {strategy}")
            sys.exit(4)

        self.__samplers = get_samplers()
        self.__strategy = strategy if len(counters) > 1 else None
        self.__metric = None

        if self.__strategy == "gpu-id" or self.__strategy is None:
            self.__update_method = self.updateMetricsConstant

            # Distribute a set of counters to each sampler, cycling through
            # counters if there are more samplers than sets of counters.
            self.__names = []
            for i in range(len(self.__samplers)):
                self.__names.append(counters[i % len(counters)])

            try:
                for i, sampler in enumerate(self.__samplers):
                    sampler.start(self.__names[i])
            except Exception as e:
                logging.error(f"ERROR: {e}")
                sys.exit(4)

        elif self.__strategy == "periodic":
            self.__update_method = self.updateMetricsPeriodic

            # All GPUs track all counters, and we keep track of the active
            # counter set with __current_set.
            self.__names = counters
            self.__current_set = 0

            try:
                # Start all available profiles so they are cached during
                # initialization and sampling remains more stable afterwards.
                for i in range(len(self.__names)):
                    for sampler in self.__samplers:
                        sampler.start(self.__names[i])
                        sampler.stop()

                for sampler in self.__samplers:
                    sampler.start(self.__names[self.__current_set])
            except Exception as e:
                logging.error(f"ERROR: {e}")
                sys.exit(4)

    def registerMetrics(self):
        logging.info(f"Number of GPU devices = {len(self.__samplers)}")

        if self.__strategy is None:
            logging.info(f"Counters = {self.__names[0]}")
        else:
            logging.info(f"Distribution strategy = {self.__strategy}")
            strategy_set_names = {
                "gpu-id": "GPU ID",
                "periodic": "Period",
            }
            set_name = strategy_set_names[self.__strategy]
            for i, names in enumerate(self.__names):
                logging.info(f"{set_name} {i} counters = {names}")

        metric = "omnistat_hardware_counter"
        labels = ["source", "card", "name"]
        self.__metric = Gauge(metric, "Performance counter data from rocprofiler-sdk", labelnames=labels)
        logging.info(f"--> [registered] {metric} (gauge)")

    def updateMetrics(self):
        self.__update_method()

    def updateMetricsConstant(self):
        for i, sampler in enumerate(self.__samplers):
            values = sampler.sample()
            for j, name in enumerate(self.__names[i]):
                self.__metric.labels("gpu", i, name).set(values[j])
        return

    def updateMetricsPeriodic(self):
        for i, sampler in enumerate(self.__samplers):
            values = sampler.sample()
            for j, name in enumerate(self.__names[self.__current_set]):
                self.__metric.labels("gpu", i, name).set(values[j])

        self.__current_set = (self.__current_set + 1) % len(self.__names)

        try:
            for _, sampler in enumerate(self.__samplers):
                sampler.stop()
                sampler.start(self.__names[self.__current_set])
        except Exception as e:
            logging.error(f"ERROR: {e}")
            sys.exit(4)

        return
