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

# Prometheus data collector for HPC systems.
#
# Supporting monitor class to implement a prometheus data collector with one
# or more custom collector(s).
# --

import configparser
import importlib.resources
import logging
import os
import platform
import re
import sys
from pathlib import Path

from prometheus_client import CollectorRegistry, generate_latest

from omnistat import utils


class Monitor:
    def __init__(self, config, logFile=None):

        if logFile:
            hostname = platform.node().split(".", 1)[0]
            logging.basicConfig(
                format=f"[{hostname}: %(asctime)s] %(message)s",
                level=logging.INFO,
                filename=logFile,
                datefmt="%H:%M:%S",
            )
        else:
            logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stdout)

        self.runtimeConfig = {}

        self.runtimeConfig["collector_enable_rocm_smi"] = config["omnistat.collectors"].getboolean(
            "enable_rocm_smi", True
        )
        self.runtimeConfig["collector_enable_rms"] = config["omnistat.collectors"].getboolean("enable_rms", False)
        self.runtimeConfig["collector_enable_amd_smi"] = config["omnistat.collectors"].getboolean(
            "enable_amd_smi", False
        )
        self.runtimeConfig["collector_enable_network"] = config["omnistat.collectors"].getboolean(
            "enable_network", True
        )
        self.runtimeConfig["collector_enable_vendor_counters"] = config["omnistat.collectors"].getboolean(
            "enable_vendor_counters", False
        )

        # verify only one SMI collector is enabled
        if self.runtimeConfig["collector_enable_rocm_smi"] and self.runtimeConfig["collector_enable_amd_smi"]:
            logging.error("")
            logging.error("[ERROR]: Only one SMI GPU data collector may be configured at a time.")
            logging.error("")
            logging.error('Please choose either "enable_rocm_smi" or "enable_amd_smi" in runtime config')
            sys.exit(1)

        self.runtimeConfig["collector_enable_amd_smi_process"] = config["omnistat.collectors"].getboolean(
            "enable_amd_smi_process", False
        )
        self.runtimeConfig["collector_enable_events"] = config["omnistat.collectors"].getboolean("enable_events", False)
        self.runtimeConfig["collector_port"] = config["omnistat.collectors"].get("port", 8001)
        self.runtimeConfig["collector_rocm_path"] = config["omnistat.collectors"].get("rocm_path", "/opt/rocm")
        self.runtimeConfig["collector_ras_ecc"] = config["omnistat.collectors"].getboolean("enable_ras_ecc", True)
        self.runtimeConfig["collector_cu_occupancy"] = config["omnistat.collectors"].getboolean(
            "enable_cu_occupancy", False
        )
        self.runtimeConfig["collector_power_capping"] = config["omnistat.collectors"].getboolean(
            "enable_power_cap", False
        )
        self.runtimeConfig["collector_vcn"] = config["omnistat.collectors"].getboolean("enable_vcn", False)

        self.runtimeConfig["collector_enable_rocprofiler"] = config["omnistat.collectors"].getboolean(
            "enable_rocprofiler", False
        )

        allowed_ips = config["omnistat.collectors"].get("allowed_ips", "127.0.0.1")
        # convert comma-separated string into list
        self.runtimeConfig["collector_allowed_ips"] = re.split(r",\s*", allowed_ips)
        logging.info("Allowed query IPs = %s" % self.runtimeConfig["collector_allowed_ips"])

        # additional RMS collector controls
        if self.runtimeConfig["collector_enable_rms"] == True:
            self.jobDetection = {}
            self.runtimeConfig["rms_collector_annotations"] = config["omnistat.collectors.rms"].getboolean(
                "enable_annotations", False
            )
            self.jobDetection["mode"] = config["omnistat.collectors.rms"].get("job_detection_mode", "file-based")
            self.jobDetection["file"] = config["omnistat.collectors.rms"].get(
                "job_detection_file", "/tmp/omni_rmsjobinfo"
            )
            self.jobDetection["stepfile"] = config["omnistat.collectors.rms"].get(
                "step_detection_file", "/tmp/omni_rmsjobinfo_step"
            )
            if config.has_option("omnistat.collectors.rms", "host_skip"):
                self.runtimeConfig["rms_collector_host_skip"] = config["omnistat.collectors.rms"]["host_skip"]

        self.runtimeConfig["rocprofiler_metrics"] = []
        if config.has_option("omnistat.collectors.rocprofiler", "metrics"):
            self.runtimeConfig["rocprofiler_metrics"] = config["omnistat.collectors.rocprofiler"]["metrics"].split(",")

        # defined global prometheus metrics
        self.__globalMetrics = {}
        self.__registry_global = CollectorRegistry()

        # define desired collectors
        self.__collectors = []

        # allow for disablement of resource manager data collector via regex match
        if self.runtimeConfig["collector_enable_rms"]:
            if config.has_option("omnistat.collectors.rms", "host_skip"):
                host_skip = utils.removeQuotes(config["omnistat.collectors.rms"]["host_skip"])
                hostname = platform.node().split(".", 1)[0]
                p = re.compile(host_skip)
                if p.match(hostname):
                    self.runtimeConfig["collector_enable_rms"] = False
                    logging.info("Disabling RMS collector via host_skip match (%s)" % host_skip)

        logging.debug("Completed collector initialization (base class)")
        return

    def initMetrics(self):

        if self.runtimeConfig["collector_enable_vendor_counters"]:
            from omnistat.collector_pm_counters import PM_COUNTERS

            self.__collectors.append(PM_COUNTERS())

        if self.runtimeConfig["collector_enable_network"]:
            from omnistat.collector_network import NETWORK

            self.__collectors.append(NETWORK())

        if self.runtimeConfig["collector_enable_rocm_smi"]:
            from omnistat.collector_smi import ROCMSMI

            self.__collectors.append(ROCMSMI(runtimeConfig=self.runtimeConfig))
        if self.runtimeConfig["collector_enable_amd_smi"]:
            from omnistat.collector_smi_v2 import AMDSMI

            self.__collectors.append(AMDSMI(runtimeConfig=self.runtimeConfig))
        if self.runtimeConfig["collector_enable_amd_smi_process"]:
            from omnistat.collector_smi_process import AMDSMIProcess

            self.__collectors.append(AMDSMIProcess())
        if self.runtimeConfig["collector_enable_rms"]:
            from omnistat.collector_rms import RMSJob

            self.__collectors.append(
                RMSJob(
                    annotations=self.runtimeConfig["rms_collector_annotations"],
                    jobDetection=self.jobDetection,
                )
            )
        if self.runtimeConfig["collector_enable_events"]:
            from omnistat.collector_events import ROCMEvents

            self.__collectors.append(ROCMEvents())

        if self.runtimeConfig["collector_enable_rocprofiler"]:
            from omnistat.collector_rocprofiler import rocprofiler

            self.__collectors.append(
                rocprofiler(self.runtimeConfig["collector_rocm_path"], self.runtimeConfig["rocprofiler_metrics"])
            )

        # Initialize all metrics
        for collector in self.__collectors:
            collector.registerMetrics()

        # Gather metrics on startup
        for collector in self.__collectors:
            collector.updateMetrics()

    def updateAllMetrics(self):
        for collector in self.__collectors:
            collector.updateMetrics()
        return generate_latest()
