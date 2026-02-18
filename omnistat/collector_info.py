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

"""Info metric

Implements an info metric to log execution details including sampling mode, schema,
code version, and intervals in user mode.  Examples:

omnistat_info{mode="system",schema="1.0",version="1.9.0"} 1.0
omnistat_info{mode="user",schema="1.0",version="1.9.0",interval_secs="1",push_interval_secs="300"} 1.0
"""

import configparser
import logging

from prometheus_client import Gauge

import omnistat.utils as utils
from omnistat.collector_base import Collector


class INFO(Collector):
    def __init__(self, config: configparser.ConfigParser):
        """Initialize info metric.

        Args:
            config (configparser.ConfigParser): Cached copy of runtime configuration.
        """
        logging.debug(f"Initializing {self.__class__.__name__} data collector")

        self.__version = utils.getVersion(includeGitSHA=False)
        self.__schema = 1.0

        self.__mode = config["omnistat.internal"]["mode"]
        self.__interval = config["omnistat.internal"]["interval_secs"]
        self.__push_interval = config["omnistat.internal"]["push_interval_secs"]

    def registerMetrics(self):
        """Register metrics of interest"""

        labels = ["version", "mode", "schema"]
        if self.__mode == "user":
            labels += ["interval_secs", "push_interval_secs"]

        self.__info = Gauge("omnistat_info", "Info metric", labelnames=labels)

        if self.__mode == "user":
            self.__info.labels(
                version=self.__version,
                mode=self.__mode,
                schema=self.__schema,
                interval_secs=self.__interval,
                push_interval_secs=self.__push_interval,
            ).set(1)
        else:
            self.__info.labels(version=self.__version, mode=self.__mode, schema=self.__schema).set(1)

    def updateMetrics(self):
        """Update registered metrics of interest"""

        return
