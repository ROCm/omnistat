import configparser
import threading
from unittest.mock import Mock, patch

import orjson
import pytest
from flask import Flask

from omnistat.collector_kernel_trace import KernelTrace


def s_to_ns(s):
    """Convert seconds (int or float) to nanoseconds."""
    return int(s * 1_000_000_000)


# The mock_time fixture starts both unix and boot clocks at INIT_TIME_NS (1 second),
# making __offset_ns = 0 (GPU timestamps equal unix timestamps). With a 1-second
# interval, the initial bin is at FIRST_BIN_MS (2000 ms).
INIT_TIME_NS = s_to_ns(1)
INTERVAL_S = 1.0
INTERVAL_MS = 1000
FIRST_BIN_S = 2
FIRST_BIN_MS = 2000
LABEL_DEFAULTS = 'instance="node.test"'

COUNT_METRIC = "omnistat_kernel_dispatch_count"
DURATION_METRIC = "omnistat_kernel_total_duration_ns"
DROPPED_METRIC = "omnistat_kernel_dropped_dispatches"


def set_time(mock_time, seconds):
    """Advance the mocked wall clock to the given time in seconds."""
    mock_time["time_ns"].return_value = s_to_ns(seconds)


def make_dispatch(gpu_id, kernel, end_ns, duration_ns):
    """Build a dispatch tuple matching __process_dispatches input format:
    (gpu_id, kernel_name, end_ns, duration_ns)."""
    return (gpu_id, kernel, end_ns, duration_ns)


def collect_metrics(collector_instance, flush=True):
    """Collect formatted metrics and split into kernel vs dropped lines.

    Returns a dict with:
      "all"     - all non-newline metric lines
      "kernel"  - kernel dispatch/duration lines only
      "dropped" - omnistat_kernel_dropped_dispatches lines only
    """
    kernel = []
    dropped = []
    for line in collector_instance.formatMetrics(LABEL_DEFAULTS, flush=flush):
        if line == b"\n":
            continue
        decoded = line.decode()
        if DROPPED_METRIC in decoded:
            dropped.append(decoded)
        else:
            kernel.append(decoded)
    return {"all": kernel + dropped, "kernel": kernel, "dropped": dropped}


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


@pytest.fixture
def collector_instance(mock_time):
    """KernelTrace instance at t=1s with 1s interval and __offset_ns=0."""
    config = configparser.ConfigParser()
    return KernelTrace(config, Mock(), INTERVAL_S)


class TestPopBins:
    def test_exact_cutoff_boundary(self, collector_instance):
        """Bins exactly at cutoff are popped (cutoff bin is inclusive)."""
        ts = collector_instance._KernelTrace__ts
        ts.clear()
        ts[1000] = {}
        ts[2000] = {}
        ts[3000] = {}

        result = collector_instance._KernelTrace__pop_bins(2000)

        assert len(result) == 2
        assert result[0] == (1000, {})
        assert result[1] == (2000, {})
        assert list(ts.keys()) == [3000]

    def test_empty_ts(self, collector_instance):
        """pop_bins on an empty OrderedDict returns empty list without error."""
        collector_instance._KernelTrace__ts.clear()
        result = collector_instance._KernelTrace__pop_bins(9999)
        assert result == []

    def test_cutoff_below_all_bins(self, collector_instance):
        """When cutoff is below all bins, nothing is popped."""
        ts = collector_instance._KernelTrace__ts
        ts.clear()
        ts[2000] = {}
        ts[3000] = {}

        result = collector_instance._KernelTrace__pop_bins(1000)

        assert result == []
        assert list(ts.keys()) == [2000, 3000]


