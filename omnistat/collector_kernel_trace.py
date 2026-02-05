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
import csv
import logging
import threading
import time
from collections import OrderedDict
from io import StringIO

from flask import Flask, jsonify, request

from omnistat.collector_base import EndpointCollector


class KernelTrace(EndpointCollector):
    def __init__(self, config: configparser.ConfigParser, route: Flask.route, interval: float):
        logging.debug("Initializing kernel trace collector")

        self.__interval_ms = max(1, int(interval * 1_000))
        self.__window_ms = 30_000

        # Unprocessed dispatch data, almost the same as recieved from
        # rsdk-based library, but parsed to extract specific fields
        self.__dispatches = []
        self.__dispatches_lock = threading.Lock()

        # Accumulated metric values for. Keys and values are both tuples:
        #   Keys: (gpu_id, kernel_name)
        #   Values: (duration, num_dispatches)
        self.__values = {}

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

        route("/kernel_trace", methods=["POST"])(self.handleRequest)

    def handleRequest(self):
        try:
            csv_data = request.data.decode("utf-8").strip()
            csv_file = StringIO(csv_data)
            csv_reader = csv.reader(csv_file)

            dispatches = []
            for gpu_id, kernel, start_ns, end_ns in csv_reader:
                start_ns = int(start_ns)
                end_ns = int(end_ns)
                duration_ns = end_ns - start_ns
                dispatch = (gpu_id, kernel, end_ns, duration_ns)
                dispatches.append(dispatch)

            with self.__dispatches_lock:
                self.__dispatches.extend(dispatches)

            return jsonify({"status": "ok"}), 200

        except Exception as e:
            return jsonify({"error": str(e)}), 400

    def updateMetrics(self):
        logging.debug("Checking kernel tracing data...")
        last_bin = self.__process_dispatches()

        # Return all time-series data outside of the accumulation window
        return self.__extract_metrics(last_bin - self.__window_ms)

    def flushMetrics(self):
        logging.debug("Flushing kernel tracing data...")
        last_bin = self.__process_dispatches()

        # Return all time-series data
        return self.__extract_metrics(last_bin)

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
                dispatches = self.__dispatches.copy()
                self.__dispatches.clear()

        for gpu_id, name, end_ns, duration_ns in dispatches:
            end_ms = (end_ns + self.__offset_ns) // 1_000_000
            end_bin = ((end_ms // self.__interval_ms) * self.__interval_ms) + self.__interval_ms

            if end_bin < first_bin or end_bin > last_bin:
                logging.info(f"Ignore out of range dispatch of kernel {name} = {end_bin}")
                continue

            key = (gpu_id, name)
            if not key in self.__values:
                self.__values[key] = (0, 0)

            num_dispatches, total_duration = self.__values[key]
            value = (num_dispatches + 1, total_duration + duration_ns)
            self.__values[key] = value
            self.__ts[end_bin][key] = value

        return last_bin

    def __extract_metrics(self, cutoff_bin):
        """Extract accumulated kernel metrics as database entries

        Pops time-series bins from the internal buffer (self.__ts) up to the
        specified cutoff point and converts them into metric entries ready for
        database ingestion. Bins after the cutoff remain in the buffer.

        Args:
            cutoff_bin: Extract bins up to and including this timestamp (in
                ms). Bins after this point remain in the buffer for future
                accumulation.

        Returns:
            List of metric entries, where each entry is a list:
            [metric_name, labels_string, value, timestamp_bin].

            Two metrics are generated per kernel dispatch:
            - omnistat_kernel_dispatch_count: number of dispatches
            - omnistat_kernel_total_duration_ns: cumulative duration in nanoseconds
        """
        entries = []

        num_push_intervals = 0
        for interval_bin, _ in self.__ts.items():
            if interval_bin > cutoff_bin:
                break
            num_push_intervals += 1

        push_intervals = []
        for _ in range(num_push_intervals):
            item = self.__ts.popitem(last=False)
            push_intervals.append(item)

        for interval_bin, kernels in push_intervals:
            for (gpu_id, name), (num_dispatches, total_duration) in kernels.items():
                entries.extend(
                    [
                        [
                            "omnistat_kernel_dispatch_count",
                            f'card="{gpu_id}",kernel="{name}"',
                            num_dispatches,
                            interval_bin,
                        ],
                        [
                            "omnistat_kernel_total_duration_ns",
                            f'card="{gpu_id}",kernel="{name}"',
                            total_duration,
                            interval_bin,
                        ],
                    ]
                )

        return entries
