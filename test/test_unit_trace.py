import configparser
import random
import threading
from collections import defaultdict
from unittest.mock import Mock, patch

import orjson
import pytest
from flask import Flask

from omnistat.collector_trace_base import BinnedTraceCollector
from omnistat.collector_trace_kernel import KernelTrace
from omnistat.collector_trace_rccl import RcclTrace, nranks_label, size_bucket

# ==============================================================================
# Shared helpers and fixtures
# ==============================================================================


def _totals(series):
    """Sum per-bin deltas across every live bin.

    Reproduces what the old global accumulators held, so assertions can stay
    value-based now that bins carry deltas rather than cumulative snapshots.
    """
    out = defaultdict(lambda: [0, 0])
    for values in series.bins.values():
        for key, delta in values.items():
            out[key][0] += delta[0]
            out[key][1] += delta[1]
    return out


def kernel_totals(collector):
    return _totals(collector._KernelTrace__ts)


def collective_totals(collector):
    return _totals(collector._RcclTrace__collective_ts)


def comm_totals(collector):
    """(gpu_id, nranks) -> [creates, init_ns]."""
    return _totals(collector._RcclTrace__comm_ts)


def s_to_ns(s):
    """Convert seconds (int or float) to nanoseconds."""
    return int(s * 1_000_000_000)


# The mock_time fixture starts both unix and boot clocks at INIT_TIME_NS (1 second),
# making _offset_ns = 0 (GPU timestamps equal unix timestamps). With a 1-second
# interval, the initial bin is at FIRST_BIN_MS (2000 ms).
INIT_TIME_NS = s_to_ns(1)
INTERVAL_S = 1.0
INTERVAL_MS = 1000
FIRST_BIN_S = 2
FIRST_BIN_MS = 2000
LABEL_DEFAULTS = 'instance="node.test"'


def set_time(mock_time, seconds):
    """Advance the mocked wall clock to the given time in seconds."""
    mock_time["time_ns"].return_value = s_to_ns(seconds)


def metric_lines(collector, flush=True):
    """Decode formatMetrics output into a list of metric lines."""
    return [line.decode() for line in collector.formatMetrics(LABEL_DEFAULTS, flush=flush) if line != b"\n"]


def post(collector, flask_app, data):
    """POST a JSON payload to the collector's endpoint, returning its response."""
    with flask_app.test_request_context(data=data, content_type="application/json"):
        return collector.handleRequest()


@pytest.fixture
def mock_time():
    """Fixture to mock time functions for deterministic tests."""
    with patch("time.time_ns") as mock_time_ns, patch("time.clock_gettime_ns") as mock_clock_gettime_ns:
        # unix time and boot time both at INIT_TIME_NS, so __offset_ns=0.
        mock_time_ns.return_value = INIT_TIME_NS
        mock_clock_gettime_ns.return_value = INIT_TIME_NS
        yield {"time_ns": mock_time_ns, "clock_gettime_ns": mock_clock_gettime_ns}


@pytest.fixture
def flask_app():
    """Create a Flask app for testing request contexts."""
    return Flask(__name__)


# ==============================================================================
# 1. Time binning (BinnedTraceCollector)
# ==============================================================================
#
# Both trace collectors inherit this behaviour, so it is tested once here rather
# than through either subclass. The base is abstract, so the tests drive it
# through a minimal concrete stub.


class StubBinned(BinnedTraceCollector):
    """Minimal concrete subclass: the base's binning, no record handling."""

    def handleRequest(self):
        return "", 204

    def updateMetrics(self):
        return

    def formatMetrics(self, label_defaults, flush=False):
        return iter(())


@pytest.fixture
def binned(mock_time):
    """StubBinned at t=1s with a 1s interval and _offset_ns = 0."""
    return StubBinned(INTERVAL_S)


class TestBinInit:
    def test_offset_is_unix_minus_boot(self, mock_time):
        """GPU timestamps are CLOCK_BOOTTIME; the offset shifts them to unix."""
        mock_time["time_ns"].return_value = s_to_ns(10)
        mock_time["clock_gettime_ns"].return_value = s_to_ns(3)
        assert StubBinned(INTERVAL_S)._offset_ns == s_to_ns(7)

    def test_interval_from_seconds(self, mock_time):
        assert StubBinned(1.0)._interval_ms == 1000
        assert StubBinned(0.5)._interval_ms == 500  # the production default
        assert StubBinned(0.001)._interval_ms == 1  # clamped, never zero
        assert StubBinned(0.0)._interval_ms == 1

    def test_new_series_seeds_the_current_bin(self, binned):
        assert list(binned._new_series().bins.keys()) == [FIRST_BIN_MS]


class TestBinAssignment:
    def test_bin_for_files_under_the_upper_edge(self, binned):
        """Inside a bin files under its upper edge; exactly on a boundary
        belongs to the next bin, not the one closing."""
        assert binned._bin_for(s_to_ns(1) + 1) == FIRST_BIN_MS
        assert binned._bin_for(s_to_ns(2)) == 3000

    def test_bin_for_applies_the_offset(self, mock_time):
        mock_time["time_ns"].return_value = s_to_ns(10)
        mock_time["clock_gettime_ns"].return_value = s_to_ns(3)
        assert StubBinned(INTERVAL_S)._bin_for(s_to_ns(1)) == 9000  # +7s offset

    def test_extend_bins_grows_to_now(self, binned, mock_time):
        series = binned._new_series()
        set_time(mock_time, 4)
        first, last = binned._extend_bins(series, binned._current_bin())
        assert (first, last) == (FIRST_BIN_MS, 5000)
        assert list(series.bins.keys()) == [2000, 3000, 4000, 5000]

    def test_extend_bins_reseeds_when_empty(self, binned):
        """A series emptied by a flush=True release is re-seeded, not left bare."""
        series = binned._new_series()
        series.bins.clear()
        assert binned._extend_bins(series, binned._current_bin()) == (FIRST_BIN_MS, FIRST_BIN_MS)

    def test_in_window_is_inclusive_at_both_ends(self, binned):
        assert binned._in_window(2000, 2000, 4000)
        assert binned._in_window(4000, 2000, 4000)
        assert not binned._in_window(1999, 2000, 4000)
        assert not binned._in_window(4001, 2000, 4000)


