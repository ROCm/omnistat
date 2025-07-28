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

import logging
import os
import re
import sys
from collections import defaultdict

from prometheus_client import Gauge

import omnistat.utils as utils
from omnistat.collector_base import Collector


class KmsgCollector(Collector):
    def __init__(self):
        logging.debug("Initializing kmsg collector")
        self.__name = "omnistat_num_kmsg_events"
        self.__metric = None
        self.__kmsg = None

    def registerMetrics(self):
        description = "Number of kmsg events"
        self.__metric = Gauge(self.__name, description, labelnames=["severity"])
        logging.info(f"--> [registered] {self.__name} -> {description} (gauge)")

        try:
            self.__kmsg = os.open("/dev/kmsg", os.O_NONBLOCK)
            os.lseek(self.__kmsg, 0, os.SEEK_END)
        except PermissionError:
            print("Error: Permission denied reading /dev/kmsg", file=sys.stderr)
            return 1
        except FileNotFoundError:
            print("Error: /dev/kmsg not found", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Unexpected error: {e}", file=sys.stderr)
            return 1

    def _parse_message(self, data):
        data = data.decode("utf-8").strip()

        # Default to INFO severity
        severity = 6
        message = data

        match = re.match(r"^(\d+),(\d+),(\d+),([^;]*);(.*)$", data)
        if match:
            priority = int(match.group(1))
            severity = priority % 8
            facility = priority // 8
            message = match.group(5)

        return severity, message

    def _is_amdgpu(self, message):
        keywords = [
            "amdgpu",
            "amd-vi",
            "radeon",
            "drm:amdgpu",
            "amd_iommu",
            "amd-vi",
            "amdgpu",
        ]
        message = message.lower()
        return any(keyword in message for keyword in keywords)

    def updateMetrics(self):
        """Update registered metrics of interest"""

        severity_count = defaultdict(int)

        # Process all new messages in the kmsg buffer.
        while True:
            try:
                data = os.read(self.__kmsg, 8192)
                severity, msg = self._parse_message(data)
                if self._is_amdgpu(msg):
                    severity_count[severity] += 1
            except BrokenPipeError:
                # Indicates messages have been overwritten in the circular
                # buffer. Subsequent reads will return records again.
                pass
            except BlockingIOError:
                # Reached last message, nothing else to read in this sample.
                break

        if severity_count:
            for severity, count in severity_count.items():
                self.__metric.labels(severity=severity).set(count)

        return
