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

        # Files to check for AMD AI NIC (AINIC) devices, i.e. AMD Pensando
        # "ionic" NICs such as Pollara. These appear under
        # /sys/class/infiniband but do not expose the standard IB port
        # counters; byte totals live in hw_counters instead. Unicast +
        # multicast bytes feed the shared rx/tx metrics and capture both
        # point-to-point RDMA and GPU-collective (RCCL) throughput.
        self.__ainic_rx_data_paths = {}
        self.__ainic_tx_data_paths = {}

    def __is_ainic(self, nic):
        """Return True if an infiniband-class device is an AMD AI NIC (AINIC).

        AINICs (AMD Pensando "ionic" NICs, e.g. Pollara) use the "ionic"
        driver; detect them via the device's uevent (robust). Fall back to the
        sysfs signature of empty standard IB counters alongside populated
        hw_counters if the uevent is unreadable.
        """
        try:
            uevent = (nic / "device" / "uevent").read_text()
            for line in uevent.splitlines():
                if line == "DRIVER=ionic":
                    return True
            return False
        except OSError:
            pass

        # Fallback: no standard IB byte counters, but hw_counters present.
        for port in (nic / "ports").iterdir():
            counters = port / "counters" / "port_rcv_data"
            hw_counters = port / "hw_counters" / "rx_rdma_ucast_bytes"
            if not counters.is_file() and hw_counters.is_file():
                return True
        return False

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
        # AMD AI NICs ("ionic" driver, e.g. Pollara) also appear under as
        # Infiniband but expose bytes via hw_counters instead of the standard
        # IB counters, so they are detected and handled separately in the
        # loop below. Their unicast/multicast byte paths are indexed by
        # interface and port ID. For example, for Rx bandwidth:
        #   __ainic_rx_data_paths = {
        #       "ionic_0:1": [
        #           "/sys/class/infiniband/ionic_0/ports/1/hw_counters/rx_rdma_ucast_bytes",
        #           "/sys/class/infiniband/ionic_0/ports/1/hw_counters/rx_rdma_mcast_bytes",
        #       ]
        #   }
        ib_base_path = Path("/sys/class/infiniband")

        ib_nics = []
        if ib_base_path.is_dir():
            ib_nics = ib_base_path.iterdir()

        for nic in ib_nics:
            if not nic.is_dir():
                continue

            ports = nic / "ports"

            if self.__is_ainic(nic):
                for port in ports.iterdir():
                    nic_name = f"{nic.name}:{port.name}"
                    hw = port / "hw_counters"

                    rx_ucast = hw / "rx_rdma_ucast_bytes"
                    rx_mcast = hw / "rx_rdma_mcast_bytes"
                    if rx_ucast.is_file() and rx_ucast.stat().st_size > 0:
                        self.__ainic_rx_data_paths[nic_name] = [rx_ucast, rx_mcast]

                    tx_ucast = hw / "tx_rdma_ucast_bytes"
                    tx_mcast = hw / "tx_rdma_mcast_bytes"
                    if tx_ucast.is_file() and tx_ucast.stat().st_size > 0:
                        self.__ainic_tx_data_paths[nic_name] = [tx_ucast, tx_mcast]
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
            self.__ainic_rx_data_paths,
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
            self.__ainic_tx_data_paths,
        ]
        num_tx = sum([len(x) for x in tx_data_paths])
        if num_tx > 0:
            logging.debug(self.__net_tx_data_paths)
            metric = self.__prefix + "tx_bytes"
            description = "Network transmitted (bytes)"
            self.__tx_metric = Gauge(metric, description, labelnames=labels)
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

        # AINIC: hw_counters are already in bytes (no scaling, no timestamp).
        # rx/tx aggregate unicast + multicast.
        ainic_data = [
            (self.__ainic_rx_data_paths, self.__rx_metric),
            (self.__ainic_tx_data_paths, self.__tx_metric),
        ]

        for data_paths, metric in ainic_data:
            for nic, paths in data_paths.items():
                total = 0
                for path in paths:
                    try:
                        with open(path, "r") as f:
                            total += int(f.read().strip())
                    except:
                        pass
                metric.labels(device_class="ainic", interface=nic).set(total)

        return