class TestBinFreezing:
    def test_cutoff_holds_a_window_unless_flushing(self, binned):
        assert binned._cutoff(20_000, flush=False) == 20_000 - binned._window_ms
        assert binned._cutoff(20_000, flush=True) == 20_000

    def test_freezing_moves_bins_up_to_and_including_cutoff(self, binned):
        series = binned._new_series()
        series.bins.clear()
        series.bins[1000], series.bins[2000], series.bins[3000] = {}, {}, {}

        series.freeze(1000 - 1)  # cutoff below all: nothing moves
        assert series.pop_frozen() == ([], {})

        series.freeze(2000)  # inclusive at the cutoff
        bins, _ = series.pop_frozen()
        assert bins == [(1000, {}), (2000, {})]
        assert list(series.bins.keys()) == [3000]

        series.bins.clear()
        series.freeze(9999)  # empty series is not an error
        assert series.pop_frozen() == ([], {})

    def test_freezing_keeps_deltas_and_advances_totals(self, binned):
        """Frozen bins still hold deltas; only the series total moves."""
        series = binned._new_series()
        series.bins.clear()
        series.bins[1000] = {"k": [3, 30]}
        series.bins[2000] = {"k": [2, 20]}

        series.freeze(2000)
        bins, frozen_totals = series.pop_frozen()

        assert bins == [(1000, {"k": [3, 30]}), (2000, {"k": [2, 20]})]  # deltas
        assert series.totals["k"] == [5, 50]  # total advances as bins close
        assert frozen_totals == {}  # nothing had been published before these bins

    def test_frozen_totals_carries_the_starting_point_across_pops(self, binned):
        """A later publish resumes from where the previous one ended.

        Totals advance when a bin freezes, so an abandoned publish cannot lose
        them; frozen_totals is what lets the next publish emit cumulative values
        without the bins ever storing one.
        """
        series = binned._new_series()
        series.bins.clear()

        series.bins[1000] = {"k": [3, 30]}
        series.freeze(1000)
        bins, frozen_totals = series.pop_frozen()
        assert (bins, frozen_totals) == ([(1000, {"k": [3, 30]})], {})

        series.bins[2000] = {"k": [2, 20]}
        series.freeze(2000)
        bins, frozen_totals = series.pop_frozen()
        assert bins == [(2000, {"k": [2, 20]})]
        assert frozen_totals == {"k": [3, 30]}  # resumes from the first publish

    def test_frozen_totals_is_detached_from_the_series_total(self, binned):
        """The publish-time accumulator must not alias series.totals.

        __format mutates the totals copy as it emits, on the push thread, while
        the polling thread keeps advancing totals.
        """
        series = binned._new_series()
        series.bins.clear()
        series.bins[1000] = {"k": [1, 10]}
        series.freeze(1000)
        series.pop_frozen()

        series.bins[2000] = {"k": [1, 10]}
        series.freeze(2000)
        _, frozen_totals = series.pop_frozen()

        frozen_totals["k"][0] += 99  # as __format would, while emitting
        assert series.totals["k"] == [2, 20]  # untouched

    def test_pop_frozen_is_empty_until_something_is_frozen(self, binned):
        series = binned._new_series()
        assert series.pop_frozen() == ([], {})


class TestInterning:
    def test_equal_values_share_one_object(self, binned):
        """Labels repeat on every record; interning keeps one copy per value."""
        assert binned._intern("ncclAllReduce") is binned._intern("nccl" + "AllReduce")

    def test_distinct_values_kept_apart(self, binned):
        binned._intern("one")
        binned._intern("two")
        assert len(binned._strings) == 2

    def test_late_records_starts_at_zero(self, binned):
        assert binned._late_records == 0


# ==============================================================================
# 2. Kernel tracing
# ==============================================================================

COUNT_METRIC = "omnistat_kernel_dispatch_count"
DURATION_METRIC = "omnistat_kernel_total_duration_ns"
DROPPED_METRIC = "omnistat_kernel_late_records"


def make_dispatch(gpu_id, kernel, end_ns, duration_ns):
    """Build a dispatch tuple matching __process_dispatches input format:
    (gpu_id, kernel_name, end_ns, duration_ns)."""
    return (gpu_id, kernel, end_ns, duration_ns)


def collect_metrics(kernel_collector, flush=True):
    """Kernel metric lines split into dispatch/duration vs dropped-count lines."""
    lines = metric_lines(kernel_collector, flush)
    return {
        "all": lines,
        "kernel": [l for l in lines if DROPPED_METRIC not in l],
        "dropped": [l for l in lines if DROPPED_METRIC in l],
    }


@pytest.fixture
def kernel_collector(mock_time):
    """KernelTrace instance at t=1s with 1s interval and __offset_ns=0."""
    config = configparser.ConfigParser()
    return KernelTrace(config, Mock(), INTERVAL_S)


