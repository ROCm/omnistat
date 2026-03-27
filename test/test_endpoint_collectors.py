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
import os
import re
import threading
import time

import pytest
import requests
from flask import Flask
from prometheus_client.parser import text_string_to_metric_families
from werkzeug.serving import make_server

import test.config
import test.workloads as workloads
from omnistat.collector_kernel_trace import KernelTrace

requires_rocm = pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")

requires_tracing = pytest.mark.skipif(
    not test.config.rocm_host or "ROCP_TOOL_LIBRARIES" not in os.environ,
    reason="requires ROCm and ROCP_TOOL_LIBRARIES",
)

METRIC_KERNEL_DROPPED = "omnistat_kernel_dropped_dispatches"
METRIC_KERNEL_DISPATCH_COUNT = "omnistat_kernel_dispatch_count"
METRIC_KERNEL_TOTAL_DURATION = "omnistat_kernel_total_duration_ns"


class StandaloneTestServer:
    """Test server for endpoint collectors (e.g. KernelTrace).

    Mirrors how Standalone initializes endpoint collectors: creates a Flask
    app, instantiates the collector, and periodically calls updateMetrics()
    in a background thread.
    """

    def __init__(self, collector_cls, interval=1.0):
        self.config = configparser.ConfigParser()
        self.app = Flask("omnistat_test")
        self.interval = interval
        self.collector = collector_cls(config=self.config, route=self.app.route, interval=interval)
        self.label_defaults = 'instance="test"'

        self._address = "127.0.0.1"
        self._timeout = 5

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._update_loop, daemon=True)
        self._thread.start()

        self._http_server = make_server(self._address, 0, self.app)
        self._port = self._http_server.socket.getsockname()[1]
        self._http_thread = threading.Thread(target=self._http_server.serve_forever, daemon=True)
        self._http_thread.start()
        self._wait_for_server()

    def _update_loop(self):
        while not self._stop_event.is_set():
            self.collector.updateMetrics()
            self._stop_event.wait(self.interval)

    def _wait_for_server(self):
        url = f"http://{self._address}:{self._port}/"
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            try:
                requests.get(url, timeout=0.1)
                return
            except requests.ConnectionError:
                time.sleep(0.05)
        raise TimeoutError(f"Server did not start within {self._timeout}s")

    def get_metrics(self, flush=False):
        """Return {name: {frozenset(labels): [values]}}."""
        lines = list(self.collector.formatMetrics(self.label_defaults, flush=flush))
        text = b"".join(lines).decode()
        results = {}
        for family in text_string_to_metric_families(text):
            for sample in family.samples:
                key = frozenset(sample.labels.items())
                results.setdefault(sample.name, {}).setdefault(key, []).append(sample.value)
        return results

    @property
    def trace_env(self):
        return {"OMNISTAT_TRACE_ENDPOINT_PORT": str(self._port)}

    def stop(self):
        if self._http_server is not None:
            self._http_server.shutdown()
        self._stop_event.set()
        self._thread.join(timeout=3)


def assert_no_drops(metrics):
    assert METRIC_KERNEL_DROPPED in metrics
    for label_set, values in metrics[METRIC_KERNEL_DROPPED].items():
        assert set(dict(label_set).keys()) == {"instance"}
        assert all(v == 0 for v in values)


def assert_dispatches(metrics, expected):
    assert METRIC_KERNEL_DISPATCH_COUNT in metrics
    assert METRIC_KERNEL_TOTAL_DURATION in metrics

    assert len(metrics[METRIC_KERNEL_TOTAL_DURATION]) == len(
        metrics[METRIC_KERNEL_DISPATCH_COUNT]
    ), "Mismatch between duration and dispatch count label sets"

    for label_set, values in metrics[METRIC_KERNEL_DISPATCH_COUNT].items():
        assert set(dict(label_set).keys()) == {"instance", "card", "kernel"}
        assert all(v > 0 for v in values)
        assert all(a <= b for a, b in zip(values, values[1:]))

    for label_set, values in metrics[METRIC_KERNEL_TOTAL_DURATION].items():
        assert set(dict(label_set).keys()) == {"instance", "card", "kernel"}
        assert all(v > 0 for v in values)

    total = sum(v[-1] for v in metrics[METRIC_KERNEL_DISPATCH_COUNT].values())
    assert total == expected


