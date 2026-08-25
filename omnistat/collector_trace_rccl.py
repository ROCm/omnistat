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

# RCCL trace endpoint collector.
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
# created-vs-destroyed cannot distinguish a communicator leak from an
# application that simply never calls destroy. It does NOT measure GPU
# execution time: the kernel-dispatch tracing + correlation-id join that
# produced group timing (group_duration_ns / group_dispatch_latency_ns) has
# been removed to keep collection minimal and avoid the ~13x wire volume and
# per-dispatch overhead of kernel-dispatch tracing.
#
# Time-series binning, boot->unix offset, string interning, and the hold-window
# release mirror collector_trace_kernel.py.

import configparser
import logging
import threading
import time
from collections import OrderedDict, defaultdict

import orjson
from flask import Flask, request

from omnistat.collector_trace_base import BinnedTraceCollector

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

# Communicator size (nranks) is reported EXACTLY, not binned.
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


class RcclTrace(BinnedTraceCollector):
    def __init__(self, config: configparser.ConfigParser, route: Flask.route, interval: float):
        logging.debug("Initializing RCCL trace collector")

        super().__init__(interval)

        # Raw staged records from POSTs, drained under lock during processing.
        self.__collectives = []
        self.__comms = []
        self.__lock = threading.Lock()

        # Global cumulative accumulators.
        #   collective semantics (EXACT, no duration):
        #     key:   (gpu_id, collective, datatype, size_bucket, comm_size)
        #     value: [count, bytes]
        self.__collective_values = defaultdict(lambda: [0, 0])
        #   comm init duration: gpu_id -> cumulative ns spent creating communicators
        self.__comm_init_ns = defaultdict(int)
        #   communicators created: gpu_id -> nranks label -> cumulative count
        self.__comm_created = defaultdict(lambda: defaultdict(int))

        # Time-series snapshot buffers (bin -> {key: snapshot}). One per family.
        self.__collective_ts = self._new_series()
        self.__comm_ts = self._new_series()

        # comm handle -> nranks, for the collective's comm_size lookup.
        self.__comm_nranks = {}

        # Explicit endpoint name: Flask derives the endpoint from the view
        # function's __name__, which would collide with KernelTrace.handleRequest
        # when both trace collectors are enabled. Give ours a unique name.
        route("/rccl_trace", methods=["POST"], endpoint="rccl_trace")(self.handleRequest)

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
        cutoff = self._cutoff(last_bin, flush)
        collective_bins = self._pop_bins(self.__collective_ts, cutoff)
        comm_bins = self._pop_bins(self.__comm_ts, cutoff)
        return self.__format(collective_bins, comm_bins, label_defaults)

    def __process(self):
        """Drain staged records, update accumulators, snapshot into bins.

        Returns the most recent collective bin (ms).
        """
        first_bin, last_bin = self._extend_bins(self.__collective_ts)
        comm_first, comm_last = self._extend_bins(self.__comm_ts)

        collectives = comms = []
        if self.__collectives or self.__comms:
            with self.__lock:
                collectives, self.__collectives = self.__collectives, []
                comms, self.__comms = self.__comms, []

        # 1) Comm CREATES: register comm -> nranks so collectives in this same
        # cycle can resolve their comm_size. Teardown ops are not collected --
        # see the module docstring.
        for gpu_id, op, comm, nranks, h_start, h_end in comms:
            if op not in COMM_CREATE_OPS:
                continue
            end_bin = self._bin_for(h_end)
            if not self._in_window(end_bin, comm_first, comm_last):
                self._late_records += 1
                continue
            self.__comm_init_ns[gpu_id] += h_end - h_start
            self.__comm_nranks[comm] = nranks
            self.__comm_created[gpu_id][self._intern(nranks_label(nranks))] += 1
            self.__snapshot_comm(gpu_id, end_bin)

        # 2) Collectives: record EXACT semantics (count, bytes). Enumeration
        # only — no GPU duration, which would require the kernel join.
        for gpu_id, op, count, dtype, comm, ts in collectives:
            end_bin = self._bin_for(ts)
            if not self._in_window(end_bin, first_bin, last_bin):
                self._late_records += 1
                continue
            collective = self._intern(collective_label(op))
            dname, dsize = NCCL_DTYPE.get(dtype, (f"dtype{dtype}", 0))
            sbucket = self._intern(size_bucket(count * dsize))
            key = (
                gpu_id,
                collective,
                self._intern(dname),
                sbucket,
                self._intern(nranks_label(self.__comm_nranks.get(comm))),
            )
            val = self.__collective_values[key]
            val[0] += 1
            val[1] += count * dsize
            self.__collective_ts[end_bin][key] = val[:]

        return last_bin

    def __snapshot_comm(self, gpu_id, end_bin):
        self.__comm_ts[end_bin][gpu_id] = (self.__comm_init_ns[gpu_id], dict(self.__comm_created[gpu_id]))

    def __format(self, collective_bins, comm_bins, label_defaults):
        # Collective semantics — EXACT (count, bytes). No duration here.
        for interval_bin, keys in collective_bins:
            for (gpu_id, collective, dtype, sbucket, comm_size), value in keys.items():
                labels = f'{label_defaults},card="{gpu_id}",collective="{collective}",datatype="{dtype}",size_bucket="{sbucket}",comm_size="{comm_size}"'
                yield f"omnistat_rccl_collective_count{{{labels}}} {value[0]} {interval_bin}".encode()
                yield b"\n"
                yield f"omnistat_rccl_collective_total_bytes{{{labels}}} {value[1]} {interval_bin}".encode()
                yield b"\n"
            yield f"omnistat_rccl_late_records{{{label_defaults}}} {self._late_records} {interval_bin}".encode()
            yield b"\n"

        for interval_bin, gpus in comm_bins:
            for gpu_id, (init_ns, created) in gpus.items():
                yield f'omnistat_rccl_comm_init_total_duration_ns{{{label_defaults},card="{gpu_id}"}} {init_ns} {interval_bin}'.encode()
                yield b"\n"
                for nranks, count in created.items():
                    yield f'omnistat_rccl_comm_created_count{{{label_defaults},card="{gpu_id}",nranks="{nranks}"}} {count} {interval_bin}'.encode()
                    yield b"\n"