class TestKernelProcess:
    def test_empty_dispatches(self, kernel_collector, mock_time):
        """With no dispatches, bins are extended but values stay empty."""
        assert len(kernel_collector._KernelTrace__dispatches) == 0

        start_s = FIRST_BIN_S
        duration_s = 10
        for i in range(start_s, start_s + duration_s):
            set_time(mock_time, i)
            last_bin = kernel_collector._KernelTrace__process()

            assert last_bin == (i * INTERVAL_MS) + INTERVAL_MS
            assert len(kernel_totals(kernel_collector)) == 0

            bins = kernel_collector._KernelTrace__ts.bins
            assert last_bin in bins
            assert len(bins[last_bin]) == 0

        assert len(kernel_collector._KernelTrace__ts.bins) == duration_s + 1

    def test_single_dispatch(self, kernel_collector, mock_time):
        """A single dispatch is accumulated and snapshotted into the correct bin."""
        # end_ns at t=2s -> end_bin=3000
        dispatch = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2), duration_ns=42)
        kernel_collector._KernelTrace__dispatches.append(dispatch)

        set_time(mock_time, 3)
        last_bin = kernel_collector._KernelTrace__process()

        values = kernel_totals(kernel_collector)
        ts = kernel_collector._KernelTrace__ts.bins

        key = ("0", "kernel_a")
        assert key in values
        assert values[key] == [1, 42]

        assert 3000 in ts
        assert key in ts[3000]
        assert ts[3000][key] == [1, 42]

        assert len(kernel_collector._KernelTrace__dispatches) == 0

    def test_same_kernel_same_bin(self, kernel_collector, mock_time):
        """Two dispatches of the same kernel in the same bin accumulate."""
        # Both end between t=2s and t=3s -> end_bin=3000
        d1 = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=30)
        d2 = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.8), duration_ns=50)
        kernel_collector._KernelTrace__dispatches.extend([d1, d2])

        set_time(mock_time, 3)
        kernel_collector._KernelTrace__process()

        values = kernel_totals(kernel_collector)
        ts = kernel_collector._KernelTrace__ts.bins
        key = ("0", "kernel_a")

        assert values[key] == [2, 80]
        assert ts[3000][key] == [2, 80]

    def test_published_values_are_cumulative(self, kernel_collector, mock_time):
        """Bins hold per-bin deltas; published values are cumulative totals."""
        key = ("0", "kernel_a")
        series = kernel_collector._KernelTrace__ts

        for end_s, dur in ((2.5, 40), (3.5, 25)):
            kernel_collector._KernelTrace__dispatches.append(
                make_dispatch("0", "kernel_a", end_ns=s_to_ns(end_s), duration_ns=dur)
            )
            set_time(mock_time, int(end_s) + 1)
            kernel_collector._KernelTrace__process()

        # Each bin counts only the dispatches that ended within it...
        assert series.bins[3000][key] == [1, 40]
        assert series.bins[4000][key] == [1, 25]

        # ...and stays that way once released; the cumulative view is built
        # only when the metrics are published.
        set_time(mock_time, 30)
        counts = [
            int(float(line.split("}")[1].split()[0]))
            for line in collect_metrics(kernel_collector)["kernel"]
            if line.startswith(COUNT_METRIC)
        ]
        assert counts == [1, 2]

    def test_multiple_kernels_multiple_gpus(self, kernel_collector, mock_time):
        """4 dispatches across 2 GPUs and 2 kernels all go into the same bin."""
        dispatches = [
            make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.1), duration_ns=11),
            make_dispatch("0", "kernel_b", end_ns=s_to_ns(2.2), duration_ns=22),
            make_dispatch("1", "kernel_a", end_ns=s_to_ns(2.3), duration_ns=33),
            make_dispatch("1", "kernel_b", end_ns=s_to_ns(2.4), duration_ns=44),
        ]
        kernel_collector._KernelTrace__dispatches.extend(dispatches)

        set_time(mock_time, 3)
        kernel_collector._KernelTrace__process()

        values = kernel_totals(kernel_collector)
        ts = kernel_collector._KernelTrace__ts.bins

        assert len(values) == 4
        assert len(ts[3000]) == 4
        assert values[("0", "kernel_a")] == [1, 11]
        assert values[("1", "kernel_b")] == [1, 44]

    def test_assigned_to_past_bin(self, kernel_collector, mock_time):
        """A dispatch with an old end timestamp goes into its correct past bin."""
        # Current time t=4s (last_bin=5000). Dispatch end at t=1.5s -> end_bin=2000 (past).
        dispatch = make_dispatch("0", "kernel_a", end_ns=s_to_ns(1.5), duration_ns=15)
        kernel_collector._KernelTrace__dispatches.append(dispatch)

        set_time(mock_time, 4)
        kernel_collector._KernelTrace__process()

        ts = kernel_collector._KernelTrace__ts.bins
        key = ("0", "kernel_a")

        assert key in ts[2000]
        assert key not in ts.get(3000, {})
        assert key not in ts.get(5000, {})

    def test_out_of_range_dropped_and_counted(self, kernel_collector, mock_time):
        """Dispatches either side of the retained range are dropped and counted."""
        for end_s in (0.5, 100):  # before first_bin, after last_bin
            kernel_collector._KernelTrace__dispatches.append(
                make_dispatch("0", "kernel_a", end_ns=s_to_ns(end_s), duration_ns=10)
            )

        set_time(mock_time, 3)
        kernel_collector._KernelTrace__process()

        assert kernel_totals(kernel_collector) == {}
        assert kernel_collector._late_records == 2


class TestKernelFormatMetrics:
    def test_flush_publishes_what_no_flush_holds(self, kernel_collector, mock_time):
        """The same recent bin is held with flush=False and released with flush=True."""
        kernel_collector._KernelTrace__dispatches.append(
            make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=50)
        )
        set_time(mock_time, 3)

        assert collect_metrics(kernel_collector, flush=False)["all"] == []

        metrics = collect_metrics(kernel_collector, flush=True)
        assert len(metrics["kernel"]) == 2
        assert len(kernel_collector._KernelTrace__ts.bins) == 0

    def test_no_flush_publishes(self, kernel_collector, mock_time):
        """Bins older than the hold window are released with flush=False."""
        dispatch = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=50)
        kernel_collector._KernelTrace__dispatches.append(dispatch)
        set_time(mock_time, 3)
        kernel_collector._KernelTrace__process()

        # Advance past the hold window so bins 2000..6000 fall outside it.
        # Derived from the collector rather than hardcoded: the window is
        # coupled to the tracer's flush interval and has been retuned before.
        window_s = kernel_collector._window_ms // 1000
        set_time(mock_time, 5 + window_s)
        metrics = collect_metrics(kernel_collector, flush=False)

        assert len(metrics["kernel"]) == 2
        assert len(metrics["dropped"]) >= 1

    def test_exact_prometheus_line_format(self, kernel_collector, mock_time):
        """Verify the exact bytes of the Prometheus output lines."""
        dispatch = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=77)
        kernel_collector._KernelTrace__dispatches.append(dispatch)

        set_time(mock_time, 3)
        metrics = collect_metrics(kernel_collector)

        # Find the lines for bin 3000 (which contains the kernel data)
        bin3000_lines = [l for l in metrics["all"] if l.endswith("3000")]
        assert bin3000_lines[0] == f'{COUNT_METRIC}{{{LABEL_DEFAULTS},card="0",kernel="kernel_a"}} 1 3000'
        assert bin3000_lines[1] == f'{DURATION_METRIC}{{{LABEL_DEFAULTS},card="0",kernel="kernel_a"}} 77 3000'
        assert bin3000_lines[2] == f"{DROPPED_METRIC}{{{LABEL_DEFAULTS}}} 0 3000"

    def test_dropped_dispatch_metric(self, kernel_collector, mock_time):
        """Verify formatMetrics includes dropped dispatch count with correct value."""
        in_range = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=10)
        early = make_dispatch("0", "kernel_b", end_ns=s_to_ns(0.5), duration_ns=20)
        late = make_dispatch("0", "kernel_c", end_ns=s_to_ns(100), duration_ns=30)
        kernel_collector._KernelTrace__dispatches.extend([in_range, early, late])

        set_time(mock_time, 3)
        metrics = collect_metrics(kernel_collector)

        # The bin containing the in-range dispatch should show dropped count = 2
        assert len(metrics["dropped"]) >= 1
        assert f"{DROPPED_METRIC}{{{LABEL_DEFAULTS}}} 2 3000" in metrics["dropped"]

    def test_multiple_calls_state_transition(self, kernel_collector, mock_time):
        """After flush=True consumes all bins, a second flush=True yields only dropped count lines."""
        dispatch = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=50)
        kernel_collector._KernelTrace__dispatches.append(dispatch)

        set_time(mock_time, 3)
        first = collect_metrics(kernel_collector)
        assert len(first["kernel"]) == 2

        set_time(mock_time, 4)
        second = collect_metrics(kernel_collector)
        assert len(second["kernel"]) == 0
        assert all(DROPPED_METRIC in l for l in second["all"])


