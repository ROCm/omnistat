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
import logging
import multiprocessing
import operator
import os
import re
import socket
import time

import pytest
import requests
from flask import Flask
from prometheus_client.parser import text_string_to_metric_families

import test.config
from omnistat.monitor import Monitor
from omnistat.node_monitoring import OmnistatServer
from omnistat.utils import runShellCommand

# fmt: off
SMI_METRICS = [
    {"name":"rocm_num_gpus",                                "validate":">=1",                "labels":None},
    {"name":"rocm_version_info",                            "validate":"==1.0",              "labels":["card","driver_ver","type"]},
    {"name":"rocm_temperature_celsius",                     "validate":">=10",               "labels":["card","location"]},
    {"name":"rocm_temperature_memory_celsius",              "validate":">=10",               "labels":["card","location"]},
    {"name":"rocm_average_socket_power_watts",              "validate":">=10",               "labels":["card"]},
    {"name":"rocm_sclk_clock_mhz",                          "validate":">=90" ,              "labels":["card"]},
    {"name":"rocm_mclk_clock_mhz",                          "validate":">=100",              "labels":["card"]},
    {"name":"rocm_vram_total_bytes",                        "validate":">1073741824",        "labels":["card"]},
    {"name":"rocm_vram_used_percentage",                    "validate":">=0",                "labels":["card"]},
    {"name":"rocm_vram_busy_percentage",                    "validate":">=0.0",              "labels":["card"]},
    {"name":"rocm_utilization_percentage",                  "validate":">=0.0",              "labels":["card"]},
]

RAS_METRICS = [
    {"name": "rocm_ras_umc_correctable_count",              "validate": ">=0",               "labels": ["card"],        "skip":["borg","frontier","tuolumne"]},
    {"name": "rocm_ras_sdma_correctable_count",             "validate": ">=0",               "labels": ["card"]},
    {"name": "rocm_ras_gfx_correctable_count",              "validate": ">=0",               "labels": ["card"]},
    {"name": "rocm_ras_mmhub_correctable_count",            "validate": ">=0",               "labels": ["card"]},
    {"name": "rocm_ras_pcie_bif_correctable_count",         "validate": ">=0",               "labels": ["card"],        "hardware":["MI210"]},
    {"name": "rocm_ras_hdp_correctable_count",              "validate": ">=0",               "labels": ["card"],        "hardware":["MI210"]},
    {"name": "rocm_ras_umc_uncorrectable_count",            "validate": ">=0",               "labels": ["card"],        "skip":["borg","frontier","tuolumne"]},
    {"name": "rocm_ras_sdma_uncorrectable_count",           "validate": ">=0",               "labels": ["card"]},
    {"name": "rocm_ras_gfx_uncorrectable_count",            "validate": ">=0",               "labels": ["card"]},
    {"name": "rocm_ras_mmhub_uncorrectable_count",          "validate": ">=0",               "labels": ["card"]},
    {"name": "rocm_ras_pcie_bif_uncorrectable_count",       "validate": ">=0",               "labels": ["card"],        "hardware":["MI210"]},
    {"name": "rocm_ras_hdp_uncorrectable_count",            "validate": ">=0",               "labels": ["card"],        "hardware":["MI210"]},
    {"name": "rocm_ras_umc_deferred_count",                 "validate": ">=0",               "labels": ["card"],        "skip":["borg","frontier","tuolumne"]},
    {"name": "rocm_ras_sdma_deferred_count",                "validate": ">=0",               "labels": ["card"]},
    {"name": "rocm_ras_gfx_deferred_count",                 "validate": ">=0",               "labels": ["card"]},
    {"name": "rocm_ras_mmhub_deferred_count",               "validate": ">=0",               "labels": ["card"]},
    {"name": "rocm_ras_pcie_bif_deferred_count",            "validate": ">=0",               "labels": ["card"],        "hardware":["MI210"]},
    {"name": "rocm_ras_hdp_deferred_count",                 "validate": ">=0",               "labels": ["card"],        "hardware":["MI210"]},
]

