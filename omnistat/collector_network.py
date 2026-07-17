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
exposed under /sys/class/{net,cxi,infiniband}.
"""

import configparser
import logging
import os
import platform
import re
import sys
from pathlib import Path

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

        # Files to check for IP devices.
        self.__net_rx_data_paths = {}
        self.__net_tx_data_paths = {}

        # Files to check for for slingshot (CXI) devices.
        self.__cxi_rx_data_paths = {}
        self.__cxi_tx_data_paths = {}

        # Files to check for for infiniband devices.
        self.__ib_rx_data_paths = {}
        self.__ib_tx_data_paths = {}

        # hw_counter NIC (ainic, thor, ...) paths, keyed by counter and
        # interface, e.g.:
        #   {"rx_bytes": {"ionic_0:1": ("ainic", [Path(".../rx_rdma_ucast_bytes"), ...])}}
        # shared_counters feed the cross-class rx/tx gauges; extra_counters each
        # get their own hw-only gauge named after the counter.
        self.__hw_shared_data_paths = {}
        self.__hw_extra_data_paths = {}

    # RoCE-over-Ethernet NICs that appear under /sys/class/infiniband but report
    # byte totals via hw_counters (already in bytes, no IB octet/4 scaling)
    # rather than the standard IB port counters. Each is detected by driver
    # string and feeds the shared rx/tx metrics under its own device_class.
    _HW_COUNTER_NICS = {
        "ionic": {
            # AMD AINIC (Pensando "ionic", e.g. Pollara)
            "device_class": "ainic",
            "shared_counters": {
                "rx_bytes": ["rx_rdma_ucast_bytes", "rx_rdma_mcast_bytes"],
                "tx_bytes": ["tx_rdma_ucast_bytes", "tx_rdma_mcast_bytes"],
            },
            "extra_counters": {
                "rx_packets": ["rx_rdma_ucast_pkts", "rx_rdma_mcast_pkts"],
                "tx_packets": ["tx_rdma_ucast_pkts", "tx_rdma_mcast_pkts"],
                "retx_packets": ["tx_rdma_retx_pkts"],
                "out_of_sequence_packets": ["req_rx_pkt_seq_err"],
            },
        },
        "bnxt_en": {
            # Broadcom "Thor" (bnxt_re)
            "device_class": "thor",
            "shared_counters": {
                "rx_bytes": ["rx_bytes"],
                "tx_bytes": ["tx_bytes"],
            },
            "extra_counters": {
                "rx_packets": ["rx_pkts"],
                "tx_packets": ["tx_pkts"],
                "out_of_sequence_packets": ["out_of_sequence"],
                "rx_discarded_packets": ["rx_roce_discards"],
                "tx_discarded_packets": ["tx_roce_discards"],
            },
        },
    }

    def __hw_counter_spec(self, nic):
        """Return the _HW_COUNTER_NICS spec for an infiniband-class device, else None.

        Matched purely on the uevent DRIVER string. A None result means the
        device is a standard IB NIC and handled by the generic IB branch.
        """
        try:
            uevent = (nic / "device" / "uevent").read_text()
        except OSError:
            return None
        for line in uevent.splitlines():
            if line.startswith("DRIVER="):
                return self._HW_COUNTER_NICS.get(line[len("DRIVER=") :])
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
        cxi_re_pattern = "hni_(tx|rx)_ok_(\d+)[_to]*(\d+)?"
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

            for bucket in nic.glob(cxi_glob_pattern):
                match = re.match(cxi_re_pattern, bucket.name)
                if not match:
                    continue

                kind = match.group(1)
                min_size = int(match.group(2))
                cxi_data_paths[kind][nic_name][min_size] = bucket

        # Infiniband traffic (/sys/class/infiniband): store data paths to
        # counters, indexed by interface ID and port ID. For example, for Rx
        # bandwidth:
        #   __infiniband_rx_data_paths = {
        #       "mlx5_0:1": "/sys/class/infiniband/mlx5_0/ports/1/counters/port_rcv_data",
        #       "mlx5_1:1": "/sys/class/infiniband/mlx5_1/ports/1/counters/port_rcv_data",
        #       }
        #   }
        #
        # hw_counter NICs ("ionic"/AINIC, "bnxt_en"/Thor, ...) also appear under
        # Infiniband but expose bytes via hw_counters instead of the standard IB
        # counters, so they are detected via __hw_counter_spec and handled in a
        # separate branch below. Their byte paths are indexed by interface and
        # port ID, with each value carrying the device_class and the list of
        # counter files to sum. For example, for Rx bandwidth:
        #   __hw_shared_data_paths = {
        #       "rx_bytes": {
        #           "ionic_0:1": ("ainic", [
        #               "/sys/class/infiniband/ionic_0/ports/1/hw_counters/rx_rdma_ucast_bytes",
        #               "/sys/class/infiniband/ionic_0/ports/1/hw_counters/rx_rdma_mcast_bytes",
        #           ]),
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
            spec = self.__hw_counter_spec(nic)

            if spec is not None:
                # hw_counter NIC (ainic, thor): byte totals live in hw_counters
                # (already in bytes). Claimed here so they never fall through to
                # the generic IB branch, which would mislabel/double-count them.
                dclass = spec["device_class"]
                for port in ports.iterdir():
                    nic_name = f"{nic.name}:{port.name}"
                    hw = port / "hw_counters"

                    for dest, group in (
                        (self.__hw_shared_data_paths, spec.get("shared_counters", {})),
                        (self.__hw_extra_data_paths, spec.get("extra_counters", {})),
                    ):
                        for name, counters in group.items():
                            paths = [hw / c for c in counters]
                            if paths[0].is_file() and paths[0].stat().st_size > 0:
                                dest.setdefault(name, {})[nic_name] = (dclass, paths)
            else:
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

        rx_data_paths = [
            self.__net_rx_data_paths,
            self.__cxi_rx_data_paths,
            self.__ib_rx_data_paths,
            self.__hw_shared_data_paths.get("rx_bytes", {}),
        ]
        num_rx = sum([len(x) for x in rx_data_paths])
        if num_rx > 0:
            logging.debug(self.__net_rx_data_paths)
            metric = self.__prefix + "rx_bytes"
            description = "Network received (bytes)"
            self.__rx_metric = Gauge(metric, description, labelnames=labels)
            logging.info(f"--> [registered] {metric} -> {description} (gauge)")

        tx_data_paths = [
            self.__net_tx_data_paths,
            self.__cxi_tx_data_paths,
            self.__ib_tx_data_paths,
            self.__hw_shared_data_paths.get("tx_bytes", {}),
        ]
        num_tx = sum([len(x) for x in tx_data_paths])
        if num_tx > 0:
            logging.debug(self.__net_tx_data_paths)
            metric = self.__prefix + "tx_bytes"
            description = "Network transmitted (bytes)"
            self.__tx_metric = Gauge(metric, description, labelnames=labels)
            logging.info(f"--> [registered] {metric} -> {description} (gauge)")

        # shared_counters map to the pre-existing cross-class gauges above.
        self.__hw_shared_metrics = {"rx_bytes": self.__rx_metric, "tx_bytes": self.__tx_metric}

        # One gauge per extra_counter discovered on any hw_counter NIC, named
        # after the counter (e.g. rx_packets, retx_packets, out_of_sequence_packets).
        self.__hw_extra_metrics = {}
        for name in self.__hw_extra_data_paths:
            metric = self.__prefix + name
            description = f"Network {name.replace('_', ' ')}"
            self.__hw_extra_metrics[name] = Gauge(metric, description, labelnames=labels)
            logging.info(f"--> [registered] {metric} -> {description} (gauge)")

    def updateMetrics(self):
        """Update registered metrics of interest"""

        net_data = [
            (self.__net_rx_data_paths, self.__rx_metric),
            (self.__net_tx_data_paths, self.__tx_metric),
        ]

        for data_paths, metric in net_data:
            for nic, path in data_paths.items():
                try:
                    with open(path, "r") as f:
                        data = int(f.read().strip())
                        metric.labels(device_class="net", interface=nic).set(data)
                except:
                    pass

        cxi_data = [
            (self.__cxi_rx_data_paths, self.__rx_metric),
            (self.__cxi_tx_data_paths, self.__tx_metric),
        ]

        # For CXI, estimate lower bound of the total amount of bytes:
        # aggregate values from all buckets using the minimum packet size of
        # each bucket.
        for data_paths, metric in cxi_data:
            for nic, buckets in data_paths.items():
                total = 0
                for size, path in buckets.items():
                    try:
                        with open(path, "r") as f:
                            data = f.read().strip()
                            fields = data.split("@")
                            count = int(fields[0])
                            total += count * size
                    except:
                        pass
                metric.labels(device_class="cxi", interface=nic).set(total)

        ib_data = [
            (self.__ib_rx_data_paths, self.__rx_metric),
            (self.__ib_tx_data_paths, self.__tx_metric),
        ]

        for data_paths, metric in ib_data:
            for nic, path in data_paths.items():
                try:
                    with open(path, "r") as f:
                        data = int(f.read().strip())
                        # Counters for infiniband are reported as "octets divided by 4";
                        # multiply to collect the expected value in bytes.
                        metric.labels(device_class="infiniband", interface=nic).set(data * 4)
                except:
                    pass

        # hw_counter NICs (ainic, thor): hw_counters are already in bytes/packets
        # (no scaling); each metric sums its per-device counter list. device_class
        # travels with each interface's paths. shared_counters write the shared
        # rx/tx gauges; extra_counters write their own gauge.
        hw_metrics = [
            (self.__hw_shared_data_paths, self.__hw_shared_metrics),
            (self.__hw_extra_data_paths, self.__hw_extra_metrics),
        ]

        for data_paths, metrics in hw_metrics:
            for name, interfaces in data_paths.items():
                metric = metrics[name]
                for nic, (dclass, paths) in interfaces.items():
                    total = 0
                    for path in paths:
                        try:
                            with open(path, "r") as f:
                                total += int(f.read().strip())
                        except:
                            pass
                    metric.labels(device_class=dclass, interface=nic).set(total)

        return