class TestKernelHandleRequest:
    def test_json_format(self, kernel_collector, flask_app):
        """Parses JSON array of arrays and builds correct dispatch tuples."""
        json_data = b'[[0,"kernel_a",1000000000,2000000000],[1,"kernel_b",3000000000,4000000000]]'

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            response, status = kernel_collector.handleRequest()

            assert status == 204
            assert len(kernel_collector._KernelTrace__dispatches) == 2
            assert kernel_collector._KernelTrace__dispatches[0] == (0, "kernel_a", 2000000000, 1000000000)

    def test_empty_json_array(self, kernel_collector, flask_app):
        """An empty JSON array returns 204 and adds no dispatches."""
        with flask_app.test_request_context(data=b"[]", content_type="application/json"):
            response, status = kernel_collector.handleRequest()

            assert status == 204
            assert len(kernel_collector._KernelTrace__dispatches) == 0

    def test_complex_kernel_names(self, kernel_collector, flask_app):
        """Kernel names with C++ template syntax are preserved verbatim."""
        json_data = orjson.dumps([[0, "std::vector<int>::push_back(int const&)", 100, 200]])

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            response, status = kernel_collector.handleRequest()

            assert status == 204
            assert kernel_collector._KernelTrace__dispatches[0][1] == "std::vector<int>::push_back(int const&)"

    def test_bad_payloads_rejected(self, kernel_collector, flask_app):
        """Malformed bodies are rejected wholesale and stage nothing."""
        bad = [
            b"{invalid json",
            orjson.dumps([[0, "kernel_a", s_to_ns(1)]]),  # too few fields
            orjson.dumps([[0, "kernel_a", s_to_ns(1), s_to_ns(2), "extra"]]),  # too many
            orjson.dumps([0, "kernel_a", s_to_ns(1), s_to_ns(2)]),  # flat, not nested
        ]
        for payload_bytes in bad:
            _, status = post(kernel_collector, flask_app, payload_bytes)
            assert status == 400

        assert kernel_collector._KernelTrace__dispatches == []

    def test_partial_success_is_atomic(self, kernel_collector, flask_app):
        """One valid record + one invalid record -> 400, zero dispatches added."""
        records = [[0, "kernel_a", s_to_ns(1), s_to_ns(2)], [1, "kernel_b", s_to_ns(3)]]
        json_data = orjson.dumps(records)

        _, status = post(kernel_collector, flask_app, json_data)

        assert status == 400
        assert len(kernel_collector._KernelTrace__dispatches) == 0

    def test_non_json_content_returns_400(self, kernel_collector, flask_app):
        """Plain text body returns 400 and adds no dispatches."""
        with flask_app.test_request_context(data=b"plain text", content_type="text/plain"):
            _, status = kernel_collector.handleRequest()

        assert status == 400
        assert len(kernel_collector._KernelTrace__dispatches) == 0

    def test_builds_dispatches_correctly(self, kernel_collector, flask_app):
        """dispatch tuple = (gpu_id, kernel, end_ns, end_ns - start_ns)."""
        json_data = orjson.dumps([[0, "kernel_a", s_to_ns(1), s_to_ns(3)]])

        _, status = post(kernel_collector, flask_app, json_data)

        assert status == 204
        assert kernel_collector._KernelTrace__dispatches[0] == (0, "kernel_a", s_to_ns(3), s_to_ns(2))


class TestKernelThreadSafety:
    def test_no_data_loss(self, kernel_collector, flask_app):
        """Two threads each posting 5 dispatches: all 10 are recorded."""

        def post_dispatches(gpu_id, count):
            records = [[gpu_id, "kernel_a", s_to_ns(i), s_to_ns(i + 1)] for i in range(1, count + 1)]
            json_data = orjson.dumps(records)
            post(kernel_collector, flask_app, json_data)

        t1 = threading.Thread(target=post_dispatches, args=(0, 5))
        t2 = threading.Thread(target=post_dispatches, args=(1, 5))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        all_dispatches = kernel_collector._KernelTrace__dispatches
        assert len(all_dispatches) == 10
        assert sum(1 for d in all_dispatches if d[0] == 0) == 5
        assert sum(1 for d in all_dispatches if d[0] == 1) == 5

    def test_handleRequest_and_process(self, kernel_collector, mock_time, flask_app):
        """Dispatches posted across two handleRequest calls are all accumulated."""
        # First batch: 3 dispatches with end at t=2.5s -> end_bin=3000
        records_first = [[0, "kernel_a", s_to_ns(0.5), s_to_ns(2.5)]] * 3
        post(kernel_collector, flask_app, orjson.dumps(records_first))

        set_time(mock_time, 3)
        kernel_collector._KernelTrace__process()

        # Second batch: 2 dispatches with end at t=3.5s -> end_bin=4000
        records_second = [[0, "kernel_a", s_to_ns(1), s_to_ns(3.5)]] * 2
        post(kernel_collector, flask_app, orjson.dumps(records_second))

        set_time(mock_time, 4)
        kernel_collector._KernelTrace__process()

        # 3 first batch + 2 second batch = 5 total
        assert kernel_totals(kernel_collector)[(0, "kernel_a")][0] == 5


