# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2026 Advanced Micro Devices, Inc. All Rights Reserved.
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
from pathlib import Path
from unittest.mock import Mock, patch

import orjson
import pytest
import requests
from flask import Flask

from omnistat.collector_kernel_trace import KernelTrace
from omnistat.standalone import push_to_victoria_metrics
from omnistat.utils import readConfig
from test.generate_kernels import KernelGenerator

test_path = Path(__file__).resolve().parent
CONFIG_FILE = f"{test_path}/docker/victoriametrics/omnistat-query.config"

config = readConfig(CONFIG_FILE)
URL = config["omnistat.query"]["prometheus_url"]

METRIC_COUNT = "omnistat_kernel_dispatch_count"
METRIC_DURATION = "omnistat_kernel_total_duration_ns"


def run_kernel_trace_pipeline(trace, advance_ms=None):
    """Simulate a complete kernel-trace collection cycle and push results.

    Creates a KernelTrace collector, feeds it every batch of dispatch records
    from the trace, processes each batch at the correct simulated time, then
    formats remaining bins and pushes to VictoriaMetrics.

    Args:
        trace: A KernelGenerator with dispatches already added.
        advance_ms: Milliseconds to advance the simulated clock.
                    Defaults to 1s beyond the collector's buffer window.
    """
    interval_s = trace.interval_ms / 1000.0
    init_time_ns = trace.base_bin_ms * 1_000_000

    # Initialize the collector with offset_ns=0 by aligning both clocks.
    with patch("time.time_ns", return_value=init_time_ns), patch("time.clock_gettime_ns", return_value=init_time_ns):
        collector = KernelTrace(configparser.ConfigParser(), Mock(), interval_s)

    if advance_ms is None:
        advance_ms = collector._KernelTrace__window_ms + 1000

    # Inject each batch via a Flask request context, then process dispatches
    # at the batch's simulated wall-clock time.
    flask_app = Flask(__name__)
    for i, process_time_ns, records in trace.generate_batches(advance_ms=advance_ms):
        json_data = orjson.dumps(records)
        with flask_app.test_request_context(data=json_data, content_type="application/json"):
            collector.handleRequest()
        with patch("time.time_ns", return_value=process_time_ns):
            collector._KernelTrace__process_dispatches()

    # Format remaining bins at advance_ms past the last bin (no flush=True).
    flush_time_ns = (trace.base_bin_ms + (trace.num_intervals * trace.interval_ms) + advance_ms) * 1_000_000
    with patch("time.time_ns", return_value=flush_time_ns):
        stream = collector.formatMetrics(trace.label_defaults)
    push_to_victoria_metrics([], [stream], URL)


def query_at(metric, labels_str, timestamp_ms):
    """Query VictoriaMetrics for a single metric value at an exact timestamp.

    Returns the int value if a result exists, or None if the query matched
    no time series.
    """
    resp = requests.get(
        URL + "/api/v1/query",
        params={
            "query": f"{metric}{{{labels_str}}}",
            "time": timestamp_ms / 1000.0,
        },
    )
    results = resp.json()["data"]["result"]
    return int(float(results[0]["value"][1])) if results else None


