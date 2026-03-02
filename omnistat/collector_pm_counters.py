# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2023 - 2026 Advanced Micro Devices, Inc. All Rights Reserved.
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

import configparser
import logging
import os
import re
import sys
import time
import threading
from pathlib import Path

from prometheus_client import Gauge

import omnistat.utils as utils
from omnistat.collector_base import Collector


class PM_COUNTERS(Collector):
    def __init__(self, config: configparser.ConfigParser):
        """Initialize the PM_COUNTERS data collector.

        Args:
            config (configparser.ConfigParser): Cached copy of runtime configuration.
        """

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

        # usage mode details
        self.__interval = config["omnistat.internal"]["interval_secs"]
        self.__mode = config["omnistat.internal"]["mode"]
        self.__local_counter = 0
        self.__disabled = True

    def registerMetrics(self):
        """Register metrics of interest"""

        definedMetrics = {}

        logging.info("Scanning files in %s" % self.__pm_counter_dir)

        # define user-mode polling frequency based on underlying sysfs file update rate and collector sampling interval
        try:
            scan_hz_file = self.__pm_counter_dir + "/raw_scan_hz"
            with open(scan_hz_file, "r") as f:
                data = f.readline().strip().split()
                file_update_frequency_secs = 1 / float(data[0])
                logging.info(f"Underlying counter file update frequency: {file_update_frequency_secs} secs")
                if self.__mode == "user":
                    poll_interval_secs = float(self.__interval)
                else:
                    poll_interval_secs = 3.0  # assuming 3 sec or higher for system mode
                min_thread_interval = 0.5 * file_update_frequency_secs
                max_thread_interval = 0.5
                if poll_interval_secs < 2:
                    self.__pm_counter_sampling_interval = min_thread_interval
                else:
                    self.__pm_counter_sampling_interval = max_thread_interval
                logging.info(f"PM counter sampling thread interval: {self.__pm_counter_sampling_interval} seconds")

        except Exception as e:
            logging.warning("--> PM counter file %s does not exist" % scan_hz_file)
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

        # Initiate background sampler thread
        self.__cached_data = {}
        self.__num_samples = 0
        self.__polling_lock = threading.Lock()
        self.__sampler_running = True
        filenames = [entry[1] for entry in self.__pm_files_gpu] + [entry[1] for entry in self.__pm_files_host]
        self.__sampler_thread = threading.Thread(
            target=self.pm_counter_sampler,
            args=(self.__pm_counter_sampling_interval, filenames),
            daemon=True,
            name="sysfs counter sampler",
        )
        self.__sampler_thread.start()
        time.sleep(0.5)

        # additional metrics to track sampling behavior
        self.__skip_counter = Gauge(
            self.__prefix + "samples_skipped_total", "Total number of PM counter samples skipped", labelnames=["vendor"]
        )
        self.__skip_counter.labels(vendor=self.__vendor).set(0)
        self.__counter = Gauge(
            self.__prefix + "samples_total", "Total number of PM counter samples", labelnames=["vendor"]
        )
        self.__disabled = False

    def pm_counter_sampler(self, sample_interval: float, filenames: list):
        """Background thread to cache PM counter data.

        Args:
            sample_interval (float): Time in seconds between samples.
            filenames (list): List of filenames to cache PM counter data from.
        """

        # Sampling loop
        while self.__sampler_running:
            time.sleep(sample_interval)
            data = self.read_sysfs_metrics(filenames)
            with self.__polling_lock:
                self.__cached_data = data
                self.__num_samples += 1

    def read_sysfs_metrics(self, filenames: list):
        """Read PM counter data directly from sysfs files."""

        data = {}
        for filePath in filenames:
            try:
                with open(filePath, "r") as f:
                    data[filePath] = f.readline().strip().split()
            except:
                pass

        return data

    def updateMetrics(self):
        """Update metrics using cached data from separate polling thread"""

        if self.__disabled:
            return

        self.__counter.labels(vendor=self.__vendor).inc()
        with self.__polling_lock:
            data = self.__cached_data
            if self.__num_samples > self.__local_counter:
                self.__local_counter = self.__num_samples
            else:
                self.__skip_counter.labels(vendor=self.__vendor).inc()
                return

        # Host-level data...
        for entry in self.__pm_files_host:
            gaugeMetric = entry[0]
            filePath = entry[1]
            try:
                gaugeMetric.labels(vendor=self.__vendor).set(float(data[filePath][0]))
            except:
                pass

        # GPU data...
        for entry in self.__pm_files_gpu:
            gaugeMetric = entry[0]
            filePath = entry[1]
            gpuIndex = entry[2]
            try:
                gaugeMetric.labels(accel=gpuIndex, vendor=self.__vendor).set(data[filePath][0])
            except:
                pass