class TestKernelUpdateMetrics:
    def test_drains_staged_dispatches(self, kernel_collector, mock_time, flask_app):
        """updateMetrics() is what the sampling loop calls between pushes."""
        post(kernel_collector, flask_app, orjson.dumps([[0, "kernel_a", s_to_ns(1), s_to_ns(2.5)]]))
        set_time(mock_time, 3)
        kernel_collector.updateMetrics()

        assert kernel_collector._KernelTrace__dispatches == []
        assert kernel_totals(kernel_collector)[(0, "kernel_a")][0] == 1


class TestKernelNameInterning:
    def test_long_cpp_template_name(self, kernel_collector, flask_app):
        """A long C++ template name is interned; second request shares the same object."""
        long_name = "void HipKernel<" + "T," * 100 + "int>(T*, int)"
        assert len(long_name) >= 200  # sanity check it's reasonably long

        json_data = orjson.dumps([[0, long_name, s_to_ns(1), s_to_ns(2)]])

        post(kernel_collector, flask_app, json_data)
        name1 = kernel_collector._KernelTrace__dispatches[-1][1]
        kernel_collector._KernelTrace__dispatches.clear()

        post(kernel_collector, flask_app, json_data)
        name2 = kernel_collector._KernelTrace__dispatches[-1][1]

        assert name1 is name2
        assert len(kernel_collector._strings) == 1


# ==============================================================================
# 3. RCCL tracing
# ==============================================================================

# ncclFloat32 = 7 (4 bytes); ncclFloat16 = 6 (2 bytes)
DT_F32 = 7
DT_F16 = 6


def coll(gpu, op, count, dtype, comm, ts):
    """Build a collectives wire row: [gpu, op, count, dtype, comm, ts].

    Enumeration only — no correlation id / group_end_corr, which the removed
    kernel join would have needed.
    """
    return [gpu, op, count, dtype, comm, ts]


def collectives_payload(rows):
    return orjson.dumps({"collectives": rows, "comms": []})


def payload(collectives=None, comms=None):
    return orjson.dumps({"collectives": collectives or [], "comms": comms or []})


@pytest.fixture
def rccl_collector(mock_time):
    return RcclTrace(configparser.ConfigParser(), Mock(), INTERVAL_S)


class TestOutOfOrderRecords:
    """Published counters must not depend on arrival order.

    For kernels the exposure is structural: rocprofiler-sdk delivers dispatches
    in completion-handler order, which is not end-timestamp order.

    For RCCL it is narrower and has not been observed. These tests pin the
    collector's order independence, not any claim about how RCCL records
    actually arrive.
    """

    EARLY = 2.1  # -> bin 3000
    LATE = 3.1  # -> bin 4000

    @staticmethod
    def _counts(lines, metric):
        out = []
        for line in lines:
            text = line.decode() if isinstance(line, bytes) else line
            if text.startswith(metric):
                out.append(int(float(text.split("}")[1].split()[0])))
        return out

    def _kernel_counts(self, collector, order):
        for end_s in order:
            collector._KernelTrace__dispatches.append(
                make_dispatch("0", "kernel_a", end_ns=s_to_ns(end_s), duration_ns=10)
            )
        return self._counts(
            collector.formatMetrics(LABEL_DEFAULTS, flush=True),
            "omnistat_kernel_dispatch_count",
        )

    def test_kernel_in_order_baseline(self, kernel_collector, mock_time):
        set_time(mock_time, 4)
        assert self._kernel_counts(kernel_collector, [self.EARLY] * 3 + [self.LATE] * 2) == [3, 5]

    def test_kernel_inverted_within_batch(self, kernel_collector, mock_time):
        """Batch ends on a record belonging to the EARLIER bin."""
        set_time(mock_time, 4)
        order = [self.LATE, self.LATE, self.EARLY, self.EARLY, self.EARLY]
        assert self._kernel_counts(kernel_collector, order) == [3, 5]

    def test_kernel_inverted_across_batches(self, kernel_collector, mock_time):
        """A later batch carrying earlier timestamps; sorting cannot fix this."""
        set_time(mock_time, 4)
        for end_s in [self.LATE, self.LATE]:
            kernel_collector._KernelTrace__dispatches.append(
                make_dispatch("0", "kernel_a", end_ns=s_to_ns(end_s), duration_ns=10)
            )
        kernel_collector._KernelTrace__process()
        assert self._kernel_counts(kernel_collector, [self.EARLY] * 3) == [3, 5]

    @pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
    def test_kernel_permutation_invariant(self, kernel_collector, mock_time, seed):
        set_time(mock_time, 4)
        order = [self.EARLY] * 3 + [self.LATE] * 2
        random.Random(seed).shuffle(order)
        assert self._kernel_counts(kernel_collector, order) == [3, 5]

    # RCCL stages records the same way and had the same defect. Both of its
    # series are covered: they accumulate independently.

    def _rccl_counts(self, collector, flask_app, order, metric):
        rows = [coll(0, "ncclAllReduce", 1024, DT_F32, 111, s_to_ns(t)) for t in order]
        post(collector, flask_app, collectives_payload(rows))
        return self._counts(collector.formatMetrics(LABEL_DEFAULTS, flush=True), metric)

    def _comm_durations(self, collector, flask_app, order):
        comms = [[0, "ncclCommInitRank", 500 + i, 8, s_to_ns(t), s_to_ns(t) + 100] for i, t in enumerate(order)]
        post(collector, flask_app, payload(comms=comms))
        return self._counts(
            collector.formatMetrics(LABEL_DEFAULTS, flush=True),
            "omnistat_rccl_comm_init_total_duration_ns",
        )

    def test_rccl_collective_in_order_baseline(self, rccl_collector, mock_time, flask_app):
        set_time(mock_time, 4)
        order = [self.EARLY] * 3 + [self.LATE] * 2
        assert self._rccl_counts(rccl_collector, flask_app, order, "omnistat_rccl_collective_count") == [3, 5]

    def test_rccl_collective_inverted_within_batch(self, rccl_collector, mock_time, flask_app):
        set_time(mock_time, 4)
        order = [self.LATE, self.LATE, self.EARLY, self.EARLY, self.EARLY]
        assert self._rccl_counts(rccl_collector, flask_app, order, "omnistat_rccl_collective_count") == [3, 5]

    @pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
    def test_rccl_collective_permutation_invariant(self, rccl_collector, mock_time, flask_app, seed):
        set_time(mock_time, 4)
        order = [self.EARLY] * 3 + [self.LATE] * 2
        random.Random(seed).shuffle(order)
        assert self._rccl_counts(rccl_collector, flask_app, order, "omnistat_rccl_collective_count") == [3, 5]

    def test_rccl_comm_inverted_within_batch(self, rccl_collector, mock_time, flask_app):
        """The comm series accumulates duration, so inversion showed up there too."""
        set_time(mock_time, 4)
        order = [self.LATE, self.EARLY, self.EARLY]
        assert self._comm_durations(rccl_collector, flask_app, order) == [200, 300]


