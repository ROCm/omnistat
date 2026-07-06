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

"""Static configuration for omnistat-inspect.

Self-contained: only stdlib ``typing`` is imported, so any module can depend on
this one without risking an import cycle.
"""

from __future__ import annotations

from typing import NamedTuple

# Tunable defaults
DEFAULT_CV_THRESHOLD = 0.05
SCAN_STEP = 60.0
SCAN_DAYS = 365
VM_MAX_POINTS = 90000

# Iteration-detection defaults.
DEFAULT_ITER_METRIC = "rocm_utilization_percentage"
DEFAULT_ITER_LOW_THRESHOLD = 20.0
DEFAULT_ITER_HIGH_THRESHOLD = 70.0
DEFAULT_ITER_MIN_IDLE_SECONDS = 30.0
DEFAULT_ITER_MIN_ITERATION_SECONDS = 60.0

# Single percentile set used for every distribution summary — pooled gauge
# samples in ``stats.gauges[]`` and per-key means in every ``stats.variance.by_*``
# section. Box-plot + tails, no p1/p99 (noisy at small n, rarely cited at large n).
PERCENTILES: tuple[float, ...] = (5, 25, 50, 75, 95)

# Variance entries with ``n <= INLINE_ALL_THRESHOLD`` emit ``all`` (every
# key explicitly) instead of ``percentiles``. Below this size, percentiles
# essentially collapse to min/max and the reader can scan every key directly.
INLINE_ALL_THRESHOLD = 16

# Kernel-tracing report: number of top kernels (by total GPU time) carried into
# ``stats.kernels.top`` and the variance/drift drill-downs.
TOP_KERNELS_LIMIT = 10

# Kernel-tracing metric names (cumulative counters emitted by the
# ``enable_kernel_trace`` collector). These are not per-node gauges/counters, so
# they have no ``Metric`` rows in GAUGE_LIST / COUNTER_LIST.
KERNEL_DURATION_METRIC = "omnistat_kernel_total_duration_ns"
KERNEL_COUNT_METRIC = "omnistat_kernel_dispatch_count"
KERNEL_DROPPED_METRIC = "omnistat_kernel_dropped_dispatches"


class Metric(NamedTuple):
    """One gauge or counter metric tracked by the report.

    ``source`` is the human-readable grouping shown in renderings; ``unit`` is
    the native unit of the emitted value — see ``GAUGE_LIST`` / ``COUNTER_LIST``
    below.
    """

    source: str
    label: str
    name: str
    unit: str


# ---------------------------------------------------------------------------
# Gauge and counter metrics tracked by the report.
# ---------------------------------------------------------------------------

# Each entry's ``unit`` field declares the unit of the emitted value, using the
# raw metric's *native* unit (bytes stays bytes, kilobytes stays KiB, joules
# stays joules — no upscaling to a finer common base). Renderers pick the most
# readable display unit (GiB, kWh, GB/s, …) from the declared base, see
# SKILL.md.
GAUGE_LIST: tuple[Metric, ...] = (
    Metric("GPU", "Utilization", "rocm_utilization_percentage", "%"),
    Metric("GPU", "Memory utilization", "rocm_vram_used_percentage", "%"),
    Metric("GPU", "Power", "rocm_average_socket_power_watts", "W"),
    Metric("GPU", "Frequency", "rocm_sclk_clock_mhz", "MHz"),
    Metric("GPU", "Temperature", "rocm_temperature_celsius", "°C"),
    Metric("GPU", "HBM Temperature", "rocm_temperature_memory_celsius", "°C"),
    Metric("GPU", "xGMI read rate", "rocm_xgmi_total_read_kilobytes", "KiB/s"),
    Metric("GPU", "xGMI write rate", "rocm_xgmi_total_write_kilobytes", "KiB/s"),
    Metric("Host", "CPU utilization", "omnistat_host_cpu_aggregate_core_utilization", "%"),
    Metric("Host", "Memory available", "omnistat_host_mem_available_bytes", "bytes"),
    Metric("Network", "RX rate", "omnistat_network_rx_bytes", "B/s"),
    Metric("Network", "TX rate", "omnistat_network_tx_bytes", "B/s"),
    Metric("Vendor", "Total power", "omnistat_vendor_power_watts", "W"),
    Metric("Vendor", "Accelerator power", "omnistat_vendor_accel_power_watts", "W"),
    Metric("Vendor", "CPU power", "omnistat_vendor_cpu_power_watts", "W"),
    Metric("Vendor", "Memory power", "omnistat_vendor_memory_power_watts", "W"),
)

