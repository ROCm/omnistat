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

"""Time-binning shared by the trace endpoint collectors.

Trace records carry their own timestamps and arrive late: the tracer batches
them and POSTs on a flush, so a record describing t=0 may not arrive until
several seconds later. Each collector therefore keeps a series of time bins
keyed by record timestamp and releases a bin only once it is older than a hold
window, giving stragglers time to land.

The hold window must exceed the tracer's flush interval plus its HTTP timeout
(10s + 2s, both in rocprofiler-sdk/trace.hpp). Above ~120 dispatches/s the
kernel buffer watermark fires first and the lag is far smaller.
"""

import time
from collections import OrderedDict

from omnistat.collector_endpoint_base import EndpointCollector


class BinnedTraceCollector(EndpointCollector):
    def __init__(self, interval: float, window_ms: int = 15_000):
        self._interval_ms = max(1, int(interval * 1_000))
        self._window_ms = window_ms

        # GPU timestamps are CLOCK_BOOTTIME; bins are unix milliseconds.
        self._offset_ns = time.time_ns() - time.clock_gettime_ns(time.CLOCK_BOOTTIME)

        # Records that arrived too late (or too early) to be placed in the
        # retained series. Both collectors count this the same way; they differ
        # only in the metric name they publish it under.
        self._late_records = 0

        # Interned label strings. Kernel names in particular can exceed 600
        # bytes and repeat across every dispatch, so keys and yielded tuples
        # share one object per distinct string.
        self._strings = {}

    def _intern(self, s):
        ref = self._strings.get(s)
        if ref is None:
            self._strings[s] = s
            ref = s
        return ref

    def _new_series(self):
        """An empty bin series, seeded with the current bin."""
        ts = OrderedDict()
        ts[self._current_bin()] = {}
        return ts

    def _current_bin(self):
        time_ms = time.time_ns() // 1_000_000
        return ((time_ms // self._interval_ms) + 1) * self._interval_ms

    def _bin_for(self, timestamp_ns):
        """Bin a record belongs to, from its own timestamp."""
        ms = (timestamp_ns + self._offset_ns) // 1_000_000
        return ((ms // self._interval_ms) * self._interval_ms) + self._interval_ms

    def _extend_bins(self, ts):
        """Grow the series up to the current bin. Returns (first_bin, last_bin).

        Re-seeds when the series is empty, which happens after a flush=True
        formatMetrics call has popped everything.
        """
        current_bin = self._current_bin()
        if not ts:
            ts[current_bin] = {}
        last_bin = next(reversed(ts))
        for interval_bin in range(last_bin + self._interval_ms, current_bin + 1, self._interval_ms):
            ts[interval_bin] = {}
            last_bin = interval_bin
        return next(iter(ts)), last_bin

    def _in_window(self, bin_ms, first_bin, last_bin):
        """True when a record's bin can still be placed in the series."""
        return first_bin <= bin_ms <= last_bin

    def _cutoff(self, last_bin, flush):
        """Newest bin eligible for release. flush=True releases everything."""
        return last_bin if flush else last_bin - self._window_ms

    def _pop_bins(self, ts, cutoff_bin):
        """Remove and return [(bin, values)] for every bin at or below cutoff."""
        num_pop = 0
        for interval_bin in ts:
            if interval_bin > cutoff_bin:
                break
            num_pop += 1
        return [ts.popitem(last=False) for _ in range(num_pop)]
