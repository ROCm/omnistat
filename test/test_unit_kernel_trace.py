import configparser
import threading
from unittest.mock import Mock, patch

import orjson
import pytest
from flask import Flask

from omnistat.collector_kernel_trace import KernelTrace


@pytest.fixture
def mock_time():
    """Fixture to mock time functions for deterministic tests."""
    with patch("time.time_ns") as mock_time_ns, patch("time.clock_gettime_ns") as mock_clock_gettime_ns:
        # unix time and boot time both at t=1s, so __offset_ns=0.
        mock_time_ns.return_value = 1_000_000_000
        mock_clock_gettime_ns.return_value = 1_000_000_000
        yield {"time_ns": mock_time_ns, "clock_gettime_ns": mock_clock_gettime_ns, "time_ns_init": 1_000_000_000}


@pytest.fixture
def flask_app():
    """Create a Flask app for testing request contexts."""
    return Flask(__name__)


@pytest.fixture
def collector_instance(mock_time):
    """KernelTrace instance at t=1s with 1s interval and __offset_ns=0."""
    config = configparser.ConfigParser()
    return KernelTrace(config, Mock(), 1.0)


@pytest.fixture
def label_defaults():
    return 'host="testnode"'


class TestPopBins:
    def test_exact_cutoff_boundary(self, collector_instance):
        """Bins exactly at cutoff ARE popped (cutoff bin is inclusive)."""
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
        mock_time["time_ns"].return_value = 10_000_000_000
        mock_time["clock_gettime_ns"].return_value = 3_000_000_000
        kt = KernelTrace(configparser.ConfigParser(), Mock(), 1.0)

        assert kt._KernelTrace__offset_ns == 7_000_000_000

    def test_initial_bin(self, mock_time):
        """At t=1s with 1s interval, initial bin is 2000 ms."""
        kt = KernelTrace(configparser.ConfigParser(), Mock(), interval=1.0)

        ts = kt._KernelTrace__ts
        assert len(ts) == 1
        assert 2000 in ts

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

        duration = 10
        start_seconds = (mock_time["time_ns"].return_value // 1_000_000_000) + 1
        end_seconds = start_seconds + duration
        for i in range(start_seconds, end_seconds):
            mock_time["time_ns"].return_value = i * 1_000_000_000
            last_bin = collector_instance._KernelTrace__process_dispatches()

            assert last_bin == (mock_time["time_ns"].return_value // 1_000_000) + 1_000
            assert len(collector_instance._KernelTrace__values) == 0

            bins = collector_instance._KernelTrace__ts
            assert last_bin in bins
            assert len(bins[last_bin]) == 0

        assert len(collector_instance._KernelTrace__ts) == duration + 1

    def test_single_dispatch(self, collector_instance, mock_time):
        """A single dispatch is accumulated and snapshotted into the correct bin."""
        # end_ms = (3s - 1s) / 1ms = 2000 -> end_bin = 3000
        gpu_end_ns = 3_000_000_000 - mock_time["time_ns_init"]
        duration_ns = 5_000_000
        dispatch = ("0", "kernel_a", gpu_end_ns, duration_ns)
        collector_instance._KernelTrace__dispatches.append(dispatch)

        mock_time["time_ns"].return_value = 3 * 1_000_000_000
        last_bin = collector_instance._KernelTrace__process_dispatches()

        values = collector_instance._KernelTrace__values
        ts = collector_instance._KernelTrace__ts

        key = ("0", "kernel_a")
        assert key in values
        assert values[key] == [1, duration_ns]

        expected_bin = mock_time["time_ns"].return_value // 1_000_000
        assert expected_bin in ts
        assert key in ts[expected_bin]
        assert ts[expected_bin][key] == [1, duration_ns]

        assert len(collector_instance._KernelTrace__dispatches) == 0

    def test_same_kernel_same_bin(self, collector_instance, mock_time):
        """Two dispatches of the same kernel in the same bin accumulate."""
        # Both end between t=2s and t=3s -> end_bin=3000
        d1 = ("0", "kernel_a", 2_500_000_000, 100_000_000)
        d2 = ("0", "kernel_a", 2_800_000_000, 200_000_000)
        collector_instance._KernelTrace__dispatches.extend([d1, d2])

        mock_time["time_ns"].return_value = 3_000_000_000
        collector_instance._KernelTrace__process_dispatches()

        values = collector_instance._KernelTrace__values
        ts = collector_instance._KernelTrace__ts
        key = ("0", "kernel_a")

        assert values[key] == [2, 300_000_000]
        assert ts[3000][key] == [2, 300_000_000]

    def test_same_kernel_cumulative_snapshot(self, collector_instance, mock_time):
        """Snapshots in __ts are cumulative totals, not per-bin deltas."""
        key = ("0", "kernel_a")

        # First dispatch: end_ms=2500 -> end_bin=3000
        collector_instance._KernelTrace__dispatches.append(("0", "kernel_a", 2_500_000_000, 100_000_000))
        mock_time["time_ns"].return_value = 3_000_000_000
        collector_instance._KernelTrace__process_dispatches()

        ts = collector_instance._KernelTrace__ts
        assert ts[3000][key] == [1, 100_000_000]

        # Second dispatch: end_ms=3500 -> end_bin=4000
        collector_instance._KernelTrace__dispatches.append(("0", "kernel_a", 3_500_000_000, 50_000_000))
        mock_time["time_ns"].return_value = 4_000_000_000
        collector_instance._KernelTrace__process_dispatches()

        # ts[4000] snapshot includes both dispatches
        assert ts[4000][key] == [2, 150_000_000]
        # ts[3000] is unchanged
        assert ts[3000][key] == [1, 100_000_000]

    def test_multiple_kernels_multiple_gpus(self, collector_instance, mock_time):
        """4 dispatches across 2 GPUs and 2 kernels all go into the same bin."""
        dispatches = [
            ("0", "kernel_a", 2_100_000_000, 10_000_000),
            ("0", "kernel_b", 2_200_000_000, 20_000_000),
            ("1", "kernel_a", 2_300_000_000, 30_000_000),
            ("1", "kernel_b", 2_400_000_000, 40_000_000),
        ]
        collector_instance._KernelTrace__dispatches.extend(dispatches)

        mock_time["time_ns"].return_value = 3_000_000_000
        collector_instance._KernelTrace__process_dispatches()

        values = collector_instance._KernelTrace__values
        ts = collector_instance._KernelTrace__ts

        assert len(values) == 4
        assert len(ts[3000]) == 4
        assert values[("0", "kernel_a")] == [1, 10_000_000]
        assert values[("1", "kernel_b")] == [1, 40_000_000]

    def test_assigned_to_past_bin(self, collector_instance, mock_time):
        """A dispatch with an old end timestamp goes into its correct past bin."""
        # Current time t=4s (last_bin=5000). Dispatch end_ms=1500 -> end_bin=2000 (past).
        collector_instance._KernelTrace__dispatches.append(("0", "kernel_a", 1_500_000_000, 5_000_000))

        mock_time["time_ns"].return_value = 4_000_000_000
        collector_instance._KernelTrace__process_dispatches()

        ts = collector_instance._KernelTrace__ts
        key = ("0", "kernel_a")

        assert key in ts[2000]
        assert key not in ts.get(3000, {})
        assert key not in ts.get(5000, {})

    def test_exact_interval_boundary(self, collector_instance, mock_time):
        """A dispatch ending exactly on a boundary goes into the NEXT bin."""
        # end_ns=2_000_000_000 -> end_ms=2000 (exactly on boundary) -> end_bin=3000
        collector_instance._KernelTrace__dispatches.append(("0", "kernel_a", 2_000_000_000, 1_000_000))

        mock_time["time_ns"].return_value = 3_000_000_000
        collector_instance._KernelTrace__process_dispatches()

        ts = collector_instance._KernelTrace__ts
        key = ("0", "kernel_a")

        assert key in ts[3000]
        assert key not in ts.get(2000, {})

    def test_out_of_range_before_first_bin(self, collector_instance, mock_time):
        """A dispatch whose end_bin < first_bin is silently dropped."""
        # __offset_ns=0, so end_ms = end_ns // 1_000_000. Use a large negative
        # end_ns to force end_bin below first_bin=2000.
        collector_instance._KernelTrace__dispatches.append(("0", "kernel_a", -2_000_000_000, 1_000_000))

        mock_time["time_ns"].return_value = 3_000_000_000
        collector_instance._KernelTrace__process_dispatches()

        assert len(collector_instance._KernelTrace__values) == 0
        for bin_data in collector_instance._KernelTrace__ts.values():
            assert len(bin_data) == 0

    def test_out_of_range_after_last_bin(self, collector_instance, mock_time):
        """A dispatch whose end_bin > last_bin is silently dropped."""
        # current t=3s -> last_bin=4000; dispatch ends at t=100s -> end_bin=101000
        collector_instance._KernelTrace__dispatches.append(("0", "kernel_a", 100_000_000_000, 1_000_000))

        mock_time["time_ns"].return_value = 3_000_000_000
        collector_instance._KernelTrace__process_dispatches()

        assert len(collector_instance._KernelTrace__values) == 0

    def test_mixed_range_in_and_out(self, collector_instance, mock_time):
        """In-range dispatches are counted; out-of-range ones are dropped."""
        # In-range: end_ms=2500 -> end_bin=3000 (within first_bin=2000..last_bin=4000)
        d_in = ("0", "kernel_a", 2_500_000_000, 10_000_000)
        # Too early: end_ns very negative -> end_bin < first_bin=2000
        d_early = ("0", "kernel_b", -2_000_000_000, 5_000_000)
        # Too late: end_ms=100000 -> end_bin=101000 > last_bin=4000
        d_late = ("0", "kernel_c", 100_000_000_000, 7_000_000)
        collector_instance._KernelTrace__dispatches.extend([d_in, d_early, d_late])

        mock_time["time_ns"].return_value = 3_000_000_000
        collector_instance._KernelTrace__process_dispatches()

        values = collector_instance._KernelTrace__values
        assert ("0", "kernel_a") in values
        assert ("0", "kernel_b") not in values
        assert ("0", "kernel_c") not in values


class TestFormatMetrics:
    def test_no_flush_holds_recent_data(self, collector_instance, mock_time, label_defaults):
        """With flush=False, bins within the 15s window are held back."""
        # end_ms=2500 -> end_bin=3000; at t=3s last_bin=4000, cutoff=4000-15000=-11000
        collector_instance._KernelTrace__dispatches.append(("0", "kernel_a", 2_500_000_000, 10_000_000))

        mock_time["time_ns"].return_value = 3_000_000_000
        output = list(collector_instance.formatMetrics(label_defaults, flush=False))

        assert [line for line in output if line != b"\n"] == []

    def test_no_flush_releases_old_bins(self, collector_instance, mock_time, label_defaults):
        """Bins older than the 15s window are released with flush=False."""
        # Dispatch end_ms=2500 -> end_bin=3000
        collector_instance._KernelTrace__dispatches.append(("0", "kernel_a", 2_500_000_000, 10_000_000))
        mock_time["time_ns"].return_value = 3_000_000_000
        collector_instance._KernelTrace__process_dispatches()

        # Advance to t=20s -> last_bin=21000, cutoff=6000; bin 3000 is now old
        mock_time["time_ns"].return_value = 20_000_000_000
        output = [line for line in collector_instance.formatMetrics(label_defaults, flush=False) if line != b"\n"]

        # bin 3000 has one kernel -> 2 metric lines (count + duration)
        assert len(output) == 2

    def test_flush_releases_all_bins(self, collector_instance, mock_time, label_defaults):
        """flush=True releases all bins including the most recent one."""
        collector_instance._KernelTrace__dispatches.append(("0", "kernel_a", 2_500_000_000, 10_000_000))

        mock_time["time_ns"].return_value = 3_000_000_000
        output = [line for line in collector_instance.formatMetrics(label_defaults, flush=True) if line != b"\n"]

        assert len(output) == 2
        assert len(collector_instance._KernelTrace__ts) == 0

    def test_exact_prometheus_line_format(self, collector_instance, mock_time, label_defaults):
        """Verify the exact bytes of the Prometheus output lines."""
        # end_ms=2500 -> end_bin=3000; duration=1_000_000ns
        collector_instance._KernelTrace__dispatches.append(("0", "kernel_a", 2_500_000_000, 1_000_000))

        mock_time["time_ns"].return_value = 3_000_000_000
        output = [line for line in collector_instance.formatMetrics(label_defaults, flush=True) if line != b"\n"]

        assert output[0] == b'omnistat_kernel_dispatch_count{host="testnode",card="0",kernel="kernel_a"} 1 3000'
        assert (
            output[1] == b'omnistat_kernel_total_duration_ns{host="testnode",card="0",kernel="kernel_a"} 1000000 3000'
        )

    def test_multiple_gpus_multiple_kernels_output(self, collector_instance, mock_time, label_defaults):
        """4 dispatches (2 GPUs × 2 kernels) produce 8 non-newline metric lines."""
        dispatches = [
            ("0", "kernel_a", 2_100_000_000, 10_000_000),
            ("0", "kernel_b", 2_200_000_000, 20_000_000),
            ("1", "kernel_a", 2_300_000_000, 30_000_000),
            ("1", "kernel_b", 2_400_000_000, 40_000_000),
        ]
        collector_instance._KernelTrace__dispatches.extend(dispatches)

        mock_time["time_ns"].return_value = 3_000_000_000
        output = [line for line in collector_instance.formatMetrics(label_defaults, flush=True) if line != b"\n"]

        assert len(output) == 8

    def test_empty_bins_yield_nothing(self, collector_instance, mock_time, label_defaults):
        """No dispatches -> zero non-newline lines even with flush=True."""
        mock_time["time_ns"].return_value = 20_000_000_000
        output = [line for line in collector_instance.formatMetrics(label_defaults, flush=True) if line != b"\n"]

        assert len(output) == 0

    def test_multiple_calls_state_transition(self, collector_instance, mock_time, label_defaults):
        """After flush=True consumes all bins, a second flush=True call yields nothing."""
        collector_instance._KernelTrace__dispatches.append(("0", "kernel_a", 2_500_000_000, 10_000_000))

        mock_time["time_ns"].return_value = 3_000_000_000
        first = [line for line in collector_instance.formatMetrics(label_defaults, flush=True) if line != b"\n"]
        assert len(first) == 2

        mock_time["time_ns"].return_value = 4_000_000_000
        second = [line for line in collector_instance.formatMetrics(label_defaults, flush=True) if line != b"\n"]
        assert len(second) == 0


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
        json_data = orjson.dumps([[0, "kernel_a", 1_000_000_000]])

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            _, status = collector_instance.handleRequest()

        assert status == 400
        assert len(collector_instance._KernelTrace__dispatches) == 0

    def test_wrong_field_count_too_many(self, collector_instance, flask_app):
        """A record with 5 fields (too many) returns 400 and adds no dispatches."""
        json_data = orjson.dumps([[0, "kernel_a", 1_000_000_000, 2_000_000_000, 99]])

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
        records = [[0, "kernel_a", 1_000_000_000, 2_000_000_000], [1, "kernel_b", 3_000_000_000]]
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
        json_data = orjson.dumps([[0, "kernel_a", 1_000_000_000, 3_000_000_000]])

        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            _, status = collector_instance.handleRequest()

        assert status == 204
        assert collector_instance._KernelTrace__dispatches[0] == (0, "kernel_a", 3_000_000_000, 2_000_000_000)


class TestKernelNameInterning:
    def test_same_object(self, collector_instance, flask_app):
        """Same kernel name submitted twice -> same Python object (identity)."""
        json_data = orjson.dumps([[0, "kernel_a", 1_000_000_000, 2_000_000_000]])

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
        json_data_a = orjson.dumps([[0, "kernel_a", 1_000_000_000, 2_000_000_000]])
        json_data_b = orjson.dumps([[0, "kernel_b", 1_000_000_000, 2_000_000_000]])

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

        json_data = orjson.dumps([[0, long_name, 1_000_000_000, 2_000_000_000]])

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
            records = [[gpu_id, "kernel_a", i * 1_000_000_000, (i + 1) * 1_000_000_000] for i in range(1, count + 1)]
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
        # First batch: 3 dispatches with end_ns=2_500_000_000 -> end_bin=3000
        records_first = [[0, "kernel_a", 500_000_000, 2_500_000_000]] * 3
        with flask_app.test_request_context(data=orjson.dumps(records_first), content_type="application/json"):
            collector_instance.handleRequest()

        mock_time["time_ns"].return_value = 3_000_000_000
        collector_instance._KernelTrace__process_dispatches()

        # Second batch: 2 dispatches with end_ns=3_500_000_000 -> end_bin=4000
        records_second = [[0, "kernel_a", 1_000_000_000, 3_500_000_000]] * 2
        with flask_app.test_request_context(data=orjson.dumps(records_second), content_type="application/json"):
            collector_instance.handleRequest()

        mock_time["time_ns"].return_value = 4_000_000_000
        collector_instance._KernelTrace__process_dispatches()

        # 3 first batch + 2 second batch = 5 total
        assert collector_instance._KernelTrace__values[(0, "kernel_a")][0] == 5
