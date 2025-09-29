# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2023 - 2025 Advanced Micro Devices, Inc. All Rights Reserved.
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
import time

import pytest
import requests
from flask import Flask
from prometheus_client.parser import text_string_to_metric_families

import test.config
from omnistat.monitor import Monitor
from omnistat.node_monitoring import OmnistatServer

SMI_METRICS = [
    "rocm_num_gpus",
    "rocm_version_info",
    "rocm_temperature_celsius",
    "rocm_temperature_celsius",
    "rocm_temperature_memory_celsius",
    "rocm_average_socket_power_watts",
    "rocm_sclk_clock_mhz",
    "rocm_mclk_clock_mhz",
    "rocm_vram_total_bytes",
    "rocm_vram_used_percentage",
    "rocm_vram_busy_percentage",
    "rocm_utilization_percentage",
]

RAS_METRICS = [
    "rocm_ras_umc_correctable_count",
    "rocm_ras_sdma_correctable_count",
    "rocm_ras_gfx_correctable_count",
    "rocm_ras_mmhub_correctable_count",
    "rocm_ras_pcie_bif_correctable_count",
    "rocm_ras_hdp_correctable_count",
    "rocm_ras_umc_uncorrectable_count",
    "rocm_ras_sdma_uncorrectable_count",
    "rocm_ras_gfx_uncorrectable_count",
    "rocm_ras_mmhub_uncorrectable_count",
    "rocm_ras_pcie_bif_uncorrectable_count",
    "rocm_ras_hdp_uncorrectable_count",
    "rocm_ras_umc_deferred_count",
    "rocm_ras_sdma_deferred_count",
    "rocm_ras_gfx_deferred_count",
    "rocm_ras_mmhub_deferred_count",
    "rocm_ras_pcie_bif_deferred_count",
    "rocm_ras_hdp_deferred_count",
]

OCCUPANCY_METRICS = [
    "rocm_num_compute_units",
    "rocm_compute_unit_occupancy",
]

COLLECTOR_CONFIGS = [
    {
        "collectors": ["rocm_smi"],
        "metrics": SMI_METRICS,
    },
    {
        "collectors": ["amd_smi"],
        "metrics": SMI_METRICS,
    },
    {
        "collectors": ["rocm_smi", "ras_ecc"],
        "metrics": [x for x in RAS_METRICS if "_deferred_count" not in x],
    },
    {
        "collectors": ["amd_smi", "ras_ecc"],
        "metrics": RAS_METRICS,
    },
    {
        "collectors": ["rocm_smi", "cu_occupancy"],
        "metrics": OCCUPANCY_METRICS,
    },
    {
        "collectors": ["amd_smi", "cu_occupancy"],
        "metrics": OCCUPANCY_METRICS,
    },
]


SUPPORTED_COLLECTORS = set()
for config in COLLECTOR_CONFIGS:
    for collector in config["collectors"]:
        SUPPORTED_COLLECTORS.add(collector)


class OmnistatTestServer:
    def __init__(self, collectors):
        self.address = f"localhost:{test.config.port}"
        self.url = f"http://{self.address}/metrics"
        self.timeout = 3.0
        self.collectors = collectors

        config = self.generate_config(self.collectors)
        monitor = Monitor(config)

        def post_fork(server, worker):
            monitor.initMetrics()
            app.route("/metrics")(lambda: (monitor.updateAllMetrics(), {"Content-Type": "text/plain; charset=utf-8"}))

        app = Flask("omnistat")
        options = {"bind": self.address, "workers": 1, "post_fork": post_fork}
        server = OmnistatServer(app, options)

        self._process = multiprocessing.Process(target=server.run)
        self._process.start()

        running = self.wait_for_server()
        if not running:
            self.stop()
        assert running is True, "Failed to start Omnistat monitor"

    def stop(self):
        self._process.terminate()

    def generate_config(self, enabled_collectors):
        config = configparser.ConfigParser()
        collectors = {"rocm_path": test.config.rocm_path}

        for collector in SUPPORTED_COLLECTORS:
            collectors[f"enable_{collector}"] = False

        for collector in enabled_collectors:
            collectors[f"enable_{collector}"] = True

        config["omnistat.collectors"] = collectors
        return config

    def wait_for_server(self):
        # Wait until endpoint is up and running, or timeout.
        start_time = time.time()
        while time.time() - start_time < self.timeout:
            try:
                response = requests.get(self.url)
                if response.status_code == 200:
                    return True
            except requests.RequestException:
                pass
            time.sleep(1)
        return False

    def get(self):
        try:
            response = requests.get(self.url)
            return text_string_to_metric_families(response.text)
        except requests.exceptions.RequestException as e:
            print(f"Error fetching metrics: {e}")


# Fixture to manage server lifecycle
@pytest.fixture(params=[x["collectors"] for x in COLLECTOR_CONFIGS])
def server(request):
    server = OmnistatTestServer(request.param)
    yield server
    server.stop()


class TestCollectors:
    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    def test_collector_metrics(self, server):
        response = server.get()
        available_metrics = {metric.name for metric in response}

        enabled_collectors = server.collectors
        expected_metrics = next((x["metrics"] for x in COLLECTOR_CONFIGS if x["collectors"] == enabled_collectors), [])
        assert len(expected_metrics) > 1, f"Failed to find expected metrics for {enabled_collectors}"

        for metric in expected_metrics:
            assert metric in available_metrics, f"Missing metric {metric} with {enabled_collectors}"
