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

"""Lustre client-side monitoring

Reports per-RPC *server* service time: how long the storage servers take,
independent of how much the job asked for. Throughput cannot separate a
congested filesystem from an idle job; this can.

Metrics, all cumulative, so every query is a rate or an increase:

    omnistat_lustre_rpc_service_usecs{dir}   total server service time
    omnistat_lustre_rpc_count{dir}           total bulk RPCs
    omnistat_lustre_samples_total            liveness gate
    omnistat_lustre_collection_errors_total  procfs reads that failed -- a
                                             MONITORING failure, not Lustre I/O

Latency in ms, gated on the collector still sampling:

    (rate(omnistat_lustre_rpc_service_usecs{dir="write"}[5m])
       / rate(omnistat_lustre_rpc_count{dir="write"}[5m]) / 1000)
      and on(instance) (increase(omnistat_lustre_samples_total[30s]) > 0)

The gate is required. Omnistat pushes to VictoriaMetrics rather than being
scraped, so a dead collector's last values are served for ~5 minutes and the
ungated query reports a plausible but wrong latency for that whole time. The
gate window must be >= ~2x sampling_interval, or increase() lacks samples even
when healthy and the query blanks a working collector.

Source, per OST: usecs_per_rpc from `import` is an integer cumulative mean and
uselessly damped alone, but times the RPC count from `rpc_stats` it recovers the
cumulative total -- which is additive, so differencing it is exact.

Sampling runs on a background thread (pattern from collector_pm_counters.py):
one sweep costs ~50 ms across 1350 OSTs, too much for a sub-second poll loop.
Decoupling is safe because the counters are cumulative; a slower cadence loses
no data, only time resolution.

Set debug=True in [omnistat.collectors.lustre] for extra diagnostics.
"""

import configparser
import glob
import logging
import os
import threading
import time

from prometheus_client import Gauge

from omnistat.collector_base import Collector

BUF = 1 << 16


