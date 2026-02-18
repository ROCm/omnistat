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

# Prometheus data collector for HPC systems.
#
# Supporting monitor class to implement a prometheus data collector with one
# or more custom collector(s).
# --

import configparser
import importlib.resources
import json
import logging
import os
import platform
import re
import sys
import time
from pathlib import Path

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from omnistat import utils


class Monitor:
    def __init__(self, config, logFile=None, mode="Unknown", interval_secs="Unknown", push_interval_mins="Unknown"):

        self.config = config  # cache runtime configuration

        logLevel = os.environ.get("OMNISTAT_LOG_LEVEL", "INFO").upper()
        if logFile:
            hostname = platform.node().split(".", 1)[0]
            logging.basicConfig(
                format=f"[{hostname}: %(asctime)s] %(message)s",
                level=logLevel,
                filename=logFile,
                datefmt="%H:%M:%S",
            )
        else:
            logging.basicConfig(format="%(message)s", level=logLevel, stream=sys.stdout)

        # embed additional info into runtime config
        if not self.config.has_section("omnistat.internal"):
            self.config.add_section("omnistat.internal")
            self.config["omnistat.internal"]["mode"] = mode
            self.config["omnistat.internal"]["interval_secs"] = str(interval_secs)
            try:
                if int(push_interval_mins) > 0:
                    self.config["omnistat.internal"]["push_interval_secs"] = str(60 * push_interval_mins)
                else:
                    self.config["omnistat.internal"]["push_interval_secs"] = "Unknown"
            except (ValueError, TypeError):
                self.config["omnistat.internal"]["push_interval_secs"] = "Unknown"

        self.enforce_global_runtime_constraints()

        allowed_ips = config["omnistat.collectors"].get("allowed_ips", "127.0.0.1")
        allowed_ips = re.split(r",\s*", allowed_ips)
        logging.info("Allowed query IPs = %s" % allowed_ips)

        # defined global prometheus metrics
        self.__globalMetrics = {}
        self.__registry_global = CollectorRegistry()

        # initialize collection of data collectors
        self.__collectors = []

        # load amd-smi python interface if needed (done here to pre-load before forking)
        if self.__enable_amd_smi or self.__enable_events:
            rocm_path = config["omnistat.collectors"].get("rocm_path", "/opt/rocm")
            utils.load_amdsmi_interface(rocm_path)

        logging.debug("Completed collector initialization (base class)")
        return

    def enforce_global_runtime_constraints(self):
        collectors = self.config["omnistat.collectors"]

        # verify only one SMI collector is enabled
        self.__enable_rocm_smi = collectors.getboolean("enable_rocm_smi", True)
        self.__enable_amd_smi = collectors.getboolean("enable_amd_smi", True)
        self.__enable_events = collectors.getboolean("enable_events", False)
        if self.__enable_rocm_smi and self.__enable_amd_smi:
            logging.error("")
            logging.error("[ERROR]: Only one SMI GPU data collector may be configured at a time.")
            logging.error("")
            logging.error('Please choose either "enable_rocm_smi" or "enable_amd_smi" in runtime config')
            sys.exit(1)

        # verify only one rocprofiler collector is enabled
        enable_rocprofiler_legacy = collectors.getboolean("enable_rocprofiler_legacy", False)
        enable_rocprofiler = collectors.getboolean("enable_rocprofiler", False)
        if enable_rocprofiler_legacy and enable_rocprofiler:
            logging.error("")
            logging.error("[ERROR]: Only one rocprofiler data collector may be configured at a time.")
            logging.error("")
            logging.error('Please choose either "enable_rocprofiler" or "enable_rocprofiler_legacy" in runtime config')
            sys.exit(1)

        # check for host exemption for RMS collector
        if collectors.getboolean("enable_rms", False):
            if self.config.has_option("omnistat.collectors.rms", "host_skip"):
                host_skip = utils.removeQuotes(self.config["omnistat.collectors.rms"]["host_skip"])
                hostname = platform.node().split(".", 1)[0]
                p = re.compile(host_skip)
                if p.match(hostname):
                    self.config["omnistat.collectors"]["enable_rms"] = "False"
                    logging.info("Disabling RMS collector via host_skip match (%s)" % host_skip)

    def initMetrics(self):

        # Load collector definitions
        _json_path = os.path.join(os.path.dirname(__file__), "collector_definitions.json")

        try:
            with open(_json_path, "r") as f:
                data = json.load(f)
                COLLECTORS = data["collectors"]
        except Exception as e:
            logging.error(f"Failed to load collector definitions from file: {_json_path}: {e}")
            sys.exit(1)

        for collector in COLLECTORS:
            runtime_option = collector["runtime_option"]
            default = collector["enabled_by_default"]
            if runtime_option:
                enabled = self.config["omnistat.collectors"].getboolean(runtime_option, default)
            else:
                enabled = default
            if enabled:
                module = importlib.import_module(collector["file"])
                cls = getattr(module, collector["class_name"])
                self.__collectors.append(cls(config=self.config))

        # Initialize all metrics
        prefix_filter = utils.PrefixFilter("   ")
        for collector in self.__collectors:
            logging.info("\nRegistering metrics for collector: %s" % collector.__class__.__name__)
            logging.getLogger().addFilter(prefix_filter)
            collector.registerMetrics()
            logging.getLogger().removeFilter(prefix_filter)

        # Register performance runtime metric(s)
        self.__subtimers = self.config["omnistat.collectors"].getboolean("enable_perf_collector_subtimers", False)
        labels = ["collector"]
        logging.info(
            "\nRegistering performance metrics for collector timing (subtimers enabled = %s)" % self.__subtimers
        )

        self.__perfMetric = Gauge(
            "omnistat_perf_runtime_seconds", "Time to complete one data collection sample in seconds", labelnames=labels
        )

        # Gather metrics on startup
        self.updateAllMetrics()

    def updateAllMetrics(self):
        start_time_total = time.perf_counter()

        for collector in self.__collectors:
            start_time = time.perf_counter()
            collector.updateMetrics()
            if self.__subtimers:
                elapsed_time = time.perf_counter() - start_time
                self.__perfMetric.labels(collector.__class__.__name__).set(elapsed_time)
        latest = generate_latest()

        elapsed_time_total = time.perf_counter() - start_time_total
        self.__perfMetric.labels("total").set(elapsed_time_total)

        return latest
