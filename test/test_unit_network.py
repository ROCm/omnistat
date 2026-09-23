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

"""Unit tests for the NETWORK collector, driven by a fake sysfs tree.

No NIC, no GPU and no ROCm are needed. Classes follow the sysfs trees they read:
net, then InfiniBand and the hw_counter NICs (ionic, bnxt_re) that share its tree
plus their background sampler, then CXI, and finally a node carrying several
device classes at once.
"""

import configparser
import functools
import time

import pytest
from prometheus_client import CollectorRegistry, Gauge

import omnistat.collector_network
from omnistat.collector_network import NETWORK

RX_BYTES = "omnistat_network_rx_bytes"
TX_BYTES = "omnistat_network_tx_bytes"

IONIC_SAMPLES_TOTAL = "omnistat_network_ionic_samples_total"


def build_net(root, nic="eth0", rx=0, tx=0):
    """Write /sys/class/net statistics for one interface."""
    statistics = root / nic / "statistics"
    statistics.mkdir(parents=True)
    (statistics / "rx_bytes").write_text(f"{rx}\n")
    (statistics / "tx_bytes").write_text(f"{tx}\n")


def build_infiniband(root, nic="mlx5_0", rx=0, tx=0):
    """Write standard IB port counters, in octets/4.

    A real port always exposes both, so both are always written. No uevent, so
    the device is not claimed as a hw_counter NIC.
    """
    counters = root / nic / "ports/1/counters"
    counters.mkdir(parents=True)
    (counters / "port_rcv_data").write_text(f"{rx}\n")
    (counters / "port_xmit_data").write_text(f"{tx}\n")


def build_hw_counters(root, driver, num_devices=2):
    """Write /sys/class/infiniband entries for a number of devices of one driver.

    Counter filenames come from the collector's own table, so the fixture cannot
    drift from the spec it is meant to exercise.
    """
    spec = NETWORK._HW_COUNTER_NICS[driver]
    counters = [c for group in ("shared_counters", "extra_counters") for names in spec[group].values() for c in names]

    value = 0
    for i in range(num_devices):
        device = root / f"{spec['device_class']}{i}"
        (device / "device").mkdir(parents=True)
        (device / "device/uevent").write_text(f"DRIVER={driver}\n")
        hw_counters = device / "ports/1/hw_counters"
        hw_counters.mkdir(parents=True)
        for counter in counters:
            value += 1
            (hw_counters / counter).write_text(f"{value}\n")


def build_cxi(root, nic="cxi0", buckets=()):
    """Write CXI binned telemetry, as (filename, "count@timestamp") pairs."""
    telemetry = root / nic / "device/telemetry"
    telemetry.mkdir(parents=True)
    for name, contents in buckets:
        (telemetry / name).write_text(contents)


@pytest.fixture
def sysfs(tmp_path):
    """Directory standing in for one /sys/class/* tree."""
    root = tmp_path / "class"
    root.mkdir()
    return root


@pytest.fixture
def read_passes(monkeypatch):
    """List recording the counters handed to each read_hw_counters call.

    One entry per pass over a device, so tests can tell how reads were grouped
    and when they happened.
    """
    passes = []
    read_hw_counters = NETWORK.read_hw_counters

    def record(self, counters):
        passes.append(counters)
        return read_hw_counters(self, counters)

    monkeypatch.setattr(NETWORK, "read_hw_counters", record)
    return passes


