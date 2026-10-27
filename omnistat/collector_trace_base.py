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
keyed by record timestamp and freezes a bin only once it is older than a hold
window, giving stragglers time to land.

The hold window must exceed the tracer's flush interval plus its HTTP timeout,
which default to 10s and 2s (rocprofiler-sdk/trace.hpp). The flush interval is
site-configurable via OMNISTAT_TRACE_MAX_INTERVAL, and this window is not, so
raising it beyond the window drops every record. Above ~120 dispatches/s the
kernel buffer watermark fires first and the lag is far smaller.
"""

import time
from collections import OrderedDict, defaultdict

from omnistat.collector_endpoint_base import EndpointCollector


def _new_pair():
    return [0, 0]


def _counters():
    """A {key: [count, value]} mapping that zero-fills on first touch."""
    return defaultdict(_new_pair)


class BinSeries:
    """One time-binned metric family.

    Each bin holds the counts for its own interval, so records arriving out of
    end-timestamp order commute. Freezing a bin folds it into the running
    totals and stages it; pop_frozen hands the frozen work to one push, which
    rebuilds cumulative values from frozen_totals.
    """

    def __init__(self):
        # Open bins, {bin: {key: [count, value]}}.
        self.bins = OrderedDict()

        # Running cumulative, {key: [count, value]}. Only moves forward, which
        # is what keeps the published counters monotonic.
        self.totals = _counters()

        # Staged for the next push; pop_frozen() hands both over and resets them.
        # - Bins frozen since the last push, [(bin, {key: [count, value]})].
        # - Totals as they stood before those bins froze, {key: [count, value]}.
        self.frozen_bins = []
        self.frozen_totals = _counters()

    def freeze(self, cutoff_bin):
        """Freeze every bin at or below cutoff_bin.

        Freezing is not publishing; called by collectors on every poll, so each
        call takes the bin or two that just aged out rather than the whole
        backlog.
        """
        num_pop = 0
        for interval_bin in self.bins:
            if interval_bin > cutoff_bin:
                break
            num_pop += 1

        if num_pop and not self.frozen_bins:
            # First freeze since the last push: capture totals before these bins move them.
            self.frozen_totals = _counters()
            self.frozen_totals.update((key, total[:]) for key, total in self.totals.items())

        for _ in range(num_pop):
            # Popping advances the first bin, so a record arriving late for it
            # is rejected rather than landing in a bin already counted.
            interval_bin, values = self.bins.popitem(last=False)

            # Totals move now, not at push time: a failed push then costs the
            # samples but not the count.
            for key, delta in values.items():
                total = self.totals[key]
                total[0] += delta[0]
                total[1] += delta[1]

            self.frozen_bins.append((interval_bin, values))

    def pop_frozen(self):
        """Hand one push the frozen work, as (frozen_bins, frozen_totals).

        Frozen here, running at the caller: the push-thread generator advances
        its copy as it emits. Both are swapped for fresh empties rather than
        cleared, so the series keeps no reference to what it handed over.
        """
        frozen_bins, self.frozen_bins = self.frozen_bins, []
        frozen_totals, self.frozen_totals = self.frozen_totals, _counters()
        return frozen_bins, frozen_totals


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
        series = BinSeries()
        series.bins[self._current_bin()] = _counters()
        return series

    def _current_bin(self):
        time_ms = time.time_ns() // 1_000_000
        return ((time_ms // self._interval_ms) + 1) * self._interval_ms

    def _bin_for(self, timestamp_ns):
        """Bin a record belongs to, from its own timestamp."""
        ms = (timestamp_ns + self._offset_ns) // 1_000_000
        return ((ms // self._interval_ms) * self._interval_ms) + self._interval_ms

    def _extend_bins(self, series, current_bin):
        """Grow the series up to current_bin. Returns (first_bin, last_bin).

        The caller supplies current_bin so a collector with more than one
        series extends them all from a single clock read, keeping their bins
        aligned by construction.

        Re-seeds when the series is empty, which happens after a flush=True
        formatMetrics call has popped everything.
        """
        bins = series.bins
        if not bins:
            bins[current_bin] = _counters()
        last_bin = next(reversed(bins))
        for interval_bin in range(last_bin + self._interval_ms, current_bin + 1, self._interval_ms):
            bins[interval_bin] = _counters()
            last_bin = interval_bin
        return next(iter(bins)), last_bin

    def _in_window(self, bin_ms, first_bin, last_bin):
        """True when a record's bin can still be placed in the series."""
        return first_bin <= bin_ms <= last_bin

    def _cutoff(self, last_bin, flush):
        """Newest bin eligible for freezing. flush=True closes everything."""
        return last_bin if flush else last_bin - self._window_ms
