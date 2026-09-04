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
import test.workloads as workloads
from omnistat.monitor import Monitor
from omnistat.node_monitoring import OmnistatServer
from omnistat.utils import runShellCommand

requires_counters = pytest.mark.skipif(
    not test.config.rocm_host or "ROCP_TOOL_LIBRARIES" not in os.environ,
    reason="requires ROCm and ROCP_TOOL_LIBRARIES",
)

# fmt: off
SMI_METRICS = [
    {"name":"rocm_num_gpus",                                "validate":">=1",                "labels":None},
    {"name":"rocm_version_info",                            "validate":"==1.0",              "labels":["card","driver_ver","serial","type"]},
    {"name":"rocm_temperature_celsius",                     "validate":">=10",               "labels":["card","location"]},
    {"name":"rocm_temperature_memory_celsius",              "validate":">=10",               "labels":["card","location"]},
    {"name":"rocm_average_socket_power_watts",              "validate":">=10",               "labels":["card"]},
    {"name":"rocm_sclk_clock_mhz",                          "validate":">=90" ,              "labels":["card"],         "hardware":["MI2","MI3"]},
    {"name":"rocm_mclk_clock_mhz",                          "validate":">=90",               "labels":["card"]},
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
    {"name": "rocm_num_compute_units",                      "validate": ">=32",              "labels": ["card"]},
    {"name": "rocm_compute_unit_occupancy",                 "validate": ">=0",               "labels": ["card"]},
]

