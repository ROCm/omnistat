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

import configparser
import logging
import threading
import time
from collections import OrderedDict, defaultdict

import orjson
from flask import Flask, request

from omnistat.collector_base import EndpointCollector


class KernelTrace(EndpointCollector):
    def __init__(self, config: configparser.ConfigParser, route: Flask.route, interval: float):
        logging.debug("Initializing kernel trace collector")

        self.__interval_ms = max(1, int(interval * 1_000))
        self.__window_ms = 15_000

        # Unprocessed dispatch data, almost the same as recieved from
        # rsdk-based library, but parsed to extract specific fields
        self.__dispatches = []
        self.__dispatches_lock = threading.Lock()

        # Accumulated metric values. Keys are tuples, values are lists:
        #   Keys: (gpu_id, kernel_name)
        #   Values: [num_dispatches, total_duration]
        self.__values = defaultdict(lambda: [0, 0])

        # Buffer to accumulate time series data before pushing it to the
        # database. This buffer is necessary for two different scenarios: 1)
        # to handle long-running kernels, and 2) to handle applications or
        # sections with a low rate of kernel dispatches.
        self.__ts = OrderedDict()

        # Initialize time series window buffer
        time_ms = time.time_ns() // 1_000_000
        current_bin = ((time_ms // self.__interval_ms) + 1) * self.__interval_ms
        self.__ts[current_bin] = {}

        # Measure offset applied to GPU timestamps to convert them to unix
        # timestamps in the same format as the time series database
        boot_time_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        unix_time_ns = time.time_ns()
        self.__offset_ns = unix_time_ns - boot_time_ns

        # Pool of interned kernel name strings. Each unique name is stored
        # once and all references (dispatches, time-series keys, yielded
        # tuples) share the same object, avoiding duplicate kernel name
        # strings which can be 600+ byte strings.
        self.__kernel_names = {}

        route("/kernel_trace", methods=["POST"])(self.handleRequest)

    def handleRequest(self):
        try:
            # Parse JSON array of arrays
            records = orjson.loads(request.data)

            dispatches = []
            for gpu_id, kernel, start_ns, end_ns in records:
                kernel_ref = self.__kernel_names.get(kernel)
                if kernel_ref is None:
                    self.__kernel_names[kernel] = kernel
                    kernel_ref = kernel
                dispatch = (gpu_id, kernel_ref, end_ns, end_ns - start_ns)
                dispatches.append(dispatch)

            with self.__dispatches_lock:
                self.__dispatches.extend(dispatches)

            return "", 204

        except Exception as e:
            return str(e), 400

    def updateMetrics(self):
        logging.debug("Checking kernel tracing data...")
        self.__process_dispatches()
        return

    def formatMetrics(self, label_defaults, flush=False):
        last_bin = self.__process_dispatches()
        cutoff = last_bin if flush else last_bin - self.__window_ms
        bins = self.__pop_bins(cutoff)
        return self.__format_bins(bins, label_defaults)

    def __pop_bins(self, cutoff_bin):
        num_pop = 0
        for interval_bin in self.__ts:
            if interval_bin > cutoff_bin:
                break
            num_pop += 1
        bins = []
        for _ in range(num_pop):
            bins.append(self.__ts.popitem(last=False))
        return bins

    def __format_bins(self, bins, label_defaults):
        for interval_bin, kernels in bins:
            for (gpu_id, name), value in kernels.items():
                yield f'omnistat_kernel_dispatch_count{{{label_defaults},card="{gpu_id}",kernel="{name}"}} {value[0]} {interval_bin}'.encode()
                yield b"\n"
                yield f'omnistat_kernel_total_duration_ns{{{label_defaults},card="{gpu_id}",kernel="{name}"}} {value[1]} {interval_bin}'.encode()
                yield b"\n"

    def __process_dispatches(self):
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

        time_ms = time.time_ns() // 1_000_000
        current_bin = ((time_ms // self.__interval_ms) + 1) * self.__interval_ms
        first_bin = next(iter(self.__ts))
        last_bin = next(reversed(self.__ts))

        # Keep in-order dictionary of time series intervals
        for i in range(last_bin + self.__interval_ms, current_bin + 1, self.__interval_ms):
            self.__ts[i] = {}
            last_bin = i

        if len(self.__dispatches) > 0:
            with self.__dispatches_lock:
                dispatches = self.__dispatches
                self.__dispatches = []

        for gpu_id, name, end_ns, duration_ns in dispatches:
            end_ms = (end_ns + self.__offset_ns) // 1_000_000
            end_bin = ((end_ms // self.__interval_ms) * self.__interval_ms) + self.__interval_ms

            if end_bin < first_bin or end_bin > last_bin:
                logging.info(f"Ignore out of range dispatch of kernel {name} = {end_bin}")
                continue

            key = (gpu_id, name)
            value = self.__values[key]
            value[0] += 1
            value[1] += duration_ns
            self.__ts[end_bin][key] = value[:]

        return last_bin
