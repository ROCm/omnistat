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

"""Network monitoring

Implements a prometheus info metric to track network traffic data for interfaces
exposed under /sys/class/net and /sys/class/cxi.
"""

import configparser
import logging
import os
import platform
import re
import sys
import time
from pathlib import Path
from typing import Optional

from prometheus_client import Gauge

import omnistat.utils as utils
from omnistat.collector_base import Collector


class NETWORK(Collector):
    def __init__(self, config: configparser.ConfigParser):
        """Initialize the NETWORK data collector.

        Args:
            config (configparser.ConfigParser): Cached copy of runtime configuration.
        """

        logging.debug("Initializing network data collector")

        self.__prefix = "omnistat_network_"
        self.__rx_metric = None
        self.__tx_metric = None

        # Files to check for IP devices.
        self.__net_rx_data_paths = {}
        self.__net_tx_data_paths = {}

        # Files to check for for slingshot (CXI) devices.
        self.__cxi_rx_data_paths = {}
        self.__cxi_tx_data_paths = {}
        self.__cxi_bucket_max_sizes = {"rx": {}, "tx": {}}

        # Additional CXI telemetry counters (see features.tex).
        # Maps counter group -> interface -> (optional) index -> sysfs path.
        self.__cxi_ok_octets_paths = {"rx": {}, "tx": {}}
        self.__cxi_simple_counter_paths = {}
        self.__cxi_tc_counter_paths = {}
        self.__cxi_feature_counter_paths = {}

        # Prometheus metrics for additional CXI telemetry.
        self.__cxi_simple_gauges = {}
        self.__cxi_tc_gauges = {}
        self.__cxi_bucket_gauges = {"rx": None, "tx": None}
        self.__cxi_feature_counter_gauge = None
        self.__cxi_derived_gauges = {}
        self.__cxi_prev_samples = {}

        # Files to check for for infiniband devices.
        self.__ib_rx_data_paths = {}
        self.__ib_tx_data_paths = {}
        self.__warned_sysfs_read_paths = set()

    @staticmethod
    def __read_sysfs_counter(path: Path) -> int:
        """Read a sysfs counter value.

        Some CXI telemetry entries expose values as "<count>@<timestamp>".
        """
        with open(path, "r") as f:
            value = f.read().strip()
        if "@" in value:
            value = value.split("@", 1)[0].strip()
        return int(value)

    def __read_sysfs_counter_maybe_warn(self, path: Path) -> int:
        try:
            return self.__read_sysfs_counter(path)
        except Exception as e:
            if path not in self.__warned_sysfs_read_paths:
                self.__warned_sysfs_read_paths.add(path)
                logging.debug(f"NETWORK: failed reading sysfs counter {path}: {e}")
            raise

    @staticmethod
    def __safe_delta(current: int, previous: int) -> int:
        # Treat counter resets/wraps as a delta of 0 for derived metrics.
        delta = current - previous
        return delta if delta >= 0 else 0

    def __register_cxi_derived_metric(self, name: str, description: str):
        metric = self.__prefix + "cxi_" + name
        self.__cxi_derived_gauges[name] = Gauge(metric, description, labelnames=["interface"])
        logging.info(f"--> [registered] {metric} -> {description} (gauge)")

    def __read_cxi_indexed_sum(self, nic: str, prefix: str, count: int) -> Optional[int]:
        """Sum counters named `<prefix><i>` for i in [0, count)."""
        total = 0
        found = False
        paths = self.__cxi_feature_counter_paths.get(nic, {})
        for i in range(count):
            path = paths.get(f"{prefix}{i}")
            if path is None:
                continue
            try:
                total += self.__read_sysfs_counter_maybe_warn(path)
                found = True
            except:
                pass
        return total if found else None

    def __read_cxi_named(self, nic: str, name: str) -> Optional[int]:
        path = self.__cxi_feature_counter_paths.get(nic, {}).get(name)
        if path is None:
            return None
        try:
            return self.__read_sysfs_counter_maybe_warn(path)
        except:
            return None

    def __read_cxi_telemetry(self, nic: str, filename: str) -> Optional[int]:
        """Read a CXI telemetry value by sysfs file name.

        Looks up known paths captured during `registerMetrics()` across both
        `__cxi_feature_counter_paths` and `__cxi_simple_counter_paths`.
        """
        value = self.__read_cxi_named(nic, filename)
        if value is not None:
            return value

        suffix = None
        if filename.startswith("hni_sts_"):
            suffix = filename[len("hni_sts_") :]
        elif filename.startswith("hni_"):
            suffix = filename[len("hni_") :]

        if suffix is None:
            return None

        path = self.__cxi_simple_counter_paths.get(suffix, {}).get(nic)
        if path is None:
            return None
        try:
            return self.__read_sysfs_counter_maybe_warn(path)
        except:
            return None

    def __read_cxi_tc_sum(self, suffix: str, nic: str) -> Optional[int]:
        total = 0
        found = False
        for path in self.__cxi_tc_counter_paths.get(suffix, {}).get(nic, {}).values():
            try:
                total += self.__read_sysfs_counter_maybe_warn(path)
                found = True
            except:
                pass
        return total if found else None

    def __read_cxi_bucket_count(self, kind: str, nic: str, bucket_min: int) -> Optional[int]:
        buckets = self.__cxi_rx_data_paths.get(nic, {}) if kind == "rx" else self.__cxi_tx_data_paths.get(nic, {})
        path = buckets.get(bucket_min)
        if path is None:
            return None
        try:
            return self.__read_sysfs_counter_maybe_warn(path)
        except:
            return None

    def registerMetrics(self):
        """Register metrics of interest"""

        # Standard IP (/sys/class/net): store data paths to sysfs
        # statistics files for local NICs, indexed by interface ID. For
        # example, for Rx bandwidth:
        #   __net_rx_data_paths = {
        #       "eth0": "/sys/class/net/eth0/statistics/rx_bytes"
        #   }
        for nic in Path("/sys/class/net").iterdir():
            if not nic.is_dir():
                continue

            nic_name = nic.name
            if nic_name == "lo":
                continue

            rx_path = nic / "statistics/rx_bytes"
            if rx_path.is_file() and rx_path.stat().st_size > 0:
                self.__net_rx_data_paths[nic_name] = rx_path

            tx_path = nic / "statistics/tx_bytes"
            if tx_path.is_file() and tx_path.stat().st_size > 0:
                self.__net_tx_data_paths[nic_name] = tx_path

        # Slingshot CXI traffic (/sys/class/cxi): store data paths to binned
        # telemetry files, indexed by interface ID and minimum size of the
        # bucket. For example, for Rx bandwidth:
        #   __cxi_rx_data_paths = {
        #       "cxi0": {
        #           27: "/sys/class/cxi/cx0/device/telemetry/hni_rx_ok_27",
        #           35: "/sys/class/cxi/cx0/device/telemetry/hni_rx_ok_35",
        #           36: "/sys/class/cxi/cx0/device/telemetry/hni_rx_ok_36_to_63",
        #           64: "/sys/class/cxi/cx0/device/telemetry/hni_rx_ok_64",
        #           ...
        #           8192: "/sys/class/cxi/cx0/device/telemetry/hni_rx_ok_8192_to_max",
        #       }
        #   }
        cxi_base_path = Path("/sys/class/cxi")
        cxi_glob_pattern = "device/telemetry/hni_*_ok*"
        cxi_re_pattern = r"^hni_(tx|rx)_ok_(\d+)(?:_to_(\d+|max))?$"
        cxi_data_paths = {
            "rx": self.__cxi_rx_data_paths,
            "tx": self.__cxi_tx_data_paths,
        }

        cxi_nics = []
        if cxi_base_path.is_dir():
            cxi_nics = cxi_base_path.iterdir()

        for nic in cxi_nics:
            if not nic.is_dir():
                continue

            nic_name = nic.name
            self.__cxi_rx_data_paths[nic_name] = {}
            self.__cxi_tx_data_paths[nic_name] = {}
            self.__cxi_bucket_max_sizes["rx"][nic_name] = {}
            self.__cxi_bucket_max_sizes["tx"][nic_name] = {}

            for bucket in nic.glob(cxi_glob_pattern):
                match = re.match(cxi_re_pattern, bucket.name)
                if not match:
                    continue

                kind = match.group(1)
                min_size = int(match.group(2))
                cxi_data_paths[kind][nic_name][min_size] = bucket
                max_size = match.group(3)
                self.__cxi_bucket_max_sizes[kind][nic_name][min_size] = max_size or str(min_size)

            # Optional: ingest additional Cassini telemetry counters if present.
            telemetry_dir = nic / "device" / "telemetry"
            if telemetry_dir.is_dir():
                # Any counter referenced in features.tex is expected to exist on
                # target systems. Export them generically as raw counters to
                # keep the collector maintainable (derived metrics can be built
                # in Prometheus).
                #
                # Exclude the histogram bucket counters already exported via
                # `*_ok_packets_bucket`.
                feature_exact = {
                    "cq_cq_oxe_num_flits",
                    "cq_cq_oxe_num_idles",
                    "cq_cq_oxe_num_stalls",
                    "cq_sts_credits_in_use_lpe_cmd_credits",
                    "cq_sts_credits_in_use_lpe_rcv_fifo_credits",
                    "oxe_channel_idle",
                    "oxe_ioi_pkts_ordered",
                    "oxe_ioi_pkts_unordered",
                    "parbs_sts_credits_in_use_tarb_pi_posted_credits",
                    "pct_prf_tct_status_max_tct_in_use",
                    "pct_prf_tct_status_tct_in_use",
                    "pct_req_ordered",
                    "pct_req_unordered",
                    "pct_resource_busy",
                    "pct_sct_timeouts",
                    "pct_tct_timeouts",
                    "pct_trs_replay_pend_drops",
                }
                feature_prefixes = (
                    "cq_cycles_blocked_",
                    "ixe_tc_req_ecn_pkts_",
                    "ixe_tc_req_no_ecn_pkts_",
                    "ixe_tc_rsp_ecn_pkts_",
                    "ixe_tc_rsp_no_ecn_pkts_",
                    "pct_req_rsp_latency_",
                )

                for entry in telemetry_dir.iterdir():
                    if not entry.is_file():
                        continue

                    name = entry.name
                    if name.startswith("hni_rx_ok_") or name.startswith("hni_tx_ok_"):
                        continue

                    if name in feature_exact or name.startswith(feature_prefixes):
                        self.__cxi_feature_counter_paths.setdefault(nic_name, {})[name] = entry

                # Direct octet counters (preferred over bucket-based estimate).
                for kind, filename in [
                    ("tx", "hni_sts_tx_ok_octets"),
                    ("rx", "hni_sts_rx_ok_octets"),
                ]:
                    path = telemetry_dir / filename
                    if path.is_file():
                        self.__cxi_ok_octets_paths[kind][nic_name] = path
                        # Also export as a raw counter metric for dashboards.
                        self.__cxi_simple_counter_paths.setdefault(f"{kind}_ok_octets", {})[nic_name] = path

                # Flat counters with no indexing (rate can be derived in Prometheus).
                simple_counters = {
                    "tx_octets_multi": "hni_sts_tx_octets_multi",
                    "tx_octets_ieee": "hni_sts_tx_octets_ieee",
                    "tx_octets_opt": "hni_sts_tx_octets_opt",
                    "tx_bad_octets": "hni_sts_tx_bad_octets",
                    "rx_bad_octets": "hni_sts_rx_bad_octets",
                    "pause_sent": "hni_pause_sent",
                    "fgfc_discard": "hni_fgfc_discard",
                    "pcs_corrected_cw": "hni_pcs_corrected_cw",
                    "pcs_uncorrected_cw": "hni_pcs_uncorrected_cw",
                }
                for suffix, filename in simple_counters.items():
                    path = telemetry_dir / filename
                    if path.is_file():
                        self.__cxi_simple_counter_paths.setdefault(suffix, {})[nic_name] = path

                # Traffic-class indexed counters (0..7).
                tc_counters = {
                    "pkts_sent_by_tc": "hni_pkts_sent_by_tc_{}",
                    "pkts_recv_by_tc": "hni_pkts_recv_by_tc_{}",
                    "pause_recv": "hni_pause_recv_{}",
                    "pause_xoff_sent": "hni_pause_xoff_sent_{}",
                    "discard_cntr": "hni_discard_cntr_{}",
                }
                for suffix, pattern in tc_counters.items():
                    for tc in range(8):
                        path = telemetry_dir / pattern.format(tc)
                        if path.is_file():
                            self.__cxi_tc_counter_paths.setdefault(suffix, {}).setdefault(nic_name, {})[str(tc)] = path
            else:
                logging.debug(f"NETWORK: CXI telemetry dir missing: {telemetry_dir}")

            logging.debug(
                "NETWORK: CXI %s discovered: rx_buckets=%d tx_buckets=%d ok_octets(rx=%s,tx=%s) simple=%d tc=%d feature=%d",
                nic_name,
                len(self.__cxi_rx_data_paths.get(nic_name, {})),
                len(self.__cxi_tx_data_paths.get(nic_name, {})),
                nic_name in self.__cxi_ok_octets_paths["rx"],
                nic_name in self.__cxi_ok_octets_paths["tx"],
                sum(1 for d in self.__cxi_simple_counter_paths.values() if nic_name in d),
                sum(1 for d in self.__cxi_tc_counter_paths.values() if nic_name in d),
                len(self.__cxi_feature_counter_paths.get(nic_name, {})),
            )

        # Infiniband traffic (/sys/class/infiniband): store data paths to
        # counters, indexed by interface ID and port ID. For example, for Rx
        # bandwidth:
        #   __infiniband_rx_data_paths = {
        #       "mlx5_0:1": "/sys/class/infiniband/mlx5_0/ports/1/counters/port_rcv_data",
        #       "mlx5_1:1": "/sys/class/infiniband/mlx5_1/ports/1/counters/port_rcv_data",
        #       }
        #   }
        ib_base_path = Path("/sys/class/infiniband")

        ib_nics = []
        if ib_base_path.is_dir():
            ib_nics = ib_base_path.iterdir()

        for nic in ib_nics:
            if not nic.is_dir():
                continue

            ports = nic / "ports"
            for port in ports.iterdir():
                nic_name = f"{nic.name}:{port.name}"

                rx_path = port / "counters" / "port_rcv_data"
                if rx_path.is_file() and rx_path.stat().st_size > 0:
                    self.__ib_rx_data_paths[nic_name] = rx_path

                tx_path = port / "counters" / "port_xmit_data"
                if tx_path.is_file() and tx_path.stat().st_size > 0:
                    self.__ib_tx_data_paths[nic_name] = tx_path

        # Register Prometheus metrics for Rx and Tx. Devices are identified by
        # device class and interface name. For example, the Prometheus metric
        # for Rx bytes in the standard network device eth0:
        #   network_rx_bytes{device_class="net",interface="eth0"}
        labels = ["device_class", "interface"]

        rx_data_paths = [self.__net_rx_data_paths, self.__cxi_rx_data_paths, self.__ib_rx_data_paths]
        num_rx = sum([len(x) for x in rx_data_paths]) + len(self.__cxi_ok_octets_paths["rx"])
        if num_rx > 0:
            logging.debug(self.__net_rx_data_paths)
            metric = self.__prefix + "rx_bytes"
            description = "Network received (bytes)"
            self.__rx_metric = Gauge(metric, description, labelnames=labels)
            logging.info(f"--> [registered] {metric} -> {description} (gauge)")

        tx_data_paths = [self.__net_tx_data_paths, self.__cxi_tx_data_paths, self.__ib_tx_data_paths]
        num_tx = sum([len(x) for x in tx_data_paths]) + len(self.__cxi_ok_octets_paths["tx"])
        if num_tx > 0:
            logging.debug(self.__net_tx_data_paths)
            metric = self.__prefix + "tx_bytes"
            description = "Network transmitted (bytes)"
            self.__tx_metric = Gauge(metric, description, labelnames=labels)
            logging.info(f"--> [registered] {metric} -> {description} (gauge)")

        # Additional CXI telemetry: expose raw counters to enable derived rates
        # and ratios in Prometheus.
        for suffix in sorted(self.__cxi_simple_counter_paths.keys()):
            metric = self.__prefix + "cxi_" + suffix
            description = f"CXI telemetry counter ({suffix})"
            gauge = Gauge(metric, description, labelnames=["interface"])
            self.__cxi_simple_gauges[suffix] = gauge
            logging.info(f"--> [registered] {metric} -> {description} (gauge)")

        for suffix in sorted(self.__cxi_tc_counter_paths.keys()):
            metric = self.__prefix + "cxi_" + suffix
            description = f"CXI telemetry counter ({suffix}), indexed by traffic class"
            gauge = Gauge(metric, description, labelnames=["interface", "traffic_class"])
            self.__cxi_tc_gauges[suffix] = gauge
            logging.info(f"--> [registered] {metric} -> {description} (gauge)")

        for kind in ["rx", "tx"]:
            # Export packet counts per message-size bucket.
            data_paths = self.__cxi_rx_data_paths if kind == "rx" else self.__cxi_tx_data_paths
            num_buckets = sum(len(buckets) for buckets in data_paths.values())
            if num_buckets > 0:
                metric = self.__prefix + f"cxi_{kind}_ok_packets_bucket"
                description = f"CXI ok packet counts by message-size bucket ({kind})"
                self.__cxi_bucket_gauges[kind] = Gauge(metric, description, labelnames=["interface", "bucket_min", "bucket_max"])
                logging.info(f"--> [registered] {metric} -> {description} (gauge)")

        if sum(len(counters) for counters in self.__cxi_feature_counter_paths.values()) > 0:
            metric = self.__prefix + "cxi_telemetry"
            description = "CXI telemetry counter value"
            self.__cxi_feature_counter_gauge = Gauge(metric, description, labelnames=["interface", "counter"])
            logging.info(f"--> [registered] {metric} -> {description} (gauge)")

        # Derived CXI metrics defined in features.tex (computed from deltas).
        if (
            len(self.__cxi_feature_counter_paths) > 0
            or len(self.__cxi_ok_octets_paths["rx"]) > 0
            or len(self.__cxi_ok_octets_paths["tx"]) > 0
        ):
            self.__register_cxi_derived_metric("tx_bandwidth_bytes_per_second", "CXI TX bandwidth (bytes/s)")
            self.__register_cxi_derived_metric("rx_bandwidth_bytes_per_second", "CXI RX bandwidth (bytes/s)")
            self.__register_cxi_derived_metric("bidirectional_bandwidth_bytes_per_second", "CXI bidirectional bandwidth (bytes/s)")
            self.__register_cxi_derived_metric("tx_to_rx_balance_ratio", "CXI TX/RX bandwidth balance ratio")
            self.__register_cxi_derived_metric("multicast_tx_share_fraction", "CXI multicast TX share (fraction of TX bytes)")
            self.__register_cxi_derived_metric("ieee_tx_share_fraction", "CXI IEEE TX share (fraction of TX bytes)")
            self.__register_cxi_derived_metric("optimized_tx_share_fraction", "CXI optimized-protocol TX share (fraction of TX bytes)")

            self.__register_cxi_derived_metric("packet_send_rate_packets_per_second", "CXI packet send rate (pkts/s)")
            self.__register_cxi_derived_metric("packet_receive_rate_packets_per_second", "CXI packet receive rate (pkts/s)")
            self.__register_cxi_derived_metric("avg_bytes_per_tx_packet", "CXI average bytes per TX packet (bytes/pkt)")
            self.__register_cxi_derived_metric("small_packet_fraction_tx", "CXI small packet fraction TX (fraction of TX packets)")
            self.__register_cxi_derived_metric("large_packet_fraction_tx", "CXI large packet fraction TX (fraction of TX packets)")

            self.__register_cxi_derived_metric("link_busy_fraction", "CXI link busy fraction")
            self.__register_cxi_derived_metric("link_stall_per_flit", "CXI link stalls per flit")
            self.__register_cxi_derived_metric("cq_blocked_cycles_per_second", "CXI CQ blocked cycles rate (cycles/s)")
            self.__register_cxi_derived_metric("nic_no_work_cycles_per_second", "CXI NIC no-work rate proxy (cycles/s)")

            self.__register_cxi_derived_metric("pause_received_per_second", "CXI pause received rate (events/s)")
            self.__register_cxi_derived_metric("pause_sent_per_second", "CXI pause sent rate (events/s)")
            self.__register_cxi_derived_metric("xoff_sent_per_second", "CXI XOFF sent rate (events/s)")
            self.__register_cxi_derived_metric("ecn_marking_ratio_request_fraction", "CXI ECN marking ratio (request side)")
            self.__register_cxi_derived_metric("ecn_marking_ratio_response_fraction", "CXI ECN marking ratio (response side)")
            self.__register_cxi_derived_metric("congestion_discard_per_second", "CXI congestion discard rate (events/s)")

            self.__register_cxi_derived_metric("command_credits_in_use_per_second", "CXI command credits in use rate proxy (1/s)")
            self.__register_cxi_derived_metric("receive_fifo_credits_in_use_per_second", "CXI receive FIFO credits in use rate proxy (1/s)")
            self.__register_cxi_derived_metric("pi_posted_credits_in_use_per_second", "CXI PI posted credits in use rate proxy (1/s)")
            self.__register_cxi_derived_metric("resource_busy_per_second", "CXI resource busy rate (events/s)")
            self.__register_cxi_derived_metric("endpoint_table_pressure_fraction", "CXI endpoint table pressure proxy (fraction)")

            self.__register_cxi_derived_metric("ordered_to_unordered_ratio", "CXI ordered/unordered packet ratio")
            self.__register_cxi_derived_metric("unordered_fraction", "CXI unordered packet fraction")
            self.__register_cxi_derived_metric("ordered_request_fraction", "CXI ordered request fraction")

            self.__register_cxi_derived_metric("mean_rsp_latency_bin_index", "CXI mean response latency proxy (bin index)")
            self.__register_cxi_derived_metric("tail_latency_fraction_top10pct_bins", "CXI tail latency fraction (top 10% bins)")
            self.__register_cxi_derived_metric("timeout_per_second", "CXI timeout rate (events/s)")

            self.__register_cxi_derived_metric("bad_tx_octets_per_second", "CXI bad TX octet rate (octets/s)")
            self.__register_cxi_derived_metric("bad_rx_octets_per_second", "CXI bad RX octet rate (octets/s)")
            self.__register_cxi_derived_metric("ecc_corrected_cw_per_second", "CXI ECC corrected codeword rate (cw/s)")
            self.__register_cxi_derived_metric("ecc_uncorrected_cw_per_second", "CXI ECC uncorrected codeword rate (cw/s)")

    def updateMetrics(self):
        """Update registered metrics of interest"""

        net_data = [
            (self.__net_rx_data_paths, self.__rx_metric),
            (self.__net_tx_data_paths, self.__tx_metric),
        ]

        for data_paths, metric in net_data:
            if metric is None:
                continue
            for nic, path in data_paths.items():
                try:
                    with open(path, "r") as f:
                        data = int(f.read().strip())
                        metric.labels(device_class="net", interface=nic).set(data)
                except:
                    pass

        # CXI counters.
        # Prefer exact fabric octet counters if available; otherwise, estimate a
        # lower bound from the message-size histogram buckets.
        for kind, metric in [("rx", self.__rx_metric), ("tx", self.__tx_metric)]:
            for nic in set(self.__cxi_rx_data_paths.keys()) | set(self.__cxi_tx_data_paths.keys()) | set(
                self.__cxi_ok_octets_paths[kind].keys()
            ):
                total = None
                path = self.__cxi_ok_octets_paths[kind].get(nic)
                if path is not None:
                    try:
                        total = self.__read_sysfs_counter_maybe_warn(path)
                    except:
                        total = None

                if total is None:
                    buckets = self.__cxi_rx_data_paths.get(nic, {}) if kind == "rx" else self.__cxi_tx_data_paths.get(nic, {})
                    total = 0
                    for size, bucket_path in buckets.items():
                        try:
                            count = self.__read_sysfs_counter_maybe_warn(bucket_path)
                            total += count * size
                        except:
                            pass

                if metric is not None:
                    metric.labels(device_class="cxi", interface=nic).set(total)

        # Export CXI packet counts for each size bucket.
        for kind, data_paths in [("rx", self.__cxi_rx_data_paths), ("tx", self.__cxi_tx_data_paths)]:
            gauge = self.__cxi_bucket_gauges.get(kind)
            if gauge is None:
                continue
            for nic, buckets in data_paths.items():
                for min_size, bucket_path in buckets.items():
                    try:
                        count = self.__read_sysfs_counter_maybe_warn(bucket_path)
                        max_size = self.__cxi_bucket_max_sizes[kind].get(nic, {}).get(min_size, str(min_size))
                        gauge.labels(interface=nic, bucket_min=str(min_size), bucket_max=str(max_size)).set(count)
                    except:
                        pass

        # Export additional CXI telemetry counters as raw values.
        for suffix, per_nic in self.__cxi_simple_counter_paths.items():
            gauge = self.__cxi_simple_gauges.get(suffix)
            if gauge is None:
                continue
            for nic, path in per_nic.items():
                try:
                    value = self.__read_sysfs_counter_maybe_warn(path)
                    gauge.labels(interface=nic).set(value)
                except:
                    pass

        for suffix, per_nic in self.__cxi_tc_counter_paths.items():
            gauge = self.__cxi_tc_gauges.get(suffix)
            if gauge is None:
                continue
            for nic, per_tc in per_nic.items():
                for tc, path in per_tc.items():
                    try:
                        value = self.__read_sysfs_counter_maybe_warn(path)
                        gauge.labels(interface=nic, traffic_class=tc).set(value)
                    except:
                        pass

        gauge = self.__cxi_feature_counter_gauge
        if gauge is not None:
            for nic, counters in self.__cxi_feature_counter_paths.items():
                for name, path in counters.items():
                    try:
                        value = self.__read_sysfs_counter_maybe_warn(path)
                        gauge.labels(interface=nic, counter=name).set(value)
                    except:
                        pass

        # Derived CXI metrics (features.tex). These are computed per interface
        # from deltas between successive samples.
        if len(self.__cxi_derived_gauges) > 0:
            now = time.monotonic()
            eps = 1e-9

            cxi_nics = set()
            cxi_nics |= set(self.__cxi_ok_octets_paths["rx"].keys())
            cxi_nics |= set(self.__cxi_ok_octets_paths["tx"].keys())
            cxi_nics |= set(self.__cxi_tx_data_paths.keys())
            cxi_nics |= set(self.__cxi_feature_counter_paths.keys())
            cxi_nics |= {nic for d in self.__cxi_tc_counter_paths.values() for nic in d.keys()}

            for nic in cxi_nics:
                # Gather current raw counters needed by formulas.
                current = {}

                def put(name: str, value: Optional[int]):
                    if value is not None:
                        current[name] = value

                put("hni_sts_tx_ok_octets", self.__read_cxi_telemetry(nic, "hni_sts_tx_ok_octets"))
                put("hni_sts_rx_ok_octets", self.__read_cxi_telemetry(nic, "hni_sts_rx_ok_octets"))

                for name in [
                    "hni_sts_tx_octets_multi",
                    "hni_sts_tx_octets_ieee",
                    "hni_sts_tx_octets_opt",
                    "hni_sts_tx_bad_octets",
                    "hni_sts_rx_bad_octets",
                    "hni_pcs_corrected_cw",
                    "hni_pcs_uncorrected_cw",
                    "cq_cq_oxe_num_flits",
                    "cq_cq_oxe_num_idles",
                    "cq_cq_oxe_num_stalls",
                    "oxe_channel_idle",
                    "cq_sts_credits_in_use_lpe_cmd_credits",
                    "cq_sts_credits_in_use_lpe_rcv_fifo_credits",
                    "parbs_sts_credits_in_use_tarb_pi_posted_credits",
                    "pct_resource_busy",
                    "pct_prf_tct_status_tct_in_use",
                    "pct_prf_tct_status_max_tct_in_use",
                    "oxe_ioi_pkts_ordered",
                    "oxe_ioi_pkts_unordered",
                    "pct_req_ordered",
                    "pct_req_unordered",
                    "pct_sct_timeouts",
                    "pct_tct_timeouts",
                    "pct_trs_replay_pend_drops",
                    "hni_pause_sent",
                    "hni_fgfc_discard",
                ]:
                    put(name, self.__read_cxi_telemetry(nic, name))

                # Aggregate indexed counters.
                put("cq_cycles_blocked_sum", self.__read_cxi_indexed_sum(nic, "cq_cycles_blocked_", 4))

                put("hni_pkts_sent_by_tc_sum", self.__read_cxi_tc_sum("pkts_sent_by_tc", nic))
                put("hni_pkts_recv_by_tc_sum", self.__read_cxi_tc_sum("pkts_recv_by_tc", nic))
                put("hni_pause_recv_sum", self.__read_cxi_tc_sum("pause_recv", nic))
                put("hni_pause_xoff_sent_sum", self.__read_cxi_tc_sum("pause_xoff_sent", nic))
                put("hni_discard_cntr_sum", self.__read_cxi_tc_sum("discard_cntr", nic))

                put("ixe_tc_req_ecn_pkts_sum", self.__read_cxi_indexed_sum(nic, "ixe_tc_req_ecn_pkts_", 8))
                put("ixe_tc_req_no_ecn_pkts_sum", self.__read_cxi_indexed_sum(nic, "ixe_tc_req_no_ecn_pkts_", 8))
                put("ixe_tc_rsp_ecn_pkts_sum", self.__read_cxi_indexed_sum(nic, "ixe_tc_rsp_ecn_pkts_", 8))
                put("ixe_tc_rsp_no_ecn_pkts_sum", self.__read_cxi_indexed_sum(nic, "ixe_tc_rsp_no_ecn_pkts_", 8))

                # Message-size histogram buckets (TX).
                for bucket_min in [64, 65, 128, 1024, 2048, 4096, 8192]:
                    put(f"hni_tx_ok_bucket_{bucket_min}", self.__read_cxi_bucket_count("tx", nic, bucket_min))

                # Latency histogram: pct_req_rsp_latency_<i>
                latency_bins = {}
                for name, path in self.__cxi_feature_counter_paths.get(nic, {}).items():
                    if not name.startswith("pct_req_rsp_latency_"):
                        continue
                    suffix = name[len("pct_req_rsp_latency_") :]
                    if not suffix.isdigit():
                        continue
                    try:
                        latency_bins[int(suffix)] = self.__read_sysfs_counter_maybe_warn(path)
                    except:
                        pass
                for idx, value in latency_bins.items():
                    current[f"pct_req_rsp_latency_{idx}"] = value

                prev = self.__cxi_prev_samples.get(nic)
                if prev is None:
                    self.__cxi_prev_samples[nic] = {"ts": now, "values": current}
                    continue

                dt = now - prev["ts"]
                if dt <= 0:
                    self.__cxi_prev_samples[nic] = {"ts": now, "values": current}
                    continue

                prev_values = prev["values"]

                def d(name: str) -> int:
                    if name not in current or name not in prev_values:
                        return 0
                    return self.__safe_delta(current[name], prev_values[name])

                delta_tx_ok = d("hni_sts_tx_ok_octets")
                delta_rx_ok = d("hni_sts_rx_ok_octets")

                tx_bw = delta_tx_ok / dt if delta_tx_ok else 0.0
                rx_bw = delta_rx_ok / dt if delta_rx_ok else 0.0
                self.__cxi_derived_gauges["tx_bandwidth_bytes_per_second"].labels(interface=nic).set(tx_bw)
                self.__cxi_derived_gauges["rx_bandwidth_bytes_per_second"].labels(interface=nic).set(rx_bw)
                self.__cxi_derived_gauges["bidirectional_bandwidth_bytes_per_second"].labels(interface=nic).set((delta_tx_ok + delta_rx_ok) / dt)
                self.__cxi_derived_gauges["tx_to_rx_balance_ratio"].labels(interface=nic).set(tx_bw / max(rx_bw, eps))

                self.__cxi_derived_gauges["multicast_tx_share_fraction"].labels(interface=nic).set(
                    d("hni_sts_tx_octets_multi") / max(delta_tx_ok, 1)
                )
                self.__cxi_derived_gauges["ieee_tx_share_fraction"].labels(interface=nic).set(
                    d("hni_sts_tx_octets_ieee") / max(delta_tx_ok, 1)
                )
                self.__cxi_derived_gauges["optimized_tx_share_fraction"].labels(interface=nic).set(
                    d("hni_sts_tx_octets_opt") / max(delta_tx_ok, 1)
                )

                delta_pkts_tx = d("hni_pkts_sent_by_tc_sum")
                delta_pkts_rx = d("hni_pkts_recv_by_tc_sum")
                self.__cxi_derived_gauges["packet_send_rate_packets_per_second"].labels(interface=nic).set(delta_pkts_tx / dt)
                self.__cxi_derived_gauges["packet_receive_rate_packets_per_second"].labels(interface=nic).set(delta_pkts_rx / dt)
                self.__cxi_derived_gauges["avg_bytes_per_tx_packet"].labels(interface=nic).set(delta_tx_ok / max(delta_pkts_tx, 1))

                delta_small_pkts = sum(d(f"hni_tx_ok_bucket_{m}") for m in [64, 65, 128])
                delta_large_pkts = sum(d(f"hni_tx_ok_bucket_{m}") for m in [1024, 2048, 4096, 8192])
                self.__cxi_derived_gauges["small_packet_fraction_tx"].labels(interface=nic).set(delta_small_pkts / max(delta_pkts_tx, 1))
                self.__cxi_derived_gauges["large_packet_fraction_tx"].labels(interface=nic).set(delta_large_pkts / max(delta_pkts_tx, 1))

                delta_flits = d("cq_cq_oxe_num_flits")
                delta_idles = d("cq_cq_oxe_num_idles")
                self.__cxi_derived_gauges["link_busy_fraction"].labels(interface=nic).set(delta_flits / max(delta_flits + delta_idles, 1))
                self.__cxi_derived_gauges["link_stall_per_flit"].labels(interface=nic).set(d("cq_cq_oxe_num_stalls") / max(delta_flits, 1))
                self.__cxi_derived_gauges["cq_blocked_cycles_per_second"].labels(interface=nic).set(d("cq_cycles_blocked_sum") / dt)
                self.__cxi_derived_gauges["nic_no_work_cycles_per_second"].labels(interface=nic).set(d("oxe_channel_idle") / dt)

                self.__cxi_derived_gauges["pause_received_per_second"].labels(interface=nic).set(d("hni_pause_recv_sum") / dt)
                self.__cxi_derived_gauges["pause_sent_per_second"].labels(interface=nic).set(d("hni_pause_sent") / dt)
                self.__cxi_derived_gauges["xoff_sent_per_second"].labels(interface=nic).set(d("hni_pause_xoff_sent_sum") / dt)

                delta_req_ecn = d("ixe_tc_req_ecn_pkts_sum")
                delta_req_no = d("ixe_tc_req_no_ecn_pkts_sum")
                delta_rsp_ecn = d("ixe_tc_rsp_ecn_pkts_sum")
                delta_rsp_no = d("ixe_tc_rsp_no_ecn_pkts_sum")
                self.__cxi_derived_gauges["ecn_marking_ratio_request_fraction"].labels(interface=nic).set(
                    delta_req_ecn / max(delta_req_ecn + delta_req_no, 1)
                )
                self.__cxi_derived_gauges["ecn_marking_ratio_response_fraction"].labels(interface=nic).set(
                    delta_rsp_ecn / max(delta_rsp_ecn + delta_rsp_no, 1)
                )
                self.__cxi_derived_gauges["congestion_discard_per_second"].labels(interface=nic).set(
                    (d("hni_discard_cntr_sum") + d("hni_fgfc_discard")) / dt
                )

                self.__cxi_derived_gauges["command_credits_in_use_per_second"].labels(interface=nic).set(
                    d("cq_sts_credits_in_use_lpe_cmd_credits") / dt
                )
                self.__cxi_derived_gauges["receive_fifo_credits_in_use_per_second"].labels(interface=nic).set(
                    d("cq_sts_credits_in_use_lpe_rcv_fifo_credits") / dt
                )
                self.__cxi_derived_gauges["pi_posted_credits_in_use_per_second"].labels(interface=nic).set(
                    d("parbs_sts_credits_in_use_tarb_pi_posted_credits") / dt
                )
                self.__cxi_derived_gauges["resource_busy_per_second"].labels(interface=nic).set(d("pct_resource_busy") / dt)
                self.__cxi_derived_gauges["endpoint_table_pressure_fraction"].labels(interface=nic).set(
                    d("pct_prf_tct_status_tct_in_use") / max(d("pct_prf_tct_status_max_tct_in_use"), 1)
                )

                self.__cxi_derived_gauges["ordered_to_unordered_ratio"].labels(interface=nic).set(
                    d("oxe_ioi_pkts_ordered") / max(d("oxe_ioi_pkts_unordered"), 1)
                )
                ordered = d("oxe_ioi_pkts_ordered")
                unordered = d("oxe_ioi_pkts_unordered")
                self.__cxi_derived_gauges["unordered_fraction"].labels(interface=nic).set(unordered / max(ordered + unordered, 1))

                delta_req_ordered = d("pct_req_ordered")
                delta_req_unordered = d("pct_req_unordered")
                self.__cxi_derived_gauges["ordered_request_fraction"].labels(interface=nic).set(
                    delta_req_ordered / max(delta_req_ordered + delta_req_unordered, 1)
                )

                # Latency proxies (bin index based): use the numeric suffix as
                # bin center index, since bin centers are platform-defined.
                latency_deltas = []
                for key in current.keys():
                    if not key.startswith("pct_req_rsp_latency_"):
                        continue
                    suffix = key[len("pct_req_rsp_latency_") :]
                    if not suffix.isdigit() or key not in prev_values:
                        continue
                    latency_deltas.append((int(suffix), self.__safe_delta(current[key], prev_values[key])))
                latency_deltas = [(idx, cnt) for idx, cnt in latency_deltas if cnt > 0]
                latency_deltas.sort(key=lambda x: x[0])
                total_latency = sum(cnt for _, cnt in latency_deltas)
                if total_latency > 0:
                    mean_idx = sum(idx * cnt for idx, cnt in latency_deltas) / total_latency
                    num_bins = len(latency_deltas)
                    top_n = max(1, int(round(num_bins * 0.10)))
                    tail_cnt = sum(cnt for _, cnt in latency_deltas[-top_n:])
                    tail_frac = tail_cnt / total_latency
                else:
                    mean_idx = 0.0
                    tail_frac = 0.0
                self.__cxi_derived_gauges["mean_rsp_latency_bin_index"].labels(interface=nic).set(mean_idx)
                self.__cxi_derived_gauges["tail_latency_fraction_top10pct_bins"].labels(interface=nic).set(tail_frac)

                self.__cxi_derived_gauges["timeout_per_second"].labels(interface=nic).set(
                    (d("pct_sct_timeouts") + d("pct_tct_timeouts") + d("pct_trs_replay_pend_drops")) / dt
                )

                self.__cxi_derived_gauges["bad_tx_octets_per_second"].labels(interface=nic).set(d("hni_sts_tx_bad_octets") / dt)
                self.__cxi_derived_gauges["bad_rx_octets_per_second"].labels(interface=nic).set(d("hni_sts_rx_bad_octets") / dt)
                self.__cxi_derived_gauges["ecc_corrected_cw_per_second"].labels(interface=nic).set(d("hni_pcs_corrected_cw") / dt)
                self.__cxi_derived_gauges["ecc_uncorrected_cw_per_second"].labels(interface=nic).set(d("hni_pcs_uncorrected_cw") / dt)

                self.__cxi_prev_samples[nic] = {"ts": now, "values": current}

        ib_data = [
            (self.__ib_rx_data_paths, self.__rx_metric),
            (self.__ib_tx_data_paths, self.__tx_metric),
        ]

        for data_paths, metric in ib_data:
            if metric is None:
                continue
            for nic, path in data_paths.items():
                try:
                    with open(path, "r") as f:
                        data = int(f.read().strip())
                        # Counters for infiniband are reported as "octets divided by 4";
                        # multiply to collect the expected value in bytes.
                        metric.labels(device_class="infiniband", interface=nic).set(data * 4)
                except:
                    pass

        return
