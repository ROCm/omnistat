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

CONFIG_ROCM_SMI = """
[omnistat.collectors]
enable_rocm_smi = True
enable_amd_smi = False
enable_network = False
enable_ras_ecc = False
rocm_path = /opt/rocm-6.4.1/
"""

CONFIG_AMD_SMI = """
[omnistat.collectors]
enable_rocm_smi = False
enable_amd_smi = False
enable_network = False
enable_ras_ecc = False
"""

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
            self._process.terminate()
        assert running is True

    def __del__(self):
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


class TestSMI:
    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    @pytest.mark.parametrize("smi_config", [CONFIG_ROCM_SMI, CONFIG_AMD_SMI])
    def test_smi_init(self, smi_config):
        monitor = TestMonitor(smi_config)
        response = monitor.get()

        metric_names = {metric.name for metric in response}
        for metric in SMI_METRICS:
            assert metric in metric_names
