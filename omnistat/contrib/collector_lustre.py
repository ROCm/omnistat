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

"""Lustre client-side monitoring

Reports per-RPC *server* service time: how long the storage servers take,
independent of how much the job asked for. Throughput cannot separate a
congested filesystem from an idle job; this can.

Exports cumulative counters -- {read,write}_service_seconds,
{read,write}_rpcs, samples_total and collection_errors_total -- so every query
is a rate or an increase:

    rate(omnistat_lustre_write_service_seconds[5m])
      / rate(omnistat_lustre_write_rpcs[5m])
      and on(instance) (increase(omnistat_lustre_samples_total[30s]) > 0)

giving seconds of server service time per RPC. See omnistat/contrib/README.md
for the metric table.

samples_total is the liveness gate. Omnistat pushes to VictoriaMetrics rather
than being scraped, so a dead collector's last values keep being served for
minutes, and an ungated latency query reports a plausible but wrong number that
whole time. The gate window must be >= ~2x sampling_interval.

Sampling runs on a background thread: one sweep costs ~50 ms across 1350 OSTs,
too much for a sub-second poll loop. Safe because the counters are cumulative:
a slower cadence loses no data, only time resolution.
"""

import configparser
import glob
import logging
import os
import threading
import time

from prometheus_client import Gauge

from omnistat.collector_base import Collector

# The files read here are a few KB. A read that fills this buffer means the
# content was truncated, which would corrupt every derived value.
MAX_PROCFS_BYTES = 64 * 1024

OSC_DIR = "/proc/fs/lustre/osc"

# Counters are accumulated in integer microseconds (exact) and converted only
# at export, since Omnistat metric names carry seconds.
USECS_PER_SECOND = 1_000_000