class TestInit:
    def test_offset_calculation(self, mock_time):
        """Offset is unix_time_ns minus boot_time_ns."""
        mock_time["time_ns"].return_value = s_to_ns(10)
        mock_time["clock_gettime_ns"].return_value = s_to_ns(3)
        kt = KernelTrace(configparser.ConfigParser(), Mock(), INTERVAL_S)
        assert kt._KernelTrace__offset_ns == s_to_ns(7)

    def test_initial_bin(self, mock_time):
        """At t=1s with 1s interval, initial bin is FIRST_BIN_MS (2000 ms)."""
        kt = KernelTrace(configparser.ConfigParser(), Mock(), interval=INTERVAL_S)
        ts = kt._KernelTrace__ts
        assert len(ts) == 1
        assert FIRST_BIN_MS in ts

    def test_with_fractional_interval(self, mock_time):
        """interval_ms = max(1, int(f*1000)) for various fractional inputs."""
        config = configparser.ConfigParser()

        kt_half = KernelTrace(config, Mock(), interval=0.5)
        assert kt_half._KernelTrace__interval_ms == 500

        kt_tiny = KernelTrace(config, Mock(), interval=0.001)
        assert kt_tiny._KernelTrace__interval_ms == 1


class TestProcessDispatches:
    def test_empty_dispatches(self, collector_instance, mock_time):
        """With no dispatches, bins are extended but values stay empty."""
        assert len(collector_instance._KernelTrace__dispatches) == 0

        start_s = FIRST_BIN_S
        duration_s = 10
        for i in range(start_s, start_s + duration_s):
            set_time(mock_time, i)
            last_bin = collector_instance._KernelTrace__process_dispatches()

            assert last_bin == (i * INTERVAL_MS) + INTERVAL_MS
            assert len(collector_instance._KernelTrace__values) == 0

            bins = collector_instance._KernelTrace__ts
            assert last_bin in bins
            assert len(bins[last_bin]) == 0

        assert len(collector_instance._KernelTrace__ts) == duration_s + 1

    def test_single_dispatch(self, collector_instance, mock_time):
        """A single dispatch is accumulated and snapshotted into the correct bin."""
        # end_ns at t=2s -> end_bin=3000
        dispatch = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2), duration_ns=42)
        collector_instance._KernelTrace__dispatches.append(dispatch)

        set_time(mock_time, 3)
        last_bin = collector_instance._KernelTrace__process_dispatches()

        values = collector_instance._KernelTrace__values
        ts = collector_instance._KernelTrace__ts

        key = ("0", "kernel_a")
        assert key in values
        assert values[key] == [1, 42]

        assert 3000 in ts
        assert key in ts[3000]
        assert ts[3000][key] == [1, 42]

        assert len(collector_instance._KernelTrace__dispatches) == 0

    def test_same_kernel_same_bin(self, collector_instance, mock_time):
        """Two dispatches of the same kernel in the same bin accumulate."""
        # Both end between t=2s and t=3s -> end_bin=3000
        d1 = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=30)
        d2 = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.8), duration_ns=50)
        collector_instance._KernelTrace__dispatches.extend([d1, d2])

        set_time(mock_time, 3)
        collector_instance._KernelTrace__process_dispatches()

        values = collector_instance._KernelTrace__values
        ts = collector_instance._KernelTrace__ts
        key = ("0", "kernel_a")

        assert values[key] == [2, 80]
        assert ts[3000][key] == [2, 80]

    def test_same_kernel_cumulative_snapshot(self, collector_instance, mock_time):
        """Snapshots in __ts are cumulative totals, not per-bin deltas."""
        key = ("0", "kernel_a")

        # First dispatch: end at t=2.5s -> end_bin=3000
        d1 = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=40)
        collector_instance._KernelTrace__dispatches.append(d1)
        set_time(mock_time, 3)
        collector_instance._KernelTrace__process_dispatches()

        ts = collector_instance._KernelTrace__ts
        assert ts[3000][key] == [1, 40]

        # Second dispatch: end at t=3.5s -> end_bin=4000
        d2 = make_dispatch("0", "kernel_a", end_ns=s_to_ns(3.5), duration_ns=25)
        collector_instance._KernelTrace__dispatches.append(d2)
        set_time(mock_time, 4)
        collector_instance._KernelTrace__process_dispatches()

        # ts[4000] snapshot includes both dispatches
        assert ts[4000][key] == [2, 65]
        # ts[3000] is unchanged
        assert ts[3000][key] == [1, 40]

    def test_multiple_kernels_multiple_gpus(self, collector_instance, mock_time):
        """4 dispatches across 2 GPUs and 2 kernels all go into the same bin."""
        dispatches = [
            make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.1), duration_ns=11),
            make_dispatch("0", "kernel_b", end_ns=s_to_ns(2.2), duration_ns=22),
            make_dispatch("1", "kernel_a", end_ns=s_to_ns(2.3), duration_ns=33),
            make_dispatch("1", "kernel_b", end_ns=s_to_ns(2.4), duration_ns=44),
        ]
        collector_instance._KernelTrace__dispatches.extend(dispatches)

        set_time(mock_time, 3)
        collector_instance._KernelTrace__process_dispatches()

        values = collector_instance._KernelTrace__values
        ts = collector_instance._KernelTrace__ts

        assert len(values) == 4
        assert len(ts[3000]) == 4
        assert values[("0", "kernel_a")] == [1, 11]
        assert values[("1", "kernel_b")] == [1, 44]

    def test_assigned_to_past_bin(self, collector_instance, mock_time):
        """A dispatch with an old end timestamp goes into its correct past bin."""
        # Current time t=4s (last_bin=5000). Dispatch end at t=1.5s -> end_bin=2000 (past).
        dispatch = make_dispatch("0", "kernel_a", end_ns=s_to_ns(1.5), duration_ns=15)
        collector_instance._KernelTrace__dispatches.append(dispatch)

        set_time(mock_time, 4)
        collector_instance._KernelTrace__process_dispatches()

        ts = collector_instance._KernelTrace__ts
        key = ("0", "kernel_a")

        assert key in ts[2000]
        assert key not in ts.get(3000, {})
        assert key not in ts.get(5000, {})

    def test_exact_interval_boundary(self, collector_instance, mock_time):
        """A dispatch ending exactly on a boundary goes into the NEXT bin."""
        # end at exactly t=2s (on boundary) -> end_bin=3000
        dispatch = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2), duration_ns=18)
        collector_instance._KernelTrace__dispatches.append(dispatch)

        set_time(mock_time, 3)
        collector_instance._KernelTrace__process_dispatches()

        ts = collector_instance._KernelTrace__ts
        key = ("0", "kernel_a")

        assert key in ts[3000]
        assert key not in ts.get(2000, {})

    def test_out_of_range_before_first_bin(self, collector_instance, mock_time):
        """A dispatch whose end_bin < first_bin is silently dropped."""
        early = make_dispatch("0", "kernel_a", end_ns=s_to_ns(0.5), duration_ns=10)
        collector_instance._KernelTrace__dispatches.append(early)

        set_time(mock_time, 3)
        collector_instance._KernelTrace__process_dispatches()

        assert len(collector_instance._KernelTrace__values) == 0
        assert collector_instance._KernelTrace__dropped_dispatches == 1

    def test_out_of_range_after_last_bin(self, collector_instance, mock_time):
        """A dispatch whose end_bin > last_bin is silently dropped."""
        late = make_dispatch("0", "kernel_a", end_ns=s_to_ns(100), duration_ns=10)
        collector_instance._KernelTrace__dispatches.append(late)

        set_time(mock_time, 3)
        collector_instance._KernelTrace__process_dispatches()

        assert len(collector_instance._KernelTrace__values) == 0
        assert collector_instance._KernelTrace__dropped_dispatches == 1

    def test_mixed_range(self, collector_instance, mock_time):
        """In-range dispatches are counted; out-of-range ones are dropped."""
        in_range = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=10)
        early = make_dispatch("0", "kernel_b", end_ns=s_to_ns(0.5), duration_ns=20)
        late = make_dispatch("0", "kernel_c", end_ns=s_to_ns(100), duration_ns=30)
        collector_instance._KernelTrace__dispatches.extend([in_range, early, late])

        set_time(mock_time, 3)
        collector_instance._KernelTrace__process_dispatches()

        values = collector_instance._KernelTrace__values
        assert ("0", "kernel_a") in values
        assert ("0", "kernel_b") not in values
        assert ("0", "kernel_c") not in values