@pytest.fixture
def collector(tmp_path, monkeypatch):
    """Build a collector against fake sysfs dirs, with thread teardown.

    Returns (instance, scrape), where scrape() updates and returns the exported
    series keyed by (metric, device_class, interface).
    """
    started = []
    empty = tmp_path / "empty"
    empty.mkdir()

    def build(net=None, infiniband=None, cxi=None, interval=None):
        config = configparser.ConfigParser()
        if interval:
            config.add_section("omnistat.collectors.network")
            config["omnistat.collectors.network"]["ionic_sampling_interval"] = str(interval)

        instance = NETWORK(config)
        instance._NETWORK__net_dir = str(net or empty)
        instance._NETWORK__infiniband_dir = str(infiniband or empty)
        instance._NETWORK__cxi_dir = str(cxi or empty)

        registry = CollectorRegistry()
        monkeypatch.setattr(omnistat.collector_network, "Gauge", functools.partial(Gauge, registry=registry))
        instance.registerMetrics()
        started.append(instance)

        def scrape():
            instance.updateMetrics()
            return {
                (s.name, s.labels.get("device_class"), s.labels.get("interface")): s.value
                for m in registry.collect()
                for s in m.samples
            }

        return instance, scrape

    yield build

    for instance in started:
        instance._NETWORK__sampler_stop.set()
        if instance._NETWORK__sampler_thread:
            instance._NETWORK__sampler_thread.join(timeout=5)


class TestNet:
    """Standard NICs under /sys/class/net.

    Distinct logic: byte totals are published verbatim, and loopback is skipped.
    """

    def test_verbatim_and_loopback_skipped(self, sysfs, collector):
        """Counters are published unscaled, and lo never reports.

        Counting local IPC as network traffic would inflate every node's totals,
        and lo is present on every node.
        """
        build_net(sysfs, rx=10, tx=20)
        build_net(sysfs, nic="lo", rx=999, tx=999)
        _, scrape = collector(net=sysfs)
        values = scrape()

        assert values[(RX_BYTES, "net", "eth0")] == 10
        assert values[(TX_BYTES, "net", "eth0")] == 20
        assert not [key for key in values if key[2] == "lo"]


class TestInfiniband:
    """Standard IB NICs under /sys/class/infiniband/*/ports/*/counters.

    Distinct logic: counters are reported as octets divided by four, and devices
    claimed by the hw_counter branch must not also appear here.
    """

    def test_octet_scaling(self, sysfs, collector):
        """port_rcv_data and port_xmit_data are octets divided by 4."""
        build_infiniband(sysfs, rx=1000, tx=250)
        _, scrape = collector(infiniband=sysfs)
        values = scrape()

        assert values[(RX_BYTES, "infiniband", "mlx5_0:1")] == 4000
        assert values[(TX_BYTES, "infiniband", "mlx5_0:1")] == 1000

    def test_hw_counter_nics_excluded(self, sysfs, collector):
        """A device with a known driver is claimed by the hw_counter branch."""
        build_hw_counters(sysfs, driver="ionic", num_devices=1)
        build_infiniband(sysfs, nic="ionic0", rx=1000)  # add IB counters to the same device
        _, scrape = collector(infiniband=sysfs)

        classes = {key[1] for key in scrape() if key[1]}
        assert classes == {"ionic"}, f"ionic device also reported as {classes - {'ionic'}}"


