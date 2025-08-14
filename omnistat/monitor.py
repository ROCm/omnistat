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
import importlib.util
import inspect
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

        # convert comma-separated string with allowed IPs into list
        allowed_ips = config["omnistat.collectors"].get("allowed_ips", "127.0.0.1")
        self.allowed_ips = re.split(r",\s*", allowed_ips)
        logging.info("Allowed query IPs = %s" % self.allowed_ips)

        # verify only one SMI collector is enabled
        enable_rocm_smi = config["omnistat.collectors"].getboolean("enable_rocm_smi", True)
        enable_amd_smi = config["omnistat.collectors"].getboolean("enable_amd_smi", False)
        if enable_rocm_smi and enable_amd_smi:
            logging.error("")
            logging.error("[ERROR]: Only one SMI GPU data collector may be configured at a time.")
            logging.error("")
            logging.error('Please choose either "enable_rocm_smi" or "enable_amd_smi" in runtime config')
            sys.exit(1)

        # allow for disablement of resource manager data collector via regex match
        if config.has_section("omnistat.collectors.rms"):
            host_skip = config["omnistat.collectors.rms"].get("host_skip", "login.*")
            pattern = re.compile(utils.removeQuotes(host_skip))
            hostname = platform.node().split(".", 1)[0]
            if pattern.match(hostname):
                logging.info(f"Disabling RMS collector via host_skip match ({host_skip}): {hostname}")
                config.set("omnistat.collectors", "rms", "False")

        self.__config = config
        self.__registry_global = CollectorRegistry()
        self.__collectors = []

        logging.debug("Completed monitor initialization")
        return

    def initMetrics(self):
        # Locations to search for collectors. These are used for both,
        # configuration file sections and Python module hierarchy.
        locations = ["omnistat.collectors", "omnistat.collectors.contrib"]

        # Configuration options starting with "enable_" under collector
        # locations are used to enable/disable collectors.
        pattern = r"^enable_([\w]+)$"

        # Subcollectors are collectors that depend on other collectors and are
        # not initialized on their own.
        subcollectors = {"ras_ecc", "power_capping", "cu_occupancy", "vcn"}

        for location in locations:
            if not self.__config.has_section(location):
                continue

            # Convert configuration section to arguments to be passed to all
            # collectors in this location when initializing them.
            location_args = dict(self.__config[location])

            # Loop over all options in a given location to find collectors
            # that need to be initialized: identify "enable_*" options set to
            # True that aren't subcollectors.
            for option, _ in self.__config.items(location):
                m = re.search(pattern, option)
                if m is None:
                    continue

                try:
                    enabled = self.__config[location].getboolean(option, False)
                except ValueError:
                    logging.warning(f"Ignoring {option}: value is not a boolean.")
                    continue

                if not enabled:
                    continue

                name = m.group(1)
                if name in subcollectors:
                    continue

                module_name = f"{location}.{name}"
                spec = importlib.util.find_spec(module_name)
                if spec is None:
                    logging.warning(f"Ignoring {option}: unable to find the {module_name} module")
                    continue

                module_args = {}
                if self.__config.has_section(module_name):
                    module_args = dict(self.__config[module_name])

                # Merge location options and collector options, keeping values
                # from the latter if there is any conflict.
                #
                # For example, the following configuration file:
                #   [omnistat.collectors]
                #   enable_rocm_smi = True
                #   enable_rocprofiler = True
                #   rocm_path = /opt/rocm
                #   [omnistat.collectors.rocprofiler]
                #   metrics = SQ_WAVES
                #
                # Results in the following dictionary used to initialize the
                # rocprofiler collector:
                #   {
                #     enable_rocm_smi: True,
                #     enable_rocprofiler: True,
                #     rocm_path: "/opt/rocm",
                #     metrics: "SQ_WAVES",
                #   }
                args = location_args | module_args

                # Emit warning if there is a conflict between location and
                # module options, just to be clear about the values being
                # used.
                conflict_options = location_args.keys() & module_args.keys()
                for option in conflict_options:
                    value = module_args[option]
                    logging.warning(f"Option {option} defined twice: using {value} to initialize {name}.")

                module = importlib.import_module(module_name)
                collector_class = getattr(module, f"{name}")
                logging.info(f"Loading {name} collector")

                # Identify required arguments to ensure they exist in the
                # configuration file.
                required = set()
                signature = inspect.signature(collector_class.__init__)
                parameters = list(signature.parameters.values())
                for p in parameters[1:]:
                    if p.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD and p.default == inspect.Parameter.empty:
                        required.add(p.name)

                missing = required - args.keys()
                if len(missing):
                    logging.error(f"Missing required options to initialize {module_name}: {', '.join(missing)}")
                    sys.exit(4)

                try:
                    self.__collectors.append(collector_class(**args))
                except TypeError as t:
                    logging.error(f"Failed to initialize {module_name}: {t}")
                    sys.exit(4)

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