class Lustre(Collector):
    def __init__(self, config: configparser.ConfigParser):
        """Initialize the Lustre data collector.

        Args:
            config (configparser.ConfigParser): Cached copy of runtime configuration.
        """

        logging.debug("Initializing Lustre data collector")

        self.__prefix = "omnistat_lustre_"
        self.__osc_dir = "/proc/fs/lustre/osc"
        self.__disabled = True

        self.__interval = config["omnistat.internal"]["interval_secs"]

        # Background sweep cadence, independent of the poll interval. Slow by
        # default: counters are cumulative, so sampling faster adds no information.
        self.__sampling_interval = 10.0
        self.__use_idle_filter = True
        self.__debug = False
        section = "omnistat.collectors.lustre"
        if config.has_section(section):
            self.__sampling_interval = config[section].getfloat("sampling_interval", self.__sampling_interval)
            self.__use_idle_filter = config[section].getboolean("idle_filter", self.__use_idle_filter)
            self.__osc_dir = config[section].get("osc_dir", self.__osc_dir)
            self.__debug = config[section].getboolean("debug", False)

        self.__targets = {}
        self.__cached = None
        self.__polling_lock = threading.Lock()
        self.__sampler_running = False
        self.__prev_totals = None

        # samples_total is the liveness gate. The sampler can hang (procfs reads
        # have no timeout) or fail every cycle; both freeze the counters, which
        # downstream looks identical to an idle job. When it stops advancing,
        # increase() decays and the gated latency query suppresses itself.
        self.__samples = 0  # successful samples; the liveness gate
        self.__failures = 0  # samples that raised (debug only)
        self.__collection_errors = 0  # cumulative: files that could not be read

        # Last known per-target counters. A target skipped by the idle filter
        # still contributes these, or the exported sum would go backwards.
        # Exact: an idle target's counters cannot have changed.
        self.__last = {}

        # The first sweep must be unfiltered: with no cache yet, skipped targets
        # contribute nothing, and each would later add its whole lifetime counter
        # at once -- which rate() reports as a burst of I/O that never happened.
        self.__primed = False

    # ------------------------------------------------------------------ helpers

    def __read(self, path):
        """Read a procfs file whole. Returns None on any error: a collector
        must not raise because one file misbehaved."""
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return None
        try:
            data = os.read(fd, BUF)
            # A full buffer means truncation, which would corrupt every delta.
            return None if len(data) == BUF else data
        except OSError:
            return None
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def __discover(self):
        """Map OST index -> procfs directory."""
        targets = {}
        for path in glob.glob(os.path.join(self.__osc_dir, "*/")):
            i = path.find("OST")
            if i < 0:
                continue
            try:
                targets[int(path[i + 3 : i + 7], 16)] = path
            except ValueError:
                pass
        return targets

    @staticmethod
    def __usecs(buf, tag):
        """usecs_per_rpc from a read/write_data_averages block."""
        i = buf.find(tag)
        if i < 0:
            return 0
        j = buf.find(b"usecs_per_rpc:", i)
        if j < 0 or j - i > 200:
            return 0
        try:
            return int(buf[j + 14 : buf.find(b"\n", j)])
        except ValueError:
            return 0

    def __sweep(self):
        """One pass over all OSTs. Returns cumulative sums plus sweep metadata."""
        cut = (self.__sampling_interval + 30) if self.__use_idle_filter else None
        if not self.__primed:
            cut = None  # full sweep to populate __last
        totals = {"read": [0, 0], "write": [0, 0]}  # [usecs, count]
        active = {"read": 0, "write": 0}
        errs = 0
        nread = 0

        for idx, d in self.__targets.items():
            try:
                buf = self.__read(d + "import")
                if buf is None:
                    errs += 1
                    # fall through to the cached value below
                    cur = self.__last.get(idx)
                    if cur:
                        self.__accumulate(totals, cur)
                    continue

                i = buf.find(b"idle:")
                try:
                    idle = int(buf[i + 5 : buf.find(b" sec", i)]) if i >= 0 else -1
                except ValueError:
                    idle = -1

                # idle < 0 means unparseable: fail OPEN and read it anyway.
                if cut is not None and 0 <= cut < idle:
                    cur = self.__last.get(idx)
                    if cur:
                        self.__accumulate(totals, cur)
                    continue

                ur = self.__usecs(buf, b"read_data_averages")
                uw = self.__usecs(buf, b"write_data_averages")

                buf = self.__read(d + "rpc_stats")
                if buf is None:
                    errs += 1
                    cur = self.__last.get(idx)
                    if cur:
                        self.__accumulate(totals, cur)
                    continue
                nread += 1

                cr = cw = 0
                i = buf.find(b"rpcs in flight")
                if i >= 0:
                    j = buf.find(b"\noffset", i)
                    block = buf[buf.find(b"\n", i) + 1 : j if j > 0 else len(buf)]
                    for line in block.split(b"\n"):
                        f = line.split()
                        # read counts in f[1], write in f[5]; f[4] is a literal '|'
                        if len(f) < 6 or not f[0].endswith(b":"):
                            continue
                        cr += int(f[1])
                        cw += int(f[5])

                cur = (ur * cr, cr, uw * cw, cw)
                prev = self.__last.get(idx)
                if prev is not None:
                    if cur[1] > prev[1]:
                        active["read"] += 1
                    if cur[3] > prev[3]:
                        active["write"] += 1
                self.__last[idx] = cur
                self.__accumulate(totals, cur)

            except (ValueError, IndexError, OSError):
                errs += 1

        self.__primed = True
        return totals, active, errs, nread

    @staticmethod
    def __accumulate(totals, cur):
        totals["read"][0] += cur[0]
        totals["read"][1] += cur[1]
        totals["write"][0] += cur[2]
        totals["write"][1] += cur[3]

    def lustre_sampler(self, sample_interval: float):
        """Background thread caching Lustre counters.

        Args:
            sample_interval (float): Time in seconds between sweeps.
        """
        while self.__sampler_running:
            start = time.perf_counter()
            try:
                totals, active, errs, nread = self.__sweep()
                duration = time.perf_counter() - start
                # Cumulative: a decrease means a cache or filter bug, and rate()
                # would silently read it as a counter reset.
                if self.__prev_totals is not None:
                    for d in ("read", "write"):
                        for i, what in ((0, "usecs"), (1, "count")):
                            if totals[d][i] < self.__prev_totals[d][i]:
                                logging.warning(
                                    "Lustre %s %s total decreased (%d -> %d)"
                                    % (d, what, self.__prev_totals[d][i], totals[d][i])
                                )
                self.__prev_totals = {d: list(totals[d]) for d in totals}
                with self.__polling_lock:
                    self.__cached = (totals, active, errs, nread, duration)
                    self.__samples += 1
                    self.__collection_errors += errs
            except Exception as e:  # never let the thread die
                with self.__polling_lock:
                    self.__failures += 1
                logging.warning("Lustre sweep failed: %s" % e)
            time.sleep(sample_interval)

    # -- Required API

    def registerMetrics(self):
        """Register metrics of interest"""

        if not os.path.isdir(self.__osc_dir):
            logging.warning("--> %s does not exist" % self.__osc_dir)
            logging.warning("--> skipping Lustre data collection")
            return

        self.__targets = self.__discover()
        if not self.__targets:
            logging.warning("--> no Lustre OSTs found under %s" % self.__osc_dir)
            logging.warning("--> skipping Lustre data collection")
            return

        # Never sweep faster than the poll interval; a cumulative counter gains
        # nothing from being sampled more often than it is read.
        try:
            poll = float(self.__interval)
        except (TypeError, ValueError):
            poll = 1.0
        interval = max(self.__sampling_interval, poll)
        logging.info("Lustre OSTs detected: %d" % len(self.__targets))
        logging.info("Lustre sampling thread interval: %.2f seconds" % interval)
        logging.info("Lustre idle filter: %s" % ("enabled" if self.__use_idle_filter else "disabled"))

        self.__metrics = {}
        gauges = [
            ("rpc_service_usecs", "Cumulative RPC service time reported by servers (usecs)", ["dir"]),
            ("rpc_count", "Cumulative bulk RPCs issued by this client", ["dir"]),
            ("samples_total", "Successful Lustre samples since startup", None),
            (
                "collection_errors_total",
                "Cumulative procfs files the collector could not read "
                "(a monitoring failure, NOT a Lustre I/O error)",
                None,
            ),
        ]
        if self.__debug:
            gauges += [
                ("active_targets", "OSTs with new bulk RPCs in the last sample", ["dir"]),
                ("sample_duration_seconds", "Duration of the last sample (secs)", None),
                ("targets_read", "OSTs whose rpc_stats was read in the last sample", None),
                ("sample_failures_total", "Samples that raised an exception", None),
            ]
        for name, description, labels in gauges:
            metric = self.__prefix + name
            if labels:
                self.__metrics[name] = Gauge(metric, description, labelnames=labels)
            else:
                self.__metrics[name] = Gauge(metric, description)
            logging.info("--> [registered] %s -> %s (gauge)" % (metric, description))

        # Initiate background sampler thread
        self.__sampler_running = True
        self.__sampler_thread = threading.Thread(
            target=self.lustre_sampler,
            args=(interval,),
            daemon=True,
            name="lustre counter sampler",
        )
        self.__warned_dead = False
        self.__sampler_thread.start()

        # Prime the cache so the first scrape has data rather than a gap.
        deadline = time.time() + 10.0
        while self.__cached is None and time.time() < deadline:
            time.sleep(0.1)
        if self.__cached is None:
            logging.warning("--> Lustre sampler produced no data within 10s")

        self.__disabled = False
        return

    def updateMetrics(self):
        """Update metrics using cached data from the sampler thread"""

        if self.__disabled:
            return

        with self.__polling_lock:
            cached = self.__cached
            samples, failures = self.__samples, self.__failures
            coll_errs = self.__collection_errors

        # Published even with no cached data, so a collector that never sampled
        # is visible rather than absent.
        self.__metrics["samples_total"].set(samples)
        self.__metrics["collection_errors_total"].set(coll_errs)
        if self.__debug:
            self.__metrics["sample_failures_total"].set(failures)

        if not self.__sampler_thread.is_alive() and not self.__warned_dead:
            logging.warning("Lustre sampler thread is no longer alive; values are frozen")
            self.__warned_dead = True

        if cached is None:
            return
        totals, active, errs, nread, duration = cached

        for direction in ("read", "write"):
            self.__metrics["rpc_service_usecs"].labels(dir=direction).set(totals[direction][0])
            self.__metrics["rpc_count"].labels(dir=direction).set(totals[direction][1])
            if self.__debug:
                self.__metrics["active_targets"].labels(dir=direction).set(active[direction])
        if self.__debug:
            self.__metrics["sample_duration_seconds"].set(duration)
            self.__metrics["targets_read"].set(nread)
        return