class TestRcclBucketing:
    def test_size_bucket_edges(self):
        assert size_bucket(1) == "4K"
        assert size_bucket(4096) == "4K"
        assert size_bucket(4097) == "64K"
        assert size_bucket(33554432) == "64M"  # 32 MiB
        assert size_bucket(1073741824) == "1G"
        assert size_bucket(10 * 1024**3) == "inf"

    def test_nranks_label(self):
        # Exact rank count (not binned); "unknown" when unavailable.
        assert nranks_label(2) == "2"
        assert nranks_label(8) == "8"
        assert nranks_label(9) == "9"
        assert nranks_label(1024) == "1024"
        assert nranks_label(None) == "unknown"
        assert nranks_label(-1) == "unknown"


class TestRcclHandleRequest:
    def test_valid_collective(self, rccl_collector, flask_app):
        rows = [coll(0, "ncclAllReduce", 8388608, DT_F32, 111, s_to_ns(2))]
        _, status = post(rccl_collector, flask_app, collectives_payload(rows))
        assert status == 204
        assert len(rccl_collector._RcclTrace__collectives) == 1

    def test_bad_payloads_rejected(self, rccl_collector, flask_app):
        """Malformed bodies are rejected wholesale and stage nothing."""
        for body in (collectives_payload([[0, "ncclAllReduce", 8388608]]), b"[invalid"):
            _, status = post(rccl_collector, flask_app, body)
            assert status == 400

        assert rccl_collector._RcclTrace__collectives == []

    def test_atomic_on_bad_record(self, rccl_collector, flask_app):
        # one good collective, one bad comm -> nothing added
        data = payload(
            collectives=[coll(0, "ncclAllReduce", 100, DT_F32, 9, s_to_ns(2))],
            comms=[[0, "ncclCommInitAll", 9]],  # too few
        )
        _, status = post(rccl_collector, flask_app, data)
        assert status == 400
        assert len(rccl_collector._RcclTrace__collectives) == 0
        assert len(rccl_collector._RcclTrace__comms) == 0


class TestRcclCollectiveAccounting:
    def test_count_and_bytes(self, rccl_collector, mock_time, flask_app):
        # 8388608 float32 = 32 MiB -> size_bucket 64M
        rows = [coll(0, "ncclAllReduce", 8388608, DT_F32, 111, s_to_ns(2))]
        post(rccl_collector, flask_app, collectives_payload(rows))
        set_time(mock_time, 3)
        rccl_collector._RcclTrace__process()

        key = (0, "AllReduce", "float32", "64M", "unknown")  # card=0
        val = collective_totals(rccl_collector)[key]
        assert val == [1, 8388608 * 4]  # [count, bytes] — enumeration only

    def test_count_accumulates(self, rccl_collector, mock_time, flask_app):
        rows = [
            coll(0, "ncclAllReduce", 1024, DT_F32, 111, s_to_ns(2)),
            coll(0, "ncclAllReduce", 1024, DT_F32, 111, s_to_ns(2)),
            coll(1, "ncclAllReduce", 1024, DT_F32, 111, s_to_ns(2)),
        ]
        post(rccl_collector, flask_app, collectives_payload(rows))
        set_time(mock_time, 3)
        rccl_collector._RcclTrace__process()

        assert collective_totals(rccl_collector)[(0, "AllReduce", "float32", "4K", "unknown")] == [
            2,
            2 * 4096,
        ]
        assert collective_totals(rccl_collector)[(1, "AllReduce", "float32", "4K", "unknown")] == [1, 4096]

    def test_comm_size_bucket_from_comm(self, rccl_collector, mock_time, flask_app):
        # init an 8-rank comm (handle 555), then a collective on it
        comms = [[0, "ncclCommInitRank", 555, 8, s_to_ns(1), s_to_ns(1) + 100]]
        colls = [coll(0, "ncclAllReduce", 1024, DT_F32, 555, s_to_ns(1.6))]
        post(rccl_collector, flask_app, payload(collectives=colls, comms=comms))
        set_time(mock_time, 3)
        rccl_collector._RcclTrace__process()

        key = (0, "AllReduce", "float32", "4K", "8")
        assert collective_totals(rccl_collector)[key][0] == 1

    def test_comm_size_resolves_for_trailing_collectives(self, rccl_collector, mock_time, flask_app):
        # Teardown is not traced, so a comm handle is never retired: collectives
        # arriving at any point still resolve their nranks (bucket "8"), and no
        # phantom "unknown" bucket appears.
        comms = [[0, "ncclCommInitRank", 555, 8, s_to_ns(1), s_to_ns(1) + 100]]
        colls = [coll(0, "ncclAllReduce", 1024, DT_F32, 555, s_to_ns(1.8))]
        post(rccl_collector, flask_app, payload(collectives=colls, comms=comms))
        set_time(mock_time, 3)
        rccl_collector._RcclTrace__process()

        assert collective_totals(rccl_collector)[(0, "AllReduce", "float32", "4K", "8")][0] == 1
        assert (0, "AllReduce", "float32", "4K", "unknown") not in collective_totals(rccl_collector)