class Lustre(Collector):
    def __init__(self, config: configparser.ConfigParser):
        """Initialize the Lustre data collector.

        Args:
            config (configparser.ConfigParser): Cached copy of runtime configuration.
        """
        logging.debug("Initializing Lustre data collector")

        self.__prefix = "omnistat_lustre_"
        self.__disabled = True

        self.__interval = config["omnistat.internal"]["interval_secs"]

        # Background sweep cadence, independent of the poll interval. Slow by
        # default: counters are cumulative, so sampling faster adds no information.
        self.__sampling_interval = 10.0
        self.__use_idle_filter = True
        section = "omnistat.collectors.contrib.lustre"
        if config.has_section(section):
            self.__sampling_interval = config[section].getfloat("sampling_interval", self.__sampling_interval)
            self.__use_idle_filter = config[section].getboolean("idle_filter", self.__use_idle_filter)

        self.__targets = {}
        self.__cached = None
        self.__polling_lock = threading.Lock()
        self.__sampler_running = False
        self.__prev_totals = None

        # Liveness gate: the sampler can hang (procfs reads have no timeout) or
        # fail every cycle, and both freeze the counters. When this stops
        # advancing, the gated latency query suppresses itself.
        self.__samples = 0
        self.__collection_errors = 0  # cumulative files that could not be read

        # Last known per-target counters. A target skipped by the idle filter
        # still contributes these, or the exported sum would go backwards.
        # Exact: an idle target's counters cannot have changed.
        self.__last_counters = {}

        # The first sweep must be unfiltered: with no cache yet, skipped targets
        # contribute nothing, and each would later add its whole lifetime counter
        # at once -- which rate() reports as a burst of I/O that never happened.
        self.__primed = False

    def __read(self, path):
        """Read a procfs file whole. Returns None on any error: a collector
        must not raise because one file misbehaved."""
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return None
        try:
            data = os.read(fd, MAX_PROCFS_BYTES)
            # A full buffer means truncation, which would corrupt every delta.
            return None if len(data) == MAX_PROCFS_BYTES else data
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
        for path in glob.glob(os.path.join(OSC_DIR, "*/")):
            ost_pos = path.find("OST")
            if ost_pos < 0:
                continue
            try:
                targets[int(path[ost_pos + 3 : ost_pos + 7], 16)] = path
            except ValueError:
                pass
        return targets

    @staticmethod
    def __usecs(buf, tag):
        """usecs_per_rpc from a read/write_data_averages block of `import`:

               connection:
                  idle: 10 sec
               ...
               read_data_averages:
                  bytes_per_rpc: 7859386
                  usecs_per_rpc: 24420
                  MB_per_sec: 321.84
               write_data_averages:
                  bytes_per_rpc: 16070623
                  usecs_per_rpc: 4163
                  MB_per_sec: 3860.34

        The search is bounded by the next *_data_averages header rather than a
        byte count: the two blocks are only ~105 bytes apart, so any fixed
        window wide enough to cover one block also reaches into the next, and a
        read block missing its usecs_per_rpc line would silently return the
        write value.
        """
        block_start = buf.find(tag)
        if block_start < 0:
            return 0
        block_end = buf.find(b"_data_averages", block_start + len(tag))
        if block_end < 0:
            block_end = len(buf)
        key = b"usecs_per_rpc:"
        field = buf.find(key, block_start, block_end)
        if field < 0:
            return 0
        eol = buf.find(b"\n", field)
        if eol < 0:
            eol = len(buf)
        try:
            return int(buf[field + len(key) : eol])
        except ValueError:
            return 0

    def __sweep(self):
        """One pass over all OSTs.

        Returns ([usecs_read, count_read, usecs_write, count_write], errors),
        with the service times in integer microseconds.
        """
        idle_cutoff = (self.__sampling_interval + 30) if self.__use_idle_filter else None
        if not self.__primed:
            idle_cutoff = None  # full sweep, to populate __last_counters
        totals = [0, 0, 0, 0]
        errors = 0

        idle_key = b"idle:"

        for idx, target_dir in self.__targets.items():
            try:
                buf = self.__read(target_dir + "import")
                if buf is None:
                    errors += 1
                    self.__carry(totals, idx)
                    continue

                # `import` reports seconds since the last completed RPC:
                #        idle: 10 sec
                pos = buf.find(idle_key)
                try:
                    idle = int(buf[pos + len(idle_key) : buf.find(b" sec", pos)]) if pos >= 0 else -1
                except ValueError:
                    idle = -1

                # idle < 0 means unparseable: fail OPEN and read it anyway.
                if idle_cutoff is not None and 0 <= idle_cutoff < idle:
                    self.__carry(totals, idx)
                    continue

                usecs_read = self.__usecs(buf, b"read_data_averages")
                usecs_write = self.__usecs(buf, b"write_data_averages")

                buf = self.__read(target_dir + "rpc_stats")
                if buf is None:
                    errors += 1
                    self.__carry(totals, idx)
                    continue

                # `rpc_stats`, summed over all bins to give cumulative RPC
                # counts. Reads are the first column group, writes the second:
                #
                #     rpcs in flight        rpcs   % cum % |       rpcs   % cum %
                #     1:                   14762  51  51   |       1406  98  98
                #     2:                    5774  20  71   |         23   1  99
                #     fields[0]           fields[1] ... fields[4] fields[5]
                reads = writes = 0
                section = buf.find(b"rpcs in flight")
                if section >= 0:
                    section_end = buf.find(b"\noffset", section)
                    block = buf[buf.find(b"\n", section) + 1 : section_end if section_end > 0 else len(buf)]
                    for line in block.split(b"\n"):
                        fields = line.split()
                        # fields[4] is a literal '|' separating the two column groups
                        if len(fields) < 6 or not fields[0].endswith(b":"):
                            continue
                        reads += int(fields[1])
                        writes += int(fields[5])

                # usecs_per_rpc is a cumulative MEAN, uselessly damped alone; times
                # the count it recovers the cumulative TOTAL service time, which is
                # additive and so exact under differencing by rate().
                self.__last_counters[idx] = (usecs_read * reads, reads, usecs_write * writes, writes)
                self.__carry(totals, idx)

            except (ValueError, IndexError, OSError):
                errors += 1

        self.__primed = True
        return totals, errors

    def __carry(self, totals, idx):
        """Add a target's last known counters to the running totals. Skipped and
        unreadable targets contribute these, or the sum would go backwards and
        rate() would read it as a counter reset."""
        last = self.__last_counters.get(idx)
        if last:
            for i in range(4):
                totals[i] += last[i]

    def lustre_sampler(self, sample_interval: float):
        """Background thread caching Lustre counters.

        Args:
            sample_interval (float): Time in seconds between sweeps.
        """
        while self.__sampler_running:
            try:
                totals, errors = self.__sweep()
                # Cumulative: a decrease means a cache or filter bug, which
                # rate() would silently read as a counter reset.
                if self.__prev_totals and any(now < prev for now, prev in zip(totals, self.__prev_totals)):
                    logging.warning("Lustre totals decreased: %s -> %s" % (self.__prev_totals, totals))
                self.__prev_totals = list(totals)
                with self.__polling_lock:
                    self.__cached = totals
                    self.__samples += 1
                    self.__collection_errors += errors
            except Exception as exc:  # never let the thread die
                logging.warning("Lustre sweep failed: %s" % exc)
            time.sleep(sample_interval)

    def registerMetrics(self):
        """Register metrics of interest"""

        if not os.path.isdir(OSC_DIR):
            logging.warning("--> %s does not exist" % OSC_DIR)
            logging.warning("--> skipping Lustre data collection")
            return

        self.__targets = self.__discover()
        if not self.__targets:
            logging.warning("--> no Lustre OSTs found under %s" % OSC_DIR)
            logging.warning("--> skipping Lustre data collection")
            return

        # Never sweep faster than the poll interval; a cumulative counter gains
        # nothing from being sampled more often than it is read.
        try:
            poll_interval = float(self.__interval)
        except (TypeError, ValueError):
            poll_interval = 1.0
        interval = max(self.__sampling_interval, poll_interval)
        logging.info("Lustre OSTs detected: %d" % len(self.__targets))
        logging.info("Lustre sampling thread interval: %.2f seconds" % interval)
        logging.info("Lustre idle filter: %s" % ("enabled" if self.__use_idle_filter else "disabled"))

        self.__metrics = {}
        gauges = [
            ("read_service_seconds", "Cumulative server service time for read RPCs (seconds)"),
            ("write_service_seconds", "Cumulative server service time for write RPCs (seconds)"),
            ("read_rpcs", "Cumulative bulk read RPCs issued by this client"),
            ("write_rpcs", "Cumulative bulk write RPCs issued by this client"),
            ("samples_total", "Successful Lustre samples since startup"),
            (
                "collection_errors_total",
                "Cumulative procfs files the collector could not read "
                "(a monitoring failure, NOT a Lustre I/O error)",
            ),
        ]
        for name, description in gauges:
            metric = self.__prefix + name
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
        """Update metrics from the sampler thread's cached values"""

        if self.__disabled:
            return

        with self.__polling_lock:
            cached = self.__cached
            samples = self.__samples
            collection_errors = self.__collection_errors

        # Published even with no cached data, so a collector that never sampled
        # is visible rather than absent.
        self.__metrics["samples_total"].set(samples)
        self.__metrics["collection_errors_total"].set(collection_errors)

        if not self.__sampler_thread.is_alive() and not self.__warned_dead:
            logging.warning("Lustre sampler thread is no longer alive; values are frozen")
            self.__warned_dead = True

        if cached is None:
            return
        totals = cached

        self.__metrics["read_service_seconds"].set(totals[0] / USECS_PER_SECOND)
        self.__metrics["read_rpcs"].set(totals[1])
        self.__metrics["write_service_seconds"].set(totals[2] / USECS_PER_SECOND)
        self.__metrics["write_rpcs"].set(totals[3])
        return