OCCUPANCY_METRICS = [
    {"name": "rocm_num_compute_units",                      "validate": ">=100",             "labels": ["card"]},
    {"name": "rocm_compute_unit_occupancy",                 "validate": ">=0",               "labels": ["card"]},
]

cores = os.cpu_count()

HOST_METRICS = [
    {"name": "omnistat_host_mem_total_bytes",                "validate": ">=1000000",         "labels": None},
    {"name": "omnistat_host_mem_available_bytes",            "validate": ">=10000",           "labels": None},
    {"name": "omnistat_host_mem_free_bytes",                 "validate": ">=10000",           "labels": None},
    {"name": "omnistat_host_io_read_total_bytes",            "validate": ">0",                "labels": ["pid","cmd"]},
    {"name": "omnistat_host_io_write_total_bytes",           "validate": ">=0",               "labels": ["pid","cmd"]},
    {"name": "omnistat_host_io_read_local_total_bytes",      "validate": ">0",                "labels": None},
    {"name": "omnistat_host_io_write_local_total_bytes",     "validate": ">=0",               "labels": None},
    {"name": "omnistat_host_cpu_aggregate_core_utilization", "validate": ">=0",               "labels": None},
    {"name": "omnistat_host_cpu_load1",                      "validate": ">=0",               "labels": None},
    {"name": "omnistat_host_cpu_num_physical_cores",         "validate": ">=%i" % (cores/2),  "labels": None},
    {"name": "omnistat_host_cpu_num_logical_cores",          "validate": "==%i" % cores,      "labels": None},
    {"name": "omnistat_host_boot_time_seconds",              "validate": ">1000",             "labels": None},
]

# fmt: on


def get_gpu_type(device=0):
    """Return GPU Card Series by running `rocm-smi --showproductname -d <device>`."""
    cmd = ["rocm-smi", "--showproductname", "-d", str(device)]
    result = runShellCommand(cmd, capture_output=True, text=True, timeout=5)
    if not result or result.returncode != 0:
        logging.error(f"Failed to run rocm-smi for device {device}")
        return ""
    for line in result.stdout.splitlines():
        if "Card Series:" in line:
            parts = line.split("Card Series:")
            if len(parts) == 2:
                return parts[1].strip()
    logging.warning("Card Series not found in rocm-smi output")
    return ""


gpu_type = get_gpu_type()

# Cache hostname for skip checks
try:
    full_hostname = socket.getfqdn()
except:
    full_hostname = "unknown"
print(f"test execution hostname: {full_hostname}\n")

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
        "metrics": [
            x
            for x in RAS_METRICS
            if "_deferred_count" not in x["name"]
            and ("hardware" not in x or any(hw in gpu_type for hw in x["hardware"]))
            and ("skip" not in x or not any(pattern in full_hostname for pattern in x["skip"]))
        ],
    },
    {
        "collectors": ["amd_smi", "ras_ecc"],
        "metrics": [
            x
            for x in RAS_METRICS
            if ("hardware" not in x or any(hw in gpu_type for hw in x["hardware"]))
            and ("skip" not in x or not any(pattern in full_hostname for pattern in x["skip"]))
        ],
    },
    {
        "collectors": ["rocm_smi", "cu_occupancy"],
        "metrics": OCCUPANCY_METRICS,
    },
    {
        "collectors": ["amd_smi", "cu_occupancy"],
        "metrics": OCCUPANCY_METRICS,
    },
    {
        "collectors": ["host_metrics", "omnistat.collectors.host::enable_proc_io_stats"],
        "metrics": HOST_METRICS,
    },
    # general info metrics expected to be present regardless of collector config
    {
        "collectors": ["rocm_smi"],
        "metrics": [
            {"name": "omnistat_info", "validate": "==1.0", "labels": ["version", "mode", "schema"]},
            {"name": "omnistat_perf_runtime_seconds", "validate": ">0.0", "labels": ["collector"]},
        ],
    },
]