EVENTS_METRICS = [
    {"name": "rocm_throttle_events",                        "validate": ">=0",               "labels": ["card"]},
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

ROCPROFILER_METRICS = [
    {"name": "omnistat_hardware_counter",                    "validate": ">=0",               "labels": ["source", "card", "name"]},
]

# Network metrics are hardware-dependent (specific device classes such as
# "cxi" or "ionic" only appear on matching NICs), so assertions stay generic:
# any host with a non-loopback interface exposes rx/tx byte totals.
NETWORK_METRICS = [
    {"name": "omnistat_network_rx_bytes",                    "validate": ">=0",               "labels": ["device_class", "interface"]},
    {"name": "omnistat_network_tx_bytes",                    "validate": ">=0",               "labels": ["device_class", "interface"]},
]

# fmt: on


def get_gpu_asic_info(device=0):
    """Return GPU market name and graphics version from `amd-smi static --asic --gpu <device>`."""
    cmd = ["amd-smi", "static", "--asic", "--gpu", str(device)]
    result = runShellCommand(cmd, capture_output=True, text=True, timeout=5)
    if not result or result.returncode != 0:
        logging.error(f"Failed to run amd-smi for device {device}")
        return "", ""

    fields = {"MARKET_NAME": "", "TARGET_GRAPHICS_VERSION": ""}
    for line in result.stdout.splitlines():
        for field in fields:
            if f"{field}:" in line:
                parts = line.split(f"{field}:")
                if len(parts) == 2:
                    fields[field] = parts[1].strip()

    return fields["MARKET_NAME"], fields["TARGET_GRAPHICS_VERSION"]


gpu_type, gpu_arch = get_gpu_asic_info()

# Filter SMI_METRICS based on hardware allowlist
SMI_METRICS = [x for x in SMI_METRICS if ("hardware" not in x or any(hw in gpu_type for hw in x["hardware"]))]

# Optional energy accumulator which is not available on all hardware:
#  - rocm_smi: unsupported on MI3XX, RDNA4
#  - amd_smi:  unsupported on RDNA4
energy_supported_rocmsmi = "MI3" not in gpu_type and "Radeon" not in gpu_type
energy_supported_amdsmi = "Radeon" not in gpu_type

# Cache hostname for skip checks
try:
    full_hostname = socket.getfqdn()
except:
    full_hostname = "unknown"
print(f"test execution hostname: {full_hostname}\n")

COLLECTOR_CONFIGS = [
    {
        "collectors": ["rocm_smi", "power_cap"],
        "metrics": SMI_METRICS
        + ([{"name": "rocm_energy_joules", "validate": ">10", "labels": ["card"]}] if energy_supported_rocmsmi else [])
        + [
            {"name": "rocm_power_cap_watts", "validate": ">0", "labels": ["card"]},
        ],
    },
    {
        "collectors": ["amd_smi"],
        "metrics": (
            SMI_METRICS
            + [
                {"name": "rocm_energy_joules", "validate": ">10", "labels": ["card"]},
            ]
            if energy_supported_amdsmi
            else []
        ),
    },
    {
        "collectors": ["rocm_smi", "ras_ecc"],
        # RAS/ECC not supported on consumer GPUs (RDNA4)
        "metrics": (
            []
            if "Radeon" in gpu_type
            else [
                x
                for x in RAS_METRICS
                if "_deferred_count" not in x["name"]
                and ("hardware" not in x or any(hw in gpu_type for hw in x["hardware"]))
                and ("skip" not in x or not any(pattern in full_hostname for pattern in x["skip"]))
            ]
        ),
    },
    {
        "collectors": ["amd_smi", "ras_ecc"],
        "metrics": (
            []
            if "Radeon" in gpu_type
            else [
                x
                for x in RAS_METRICS
                if ("hardware" not in x or any(hw in gpu_type for hw in x["hardware"]))
                and ("skip" not in x or not any(pattern in full_hostname for pattern in x["skip"]))
            ]
        ),
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
        "collectors": ["events"],
        "metrics": EVENTS_METRICS,
    },
    {
        "collectors": ["host_metrics", "omnistat.collectors.host::enable_proc_io_stats"],
        "metrics": HOST_METRICS,
    },
    {
        "collectors": ["network"],
        "metrics": NETWORK_METRICS,
    },
    {
        "collectors": ["rocprofiler"],
        "metrics": ROCPROFILER_METRICS,
        "config_sections": {
            "omnistat.collectors.rocprofiler": {"profile": "default"},
            "omnistat.collectors.rocprofiler.default": {
                "sampling_mode": "constant",
                "counters": '["GRBM_COUNT", "GRBM_GUI_ACTIVE"]',
            },
        },
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


# Hardware counters unsupported on RDNA4
if gpu_arch.startswith("gfx12"):
    COLLECTOR_CONFIGS = [x for x in COLLECTOR_CONFIGS if "rocprofiler" not in x["collectors"]]

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
    def __init__(self, collectors, config_sections=None):
        self.address = f"localhost:{test.config.port}"
        self.url = f"http://{self.address}/metrics"
        self.timeout = 5.0
        self.collectors = collectors

        config = self.generate_config(self.collectors, config_sections=config_sections)
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

    def generate_config(self, enabled_collectors, config_sections=None):
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

        if config_sections:
            for section, options in config_sections.items():
                config[section] = options

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
@pytest.fixture(scope="class")
def server(request):
    collectors, config_sections = request.param
    server = OmnistatTestServer(collectors, config_sections=config_sections)
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
            config_sections = config.get("config_sections")
            for metric in config["metrics"]:
                argvalues.append(((config["collectors"], config_sections), metric))
                collector_config = config["collectors"].copy()
                if len(collector_config) > 1:
                    if "::" in collector_config[1]:
                        collector_config[1] = collector_config[1].split("::", 1)[1]
                ids.append(f"{'+'.join(collector_config)}::{metric['name']}")
        # Parametrize server (indirect via fixture) and metric together without cross-product
        metafunc.parametrize(("server", "desired_metric"), argvalues, ids=ids, scope="class", indirect=["server"])


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


class TestHardwareCounters:
    @requires_counters
    @pytest.mark.skipif("Radeon" in gpu_type, reason="hardware counters not supported on RDNA4")
    def test_counters_with_workload(self):
        config_sections = {
            "omnistat.collectors.rocprofiler": {"profile": "default"},
            "omnistat.collectors.rocprofiler.default": {
                "sampling_mode": "constant",
                "counters": '["GRBM_COUNT", "GRBM_GUI_ACTIVE", "SQ_INSTS_VALU"]',
            },
        }
        server = OmnistatTestServer(["rocprofiler"], config_sections=config_sections)

        try:
            # Run GPU workload with the tool libraries from the environment, which
            # must include the counter enablement library.
            result = workloads.run("vector_add", [1000000])
            assert result.returncode == 0, f"vector_add failed: {result.stderr}"

            # Scrape metrics, keyed by (card, counter_name)
            metrics = {}
            for metric in server.get():
                for sample in metric.samples:
                    card = sample.labels.get("card", "")
                    name = sample.labels.get("name", "")
                    if metric.name == "omnistat_hardware_counter":
                        metrics.setdefault(card, {})[name] = sample.value
        finally:
            server.stop()

        # At least one GPU should have non-zero counters from the workload
        assert len(metrics) > 0, "No hardware counter metrics found"
        counters = ["GRBM_COUNT", "GRBM_GUI_ACTIVE", "SQ_INSTS_VALU"]
        assert any(
            all(metrics[card].get(c, 0) > 0 for c in counters) for card in metrics
        ), f"No GPU had all counters > 0: {metrics}"


class TestHardwareCounterConfigValidation:
    """Verify rocprofiler_sdk config validation catches bad configs with sys.exit(4)."""

    def _make_config(self, profile_opts=None, rocprofiler_opts=None):
        config = configparser.ConfigParser()
        config["omnistat.collectors"] = {"rocm_path": test.config.rocm_path}
        if rocprofiler_opts:
            config["omnistat.collectors.rocprofiler"] = rocprofiler_opts
        if profile_opts is not None:
            config["omnistat.collectors.rocprofiler.default"] = profile_opts
        return config

    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    def test_bad_json_counters(self):
        from omnistat.collector_rocprofiler_sdk import rocprofiler_sdk

        config = self._make_config(profile_opts={"counters": "not valid json"})
        with pytest.raises(SystemExit) as exc_info:
            rocprofiler_sdk(config=config)
        assert exc_info.value.code == 4

    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    def test_missing_counters(self):
        from omnistat.collector_rocprofiler_sdk import rocprofiler_sdk

        config = self._make_config(profile_opts={"sampling_mode": "constant"})
        with pytest.raises(SystemExit) as exc_info:
            rocprofiler_sdk(config=config)
        assert exc_info.value.code == 4

    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    def test_non_list_counters(self):
        from omnistat.collector_rocprofiler_sdk import rocprofiler_sdk

        config = self._make_config(profile_opts={"counters": '"just_a_string"'})
        with pytest.raises(SystemExit) as exc_info:
            rocprofiler_sdk(config=config)
        assert exc_info.value.code == 4

    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    def test_invalid_sampling_mode(self):
        from omnistat.collector_rocprofiler_sdk import rocprofiler_sdk

        config = self._make_config(profile_opts={"sampling_mode": "bogus", "counters": '["GRBM_COUNT"]'})
        with pytest.raises(SystemExit) as exc_info:
            rocprofiler_sdk(config=config)
        assert exc_info.value.code == 4

    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    def test_constant_mode_multiple_counter_sets(self):
        from omnistat.collector_rocprofiler_sdk import rocprofiler_sdk

        config = self._make_config(
            profile_opts={"sampling_mode": "constant", "counters": '[["GRBM_COUNT"], ["GRBM_GUI_ACTIVE"]]'}
        )
        with pytest.raises(SystemExit) as exc_info:
            rocprofiler_sdk(config=config)
        assert exc_info.value.code == 4

    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    def test_deprecated_metrics_option(self):
        from omnistat.collector_rocprofiler_sdk import rocprofiler_sdk

        config = self._make_config(
            rocprofiler_opts={"metrics": '["GRBM_COUNT"]'},
            profile_opts={"sampling_mode": "constant"},
        )
        with pytest.raises(SystemExit) as exc_info:
            rocprofiler_sdk(config=config)
        assert exc_info.value.code == 4

    @pytest.mark.skipif(not test.config.rocm_host, reason="requires ROCm")
    def test_gpu_id_mode_single_counter_set(self):
        from omnistat.collector_rocprofiler_sdk import rocprofiler_sdk

        config = self._make_config(profile_opts={"sampling_mode": "gpu-id", "counters": '[["GRBM_COUNT"]]'})
        with pytest.raises(SystemExit) as exc_info:
            rocprofiler_sdk(config=config)
        assert exc_info.value.code == 4


class TestHostUserModeIO:
    """Verify per-process I/O metrics in user mode by spawning a subprocess that does I/O."""

    def test_user_mode_proc_io(self):
        import subprocess
        import sys

        config_sections = {
            "omnistat.collectors.host": {
                "cpu_load_sampling_interval": "0.02",
                "enable_proc_io_stats": "True",
                "proc_io_cmds_exclude": "flux-, systemd",
            },
            "omnistat.internal": {
                "mode": "user",
                "interval_secs": "5",
                "push_interval_secs": "Unknown",
            },
        }
        server = OmnistatTestServer(
            ["host_metrics", "omnistat.collectors.host::enable_proc_io_stats"],
            config_sections=config_sections,
        )

        # Spawn a long-running subprocess that does I/O — its PID won't be in the init-time filter.
        # When running as root (e.g. in CI containers), the collector filters out root-owned processes.
        # In that case, run the subprocess as "ubuntu" if available so it passes the root-process filter.
        io_script = "import time\nf=open('/dev/null','w')\nwhile True:\n f.write('x'*1024)\n time.sleep(0.01)"
        use_su = False
        if os.geteuid() == 0:
            try:
                import pwd

                pwd.getpwnam("ubuntu")
                use_su = True
            except KeyError:
                pass

        if use_su:
            io_proc = subprocess.Popen(["su", "-", "ubuntu", "-c", f'{sys.executable} -c "{io_script}"'])
        else:
            io_proc = subprocess.Popen([sys.executable, "-c", io_script])

        try:
            time.sleep(0.5)
            metrics = {}
            for metric in server.get():
                for sample in metric.samples:
                    metrics.setdefault(metric.name, []).append(sample)

            assert (
                "omnistat_host_io_read_total_bytes" in metrics
            ), f"Missing io_read_total_bytes, got: {list(metrics.keys())}"
            assert (
                "omnistat_host_io_write_total_bytes" in metrics
            ), f"Missing io_write_total_bytes, got: {list(metrics.keys())}"

            # Verify expected labels and at least one process with non-zero I/O
            for metric_name in ("omnistat_host_io_read_total_bytes", "omnistat_host_io_write_total_bytes"):
                samples = metrics[metric_name]
                for sample in samples:
                    assert "pid" in sample.labels, f"Missing 'pid' label for {metric_name}"
                    assert "cmd" in sample.labels, f"Missing 'cmd' label for {metric_name}"
                values = [s.value for s in samples]
                assert any(v > 0 for v in values), f"Expected non-zero {metric_name}, got: {values}"
        finally:
            io_proc.terminate()
            io_proc.wait()
            server.stop()