class TestFormatMetrics:
    def test_no_flush_holds(self, collector_instance, mock_time):
        """With flush=False, bins within the 15s window are held back."""
        dispatch = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=50)
        collector_instance._KernelTrace__dispatches.append(dispatch)

        set_time(mock_time, 3)
        metrics = collect_metrics(collector_instance, flush=False)

        assert metrics["all"] == []

    def test_no_flush_releases(self, collector_instance, mock_time):
        """Bins older than the 15s window are released with flush=False."""
        dispatch = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=50)
        collector_instance._KernelTrace__dispatches.append(dispatch)
        set_time(mock_time, 3)
        collector_instance._KernelTrace__process_dispatches()

        # Advance to t=20s -> cutoff=6000; bins 2000..6000 released
        set_time(mock_time, 20)
        metrics = collect_metrics(collector_instance, flush=False)

        assert len(metrics["kernel"]) == 2
        assert len(metrics["dropped"]) >= 1

    def test_flush_releases(self, collector_instance, mock_time):
        """flush=True releases all bins including the most recent one."""
        dispatch = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=50)
        collector_instance._KernelTrace__dispatches.append(dispatch)

        set_time(mock_time, 3)
        metrics = collect_metrics(collector_instance)

        assert len(metrics["kernel"]) == 2
        assert len(collector_instance._KernelTrace__ts) == 0

    def test_exact_prometheus_line_format(self, collector_instance, mock_time):
        """Verify the exact bytes of the Prometheus output lines."""
        dispatch = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=77)
        collector_instance._KernelTrace__dispatches.append(dispatch)

        set_time(mock_time, 3)
        metrics = collect_metrics(collector_instance)

        # Find the lines for bin 3000 (which contains the kernel data)
        bin3000_lines = [l for l in metrics["all"] if l.endswith("3000")]
        assert bin3000_lines[0] == f'{COUNT_METRIC}{{{LABEL_DEFAULTS},card="0",kernel="kernel_a"}} 1 3000'
        assert bin3000_lines[1] == f'{DURATION_METRIC}{{{LABEL_DEFAULTS},card="0",kernel="kernel_a"}} 77 3000'
        assert bin3000_lines[2] == f"{DROPPED_METRIC}{{{LABEL_DEFAULTS}}} 0 3000"

    def test_dropped_dispatch_metric(self, collector_instance, mock_time):
        """Verify formatMetrics includes dropped dispatch count with correct value."""
        in_range = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=10)
        early = make_dispatch("0", "kernel_b", end_ns=s_to_ns(0.5), duration_ns=20)
        late = make_dispatch("0", "kernel_c", end_ns=s_to_ns(100), duration_ns=30)
        collector_instance._KernelTrace__dispatches.extend([in_range, early, late])

        set_time(mock_time, 3)
        metrics = collect_metrics(collector_instance)

        # The bin containing the in-range dispatch should show dropped count = 2
        assert len(metrics["dropped"]) >= 1
        assert f"{DROPPED_METRIC}{{{LABEL_DEFAULTS}}} 2 3000" in metrics["dropped"]

    def test_multiple_gpus_multiple_kernels_output(self, collector_instance, mock_time):
        """4 dispatches (2 GPUs x 2 kernels) produce 8 kernel lines + 1 dropped line per bin."""
        dispatches = [
            make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.1), duration_ns=11),
            make_dispatch("0", "kernel_b", end_ns=s_to_ns(2.2), duration_ns=22),
            make_dispatch("1", "kernel_a", end_ns=s_to_ns(2.3), duration_ns=33),
            make_dispatch("1", "kernel_b", end_ns=s_to_ns(2.4), duration_ns=44),
        ]
        collector_instance._KernelTrace__dispatches.extend(dispatches)

        set_time(mock_time, 3)
        metrics = collect_metrics(collector_instance)

        assert len(metrics["kernel"]) == 8

    def test_empty_bins_yield_dropped_count(self, collector_instance, mock_time):
        """No dispatches -> each bin still yields the dropped dispatch count line."""
        set_time(mock_time, 20)
        metrics = collect_metrics(collector_instance)

        assert metrics["kernel"] == []
        assert len(metrics["dropped"]) > 0

    def test_multiple_calls_state_transition(self, collector_instance, mock_time):
        """After flush=True consumes all bins, a second flush=True yields only dropped count lines."""
        dispatch = make_dispatch("0", "kernel_a", end_ns=s_to_ns(2.5), duration_ns=50)
        collector_instance._KernelTrace__dispatches.append(dispatch)

        set_time(mock_time, 3)
        first = collect_metrics(collector_instance)
        assert len(first["kernel"]) == 2

        set_time(mock_time, 4)
        second = collect_metrics(collector_instance)
        assert len(second["kernel"]) == 0
        assert all(DROPPED_METRIC in l for l in second["all"])