# Counter totals. Energy in joules, byte counts in bytes — renderers convert
# to kWh / GiB / TiB based on magnitude.
COUNTER_LIST: tuple[Metric, ...] = (
    Metric("GPU", "xGMI read", "rocm_xgmi_total_read_kilobytes", "KiB"),
    Metric("GPU", "xGMI write", "rocm_xgmi_total_write_kilobytes", "KiB"),
    Metric("IO", "Local disk read", "omnistat_host_io_read_local_total_bytes", "bytes"),
    Metric("IO", "Local disk write", "omnistat_host_io_write_local_total_bytes", "bytes"),
    Metric("IO", "Proc IO read", "omnistat_host_io_read_total_bytes", "bytes"),
    Metric("IO", "Proc IO write", "omnistat_host_io_write_total_bytes", "bytes"),
    Metric("Network", "RX", "omnistat_network_rx_bytes", "bytes"),
    Metric("Network", "TX", "omnistat_network_tx_bytes", "bytes"),
    Metric("Vendor", "Total energy", "omnistat_vendor_energy_joules", "J"),
    Metric("Vendor", "Accelerator energy", "omnistat_vendor_accel_energy_joules", "J"),
    Metric("Vendor", "CPU energy", "omnistat_vendor_cpu_energy_joules", "J"),
    Metric("Vendor", "Memory energy", "omnistat_vendor_memory_energy_joules", "J"),
)


# Counter metrics: treated as monotonically-increasing cumulative counters.
COUNTER_METRICS: frozenset[str] = frozenset(
    {
        "omnistat_host_io_read_local_total_bytes",
        "omnistat_host_io_write_local_total_bytes",
        "omnistat_host_io_read_total_bytes",
        "omnistat_host_io_write_total_bytes",
        "omnistat_network_tx_bytes",
        "omnistat_network_rx_bytes",
        "omnistat_vendor_energy_joules",
        "omnistat_vendor_accel_energy_joules",
        "omnistat_vendor_cpu_energy_joules",
        "omnistat_vendor_memory_energy_joules",
        "rocm_xgmi_total_read_kilobytes",
        "rocm_xgmi_total_write_kilobytes",
    }
)

# Metrics for which series that are exactly 0 throughout the job are filtered
# out of EVERY statistical computation (gauge mean/CV, per-node means,
# per-card-slot means, per-(instance,card) means, population counts). These
# series represent sensors that are unpopulated by design — e.g. MI250X
# odd-numbered cards' rocm_average_socket_power_watts — and treating them as
# data points distorts every aggregate. Invariant: any code that aggregates
# one of these metrics must first apply ``compute.filter_zero_series``.
DROP_ZERO_SERIES_METRICS: frozenset[str] = frozenset(
    {
        "rocm_average_socket_power_watts",
    }
)

# GPU-card-level variance metrics. Used by both the ``by_gpu_id`` (per-card-slot)
# and ``by_gpu`` (per (instance, card) pair) variance sections.
GPU_VARIANCE_METRICS: tuple[str, ...] = (
    "rocm_temperature_celsius",
    "rocm_temperature_memory_celsius",
    "rocm_utilization_percentage",
    "rocm_average_socket_power_watts",
)

# Reverse index for variance code that needs source/label/unit for a metric
# name. Maps name -> the ``Metric`` row (carrying ``.source``/``.label``/``.unit``).
GAUGE_BY_METRIC: dict[str, Metric] = {r.name: r for r in GAUGE_LIST}