SUPPORTED_COLLECTORS = set()
for config in COLLECTOR_CONFIGS:
    for collector in config["collectors"]:
        SUPPORTED_COLLECTORS.add(collector)

ops = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}


class OmnistatTestServer:
    def __init__(self, collectors):
        self.address = f"localhost:{test.config.port}"
        self.url = f"http://{self.address}/metrics"
        self.timeout = 5.0
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
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=3)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=1)
        time.sleep(1.0)

    def generate_config(self, enabled_collectors):
        config = configparser.ConfigParser()
        collectors = {"rocm_path": test.config.rocm_path}

        for collector in SUPPORTED_COLLECTORS:
            collectors[f"enable_{collector}"] = False

        for collector in enabled_collectors:
            if "::" in collector:
                collector_name, option = collector.split("::", 1)
                config[collector_name] = {option: "True"}
            else:
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
@pytest.fixture(scope="session")
def server(request):
    server = OmnistatTestServer(request.param)
    yield server
    server.stop()


@pytest.fixture
def available_metrics(server):
    # Cache metrics on the server instance to avoid multiple GET calls
    if not hasattr(server, "_metrics_cache"):
        # Convert to list so generator can be reused across tests

        # server._metrics_cache = list(server.get())

        response = server.get()

        metrics = {}
        labels = {}
        for metric in response:
            if metric.samples:
                metrics[metric.name] = metric.samples[0].value
                labels[metric.name] = metric.samples[0].labels or {}
            else:
                metrics[metric.name] = None
                labels[metric.name] = {}
        server._metrics_cache = {"metrics": metrics, "labels": labels}
    return server._metrics_cache


def pytest_generate_tests(metafunc):
    # Parametrize (server, metric) pairs directly to avoid cross-product
    if "desired_metric" in metafunc.fixturenames and "server" in metafunc.fixturenames:
        argvalues = []
        ids = []
        for config in COLLECTOR_CONFIGS:
            for metric in config["metrics"]:
                argvalues.append((config["collectors"], metric))
                collector_config = config["collectors"].copy()
                if len(collector_config) > 1:
                    if "::" in collector_config[1]:
                        collector_config[1] = collector_config[1].split("::", 1)[1]
                ids.append(f"{'+'.join(collector_config)}::{metric['name']}")
        # Parametrize server (indirect via fixture) and metric together without cross-product
        metafunc.parametrize(("server", "desired_metric"), argvalues, ids=ids, scope="session", indirect=["server"])


class TestCollectors:
    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    def test_collector_metrics(self, server, available_metrics, desired_metric):
        # Ensure the fixture supplied metrics are fetched
        assert available_metrics is not None, "Failed to fetch metrics from server"
        assert (
            desired_metric["name"] in available_metrics["metrics"]
        ), f"Missing metric {desired_metric['name']} with {server.collectors}"

    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    def test_collector_labels(self, server, available_metrics, desired_metric):
        assert available_metrics is not None, "Failed to fetch metrics from server"

        name = desired_metric["name"]
        available_labels = available_metrics["labels"][name]
        if available_labels:
            for label in desired_metric["labels"]:
                assert label in available_labels, f"Missing label '{label}' for '{name}'"

    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    def test_collector_values(self, server, available_metrics, desired_metric):
        assert available_metrics is not None, "Failed to fetch metrics from server"
        validate_expr = desired_metric["validate"]

        if validate_expr.upper() == "N/A":
            return

        name = desired_metric["name"]
        value = available_metrics["metrics"][name]

        # regex: capture operator and integer/float
        match = re.match(r"(>=|<=|==|!=|>|<)\s*([0-9]*\.?[0-9]+)", validate_expr)
        if not match:
            raise ValueError(f"Invalid validate string: {validate_expr}")

        op_str, num_str = match.groups()
        threshold = float(num_str)

        assert ops[op_str](value, threshold), f"Invalid value for {name} (expecting {validate_expr}, received {value})"