class TestKernelTrace:
    def test_buffer_hold(self):
        trace = KernelGenerator(duration_s=1, interval_s=1.0)

        dispatch_start_ns = 0
        dispatch_duration_ns = 1
        trace.add_dispatch(0, "kernel_a", dispatch_start_ns, dispatch_duration_ns)

        # advance_ms=0: clock stays at the last bin, all bins are within the buffer
        run_kernel_trace_pipeline(trace, advance_ms=0)

        labels = trace.query_labels(0, "kernel_a")
        timestamp = trace.bin_timestamp_ms(interval_index=0)
        assert query_at(METRIC_COUNT, labels, timestamp) is None
        assert query_at(METRIC_DURATION, labels, timestamp) is None

    def test_buffer_release(self):
        trace = KernelGenerator(duration_s=1, interval_s=1.0)

        dispatch_start_ns = 0
        dispatch_duration_ns = 1
        trace.add_dispatch(0, "kernel_a", dispatch_start_ns, dispatch_duration_ns)

        run_kernel_trace_pipeline(trace)

        labels = trace.query_labels(0, "kernel_a")
        timestamp = trace.bin_timestamp_ms(interval_index=0)
        assert query_at(METRIC_COUNT, labels, timestamp) == 1
        assert query_at(METRIC_DURATION, labels, timestamp) == 1

    @pytest.mark.parametrize(
        "dispatch_start_ns, dispatch_duration_ns, expected_bin",
        [
            # dispatch at the very start of the first interval
            (0, 1, 0),
            # dispatch in the middle of the first interval
            (500_000_000, 1_000_000, 0),
            # dispatch at the end of the first interval, ending in the second
            (999_000_000, 1_000_000, 1),
            # dispatch spanning most of the first interval
            (0, 999_000_000, 0),
            # dispatch filling the entire first interval, end lands in second
            (0, 1_000_000_000, 1),
            # dispatch longer than one interval (1.5s), end in second bin
            (0, 1_500_000_000, 1),
        ],
        ids=[
            "start_of_interval",
            "middle_of_interval",
            "end_of_interval",
            "nearly_full_interval",
            "exact_interval",
            "spans_two_intervals",
        ],
    )
    def test_single_dispatch(self, dispatch_start_ns, dispatch_duration_ns, expected_bin):
        """Single dispatch placed at various positions relative to the interval."""
        trace = KernelGenerator(duration_s=2, interval_s=1.0)
        trace.add_dispatch(0, "kernel_a", dispatch_start_ns, dispatch_duration_ns)

        run_kernel_trace_pipeline(trace)

        labels = trace.query_labels(0, "kernel_a")
        for i in range(trace.num_intervals):
            timestamp = trace.bin_timestamp_ms(i)
            if i < expected_bin:
                assert query_at(METRIC_COUNT, labels, timestamp) is None
                assert query_at(METRIC_DURATION, labels, timestamp) is None
            else:
                # Cumulative counters: value persists at and after expected_bin
                assert query_at(METRIC_COUNT, labels, timestamp) == 1
                assert query_at(METRIC_DURATION, labels, timestamp) == dispatch_duration_ns

    @pytest.mark.parametrize(
        "duration_s, interval_s, dispatch_duration_ms, dispatch_period_ms",
        [
            (1, 1.0, 1, 1000),
            (1, 1.0, 1, 100),
            (1, 1.0, 1, 10),
            (1, 1.0, 1, 1),
            (1, 0.1, 1, 1000),
            (1, 0.1, 1, 100),
            (1, 0.1, 1, 10),
            (1, 0.1, 1, 1),
            (1, 1.0, 100, 100),
            (1, 1.0, 10, 10),
            (1, 0.1, 10, 10),
            (2, 0.5, 1, 100),
            (4, 0.5, 1, 100),
            (1, 0.1, 99, 100),
            (1, 0.1, 999, 1000),
            (1, 1.0, 999, 1000),
            (2, 1.0, 1000, 1000),  # 2s needed for 1s dispatches
            (30, 1.0, 500, 1000),
            (60, 1.0, 500, 1000),
            (90, 1.0, 500, 1000),
        ],
    )
    def test_single_kernel(self, duration_s, interval_s, dispatch_duration_ms, dispatch_period_ms):
        """Single kernel on one GPU with varying durations, intervals, and periods."""
        trace = KernelGenerator(duration_s, interval_s)

        dispatch_duration_ns = dispatch_duration_ms * 1_000_000
        dispatch_period_ns = dispatch_period_ms * 1_000_000
        trace.add_periodic_kernel(0, "kernel_a", dispatch_duration_ns, dispatch_period_ns)

        run_kernel_trace_pipeline(trace)

        labels = trace.query_labels(0, "kernel_a")
        counts = trace.expected_counts(0, "kernel_a")
        cumulative_count = 0
        cumulative_duration = 0
        for i, n in enumerate(counts):
            timestamp = trace.bin_timestamp_ms(i)
            cumulative_count += n
            cumulative_duration += n * dispatch_duration_ns
            if cumulative_count > 0:
                assert query_at(METRIC_COUNT, labels, timestamp) == cumulative_count
                assert query_at(METRIC_DURATION, labels, timestamp) == cumulative_duration

        assert cumulative_count > 0
        assert cumulative_duration > 0