class TestHandleRequest:
    def test_json_format(self, collector_instance, flask_app):
        """Parses JSON array of arrays and builds correct dispatch tuples."""
        json_data = b'[[0,"kernel_a",1000000000,2000000000],[1,"kernel_b",3000000000,4000000000]]'

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            response, status = collector_instance.handleRequest()

            assert status == 204
            assert len(collector_instance._KernelTrace__dispatches) == 2
            assert collector_instance._KernelTrace__dispatches[0] == (0, "kernel_a", 2000000000, 1000000000)

    def test_empty_json_array(self, collector_instance, flask_app):
        """An empty JSON array returns 204 and adds no dispatches."""
        with flask_app.test_request_context(data=b"[]", content_type="application/json"):
            response, status = collector_instance.handleRequest()

            assert status == 204
            assert len(collector_instance._KernelTrace__dispatches) == 0

    def test_complex_kernel_names(self, collector_instance, flask_app):
        """Kernel names with C++ template syntax are preserved verbatim."""
        json_data = orjson.dumps([[0, "std::vector<int>::push_back(int const&)", 100, 200]])

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            response, status = collector_instance.handleRequest()

            assert status == 204
            assert collector_instance._KernelTrace__dispatches[0][1] == "std::vector<int>::push_back(int const&)"

    def test_malformed_json(self, collector_instance, flask_app):
        """Malformed JSON body returns 400."""
        with flask_app.test_request_context(data=b"[invalid", content_type="application/json"):
            response, status = collector_instance.handleRequest()

            assert status == 400

    def test_wrong_field_count_too_few(self, collector_instance, flask_app):
        """A record with 3 fields (too few) returns 400 and adds no dispatches."""
        json_data = orjson.dumps([[0, "kernel_a", s_to_ns(1)]])

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            _, status = collector_instance.handleRequest()

        assert status == 400
        assert len(collector_instance._KernelTrace__dispatches) == 0

    def test_wrong_field_count_too_many(self, collector_instance, flask_app):
        """A record with 5 fields (too many) returns 400 and adds no dispatches."""
        json_data = orjson.dumps([[0, "kernel_a", s_to_ns(1), s_to_ns(2), 99]])

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            _, status = collector_instance.handleRequest()

        assert status == 400
        assert len(collector_instance._KernelTrace__dispatches) == 0

    def test_flat_array_not_nested(self, collector_instance, flask_app):
        """A flat JSON array (not nested) returns 400 and adds no dispatches."""
        json_data = orjson.dumps([1, 2, 3, 4])

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            _, status = collector_instance.handleRequest()

        assert status == 400
        assert len(collector_instance._KernelTrace__dispatches) == 0

    def test_partial_success_is_atomic(self, collector_instance, flask_app):
        """One valid record + one invalid record -> 400, zero dispatches added."""
        records = [[0, "kernel_a", s_to_ns(1), s_to_ns(2)], [1, "kernel_b", s_to_ns(3)]]
        json_data = orjson.dumps(records)

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            _, status = collector_instance.handleRequest()

        assert status == 400
        assert len(collector_instance._KernelTrace__dispatches) == 0

    def test_non_json_content_returns_400(self, collector_instance, flask_app):
        """Plain text body returns 400 and adds no dispatches."""
        with flask_app.test_request_context(data=b"plain text", content_type="text/plain"):
            _, status = collector_instance.handleRequest()

        assert status == 400
        assert len(collector_instance._KernelTrace__dispatches) == 0

    def test_builds_dispatches_correctly(self, collector_instance, flask_app):
        """dispatch tuple = (gpu_id, kernel, end_ns, end_ns - start_ns)."""
        json_data = orjson.dumps([[0, "kernel_a", s_to_ns(1), s_to_ns(3)]])

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            _, status = collector_instance.handleRequest()

        assert status == 204
        assert collector_instance._KernelTrace__dispatches[0] == (0, "kernel_a", s_to_ns(3), s_to_ns(2))