class TestHwCounters:
    """RoCE NICs under /sys/class/infiniband/*/ports/*/hw_counters.

    Distinct logic: several counter files are summed into one metric, and reads
    are grouped per device because a read costs a firmware round trip.
    """

    # Sampling interval long enough that the ionic background thread cannot tick
    # before a test ends, so every read observed came from the caller instead.
    IONIC_SAMPLER_OFF = 3000

    def test_ionic_summed_per_interface(self, sysfs, collector):
        """rx/tx bytes are the sum of the unicast and multicast counters, per device.

        Only ionic can show this: bnxt_re's shared counters are single-file.
        The counter names are spelled out rather than read back from the
        collector's table, so changing that table has to be deliberate.
        """
        build_hw_counters(sysfs, driver="ionic")
        _, scrape = collector(infiniband=sysfs)
        values = scrape()

        for i in range(2):
            hw_counters = sysfs / f"ionic{i}/ports/1/hw_counters"
            for metric, counters in (
                (RX_BYTES, ("rx_rdma_ucast_bytes", "rx_rdma_mcast_bytes")),
                (TX_BYTES, ("tx_rdma_ucast_bytes", "tx_rdma_mcast_bytes")),
            ):
                expected = sum(int((hw_counters / c).read_text()) for c in counters)
                assert values[(metric, "ionic", f"ionic{i}:1")] == expected

    def test_ionic_read_once_per_device(self, sysfs, collector, read_passes):
        """Counters are read one device at a time, not once per metric.

        A read that misses the driver's 10ms lifespan cache costs a firmware
        round trip, so revisiting a device per metric multiplies them.
        """
        build_hw_counters(sysfs, driver="ionic", num_devices=4)
        collector(infiniband=sysfs, interval=self.IONIC_SAMPLER_OFF)

        assert len(read_passes) == 4, "expected one read pass per device"

    def test_ionic_not_read_on_scrape(self, sysfs, collector, read_passes):
        """A stalled read must never land on the scrape path.

        The scrape publishes the cache primed at registration instead, so both
        halves are asserted: data came out, and no device was touched to get it.
        """
        build_hw_counters(sysfs, driver="ionic", num_devices=4)
        _, scrape = collector(infiniband=sysfs, interval=self.IONIC_SAMPLER_OFF)

        primed = len(read_passes)
        values = scrape()
        assert len(read_passes) == primed, "ionic devices were read on the scrape path"
        assert values[(RX_BYTES, "ionic", "ionic0:1")] > 0, "nothing was published"

    def test_bnxt_inline(self, sysfs, collector):
        """Only ionic is sampled in the background; other NICs keep the inline path."""
        build_hw_counters(sysfs, driver="bnxt_en")
        instance, scrape = collector(infiniband=sysfs)
        values = scrape()

        assert instance._NETWORK__hw_counters_inline
        assert instance._NETWORK__sampler_thread is None
        assert not [key for key in values if key[0] == IONIC_SAMPLES_TOTAL]

        hw_counters = sysfs / "bnxt_re0/ports/1/hw_counters"
        for metric, counter in ((RX_BYTES, "rx_bytes"), (TX_BYTES, "tx_bytes")):
            assert values[(metric, "bnxt_re", "bnxt_re0:1")] == int((hw_counters / counter).read_text())


class TestBackgroundSampler:
    """The thread that keeps ionic hw_counters off the scrape path."""

    def test_publishes_new_values(self, sysfs, collector):
        """The thread refreshes the cache; updateMetrics only publishes it."""
        build_hw_counters(sysfs, driver="ionic", num_devices=1)
        _, scrape = collector(infiniband=sysfs, interval=0.05)

        key = (RX_BYTES, "ionic", "ionic0:1")
        samples = (IONIC_SAMPLES_TOTAL, None, None)
        assert scrape()[key] > 0

        hw_counters = sysfs / "ionic0/ports/1/hw_counters"
        (hw_counters / "rx_rdma_ucast_bytes").write_text("900000\n")
        (hw_counters / "rx_rdma_mcast_bytes").write_text("99\n")
        before = scrape()[samples]

        # Exit on the published value rather than a fixed number of passes, so the
        # test never has to know which pass was in flight when the files changed.
        # The sample count rides in the same condition rather than a following
        # assert, because the sampler increments it one statement after publishing
        # the cache, which a post-loop assert could observe too early.
        deadline = time.monotonic() + 10
        while True:
            values = scrape()
            if values[key] == 900099 and values[samples] > before:
                break
            assert time.monotonic() < deadline, "sampler did not publish the new values and count a pass"
            time.sleep(0.01)

    def test_daemon_thread_stops_on_request(self, sysfs, collector):
        """A non-daemon thread would block interpreter shutdown."""
        build_hw_counters(sysfs, driver="ionic", num_devices=1)
        instance, _ = collector(infiniband=sysfs, interval=0.05)

        thread = instance._NETWORK__sampler_thread
        assert thread.daemon and thread.is_alive()

        instance._NETWORK__sampler_stop.set()
        thread.join(timeout=5)
        assert not thread.is_alive(), "sampler thread ignored the stop request"

    @pytest.mark.parametrize(
        "mode,interval,expected",
        [
            ("user", "1", 1.0),  # floored
            ("user", "5", 2.5),  # half the collection interval
            ("user", "30", 15.0),  # no ceiling
            ("system", "Unknown", 5.0),  # Prometheus owns the scrape interval
        ],
    )
    def test_interval(self, mode, interval, expected):
        config = configparser.ConfigParser()
        config.add_section("omnistat.internal")
        config["omnistat.internal"]["mode"] = mode
        config["omnistat.internal"]["interval_secs"] = interval

        assert NETWORK(config)._NETWORK__sampler_interval == expected

    @pytest.mark.parametrize("override,expected", [("0.25", 0.25), ("0", 5.0), ("-1", 5.0)])
    def test_interval_override(self, override, expected):
        """A positive value wins; anything else falls back to the derived interval."""
        config = configparser.ConfigParser()
        config.add_section("omnistat.collectors.network")
        config["omnistat.collectors.network"]["ionic_sampling_interval"] = override

        assert NETWORK(config)._NETWORK__sampler_interval == expected


