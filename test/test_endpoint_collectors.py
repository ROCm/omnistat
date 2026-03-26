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

TRACE_LIB = os.path.join(os.path.dirname(__file__), "..", "..", "build", "libomnistat_trace.so")

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
        self._port = test.config.port
        self._timeout = 5

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._update_loop, daemon=True)
        self._thread.start()

        self._http_server = make_server(self._address, self._port, self.app)
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

    def stop(self):
        if self._http_server is not None:
            self._http_server.shutdown()
        self._stop_event.set()
        self._thread.join(timeout=3)


class TestKernelTraceCollector:
    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    def test_kernel_trace_no_application(self):
        server = StandaloneTestServer(KernelTrace)
        time.sleep(2)
        metrics = server.get_metrics(flush=True)
        server.stop()

        assert METRIC_KERNEL_DROPPED in metrics
        for label_set, values in metrics[METRIC_KERNEL_DROPPED].items():
            assert set(dict(label_set).keys()) == {"instance"}
            assert all(v == 0 for v in values)

    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    @pytest.mark.parametrize("num_kernels", [1, 100, 1000, 10000])
    def test_kernel_trace_with_application(self, num_kernels):
        server = StandaloneTestServer(KernelTrace)
        result = workloads.run(
            "launch_kernels",
            [num_kernels],
            env={
                "ROCP_TOOL_LIBRARIES": os.path.abspath(TRACE_LIB),
                "OMNISTAT_TRACE_ENDPOINT_PORT": test.config.port,
            },
        )
        metrics = server.get_metrics(flush=True)
        server.stop()

        assert result.returncode == 0, f"launch_kernels failed: {result.stderr}"

        assert METRIC_KERNEL_DROPPED in metrics
        assert METRIC_KERNEL_TOTAL_DURATION in metrics
        assert METRIC_KERNEL_DISPATCH_COUNT in metrics

        assert len(metrics[METRIC_KERNEL_DROPPED]) > 0
        assert len(metrics[METRIC_KERNEL_TOTAL_DURATION]) > 0
        assert len(metrics[METRIC_KERNEL_DISPATCH_COUNT]) > 0

        assert len(metrics[METRIC_KERNEL_TOTAL_DURATION]) == len(
            metrics[METRIC_KERNEL_DISPATCH_COUNT]
        ), "Mismatch between number of dispatch metrics"

        # Check dropped dispatches
        for label_set, values in metrics[METRIC_KERNEL_DROPPED].items():
            labels = dict(label_set)
            assert set(labels.keys()) == {"instance"}
            assert all(v == 0 for v in values)

        # Check durations
        for label_set, values in metrics[METRIC_KERNEL_TOTAL_DURATION].items():
            labels = dict(label_set)
            assert set(labels.keys()) == {"instance", "card", "kernel"}
            assert all(v > 0 for v in values)

        # Check dispatch counts
        for label_set, values in metrics[METRIC_KERNEL_DISPATCH_COUNT].items():
            labels = dict(label_set)
            assert set(labels.keys()) == {"instance", "card", "kernel"}
            assert re.fullmatch(
                r"(void )?empty_kernel\(\) \[clone \.kd\]",
                labels["kernel"],
            )
            assert all(v > 0 for v in values)
            assert all(a <= b for a, b in zip(values, values[1:]))

        total_dispatches = sum(values[-1] for values in metrics[METRIC_KERNEL_DISPATCH_COUNT].values())
        assert total_dispatches == num_kernels
