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

import configparser
import logging
import threading
import time
from collections import OrderedDict, defaultdict

import orjson
from flask import Flask, request

from omnistat.collector_trace_base import BinnedTraceCollector


class KernelTrace(BinnedTraceCollector):
    def __init__(self, config: configparser.ConfigParser, route: Flask.route, interval: float):
        logging.debug("Initializing kernel trace collector")

        super().__init__(interval)

        # Unprocessed dispatch data, almost the same as recieved from
        # rsdk-based library, but parsed to extract specific fields
        self.__dispatches = []
        self.__lock = threading.Lock()

        # Accumulated metric values. Keys are tuples, values are lists:
        #   Keys: (gpu_id, kernel_name)
        #   Values: [num_dispatches, total_duration]
        self.__values = defaultdict(lambda: [0, 0])

        # Buffer to accumulate time series data before pushing it to the
        # database. This buffer is necessary for two different scenarios: 1)
        # to handle long-running kernels, and 2) to handle applications or
        # sections with a low rate of kernel dispatches.
        self.__ts = self._new_series()

        route("/kernel_trace", methods=["POST"])(self.handleRequest)

    def handleRequest(self):
        try:
            # Parse JSON array of arrays
            records = orjson.loads(request.data)

            dispatches = []
            for gpu_id, kernel, start_ns, end_ns in records:
                kernel_ref = self._intern(kernel)
                dispatch = (gpu_id, kernel_ref, end_ns, end_ns - start_ns)
                dispatches.append(dispatch)

            with self.__lock:
                self.__dispatches.extend(dispatches)

            return "", 204

        except Exception as e:
            return str(e), 400

    def updateMetrics(self):
        logging.debug("Checking kernel tracing data...")
        self.__process()
        return

    def formatMetrics(self, label_defaults, flush=False):
        last_bin = self.__process()
        bins = self._pop_bins(self.__ts, self._cutoff(last_bin, flush))
        return self.__format(bins, label_defaults)

    def __process(self):
        """Process pending dispatches and update time-series bins

        Consumes dispatch data from the queue (self.__dispatches) and performs
        two key operations:
        1. Extends the time-series buffer (self.__ts) to include bins up to the
           current time, creating empty bins as needed at self.__interval_ms
           intervals.
        2. Accumulates dispatch metrics using a dual-tracking approach:
           - self.__ts: Snapshots of these totals at specific time bins
           - self.__values: Global running totals per (gpu_id, kernel_name)

        Each dispatch is assigned to a bin based on its end timestamp. The
        snapshot stored in self.__ts[bin] represents the cumulative state of
        all dispatches that completed by that bin.

        Returns:
            int: The last (most recent) bin in the time series (in ms).
        """
        dispatches = []
        first_bin, last_bin = self._extend_bins(self.__ts)

        if len(self.__dispatches) > 0:
            with self.__lock:
                dispatches = self.__dispatches
                self.__dispatches = []

        for gpu_id, name, end_ns, duration_ns in dispatches:
            end_bin = self._bin_for(end_ns)

            if not self._in_window(end_bin, first_bin, last_bin):
                logging.info(f"Ignore out of range dispatch of kernel {name} = {end_bin}")
                self._late_records += 1
                continue

            key = (gpu_id, name)
            value = self.__values[key]
            value[0] += 1
            value[1] += duration_ns
            self.__ts[end_bin][key] = value[:]

        return last_bin

    def __format(self, bins, label_defaults):
        for interval_bin, kernels in bins:
            for (gpu_id, name), value in kernels.items():
                yield f'omnistat_kernel_dispatch_count{{{label_defaults},card="{gpu_id}",kernel="{name}"}} {value[0]} {interval_bin}'.encode()
                yield b"\n"
                yield f'omnistat_kernel_total_duration_ns{{{label_defaults},card="{gpu_id}",kernel="{name}"}} {value[1]} {interval_bin}'.encode()
                yield b"\n"
            yield f"omnistat_kernel_dropped_dispatches{{{label_defaults}}} {self._late_records} {interval_bin}".encode()
            yield b"\n"