class TestKernelTraceCollector:
    @requires_rocm
    def test_no_application(self):
        server = StandaloneTestServer(KernelTrace)
        time.sleep(2)
        metrics = server.get_metrics(flush=True)
        server.stop()

        assert_no_drops(metrics)

    @requires_tracing
    @pytest.mark.parametrize("num_kernels", [1, 100, 1000, 10000])
    def test_application(self, num_kernels):
        server = StandaloneTestServer(KernelTrace)
        result = workloads.run("launch_kernels", [num_kernels], env=server.trace_env)
        metrics = server.get_metrics(flush=True)
        server.stop()

        assert result.returncode == 0, f"launch_kernels failed: {result.stderr}"

        assert_no_drops(metrics)
        assert_dispatches(metrics, expected=num_kernels)

        # Check kernel name
        for label_set in metrics[METRIC_KERNEL_DISPATCH_COUNT]:
            assert re.fullmatch(
                r"(void )?empty_kernel\(\) \[clone \.kd\]",
                dict(label_set)["kernel"],
            )

    @requires_tracing
    def test_sequential_workloads(self):
        server = StandaloneTestServer(KernelTrace)
        workloads.run("launch_kernels", [30], env=server.trace_env)
        workloads.run("launch_kernels", [70], env=server.trace_env)
        metrics = server.get_metrics(flush=True)
        server.stop()

        assert_no_drops(metrics)
        assert_dispatches(metrics, expected=100)

    @requires_tracing
    def test_cumulative_flushes(self):
        server = StandaloneTestServer(KernelTrace)
        workloads.run("launch_kernels", [50], env=server.trace_env)
        metrics1 = server.get_metrics(flush=True)
        workloads.run("launch_kernels", [50], env=server.trace_env)
        metrics2 = server.get_metrics(flush=True)
        server.stop()

        assert_no_drops(metrics1)
        assert_no_drops(metrics2)
        assert_dispatches(metrics1, expected=50)
        assert_dispatches(metrics2, expected=100)

    @requires_tracing
    def test_delayed_dispatches(self):
        # 50 kernels with 30ms delay = ~1.5s, spanning multiple 0.5s bins
        server = StandaloneTestServer(KernelTrace, interval=0.5)
        result = workloads.run("launch_kernels", [50, 30000], env=server.trace_env)
        metrics = server.get_metrics(flush=True)
        server.stop()

        assert result.returncode == 0, f"launch_kernels failed: {result.stderr}"

        assert_no_drops(metrics)
        assert_dispatches(metrics, expected=50)

        # ~1.5s across 0.5s bins should produce approximately 3 bins. Allowing
        # some extra bins related to server startup.
        num_bins = len(next(iter(metrics[METRIC_KERNEL_DROPPED].values())))
        assert 3 <= num_bins <= 5

    @requires_tracing
    def test_scrape_during_workload(self):
        server = StandaloneTestServer(KernelTrace, interval=0.5)
        result = None

        def run_workload():
            nonlocal result
            # 100 kernels with 40ms delay = ~4 seconds
            result = workloads.run("launch_kernels", [100, 40000], env=server.trace_env)

        t = threading.Thread(target=run_workload)
        t.start()

        # Scrape mid-run with flush=False (mirrors real Omnistat scrape)
        time.sleep(2)
        metrics1 = server.get_metrics(flush=False)

        t.join(timeout=30)
        metrics2 = server.get_metrics(flush=True)
        server.stop()

        assert result is not None
        assert result.returncode == 0

        # Mid-run scrape with flush=False: all bins are within the 15s hold
        # window, so nothing should be released yet
        assert metrics1 == {}

        assert_no_drops(metrics2)
        assert_dispatches(metrics2, expected=100)

    @requires_tracing
    def test_collector_unreachable(self):
        result = workloads.run("launch_kernels", [10], env={"OMNISTAT_TRACE_ENDPOINT_PORT": "19998"})
        assert result.returncode == 0, (
            f"launch_kernels should exit 0 even when collector is unreachable; " f"stderr: {result.stderr}"
        )
