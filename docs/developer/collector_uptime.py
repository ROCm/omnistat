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

import logging

from prometheus_client import Gauge

from omnistat.collector_base import Collector


class uptime(Collector):
    def __init__(self, **kwargs):
        logging.debug("Initializing node uptime event collector")
        self.__metrics = {}  # method storage for Prometheus metrics
        self.__kernelver = None  # method storage for kernel version

    # Required child methods
    def registerMetrics(self):

        # gather local Linux kernel to store as a label
        with open("/proc/version", "r") as f:
            self.__kernelver = f.readline().split()[2]

        metricName = "node_uptime_secs"
        description = "System uptime (secs)"
        labels = ["kernel"]
        self.__metrics[metricName] = Gauge(metricName, description, labels)
        logging.info("--> [registered] %s -> %s (gauge)" % (metricName, description))
        return

    def updateMetrics(self):
        # snarf current uptime; file contains two floats - first number is uptime in seconds
        with open("/proc/uptime", "r") as f:
            uptime = float(f.readline().split()[0])
            self.__metrics["node_uptime_secs"].labels(kernel=self.__kernelver).set(uptime)
        return
