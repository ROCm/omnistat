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

SAMPLING_MODES = ["constant", "gpu-id", "periodic"]

initialize()


class rocprofiler_sdk(Collector):
    def __init__(self, config):
        """
        Initialize the rocprofiler_sdk collector.

        Args:
            config (configparser.ConfigParser): Cached copy of runtime configuration.
        """
        logging.debug("Initializing rocprofiler collector")

        profile = "default"
        counters = None
        mode = "constant"

        rocprofiler_section_path = "omnistat.collectors.rocprofiler"
        if config.has_section(rocprofiler_section_path):
            section = config[rocprofiler_section_path]
            profile = section.get("profile", profile)

            # Keep backwards-compatibility with the old collector based on
            # rocprofiler v1/v2, which used the option "metrics" formatted as a
            # simple comma-separated list of counters.
            if "metrics" in section:
                logging.error('WARNING: Converting deprecated rocprofiler option "metrics"')
                counters = section["metrics"].split(",")

        profile_section_path = f"omnistat.collectors.rocprofiler.{profile}"
        if config.has_section(profile_section_path):
            section = config[profile_section_path]
            mode = section.get("sampling_mode", mode)
            if "counters" in section:
                try:
                    counters = json.loads(section.get("counters"))
                except json.JSONDecodeError as e:
                    logging.error(f"ERROR: Decoding list of counters as JSON: {e}")
                    sys.exit(4)

        if counters is None:
            logging.error(f'ERROR: Required "counters" option not found in profile "{profile}"')
            sys.exit(4)

        if not isinstance(counters, list) or len(counters) <= 0:
            logging.error("ERROR: Unexpected list of counters")
            sys.exit(4)

        # If counters is a flat list, convert to list of lists.
        if not all(isinstance(i, list) for i in counters):
            counters = [counters]

        if mode not in SAMPLING_MODES:
            logging.error(f"ERROR: Invalid sampling mode: {mode}")
            sys.exit(4)

        if mode == "constant" and len(counters) != 1:
            logging.error('ERROR: "constant" sampling mode requires a single set of counters')
            sys.exit(4)

        if mode == "gpu-id" and len(counters) == 1:
            logging.error('ERROR: "gpu-id" sampling mode requires multiple sets of counters')
            sys.exit(4)

        self.__samplers = get_samplers()
        self.__mode = mode
        self.__metric = None

        if self.__mode in ["constant", "gpu-id"]:
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

        elif self.__mode == "periodic":
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
        """Register metrics of interest"""
        logging.info(f"Number of GPU devices = {len(self.__samplers)}")
        logging.info(f"Sampling mode = {self.__mode}")

        if self.__mode == "constant":
            logging.info(f"Counters = {self.__names[0]}")
        else:
            sample_names = {
                "gpu-id": "GPU ID",
                "periodic": "Period",
            }
            sample_name = sample_names[self.__mode]
            for i, counters in enumerate(self.__names):
                logging.info(f"{sample_name} {i} counters = {counters}")

        metric = "omnistat_hardware_counter"
        labels = ["source", "card", "name"]
        self.__metric = Gauge(metric, "Performance counter data from rocprofiler-sdk", labelnames=labels)
        logging.info(f"--> [registered] {metric} (gauge)")

    def updateMetrics(self):
        """Update registered metrics of interest"""
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
