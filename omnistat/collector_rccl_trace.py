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

# RCCL trace endpoint collector (Tier 0+1: enumeration + comm lifecycle).
#
# Receives two parallel streams per flush from the rocprofiler-sdk trace library
# (positional arrays, POSTed to /rccl_trace):
#
#   collectives: [gpu_id, op, count, dtype, comm, ts]
#   comms:       [gpu_id, op, comm, nranks, h_start, h_end]   (creation only)
#
# The RCCL API call (collectives) carries the semantics (op/size/datatype). This
# variant records EXACT enumeration only — collective counts and logical bytes,
# plus communicator creation. Teardown (Destroy/Abort/Finalize) is not traced:
# those rows arrive late in process teardown and are frequently lost, and
# created-vs-destroyed could not distinguish a leak from an app that simply
# never called destroy. It does NOT measure GPU execution time: the
# kernel-dispatch tracing + correlation-id join that produced group timing (the
# "Tier 2" metrics group_duration_ns / group_dispatch_latency_ns) has been
# removed to keep collection minimal and avoid the ~13x wire volume and
# per-dispatch overhead of kernel-dispatch tracing.
#
# Time-series binning, boot->unix offset, string interning, and the hold-window
# release mirror collector_kernel_trace.py.

import configparser
import logging
import threading
import time
from collections import OrderedDict, defaultdict

import orjson
from flask import Flask, request

from omnistat.collector_endpoint_base import EndpointCollector

# ncclDataType_t enum -> (name, bytes-per-element). From rccl.h.
NCCL_DTYPE = {
    0: ("int8", 1),
    1: ("uint8", 1),
    2: ("int32", 4),
    3: ("uint32", 4),
    4: ("int64", 8),
    5: ("uint64", 8),
    6: ("float16", 2),
    7: ("float32", 4),
    8: ("float64", 8),
    9: ("bfloat16", 2),
    10: ("float8e4m3", 1),
    11: ("float8e5m2", 1),
}

# Message-size buckets (bytes), upper-inclusive. Coarse below 1MB, fine through
# the MB-GB training range. Starting default; retune against a real run.
SIZE_BUCKET_EDGES = [
    (4096, "4K"),
    (65536, "64K"),
    (262144, "256K"),
    (1048576, "1M"),
    (4194304, "4M"),
    (16777216, "16M"),
    (67108864, "64M"),
    (268435456, "256M"),
    (1073741824, "1G"),
    (4294967296, "4G"),
]
SIZE_BUCKET_OVERFLOW = "inf"

# Communicator size (nranks) is reported EXACTLY, not binned. RCCL traces are
# collected for user jobs (not system-wide), so the set of distinct
# communicator sizes is small and fixed (a natural, low-cardinality subset) —
# exact values are more useful than buckets and won't explode series count.
# "unknown" is used when the rank count is unavailable.
NRANKS_UNKNOWN = "unknown"

# RCCL comm-lifecycle op classification (op strings from rocprofiler).
COMM_CREATE_OPS = frozenset(
    ["ncclCommInitRank", "ncclCommInitAll", "ncclCommInitRankConfig", "ncclCommSplit", "ncclCommShrink"]
)


def size_bucket(nbytes):
    for edge, label in SIZE_BUCKET_EDGES:
        if nbytes <= edge:
            return label
    return SIZE_BUCKET_OVERFLOW


def nranks_label(nranks):
    """Exact communicator rank count as a label ('unknown' when unavailable)."""
    if nranks is None or nranks < 0:
        return NRANKS_UNKNOWN
    return str(nranks)


def collective_label(op):
    """Strip the 'nccl' prefix so 'ncclAllReduce' -> 'AllReduce'."""
    return op[4:] if op.startswith("nccl") else op


