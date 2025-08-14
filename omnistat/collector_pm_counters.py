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

"""PM counter monitoring

Scans available telemetry in /sys/cray/pm_counters for compute node
power-related data.
"""

import json
import logging
import os
import platform
import re
import sys
from pathlib import Path

from prometheus_client import Gauge

import omnistat.utils as utils
from omnistat.collector_base import Collector


class PM_COUNTERS(Collector):
    def __init__(self, annotations=False, jobDetection=None):
        logging.debug("Initializing pm_counter data collector")

        self.__prefix = "omnistat_vendor_"
        # currently just supporting a single vendor
        self.__pm_counter_dir = "/sys/cray/pm_counters"
        self.__vendor = "cray"
        self.__unit_mapping = {"W": "watts", "J": "joules"}
        self.__skipnames = ["power_cap", "startup", "freshness", "raw_scan_hz", "version", "generation", "_temp"]
        self.__gpumetrics = ["accel"]

        # metric data structure for host oriented metrics
        self.__pm_files_gpu = []  # entries: (gauge metric, filepath of source data)

        # metric data structure for gpu oriented
        self.__pm_files_host = []  # entries: (gauge metric, filepath, gpuindex)

    def registerMetrics(self):
        """Register metrics of interest"""

        definedMetrics = {}

        logging.info("collector_pm_counters: scanning files in %s" % self.__pm_counter_dir)
        if os.path.isdir(self.__pm_counter_dir) is False:
            logging.warning("--> PM counter directory %s does not exist" % self.__pm_counter_dir)
            logging.warning("--> skipping PM counter data collection")
            return
        for file in Path(self.__pm_counter_dir).iterdir():
            logging.debug("Examining PM counter filename: %s" % file)
            if any(name in str(file) for name in self.__skipnames):
                logging.debug("--> Skipping PM file: %s" % file)
            else:
                # check units
                try:
                    with open(file, "r") as f:
                        data = f.readline().strip().split()
                        if data[1] in self.__unit_mapping:
                            units = self.__unit_mapping[data[1]]
                            units_short = data[1]
                        else:
                            logging.error("Unknown unit specified in file: %s" % file)
                            continue
                except:
                    logging.error("Error determining units from contents of %s" % file)
                    continue

                for gpumetric in self.__gpumetrics:
                    pattern = rf"^({gpumetric})(\d+)_(.*)$"
                    match = re.match(pattern, file.name)
                    if match:
                        metric_name = match.group(1) + "_" + match.group(3) + f"_{units}"
                        gpu_id = int(match.group(2))
                        if metric_name in definedMetrics:
                            gauge = definedMetrics[metric_name]
                        else:
                            description = f"GPU {match.group(3)} ({units_short})"
                            gauge = Gauge(self.__prefix + metric_name, description, labelnames=["accel", "vendor"])
                            definedMetrics[metric_name] = gauge
                            logging.info(
                                "--> [Registered] %s -> %s (gauge)" % (self.__prefix + metric_name, description)
                            )

                        metric_entry = (gauge, str(file), gpu_id)
                        self.__pm_files_gpu.append(metric_entry)

                    else:
                        metric_name = file.name + f"_{units}"
                        description = f"Node-level {metric_name} ({units_short})"
                        gauge = Gauge(self.__prefix + metric_name, description, labelnames=["vendor"])
                        metric_entry = (gauge, str(file))
                        self.__pm_files_host.append(metric_entry)
                        logging.info("--> [registered] %s -> %s (gauge)" % (self.__prefix + metric_name, description))

    def updateMetrics(self):
        """Update registered metrics of interest"""

        # Host-level data...
        for entry in self.__pm_files_host:
            gaugeMetric = entry[0]
            filePath = entry[1]
            try:
                with open(filePath, "r") as f:
                    data = f.readline().strip().split()
                    gaugeMetric.labels(vendor=self.__vendor).set(float(data[0]))
            except:
                pass

        # GPU data...
        for entry in self.__pm_files_gpu:
            gaugeMetric = entry[0]
            filePath = entry[1]
            gpuIndex = entry[2]
            try:
                with open(filePath, "r") as f:
                    data = f.readline().strip().split()
                    gaugeMetric.labels(accel=gpuIndex, vendor=self.__vendor).set(data[0])

            except:
                pass

        return
