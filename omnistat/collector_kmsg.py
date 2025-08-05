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
from enum import IntEnum

from prometheus_client import Gauge

import omnistat.utils as utils
from omnistat.collector_base import Collector


class KmsgSeverity(IntEnum):
    EMERGENCY = 0
    ALERT = 1
    CRITICAL = 2
    ERROR = 3
    WARNING = 4
    NOTICE = 5
    INFO = 6
    DEBUG = 7


class KmsgCollector(Collector):
    def __init__(self, min_severity="ERROR", include_existing=False):
        logging.debug("Initializing kmsg collector")
        self.__name = "omnistat_num_driver_messages"
        self.__metric = None
        self.__kmsg = None

        # Lower case keywords to identify AMD GPU related kernel messages.
        keywords = ["amdgpu"]
        self.__pattern = re.compile("|".join(re.escape(k) for k in keywords))

        try:
            self.__severity_threshold = KmsgSeverity[min_severity]
        except KeyError:
            print(f"ERROR: Unsupported kmsg severity: {min_severity}")
            sys.exit(4)

        self.__severity_count = [0] * (self.__severity_threshold + 1)
        self.__include_existing = include_existing

        include = "existing and new" if include_existing else "new"
        severities = [s.name for s in KmsgSeverity if s.value <= self.__severity_threshold]
        logging.info(f"--> kmsg: report {include} messages with these severities: {', '.join(severities)}")

    def registerMetrics(self):
        description = "Number of driver messages in the kernel log buffer"
        self.__metric = Gauge(self.__name, description, labelnames=["driver", "severity"])
        logging.info(f"--> [registered] {self.__name} -> {description} (gauge)")

        try:
            self.__kmsg = os.open("/dev/kmsg", os.O_NONBLOCK)
            if not self.__include_existing:
                os.lseek(self.__kmsg, 0, os.SEEK_END)
        except PermissionError:
            print("Error: Permission denied reading /dev/kmsg", file=sys.stderr)
            sys.exit(4)
        except FileNotFoundError:
            print("Error: /dev/kmsg not found", file=sys.stderr)
            sys.exit(4)
        except Exception as e:
            print(f"Unexpected error: {e}", file=sys.stderr)
            sys.exit(4)

    def _parse_message(self, data):
        data = data.decode("utf-8").strip()

        match = re.match(r"^(\d+),(\d+),(\d+),([^;]*);(.*)", data)
        if match:
            priority = int(match.group(1))
            severity = priority % 8
            facility = priority // 8
            message = match.group(5)
            return severity, message

        return None

    def _is_amdgpu(self, message):
        match = re.search(self.__pattern, message.lower())
        return False if match is None else True

    def updateMetrics(self):
        """Update registered metrics of interest"""

        # Process all new messages in the kmsg buffer.
        while True:
            try:
                data = os.read(self.__kmsg, 8192)
                result = self._parse_message(data)
                if result is None:
                    continue
                severity, message = result
                if severity <= self.__severity_threshold and self._is_amdgpu(message):
                    self.__severity_count[severity] += 1
            except BrokenPipeError:
                # Indicates messages have been overwritten in the circular
                # buffer. Subsequent reads will return records again.
                pass
            except BlockingIOError:
                # Reached last message, nothing else to read in this sample.
                break

        for severity, count in enumerate(self.__severity_count):
            self.__metric.labels(driver="amdgpu", severity=KmsgSeverity(severity).name).set(count)

        return
