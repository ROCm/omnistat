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

COLLECTORS = [
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

CONFIG_TEMPLATE = """
[omnistat.collectors]
enable_rocm_smi = {rocm_smi}
enable_amd_smi = {amd_smi}
enable_ras_ecc = {ras_ecc}
enable_network = {network}
enable_cu_occupancy = {cu_occupancy}
rocm_path = {rocm_path}
"""


def generate_config(enabled_collectors):
    values = {
        "rocm_smi": False,
        "amd_smi": False,
        "ras_ecc": False,
        "network": False,
        "cu_occupancy": False,
        "rocm_path": test.config.rocm_path,
    }

    for collector in enabled_collectors:
        values[collector] = True

    config = CONFIG_TEMPLATE.format(**values)
    return config


class TestMonitor:
    __test__ = False

    def __init__(self, config_string):
        self.address = f"localhost:{test.config.port}"
        self.url = f"http://{self.address}/metrics"
        self.timeout = 3.0

        config = configparser.ConfigParser()
        config.read_string(config_string)
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

    def __del__(self):
        self.stop()

    def stop(self):
        self._process.terminate()

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


class TestCollectors:
    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    @pytest.mark.parametrize(
        "enabled_collectors, expected_metrics", [(x["collectors"], x["metrics"]) for x in COLLECTORS]
    )
    def test_collector_init(self, enabled_collectors, expected_metrics):
        config = generate_config(enabled_collectors)
        monitor = TestMonitor(config)

        response = monitor.get()
        available_metrics = {metric.name for metric in response}

        for metric in expected_metrics:
            assert metric in available_metrics, f"Missing metric {metric} with {enabled_collectors}"

        monitor.stop()