class TestKernelNameInterning:
    def test_same_object(self, collector_instance, flask_app):
        """Same kernel name submitted twice -> same Python object (identity)."""
        json_data = orjson.dumps([[0, "kernel_a", s_to_ns(1), s_to_ns(2)]])

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            collector_instance.handleRequest()
        name1 = collector_instance._KernelTrace__dispatches[-1][1]
        collector_instance._KernelTrace__dispatches.clear()

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            collector_instance.handleRequest()
        name2 = collector_instance._KernelTrace__dispatches[-1][1]

        assert name1 is name2
        assert len(collector_instance._KernelTrace__kernel_names) == 1

    def test_distinct_kernels(self, collector_instance, flask_app):
        """Two different kernel names -> two distinct Python objects."""
        json_data_a = orjson.dumps([[0, "kernel_a", s_to_ns(1), s_to_ns(2)]])
        json_data_b = orjson.dumps([[0, "kernel_b", s_to_ns(1), s_to_ns(2)]])

        with flask_app.test_request_context(data=json_data_a, content_type="application/json"):
            collector_instance.handleRequest()
        name_a = collector_instance._KernelTrace__dispatches[-1][1]

        with flask_app.test_request_context(data=json_data_b, content_type="application/json"):
            collector_instance.handleRequest()
        name_b = collector_instance._KernelTrace__dispatches[-1][1]

        assert name_a is not name_b
        assert len(collector_instance._KernelTrace__kernel_names) == 2

    def test_long_cpp_template_name(self, collector_instance, flask_app):
        """A long C++ template name is interned; second request shares the same object."""
        long_name = "void HipKernel<" + "T," * 100 + "int>(T*, int)"
        assert len(long_name) >= 200  # sanity check it's reasonably long

        json_data = orjson.dumps([[0, long_name, s_to_ns(1), s_to_ns(2)]])

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            collector_instance.handleRequest()
        name1 = collector_instance._KernelTrace__dispatches[-1][1]
        collector_instance._KernelTrace__dispatches.clear()

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            collector_instance.handleRequest()
        name2 = collector_instance._KernelTrace__dispatches[-1][1]

        assert name1 is name2
        assert len(collector_instance._KernelTrace__kernel_names) == 1