class TestRcclCommCreation:
    def test_create_counts_by_nranks(self, rccl_collector, mock_time, flask_app):
        comms = [[0, "ncclCommInitAll", 555, 2, s_to_ns(1), s_to_ns(1) + 200]]
        post(rccl_collector, flask_app, payload(comms=comms))
        set_time(mock_time, 3)
        rccl_collector._RcclTrace__process()

        assert comm_totals(rccl_collector)[(0, "2")] == [1, 200]

    def test_teardown_ops_ignored(self, rccl_collector, mock_time, flask_app):
        # The tracer no longer emits teardown rows, but the rccl_collector must ignore
        # them if any arrive (e.g. an older trace library).
        comms = [
            [0, "ncclCommInitRank", 555, 8, s_to_ns(1), s_to_ns(1) + 100],
            [0, "ncclCommFinalize", 555, -1, s_to_ns(2), s_to_ns(2) + 10],
            [0, "ncclCommDestroy", 555, -1, s_to_ns(2.1), s_to_ns(2.1) + 10],
        ]
        post(rccl_collector, flask_app, payload(comms=comms))
        set_time(mock_time, 3)
        rccl_collector._RcclTrace__process()

        assert comm_totals(rccl_collector)[(0, "8")] == [1, 100]
        # teardown contributes nothing, and creates no phantom "unknown" bucket
        assert (0, "unknown") not in comm_totals(rccl_collector)

    def test_comm_lines_carry_nranks(self, rccl_collector, mock_time, flask_app):
        """Both comm metrics share one label set, so both carry nranks.

        Asserted on the emitted bytes: the label is the whole point of keying
        the series by (card, nranks), and nothing else pins it.
        """
        comms = [[0, "ncclCommInitRank", 111, 8, s_to_ns(2), s_to_ns(2) + 100]]
        post(rccl_collector, flask_app, payload(comms=comms))
        set_time(mock_time, 3)

        lines = metric_lines(rccl_collector)
        assert any(
            l.startswith('omnistat_rccl_comm_created_count{') and 'card="0",nranks="8"}' in l for l in lines
        )
        assert any(
            l.startswith('omnistat_rccl_comm_init_total_duration_ns{') and 'card="0",nranks="8"}' in l
            for l in lines
        )

    def test_init_total_duration_sums_creates(self, rccl_collector, mock_time, flask_app):
        comms = [
            [0, "ncclCommInitRank", 555, 8, s_to_ns(1), s_to_ns(1) + 100],
            [0, "ncclCommInitRank", 777, 8, s_to_ns(2), s_to_ns(2) + 300],
        ]
        post(rccl_collector, flask_app, payload(comms=comms))
        set_time(mock_time, 3)
        rccl_collector._RcclTrace__process()

        assert comm_totals(rccl_collector)[(0, "8")] == [2, 400]  # 100 + 300

    def test_split_derived_nranks_exact(self, rccl_collector, mock_time, flask_app):
        # After the tracer's ncclCommCount fix, a split arrives with a real
        # nranks (e.g. 4) rather than -1, so it's labeled exactly "4", never
        # "unknown".
        comms = [[0, "ncclCommSplit", 900, 4, s_to_ns(1), s_to_ns(1) + 20]]
        post(rccl_collector, flask_app, payload(comms=comms))
        set_time(mock_time, 3)
        rccl_collector._RcclTrace__process()

        assert comm_totals(rccl_collector)[(0, "4")][0] == 1


class TestRcclLateRecords:
    """A record arriving after its time bin was released is counted, not silent.

    The absence of this counter is why a bug that discarded most of the RCCL
    stream passed validation repeatedly.
    """

    def test_late_collective_counted(self, rccl_collector, mock_time, flask_app):
        # Bins are only released when formatMetrics runs, so a record is "late"
        # only after its bin has already been emitted. Advance, release, then
        # deliver a record stamped back at t=2.
        set_time(mock_time, 60)
        metric_lines(rccl_collector, flush=False)

        rows = [coll(0, "ncclAllReduce", 1024, DT_F32, 111, s_to_ns(2))]
        post(rccl_collector, flask_app, collectives_payload(rows))
        rccl_collector._RcclTrace__process()

        assert rccl_collector._late_records == 1
        assert collective_totals(rccl_collector) == {}

    def test_in_window_collective_not_counted(self, rccl_collector, mock_time, flask_app):
        rows = [coll(0, "ncclAllReduce", 1024, DT_F32, 111, s_to_ns(2))]
        post(rccl_collector, flask_app, collectives_payload(rows))
        set_time(mock_time, 3)
        rccl_collector._RcclTrace__process()

        assert rccl_collector._late_records == 0

    def test_late_comm_create_counted(self, rccl_collector, mock_time, flask_app):
        """The comm series has its own window, and late creates are counted too."""
        set_time(mock_time, 60)
        metric_lines(rccl_collector, flush=False)

        comms = [[0, "ncclCommInitRank", 555, 8, s_to_ns(1), s_to_ns(1) + 100]]
        post(rccl_collector, flask_app, payload(comms=comms))
        rccl_collector._RcclTrace__process()

        assert rccl_collector._late_records == 1
        assert comm_totals(rccl_collector) == {}

    def test_late_comm_create_still_labels_its_collectives(self, rccl_collector, mock_time, flask_app):
        """Dropping a late create must not cost the comm its rank count.

        The create is too old to be counted, but every later collective on that
        communicator is still labelled with the size it carried.
        """
        set_time(mock_time, 60)
        metric_lines(rccl_collector, flush=False)

        comms = [[0, "ncclCommInitRank", 555, 8, s_to_ns(1), s_to_ns(1) + 100]]
        post(rccl_collector, flask_app, payload(comms=comms))
        rccl_collector._RcclTrace__process()
        assert rccl_collector._late_records == 1

        rows = [coll(0, "ncclAllReduce", 1024, DT_F32, 555, s_to_ns(60))]
        post(rccl_collector, flask_app, collectives_payload(rows))
        set_time(mock_time, 61)

        lines = [l for l in metric_lines(rccl_collector) if "comm_size=" in l]
        assert lines
        assert all('comm_size="8"' in l for l in lines)

    def test_teardown_ops_are_not_late(self, rccl_collector, mock_time, flask_app):
        """Filtering non-creation comm ops is expected, not loss."""
        comms = [[0, "ncclCommDestroy", 111, 8, s_to_ns(1), s_to_ns(2)]]
        post(rccl_collector, flask_app, payload(comms=comms))
        set_time(mock_time, 3)
        rccl_collector._RcclTrace__process()

        assert rccl_collector._late_records == 0

    def test_metric_emitted(self, rccl_collector, mock_time, flask_app):
        rows = [coll(0, "ncclAllReduce", 1024, DT_F32, 111, s_to_ns(2))]
        post(rccl_collector, flask_app, collectives_payload(rows))
        set_time(mock_time, 3)

        lines = metric_lines(rccl_collector)
        assert any(l.startswith("omnistat_rccl_late_records{") for l in lines)