class RcclTrace(EndpointCollector):
    def __init__(self, config: configparser.ConfigParser, route: Flask.route, interval: float):
        logging.debug("Initializing RCCL trace collector")

        self.__interval_ms = max(1, int(interval * 1_000))
        self.__window_ms = 15_000

        # Raw staged records from POSTs, drained under lock during processing.
        self.__collectives = []
        self.__comms = []
        self.__lock = threading.Lock()

        # Global cumulative accumulators.
        #   collective semantics (EXACT, no duration):
        #     key:   (card, collective, datatype, size_bucket, comm_size)
        #     value: [count, bytes]
        self.__values = defaultdict(lambda: [0, 0])
        #   comm init duration: card -> cumulative ns spent creating communicators
        self.__comm_init_ns = defaultdict(int)
        #   communicators created: (card, nranks) -> cumulative count
        self.__created = defaultdict(int)

        # Records that reached the collector but whose time bin had already been
        # released (or is still in the future), so they could not be placed.
        self.__late_records = 0

        # Time-series snapshot buffers (bin -> {key: snapshot}). One per family.
        self.__ts = OrderedDict()  # collective semantics
        self.__comm_ts = OrderedDict()

        time_ms = time.time_ns() // 1_000_000
        current_bin = ((time_ms // self.__interval_ms) + 1) * self.__interval_ms
        self.__ts[current_bin] = {}
        self.__comm_ts[current_bin] = {}

        boot_time_ns = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        unix_time_ns = time.time_ns()
        self.__offset_ns = unix_time_ns - boot_time_ns

        # comm handle -> nranks, for the collective's comm_size lookup.
        self.__comm_nranks = {}

        # Interned label strings (op names, bucket labels) — small fixed sets,
        # but keeps yielded tuples sharing objects like KernelTrace does.
        self.__strings = {}

        # Explicit endpoint name: Flask derives the endpoint from the view
        # function's __name__, which would collide with KernelTrace.handleRequest
        # when both trace collectors are enabled. Give ours a unique name.
        route("/rccl_trace", methods=["POST"], endpoint="rccl_trace")(self.handleRequest)

    def __intern(self, s):
        ref = self.__strings.get(s)
        if ref is None:
            self.__strings[s] = s
            ref = s
        return ref

    def handleRequest(self):
        try:
            payload = orjson.loads(request.data)
            collectives = payload.get("collectives", [])
            comms = payload.get("comms", [])

            # Validate field counts up front so a malformed batch is atomic.
            for r in collectives:
                if len(r) != 6:
                    return "bad collective record", 400
            for r in comms:
                if len(r) != 6:
                    return "bad comm record", 400

            with self.__lock:
                self.__collectives.extend(collectives)
                self.__comms.extend(comms)
            return "", 204
        except Exception as e:
            return str(e), 400

    def updateMetrics(self):
        self.__process()
        return

    def formatMetrics(self, label_defaults, flush=False):
        last_bin = self.__process()
        cutoff = last_bin if flush else last_bin - self.__window_ms
        coll_bins = self.__pop_bins(self.__ts, cutoff)
        comm_bins = self.__pop_bins(self.__comm_ts, cutoff)
        return self.__format(coll_bins, comm_bins, label_defaults)

    def __pop_bins(self, ts, cutoff_bin):
        num_pop = 0
        for interval_bin in ts:
            if interval_bin > cutoff_bin:
                break
            num_pop += 1
        return [ts.popitem(last=False) for _ in range(num_pop)]

    def __extend_bins(self, ts, current_bin):
        if not ts:
            ts[current_bin] = {}
        last_bin = next(reversed(ts))
        for i in range(last_bin + self.__interval_ms, current_bin + 1, self.__interval_ms):
            ts[i] = {}
            last_bin = i
        return next(iter(ts)), last_bin

    def __bin_for(self, end_ns):
        end_ms = (end_ns + self.__offset_ns) // 1_000_000
        return ((end_ms // self.__interval_ms) * self.__interval_ms) + self.__interval_ms

    def __process(self):
        """Drain staged records, update accumulators, snapshot into bins.

        Returns the most recent collective bin (ms).
        """
        time_ms = time.time_ns() // 1_000_000
        current_bin = ((time_ms // self.__interval_ms) + 1) * self.__interval_ms

        first_bin, last_bin = self.__extend_bins(self.__ts, current_bin)
        self.__extend_bins(self.__comm_ts, current_bin)

        collectives = comms = []
        if self.__collectives or self.__comms:
            with self.__lock:
                collectives, self.__collectives = self.__collectives, []
                comms, self.__comms = self.__comms, []

        # 1) Comm CREATES: register comm -> nranks so collectives in this same
        # cycle can resolve their comm_size. Teardown ops are not collected --
        # see the module docstring.
        comm_first = next(iter(self.__comm_ts))
        comm_last = next(reversed(self.__comm_ts))
        for gpu_id, op, comm, nranks, h_start, h_end in comms:
            if op not in COMM_CREATE_OPS:
                continue
            end_bin = self.__bin_for(h_end)
            if end_bin < comm_first or end_bin > comm_last:
                self.__late_records += 1
                continue
            card = gpu_id
            self.__comm_init_ns[card] += h_end - h_start
            self.__comm_nranks[comm] = nranks
            self.__created[(card, self.__intern(nranks_label(nranks)))] += 1
            self.__snapshot_comm(card, end_bin)

        # 2) Collectives: record EXACT semantics (count, bytes). Enumeration only
        # — no GPU duration (that required the kernel join, removed in this
        # Tier 0+1 variant).
        for gpu_id, op, count, dtype, comm, ts in collectives:
            end_bin = self.__bin_for(ts)
            if end_bin < first_bin or end_bin > last_bin:
                self.__late_records += 1
                continue
            coll = self.__intern(collective_label(op))
            dname, dsize = NCCL_DTYPE.get(dtype, (f"dtype{dtype}", 0))
            sbucket = self.__intern(size_bucket(count * dsize))
            key = (
                gpu_id,
                coll,
                self.__intern(dname),
                sbucket,
                self.__intern(nranks_label(self.__comm_nranks.get(comm))),
            )
            val = self.__values[key]
            val[0] += 1
            val[1] += count * dsize
            self.__ts[end_bin][key] = val[:]

        return last_bin

    def __snapshot_comm(self, card, end_bin):
        created = {nb: cnt for (c, nb), cnt in self.__created.items() if c == card}
        self.__comm_ts[end_bin][card] = (self.__comm_init_ns[card], created)

    def __format(self, coll_bins, comm_bins, label_defaults):
        # Collective semantics — EXACT (count, bytes). No duration here.
        for interval_bin, keys in coll_bins:
            for (card, coll, dtype, sbucket, cbucket), value in keys.items():
                labels = f'{label_defaults},card="{card}",collective="{coll}",datatype="{dtype}",size_bucket="{sbucket}",comm_size="{cbucket}"'
                yield f"omnistat_rccl_collective_count{{{labels}}} {value[0]} {interval_bin}".encode()
                yield b"\n"
                yield f"omnistat_rccl_collective_total_bytes{{{labels}}} {value[1]} {interval_bin}".encode()
                yield b"\n"
            yield f"omnistat_rccl_late_records{{{label_defaults}}} {self.__late_records} {interval_bin}".encode()
            yield b"\n"

        for interval_bin, cards in comm_bins:
            for card, (init_ns, created) in cards.items():
                yield f'omnistat_rccl_comm_init_total_duration_ns{{{label_defaults},card="{card}"}} {init_ns} {interval_bin}'.encode()
                yield b"\n"
                for nb, cnt in created.items():
                    yield f'omnistat_rccl_comm_created_count{{{label_defaults},card="{card}",nranks="{nb}"}} {cnt} {interval_bin}'.encode()
                    yield b"\n"
