# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2025 Advanced Micro Devices, Inc. All Rights Reserved.
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

"""NVIDIA DCGM-based data collector (Phase 1)

Scrapes metrics from a running dcgm-exporter instance and re-exports
a selected subset under Omnistat's /metrics endpoint with nvidia_* names.

This Phase 1 implementation:
- Keeps vendor-specific names (nvidia_*), no Grafana changes required.
- Does not alias or attempt ROCm parity names.
- Provides a minimal useful set of per-GPU metrics plus a node-level count.

Configuration (omnistat.config):
[omnistat.collectors]
enable_nvidia_dcgm = False
nvidia_dcgm_exporter_url = http://localhost:9400/metrics
enable_nvidia_uuid_label = False
"""

import configparser
import logging
import urllib.request
from typing import Dict, List, Optional, Tuple

from prometheus_client import Gauge
from prometheus_client.parser import text_string_to_metric_families

from omnistat.collector_base import Collector


def _http_get(url: str, timeout: float = 5.0) -> Optional[str]:
    """Fetch text from URL."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read()
            return data.decode("utf-8", errors="replace")
    except Exception as e:
        logging.error(f"DCGM scrape failed: {e}")
        return None


class NvidiaDCGMCollector(Collector):
    """Collector that scrapes dcgm-exporter and exports nvidia_* metrics."""

    def __init__(self, config: configparser.ConfigParser):
        logging.debug("Initializing NVIDIA DCGM data collector")
        self.__prefix = "nvidia_"
        self.__schema = 1.0

        # Config toggles
        coll_cfg = config["omnistat.collectors"]
        self.__enabled_uuid = coll_cfg.getboolean("enable_nvidia_uuid_label", False)
        self.__dcgm_url = coll_cfg.get("nvidia_dcgm_exporter_url", "http://localhost:9400/metrics")

        # Internal
        self.__gpu_ids: List[str] = []
        self.__gauges: Dict[str, Gauge] = {}
        self.__labels_by_metric: Dict[str, Tuple[str, ...]] = {}

        # Version info fields we may be able to populate from dcgm-exporter
        self.__version_info_defaults = {
            "vbios": "N/A",
            "type": "N/A",
        }

    # ---------------------------
    # scrape and parse utilities
    # ---------------------------
    def _scrape_dcgm(self) -> Optional[Dict[str, List[Tuple[Dict[str, str], float]]]]:
        """Scrape dcgm-exporter and return mapping of metric name -> list of (labels, value)."""
        text = _http_get(self.__dcgm_url)
        if text is None:
            return None

        families = {}
        try:
            for fam in text_string_to_metric_families(text):
                samples = []
                for s in fam.samples:
                    # s: (name, labels_dict, value)
                    samples.append((s.labels, float(s.value)))
                families[fam.name] = samples
        except Exception as e:
            logging.error(f"Failed to parse dcgm-exporter metrics: {e}")
            return None

        return families

    def _discover_gpu_ids(self, families: Dict[str, List[Tuple[Dict[str, str], float]]]) -> List[str]:
        """Discover GPU indices from any per-GPU metric (prefer SM_CLOCK)."""
        gpu_ids = set()

        def add_from_metric(name: str):
            for labels, _ in families.get(name, []):
                if "gpu" in labels:
                    gpu_ids.add(str(labels["gpu"]))

        # Prefer SM clock as it should exist on all GPUs
        add_from_metric("DCGM_FI_DEV_SM_CLOCK")
        if not gpu_ids:
            # Fallback to GPU util
            add_from_metric("DCGM_FI_DEV_GPU_UTIL")
        if not gpu_ids:
            # Fallback to memory temp
            add_from_metric("DCGM_FI_DEV_MEMORY_TEMP")

        return sorted(gpu_ids, key=lambda x: int(x)) if gpu_ids else []

    @staticmethod
    def _find_value_for_gpu(samples: List[Tuple[Dict[str, str], float]], gpu_id: str) -> Optional[Tuple[Dict[str, str], float]]:
        """Find the sample matching gpu=<gpu_id>."""
        for labels, value in samples:
            if labels.get("gpu") == gpu_id:
                return (labels, value)
        return None

    @staticmethod
    def _find_label_value(samples: List[Tuple[Dict[str, str], float]], gpu_id: str, label_key: str) -> Optional[str]:
        s = NvidiaDCGMCollector._find_value_for_gpu(samples, gpu_id)
        if s is None:
            return None
        labels, _ = s
        return labels.get(label_key)

    # ---------------------------
    # registration
    # ---------------------------
    def registerMetrics(self):
        """Register metrics of interest."""
        families = self._scrape_dcgm()
        if families is None:
            logging.error("NVIDIA DCGM: initial scrape failed; metrics not registered")
            return

        # Determine GPU list
        self.__gpu_ids = self._discover_gpu_ids(families)
        num_gpus = len(self.__gpu_ids)
        logging.info(f"NVIDIA DCGM: discovered {num_gpus} GPU(s)")

        # Node-level metric: nvidia_num_gpus
        g_name = self.__prefix + "num_gpus"
        self.__gauges[g_name] = Gauge(g_name, "# of NVIDIA GPUs available on host")
        self.__gauges[g_name].set(num_gpus)
        logging.info(f"--> [registered] {g_name} (gauge)")

        # Version info metric: labels card, driver_ver, vbios, type, schema [+ uuid optional]
        v_labels = ["card", "driver_ver", "vbios", "type", "schema"]
        if self.__enabled_uuid:
            v_labels.append("uuid")
        g_name = self.__prefix + "version_info"
        self.__labels_by_metric[g_name] = tuple(v_labels)
        self.__gauges[g_name] = Gauge(
            g_name,
            "GPU versioning information",
            labelnames=v_labels,
        )
        logging.info(f"--> [registered] {g_name} (gauge)")

        # Per-GPU metrics label sets
        base_labels = ["card"]
        temp_labels = ["card", "location"]
        if self.__enabled_uuid:
            base_labels.append("uuid")
            temp_labels.append("uuid")

        # Register core per-GPU metrics
        per_gpu_metrics = {
            "utilization_percentage": ("DCGM_FI_DEV_GPU_UTIL", base_labels),
            "vram_total_bytes": (None, base_labels),  # derived
            "vram_used_percentage": (None, base_labels),  # derived
            "sclk_clock_mhz": ("DCGM_FI_DEV_SM_CLOCK", base_labels),
            "mclk_clock_mhz": ("DCGM_FI_DEV_MEM_CLOCK", base_labels),
            "temperature_celsius": ("DCGM_FI_DEV_GPU_TEMP", temp_labels),  # location="gpu"
            "temperature_memory_celsius": ("DCGM_FI_DEV_MEMORY_TEMP", temp_labels),  # location="memory"
            "power_usage_watts": ("DCGM_FI_DEV_POWER_USAGE", base_labels),
        }

        for metric, (dcgm_name, labels) in per_gpu_metrics.items():
            g_name = self.__prefix + metric
            self.__labels_by_metric[g_name] = tuple(labels)
            self.__gauges[g_name] = Gauge(g_name, metric.replace("_", " "), labelnames=labels)
            logging.info(f"--> [registered] {g_name} (gauge)")

        # Populate initial values
        self._update_from_families(families, initial=True)

    # ---------------------------
    # updates
    # ---------------------------
    def updateMetrics(self):
        """Update registered metrics with latest values."""
        families = self._scrape_dcgm()
        if families is None:
            logging.error("NVIDIA DCGM: incremental scrape failed; metrics not updated")
            return

        # If GPU set changes (hotplug unlikely), refresh ids
        if not self.__gpu_ids:
            self.__gpu_ids = self._discover_gpu_ids(families)

        self._update_from_families(families, initial=False)

    # ---------------------------
    # helpers
    # ---------------------------
    def _update_from_families(self, families: Dict[str, List[Tuple[Dict[str, str], float]]], initial: bool):
        # Update version_info
        ver_samples = families.get("DCGM_FI_DRIVER_VERSION", [])
        vbios_samples = families.get("DCGM_FI_DEV_VBIOS_VERSION", [])
        brand_samples = families.get("DCGM_FI_DEV_BRAND", [])

        for gpu_id in self.__gpu_ids:
            card = gpu_id  # direct mapping
            uuid_val = None

            # Grab UUID if present and configured
            if self.__enabled_uuid:
                # Try UUID label from any per-GPU sample (prefer SM_CLOCK)
                sm = families.get("DCGM_FI_DEV_SM_CLOCK", [])
                s = self._find_value_for_gpu(sm, gpu_id)
                if s is not None:
                    uuid_val = s[0].get("UUID")

            driver_ver = self._find_label_value(ver_samples, gpu_id, "driver_version")
            vbios = self._find_label_value(vbios_samples, gpu_id, "vbios_version")
            dev_type = self._find_label_value(brand_samples, gpu_id, "brand")

            # Fallbacks
            if not vbios:
                vbios = self.__version_info_defaults["vbios"]
            if not dev_type:
                dev_type = self.__version_info_defaults["type"]

            v_labels = {
                "card": card,
                "driver_ver": driver_ver if driver_ver else "unknown",
                "vbios": vbios,
                "type": dev_type,
                "schema": str(self.__schema),
            }
            if self.__enabled_uuid and uuid_val:
                v_labels["uuid"] = uuid_val

            g_name = self.__prefix + "version_info"
            self.__gauges[g_name].labels(**v_labels).set(1)

        # Derived memory totals and usage
        fb_used = families.get("DCGM_FI_DEV_FB_USED", [])
        fb_free = families.get("DCGM_FI_DEV_FB_FREE", [])
        fb_reserved = families.get("DCGM_FI_DEV_FB_RESERVED", [])

        # Direct metrics
        metrics_map = {
            "utilization_percentage": families.get("DCGM_FI_DEV_GPU_UTIL", []),
            "sclk_clock_mhz": families.get("DCGM_FI_DEV_SM_CLOCK", []),
            "mclk_clock_mhz": families.get("DCGM_FI_DEV_MEM_CLOCK", []),
            "temperature_celsius": families.get("DCGM_FI_DEV_GPU_TEMP", []),
            "temperature_memory_celsius": families.get("DCGM_FI_DEV_MEMORY_TEMP", []),
            "power_usage_watts": families.get("DCGM_FI_DEV_POWER_USAGE", []),
        }

        for gpu_id in self.__gpu_ids:
            card = gpu_id
            base_labels = {"card": card}
            temp_gpu_labels = {"card": card, "location": "gpu"}
            temp_mem_labels = {"card": card, "location": "memory"}

            uuid_val = None
            if self.__enabled_uuid:
                sm = families.get("DCGM_FI_DEV_SM_CLOCK", [])
                s = self._find_value_for_gpu(sm, gpu_id)
                if s is not None:
                    uuid_val = s[0].get("UUID")
                if uuid_val:
                    base_labels["uuid"] = uuid_val
                    temp_gpu_labels["uuid"] = uuid_val
                    temp_mem_labels["uuid"] = uuid_val

            # Memory totals
            used_s = self._find_value_for_gpu(fb_used, gpu_id)
            free_s = self._find_value_for_gpu(fb_free, gpu_id)
            resv_s = self._find_value_for_gpu(fb_reserved, gpu_id)

            used_mib = used_s[1] if used_s else 0.0
            free_mib = free_s[1] if free_s else 0.0
            resv_mib = resv_s[1] if resv_s else 0.0

            total_mib = used_mib + free_mib + resv_mib
            total_bytes = total_mib * 1048576.0
            used_pct = 0.0
            if total_mib > 0:
                used_pct = round(100.0 * used_mib / total_mib, 4)

            # Set derived VRAM metrics
            self.__gauges[self.__prefix + "vram_total_bytes"].labels(**base_labels).set(total_bytes)
            self.__gauges[self.__prefix + "vram_used_percentage"].labels(**base_labels).set(used_pct)

            # Set direct metrics
            for metric_key, samples in metrics_map.items():
                s = self._find_value_for_gpu(samples, gpu_id)
                if s is None:
                    continue
                labels, value = s
                if metric_key == "temperature_celsius":
                    self.__gauges[self.__prefix + metric_key].labels(**temp_gpu_labels).set(value)
                elif metric_key == "temperature_memory_celsius":
                    self.__gauges[self.__prefix + metric_key].labels(**temp_mem_labels).set(value)
                else:
                    self.__gauges[self.__prefix + metric_key].labels(**base_labels).set(value)

        # Node-level GPU count can be refreshed if needed
        g_name = self.__prefix + "num_gpus"
        self.__gauges[g_name].set(len(self.__gpu_ids))

        if initial:
            logging.info("NVIDIA DCGM: initial registration and population complete")
        else:
            logging.debug("NVIDIA DCGM: metrics updated")