class TestRcclFormatMetrics:
    def test_metric_names_present(self, rccl_collector, mock_time, flask_app):
        rows = [coll(0, "ncclAllReduce", 8388608, DT_F32, 111, s_to_ns(2.1))]
        comms = [[0, "ncclCommInitAll", 555, 2, s_to_ns(2), s_to_ns(2) + 50]]
        post(rccl_collector, flask_app, payload(collectives=rows, comms=comms))
        set_time(mock_time, 3)
        lines = metric_lines(rccl_collector)
        joined = "\n".join(lines)
        # enumeration metrics present
        assert "omnistat_rccl_collective_count" in joined
        assert "omnistat_rccl_collective_total_bytes" in joined
        assert "omnistat_rccl_comm_created_count" in joined
        assert "omnistat_rccl_comm_init_total_duration_ns" in joined
        # Communicator teardown is not traced, so nothing derived from it is
        # emitted: no destroyed counter, no live gauge, no lifecycle timestamps.
        assert "omnistat_rccl_comm_destroyed_count" not in joined
        assert "omnistat_rccl_comm_num_active" not in joined
        assert "omnistat_rccl_comm_first_create_ts_ns" not in joined
        assert "omnistat_rccl_comm_last_destroy_ts_ns" not in joined
        # Running all-time max was dropped: as a monotonic high-water mark its
        # range-scoped queries reported since-job-start, not within-window.
        assert "omnistat_rccl_comm_init_max_ns" not in joined
        # group-timing metrics must NOT be emitted: the kernel join is gone
        assert "omnistat_rccl_group_duration_ns" not in joined
        assert "omnistat_rccl_group_dispatch_latency_ns" not in joined
        assert "omnistat_rccl_group_count" not in joined
        assert "omnistat_rccl_unmatched_collectives" not in joined
        assert "composition=" not in joined

    def test_exact_line_format(self, rccl_collector, mock_time, flask_app):
        rows = [coll(0, "ncclAllReduce", 1024, DT_F32, 111, s_to_ns(2))]
        post(rccl_collector, flask_app, collectives_payload(rows))
        set_time(mock_time, 3)
        lines = metric_lines(rccl_collector)
        expected = (
            'omnistat_rccl_collective_count{%s,card="0",collective="AllReduce",'
            'datatype="float32",size_bucket="4K",comm_size="unknown"} 1 3000' % LABEL_DEFAULTS
        )
        assert any(l == expected for l in lines), [l for l in lines if "collective_count" in l]


class TestRcclThreadSafety:
    def test_concurrent_posts_lose_nothing(self, rccl_collector, flask_app):
        """Two threads posting collectives concurrently: all rows are staged.

        The collector takes POSTs from every rank on the node at once while the
        push loop drains, so the staging lists are lock-guarded.
        """

        def post_rows(gpu_id, count):
            rows = [coll(gpu_id, "ncclAllReduce", 1024, DT_F32, 111, s_to_ns(2)) for _ in range(count)]
            post(rccl_collector, flask_app, collectives_payload(rows))

        threads = [threading.Thread(target=post_rows, args=(gpu, 5)) for gpu in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        staged = rccl_collector._RcclTrace__collectives
        assert len(staged) == 10
        assert sum(1 for r in staged if r[0] == 0) == 5
        assert sum(1 for r in staged if r[0] == 1) == 5

    def test_accumulates_across_process_cycles(self, rccl_collector, mock_time, flask_app):
        """Rows posted either side of a __process() call all reach the totals."""
        for at, n in ((2.5, 3), (3.5, 2)):
            rows = [coll(0, "ncclAllReduce", 1024, DT_F32, 111, s_to_ns(at))] * n
            post(rccl_collector, flask_app, collectives_payload(rows))
            set_time(mock_time, at + 0.5)
            rccl_collector._RcclTrace__process()

        key = (0, "AllReduce", "float32", "4K", "unknown")
        assert collective_totals(rccl_collector)[key][0] == 5


class TestRcclHoldWindow:
    def test_flush_publishes_what_no_flush_holds(self, rccl_collector, mock_time, flask_app):
        """The same recent bin is held with flush=False and released with flush=True."""
        rows = [coll(0, "ncclAllReduce", 1024, DT_F32, 111, s_to_ns(2.5))]
        post(rccl_collector, flask_app, collectives_payload(rows))
        set_time(mock_time, 3)

        assert metric_lines(rccl_collector, flush=False) == []
        released = metric_lines(rccl_collector, flush=True)
        assert any(l.startswith("omnistat_rccl_collective_count{") for l in released)

    def test_no_flush_publishes_once_past_the_window(self, rccl_collector, mock_time, flask_app):
        """Bins older than the hold window are released without a flush.

        The window is read from the collector rather than hardcoded: it is
        coupled to the tracer's flush interval and has been retuned before.
        """
        rows = [coll(0, "ncclAllReduce", 1024, DT_F32, 111, s_to_ns(2.5))]
        post(rccl_collector, flask_app, collectives_payload(rows))
        set_time(mock_time, 3)
        rccl_collector._RcclTrace__process()

        set_time(mock_time, 5 + rccl_collector._window_ms // 1000)
        lines = metric_lines(rccl_collector, flush=False)

        assert any(l.startswith("omnistat_rccl_collective_count{") for l in lines)


class TestRcclUpdateMetrics:
    def test_drains_staged_records(self, rccl_collector, mock_time, flask_app):
        """updateMetrics() is what the sampling loop calls between pushes."""
        rows = [coll(0, "ncclAllReduce", 1024, DT_F32, 111, s_to_ns(2))]
        post(rccl_collector, flask_app, collectives_payload(rows))
        set_time(mock_time, 3)
        rccl_collector.updateMetrics()

        assert rccl_collector._RcclTrace__collectives == []
        assert collective_totals(rccl_collector)[(0, "AllReduce", "float32", "4K", "unknown")][0] == 1

    def test_both_series_extend_from_one_clock_read(self, rccl_collector, mock_time):
        """Both series must share a last bin, so one cutoff fits both.

        formatMetrics derives its cutoff from the collective series alone, so
        the two must not drift apart. Reading the clock once per series would
        let them land an interval apart whenever the second read crossed a
        boundary. Simulating that here: _current_bin is rigged so a second
        call would report the next interval, which only changes the outcome if
        __process actually makes one.
        """
        set_time(mock_time, 10)
        first = rccl_collector._current_bin()
        with patch.object(rccl_collector, "_current_bin", side_effect=[first, first + INTERVAL_MS]):
            rccl_collector.updateMetrics()

        collective = rccl_collector._RcclTrace__collective_ts.bins
        comm = rccl_collector._RcclTrace__comm_ts.bins
        assert next(reversed(collective)) == next(reversed(comm))
