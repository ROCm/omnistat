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
import multiprocessing
import os
import time
from unittest.mock import patch

import pytest
import requests
from flask import Flask
from prometheus_client.parser import text_string_to_metric_families

import test.config
from omnistat.collector_pm_counters import PM_COUNTERS
from omnistat.monitor import Monitor
from omnistat.node_monitoring import OmnistatServer

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "pm_counters")

# Wrap PM_COUNTERS.__init__ to redirect sysfs path to test fixtures
_original_init = PM_COUNTERS.__init__


def _patched_init(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    # patching the sysfs path to point to local data for testing (as not available in CI)
    self._PM_COUNTERS__pm_counter_dir = FIXTURE_DIR


class PMCounterTestServer:
    """Test server for PM counter collector using fixture data."""

    def __init__(self):
        self.address = f"localhost:{test.config.port}"
        self.url = f"http://{self.address}/metrics"
        self.timeout = 5.0

        config = configparser.ConfigParser()
        config["omnistat.collectors"] = {
            "rocm_path": test.config.rocm_path,
            "enable_rocm_smi": "False",
            "enable_amd_smi": "False",
            "enable_vendor_counters": "True",
        }

        monitor = Monitor(config)

        def post_fork(server, worker):
            monitor.initMetrics()
            app.route("/metrics")(lambda: (monitor.updateAllMetrics(), {"Content-Type": "text/plain; charset=utf-8"}))

        app = Flask("omnistat")
        options = {"bind": self.address, "workers": 1, "post_fork": post_fork}
        server = OmnistatServer(app, options)

        self._process = multiprocessing.Process(target=server.run)
        self._process.start()

        running = self._wait_for_server()
        if not running:
            self.stop()
        assert running is True, "Failed to start PM counter test server"

    def stop(self):
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=3)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=1)
        time.sleep(1.0)

    def _wait_for_server(self):
        start_time = time.time()
        while time.time() - start_time < self.timeout:
            try:
                response = requests.get(self.url)
                if response.status_code == 200:
                    return True
            except requests.RequestException:
                pass
            time.sleep(0.5)
        return False

    def get_metrics(self):
        """Return {metric_name: {frozenset(labels): value}}."""
        response = requests.get(self.url)
        results = {}
        for family in text_string_to_metric_families(response.text):
            for sample in family.samples:
                key = frozenset(sample.labels.items())
                results.setdefault(sample.name, {})[key] = sample.value
        return results


class TestPMCounters:
    @patch.object(PM_COUNTERS, "__init__", _patched_init)
    def test_pm_counter_metrics(self):
        server = PMCounterTestServer()
        # Allow sampler thread to collect at least one sample
        time.sleep(2)
        metrics = server.get_metrics()
        server.stop()

        # --- GPU metrics (4 accelerators in fixture data) ---
        gpu_metric_names = [
            "omnistat_vendor_accel_power_watts",
            "omnistat_vendor_accel_energy_joules",
        ]
        for name in gpu_metric_names:
            assert name in metrics, f"Missing GPU metric: {name}"
            cards = {dict(labels)["accel"] for labels in metrics[name]}
            assert cards == {"0", "1", "2", "3"}, f"Expected cards 0-3 for {name}, got {cards}"
            for labels, value in metrics[name].items():
                assert value > 0, f"{name} {dict(labels)} should be > 0, got {value}"

        # --- Host metrics ---
        host_metric_names = [
            "omnistat_vendor_power_watts",
            "omnistat_vendor_energy_joules",
            "omnistat_vendor_cpu_power_watts",
            "omnistat_vendor_cpu_energy_joules",
            "omnistat_vendor_memory_power_watts",
            "omnistat_vendor_memory_energy_joules",
        ]
        for name in host_metric_names:
            assert name in metrics, f"Missing host metric: {name}"
            for labels, value in metrics[name].items():
                assert value > 0, f"{name} should be > 0, got {value}"
                assert dict(labels)["vendor"] == "cray"

        # --- Sampling counters ---
        assert "omnistat_vendor_samples_total" in metrics
        assert "omnistat_vendor_samples_skipped_total" in metrics
