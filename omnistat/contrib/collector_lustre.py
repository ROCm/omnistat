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

Derives per-RPC latency from counters the Lustre client already keeps, without
privileges. See omnistat/contrib/README.md for the metrics and how to query
them.

Each OSC's `import` reports usecs_per_rpc, an integer cumulative MEAN over that
target's lifetime, uselessly damped on its own. Multiplied by the cumulative RPC
count from `rpc_stats` it recovers cumulative TOTAL service time, which is
additive and exact under differencing.

Totals accumulate PER TARGET as deltas into collector-owned counters rather than
republishing each OST's lifetime values. The in-flight histogram in `rpc_stats`
can also be made to yield a latency, but it samples queue occupancy at admission
rather than over time, and was measured overstating by ~100x on a mostly idle
client.

Sampling runs on a background thread: a sweep costs ~140 ms across 1350 OSTs,
too much for a sub-second poll loop. Safe because the counters are cumulative,
so a slower cadence costs time resolution rather than data.
"""

import configparser
import glob
import logging
import math
import os
import threading
import time

from prometheus_client import Gauge

from omnistat.collector_base import Collector

# The files read here are a few KB. A read that fills this buffer means the
# content was truncated, which would corrupt every derived value.
MAX_PROCFS_BYTES = 64 * 1024

OSC_DIR = "/proc/fs/lustre/osc"
LOV_DIR = "/proc/fs/lustre/lov"

# Counters accumulate in integer microseconds (exact) and convert only at
# export, since Omnistat metric names carry seconds.
USECS_PER_SECOND = 1_000_000

# Highest histogram bucket (OBD_HIST_MAX - 1). lprocfs_oh_tally() clamps any
# deeper queue into it, so this bucket counts ">= 31 in flight", not "== 31".
CONGESTED_DEPTH = 31

# OSTs outside every pool still need a label value; Prometheus has no null.
POOL_NONE = "none"

# Slot layout of the accumulated totals, which are kept per direction.
SERVICE, RPCS, CONGESTED, UNCERTAINTY = range(4)

DIRECTIONS = ("read", "write")


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
        section = "omnistat.collectors.contrib.lustre"
        if config.has_section(section):
            self.__sampling_interval = config[section].getfloat("sampling_interval", self.__sampling_interval)

        self.__targets = {}
        self.__cached = None
        self.__polling_lock = threading.Lock()
        self.__sampler_running = False
        self.__sampler_thread = None
        self.__warned_dead = False

        # Per-target counters from the previous sweep; deltas against these are
        # what accumulate.
        self.__previous = {}
        self.__sweeps = 0

        # Accumulated per (filesystem, pool) label group.
        self.__totals = {}

        # Liveness gate: the sampler can hang (procfs reads have no timeout) or
        # fail every cycle, and both freeze the counters. Only advanced when a
        # sweep actually read something, so a total read failure is visible
        # rather than looking like a healthy but idle filesystem.
        self.__samples = 0
        self.__collection_errors = 0
        self.__sweep_seconds = 0.0

    def __read(self, path):
        """Read a procfs file whole. Returns None on any error: a collector
        must not raise because one file misbehaved.

        Reads in a loop rather than once. A seq_file hands back at most one
        buffer per read, so a single os.read() silently truncates anything
        larger -- the LOV pool lists came back cut at one page, which
        mislabelled two thirds of the OSTs.
        """
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return None
        try:
            chunks = []
            total = 0
            while total < MAX_PROCFS_BYTES:
                chunk = os.read(fd, MAX_PROCFS_BYTES - total)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                total += len(chunk)
            # Hitting the cap means the file is larger than anything expected
            # here, so the content is suspect and every derived value with it.
            return None
        except OSError:
            return None
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def __pools(self):
        """Map OST index -> pool name, from the LOV pool membership lists.

        Derived from membership rather than an index threshold: on Orion the
        NVMe pool happens to start at index 900, but hardcoding that would
        silently mislabel every other site.
        """
        pools = {}
        for path in glob.glob(os.path.join(LOV_DIR, "*", "pools", "*")):
            pool = os.path.basename(path)
            buf = self.__read(path)
            if buf is None:
                continue
            for line in buf.split(b"\n"):
                pos = line.find(b"OST")
                if pos < 0:
                    continue
                try:
                    index = int(line[pos + 3 : pos + 7], 16)
                except ValueError:
                    continue
                # An OST in several pools keeps the first name seen, so the
                # label stays stable rather than flapping between sweeps.
                pools.setdefault(index, pool)
        return pools

    def __discover(self):
        """Map OSC device name -> (procfs directory, filesystem, pool).

        Keyed by the full device name, not the OST index: two mounted Lustre
        filesystems share the same index space, so an index key silently drops
        one of every colliding pair, and glob ordering decides which.
        """
        pools = self.__pools()
        targets = {}
        for path in glob.glob(os.path.join(OSC_DIR, "*/")):
            device = os.path.basename(path.rstrip("/"))
            # e.g. lfs01-OST0385-osc-ffff88d5855388 -> filesystem "lfs01", index 0x385
            marker = device.find("-OST")
            if marker < 0:
                continue
            try:
                index = int(device[marker + 4 : marker + 8], 16)
            except ValueError:
                continue
            targets[device] = (path, device[:marker], pools.get(index, POOL_NONE))
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

        Lustre <= 2.15.6 spells this `usec_per_rpc` (the label is interpolated
        from the counter's units string, changed by LU-15642), so both are
        accepted -- otherwise the metric reads a silent, errorless zero there.
        """
        block_start = buf.find(tag)
        if block_start < 0:
            return 0
        block_end = buf.find(b"_data_averages", block_start + len(tag))
        if block_end < 0:
            block_end = len(buf)
        for key in (b"usecs_per_rpc:", b"usec_per_rpc:"):
            field = buf.find(key, block_start, block_end)
            if field < 0:
                continue
            eol = buf.find(b"\n", field)
            if eol < 0:
                eol = len(buf)
            try:
                return int(buf[field + len(key) : eol])
            except ValueError:
                return 0
        return 0

    @staticmethod
    def __histogram(buf):
        """Cumulative (count, deep) per direction from `rpc_stats`:

               rpcs in flight        rpcs   % cum % |       rpcs   % cum %
               1:                   14762  51  51   |       1406  98  98
               2:                    5774  20  71   |         23   1  99
               fields[0]           fields[1] ... fields[4] fields[5]

        fields[4] is a literal '|' separating reads from writes. Bucket 0 is
        always empty: the tally happens after the in-flight count is
        incremented, so every RPC sees at least itself.

        Returns (count_read, deep_read, count_write, deep_write), or None if the
        section is missing.
        """
        section = buf.find(b"rpcs in flight")
        if section < 0:
            return None
        end = buf.find(b"\noffset", section)
        block = buf[buf.find(b"\n", section) + 1 : end if end > 0 else len(buf)]
        counts = [0, 0]
        deep = [0, 0]
        for line in block.split(b"\n"):
            fields = line.split()
            if len(fields) < 6 or not fields[0].endswith(b":"):
                continue
            try:
                depth = int(fields[0][:-1])
                values = (int(fields[1]), int(fields[5]))
            except ValueError:
                continue
            for i, value in enumerate(values):
                counts[i] += value
                if depth >= CONGESTED_DEPTH:
                    deep[i] += value
        return (counts[0], deep[0], counts[1], deep[1])

    @staticmethod
    def __idle_seconds(buf):
        """Seconds since this target last completed an RPC, from `import`:

               connection:
                  idle: 10 sec

        Returns -1 when absent or unparseable, so callers fail open and read
        the target rather than skipping it on a parse failure.
        """
        key = b"idle:"
        pos = buf.find(key)
        if pos < 0:
            return -1
        try:
            return int(buf[pos + len(key) : buf.find(b" sec", pos)])
        except ValueError:
            return -1

    def __accumulate(self, key, group, current):
        """Fold one target+direction's delta into the totals for its label group.

        key is (device, direction), group is (filesystem, pool, direction), and
        is that target's cumulative (service, rpcs, deep).
        """
        previous = self.__previous.get(key)
        if previous is None:
            # First sight of a target establishes a baseline and publishes
            # nothing, so one appearing late cannot inject its lifetime counter
            # as a burst of I/O that never happened.
            self.__previous[key] = current
            return

        delta_service = current[SERVICE] - previous[SERVICE]
        delta_rpcs = current[RPCS] - previous[RPCS]
        if delta_rpcs < 0:
            self.__previous[key] = current  # counters restarted; resync
            return
        if delta_service < 0:
            # mean*count reconstructs floor(S/c)*c, so the product dips by up to
            # c whenever the integer mean ticks down -- routine when recent I/O
            # beats the lifetime average. Hold the old baseline so the next sweep
            # spans both intervals and recovers the service time; advancing it
            # would discard that. A dip past the rounding bound is a real reset.
            if -delta_service > current[RPCS]:
                self.__previous[key] = current
            return

        totals = self.__totals.setdefault(group, [0, 0, 0, 0])
        totals[SERVICE] += delta_service
        totals[RPCS] += delta_rpcs
        totals[CONGESTED] += max(0, current[CONGESTED] - previous[CONGESTED])
        # Each endpoint of the reconstruction carries a residual in [0, c), so
        # one sweep is off by at most max(c_prev, c_now) microseconds. Summed in
        # QUADRATURE because those residuals are quasi-independent between
        # sweeps: the error grows as sqrt(N), and adding worst cases would
        # overstate it enough to call a usable window noise.
        totals[UNCERTAINTY] += max(previous[RPCS], current[RPCS]) ** 2
        self.__previous[key] = current

    def __sweep(self):
        """One pass over all OSTs, folding deltas into the running totals.

        Returns (targets_read, errors).
        """
        started = time.time()
        # `import` is read for every target anyway, so skipping the second
        # file for idle ones is free. Sound because an idle target completed no
        # RPC in the interval, making the delta it would contribute zero.
        idle_cutoff = self.__sampling_interval + 30
        read = 0
        errors = 0

        for device, (path, filesystem, pool) in self.__targets.items():
            try:
                buf = self.__read(path + "import")
                if buf is None:
                    errors += 1
                    continue

                # An idle target's counters cannot have moved, so skipping it
                # loses nothing: its delta would have been zero.
                if 0 <= idle_cutoff < self.__idle_seconds(buf):
                    continue

                usecs_read = self.__usecs(buf, b"read_data_averages")
                usecs_write = self.__usecs(buf, b"write_data_averages")

                buf = self.__read(path + "rpc_stats")
                if buf is None:
                    errors += 1
                    continue
                counts = self.__histogram(buf)
                if counts is None:
                    errors += 1
                    continue
                count_read, deep_read, count_write, deep_write = counts

                for direction, service, count, deep in (
                    ("read", usecs_read * count_read, count_read, deep_read),
                    ("write", usecs_write * count_write, count_write, deep_write),
                ):
                    self.__accumulate((device, direction), (filesystem, pool, direction), (service, count, deep))
                read += 1
            except (ValueError, IndexError, OSError):
                errors += 1

        self.__sweep_seconds = time.time() - started
        return read, errors

    def lustre_sampler(self, sample_interval: float):
        """Background thread accumulating Lustre counters.

        Args:
            sample_interval (float): Time in seconds between sweeps.
        """
        while self.__sampler_running:
            started = time.time()
            try:
                read, errors = self.__sweep()
                self.__sweeps += 1
                with self.__polling_lock:
                    self.__cached = {group: list(values) for group, values in self.__totals.items()}
                    self.__collection_errors += errors
                    # Only a sweep that read something counts as a sample. A
                    # pass where every read failed leaves the totals untouched,
                    # and must not look like a healthy idle filesystem.
                    if read:
                        self.__samples += 1
            except Exception as exc:  # never let the thread die
                logging.warning("Lustre sweep failed: %s" % exc)
            # Subtract the sweep so the period is the interval, not interval+sweep.
            time.sleep(max(0.0, sample_interval - (time.time() - started)))

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
        # nothing from being sampled more often than it is read. The idle cutoff
        # is derived from this effective value, not the configured one, or a
        # larger poll interval would silently invalidate the filter.
        try:
            poll_interval = float(self.__interval)
        except (TypeError, ValueError):
            poll_interval = 1.0
        self.__sampling_interval = max(self.__sampling_interval, poll_interval)

        pools = sorted({pool for _p, _f, pool in self.__targets.values()})
        filesystems = sorted({fs for _p, fs, _t in self.__targets.values()})
        logging.info("Lustre OSTs detected: %d" % len(self.__targets))
        logging.info("Lustre filesystems: %s" % ", ".join(filesystems))
        logging.info("Lustre pools: %s" % ", ".join(pools))
        logging.info("Lustre sampling thread interval: %.2f seconds" % self.__sampling_interval)

        # Data metrics carry labels deliberately: an unlabelled Gauge is
        # materialised at 0.0 when registered, so a slow first sweep would
        # publish zeros and then jump to the first real value, which rate()
        # renders as an enormous burst. With a label no child exists until set.
        # The collector's own counters stay unlabelled, where 0 is meaningful.
        self.__metrics = {}
        metrics = [
            (d + suffix, template % d, ("filesystem", "pool"))
            for d in DIRECTIONS
            for suffix, template in (
                ("_seconds", "Cumulative seconds bulk %s RPCs spent outstanding, since collector start"),
                ("_rpcs", "Cumulative bulk %s RPCs completed, since collector start"),
                (
                    "_congested_rpcs",
                    "Cumulative %s RPCs admitted with " + str(CONGESTED_DEPTH) + " or more already in "
                    "flight to the same target; also marks where queue-depth data is censored",
                ),
                (
                    "_uncertainty_seconds",
                    "Cumulative bound, in seconds, on the reconstruction error in %s_seconds; divide "
                    "by the matching _rpcs and compare against the derived latency",
                ),
            )
        ] + [
            ("samples_total", "Sweeps that read at least one target since startup", ()),
            (
                "collection_errors_total",
                "Cumulative procfs files the collector could not read "
                "(a monitoring failure, NOT a Lustre I/O error)",
                (),
            ),
            ("sweep_seconds", "Duration of the most recent sweep", ()),
        ]
        for name, description, labels in metrics:
            metric = self.__prefix + name
            self.__metrics[name] = Gauge(metric, description, labelnames=labels)
            logging.info("--> [registered] %s -> %s (gauge)" % (metric, description))

        # Initiate background sampler thread
        self.__sampler_running = True
        self.__sampler_thread = threading.Thread(
            target=self.lustre_sampler,
            args=(self.__sampling_interval,),
            daemon=True,
            name="lustre counter sampler",
        )
        self.__sampler_thread.start()

        # Two sweeps are needed before anything can be published: the first only
        # records a baseline, the second produces the first delta.
        deadline = time.time() + min(30.0, 3 * self.__sampling_interval)
        while self.__sweeps < 2 and time.time() < deadline:
            time.sleep(0.1)
        if self.__sweeps < 2:
            logging.warning("--> Lustre sampler has not completed two sweeps yet; metrics appear once it does")

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
        self.__metrics["sweep_seconds"].set(self.__sweep_seconds)

        if self.__sampler_thread and not self.__sampler_thread.is_alive() and not self.__warned_dead:
            logging.warning("Lustre sampler thread is no longer alive; values are frozen")
            self.__warned_dead = True

        if not cached:
            return

        for (filesystem, pool, direction), totals in cached.items():
            self.__metrics[direction + "_seconds"].labels(filesystem, pool).set(totals[SERVICE] / USECS_PER_SECOND)
            self.__metrics[direction + "_rpcs"].labels(filesystem, pool).set(totals[RPCS])
            self.__metrics[direction + "_congested_rpcs"].labels(filesystem, pool).set(totals[CONGESTED])
            self.__metrics[direction + "_uncertainty_seconds"].labels(filesystem, pool).set(
                math.sqrt(totals[UNCERTAINTY]) / USECS_PER_SECOND
            )
        return