class TestCxi:
    """Slingshot NICs under /sys/class/cxi.

    Distinct logic: traffic is reported in per-packet-size buckets, so bytes are
    estimated by weighting each bucket count by the size encoded in its filename.
    """

    def test_bucket_weighting(self, sysfs, collector):
        """Bytes are a lower bound: each bucket counts at its smallest packet size.

        Bucket names carry that size, including ranges (36_to_63) and the open
        ended top bucket (8192_to_max). Values are "count@timestamp", and files
        that do not match the pattern contribute nothing.
        """
        build_cxi(
            sysfs,
            buckets=[
                ("hni_rx_ok_64", "10@12345"),
                ("hni_rx_ok_36_to_63", "7@12345"),
                ("hni_rx_ok_8192_to_max", "2@12345"),
                ("hni_tx_ok_27", "5@12345"),
                ("hni_rx_err_64", "99@12345"),  # wrong counter kind
                ("hni_rx_ok_bogus", "99@12345"),  # size not parseable
            ],
        )
        _, scrape = collector(cxi=sysfs)
        values = scrape()

        assert values[(RX_BYTES, "cxi", "cxi0")] == 10 * 64 + 7 * 36 + 2 * 8192
        assert values[(TX_BYTES, "cxi", "cxi0")] == 5 * 27


class TestMixedNode:
    """A node carrying several device classes at once.

    rx_bytes and tx_bytes are two gauges shared by all four branches, separated
    only by the device_class label, so one class must never displace another.
    """

    @pytest.fixture
    def mixed(self, tmp_path, collector):
        """A node carrying every supported device class simultaneously."""
        net, infiniband, cxi = (tmp_path / name for name in ("net", "infiniband", "cxi"))
        for root in (net, infiniband, cxi):
            root.mkdir()

        build_net(net, rx=10, tx=20)
        build_infiniband(infiniband, nic="mlx5_0", rx=1000, tx=1000)
        build_hw_counters(infiniband, driver="ionic", num_devices=2)
        build_hw_counters(infiniband, driver="bnxt_en", num_devices=1)
        build_cxi(cxi, buckets=[("hni_rx_ok_64", "1@1"), ("hni_tx_ok_64", "1@1")])

        _, scrape = collector(net=net, infiniband=infiniband, cxi=cxi)
        return scrape()

    def test_every_class_reports_bytes(self, mixed):
        """All classes coexist on the shared gauges, and none displaces another."""
        for metric in (RX_BYTES, TX_BYTES):
            series = [key for key in mixed if key[0] == metric]
            assert {key[1] for key in series} == {"net", "infiniband", "ionic", "bnxt_re", "cxi"}
            assert len(series) == 6  # 1 net + 1 mlx5 + 2 ionic + 1 bnxt_re + 1 cxi

    def test_extra_metrics_only_for_hw_counter_nics(self, mixed):
        """Packet and congestion counters come from hw_counters, nowhere else."""
        for device_class in ("ionic", "bnxt_re"):
            extras = {key[0] for key in mixed if key[1] == device_class} - {RX_BYTES, TX_BYTES}
            assert "omnistat_network_rx_packets" in extras, f"{device_class} lost its packet counters"

        others = {key[0] for key in mixed if key[1] in ("net", "infiniband", "cxi")}
        assert others == {RX_BYTES, TX_BYTES}
