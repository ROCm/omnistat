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
import threading
import time

import pytest
from flask import Flask
from prometheus_client.parser import text_string_to_metric_families

import test.config
from omnistat.collector_kernel_trace import KernelTrace

METRIC_KERNEL_DROPPED = "omnistat_kernel_dropped_dispatches"


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

        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._update_loop, daemon=True)
        self._thread.start()

    def _update_loop(self):
        while not self._stop_event.is_set():
            self.collector.updateMetrics()
            self._stop_event.wait(self.interval)

    def get_metrics(self, flush=False):
        """Return a list of (name, labels, value) tuples."""
        lines = list(self.collector.formatMetrics(self.label_defaults, flush=flush))
        text = b"".join(lines).decode()
        results = []
        for family in text_string_to_metric_families(text):
            for sample in family.samples:
                results.append((sample.name, sample.labels, sample.value))
        return results

    def stop(self):
        self._stop_event.set()
        self._thread.join(timeout=3)


class TestKernelTraceCollector:
    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    def test_kernel_trace_no_application(self):
        server = StandaloneTestServer(KernelTrace)
        time.sleep(2)
        metrics = server.get_metrics(flush=True)
        server.stop()

        assert len(metrics) > 0, "No metrics found"

        for name, labels, value in metrics:
            assert name == METRIC_KERNEL_DROPPED, f"Unexpected metric {name}"
            assert set(labels.keys()) == {"instance"}, f"Unexpected labels for {name}: {labels}"
            assert value == 0, f"Invalid value for {name} (expecting 0, received {value})"