class TestThreadSafety:
    def test_no_data_loss(self, collector_instance, flask_app):
        """Two threads each posting 5 dispatches: all 10 are recorded."""

        def post_dispatches(gpu_id, count):
            records = [[gpu_id, "kernel_a", s_to_ns(i), s_to_ns(i + 1)] for i in range(1, count + 1)]
            json_data = orjson.dumps(records)
            with flask_app.test_request_context(data=json_data, content_type="application/json"):
                collector_instance.handleRequest()

        t1 = threading.Thread(target=post_dispatches, args=(0, 5))
        t2 = threading.Thread(target=post_dispatches, args=(1, 5))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        all_dispatches = collector_instance._KernelTrace__dispatches
        assert len(all_dispatches) == 10
        assert sum(1 for d in all_dispatches if d[0] == 0) == 5
        assert sum(1 for d in all_dispatches if d[0] == 1) == 5

    def test_handleRequest_and_process(self, collector_instance, mock_time, flask_app):
        """Dispatches posted across two handleRequest calls are all accumulated."""
        # First batch: 3 dispatches with end at t=2.5s -> end_bin=3000
        records_first = [[0, "kernel_a", s_to_ns(0.5), s_to_ns(2.5)]] * 3
        with flask_app.test_request_context(data=orjson.dumps(records_first), content_type="application/json"):
            collector_instance.handleRequest()

        set_time(mock_time, 3)
        collector_instance._KernelTrace__process_dispatches()

        # Second batch: 2 dispatches with end at t=3.5s -> end_bin=4000
        records_second = [[0, "kernel_a", s_to_ns(1), s_to_ns(3.5)]] * 2
        with flask_app.test_request_context(data=orjson.dumps(records_second), content_type="application/json"):
            collector_instance.handleRequest()

        set_time(mock_time, 4)
        collector_instance._KernelTrace__process_dispatches()

        # 3 first batch + 2 second batch = 5 total
        assert collector_instance._KernelTrace__values[(0, "kernel_a")][0] == 5
