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
# closing mirror collector_trace_kernel.py.

import configparser
import logging
import threading

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

        # Bin series; each owns its per-bin deltas and running totals.
        #   collective semantics (EXACT, no duration):
        #     key:   (gpu_id, collective, datatype, size_bucket, comm_size)
        #     value: [count, bytes]
        #   comm creation:
        #     key:   (gpu_id, nranks label)
        #     value: [count, init_ns]
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
        last_bin = self.__process()
        self.__freeze(last_bin, flush=False)

    def formatMetrics(self, label_defaults, flush=False):
        last_bin = self.__process()
        self.__freeze(last_bin, flush)
        collective_bins, collective_frozen = self.__collective_ts.pop_frozen()
        comm_bins, comm_frozen = self.__comm_ts.pop_frozen()
        return self.__format(collective_bins, collective_frozen, comm_bins, comm_frozen, label_defaults)

    def __freeze(self, last_bin, flush):
        """Freeze both series on the same cutoff."""
        cutoff = self._cutoff(last_bin, flush)
        self.__collective_ts.freeze(cutoff)
        self.__comm_ts.freeze(cutoff)

    def __process(self):
        """Drain staged records and accumulate them into per-bin deltas.

        Returns the most recent collective bin (ms).
        """
        # One clock read for both series, so their bins stay aligned.
        current_bin = self._current_bin()
        first_bin, last_bin = self._extend_bins(self.__collective_ts, current_bin)
        comm_first, comm_last = self._extend_bins(self.__comm_ts, current_bin)

        collectives = comms = []
        if self.__collectives or self.__comms:
            with self.__lock:
                collectives, self.__collectives = self.__collectives, []
                comms, self.__comms = self.__comms, []

        # 1) Comm CREATES: register comm -> nranks so collectives in this same
        # cycle can resolve their comm_size. Teardown ops are not collected;
        # see the module docstring. A late create still registers its nranks
        # above, so collectives keep resolving comm_size even when its own
        # count is dropped.
        for gpu_id, op, comm, nranks, h_start, h_end in comms:
            if op not in COMM_CREATE_OPS:
                continue
            self.__comm_nranks[comm] = nranks
            end_bin = self._bin_for(h_end)
            if not self._in_window(end_bin, comm_first, comm_last):
                self._late_records += 1
                continue
            delta = self.__comm_ts.bins[end_bin][(gpu_id, self._intern(nranks_label(nranks)))]
            delta[0] += 1
            delta[1] += h_end - h_start

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
            delta = self.__collective_ts.bins[end_bin][key]
            delta[0] += 1
            delta[1] += count * dsize

        return last_bin

    def __format(self, collective_bins, collective_running, comm_bins, comm_running, label_defaults):
        # Bins hold per-interval counts; cumulative values are formed here, from
        # where the last push left off.
        for interval_bin, keys in collective_bins:
            for key, delta in keys.items():
                gpu_id, collective, dtype, sbucket, comm_size = key
                total = collective_running[key]
                total[0] += delta[0]
                total[1] += delta[1]
                labels = f'{label_defaults},card="{gpu_id}",collective="{collective}",datatype="{dtype}",size_bucket="{sbucket}",comm_size="{comm_size}"'
                yield f"omnistat_rccl_collective_count{{{labels}}} {total[0]} {interval_bin}".encode()
                yield b"\n"
                yield f"omnistat_rccl_collective_total_bytes{{{labels}}} {total[1]} {interval_bin}".encode()
                yield b"\n"
            # The collective series always has bins, so a drop on either stream
            # is reported even in a cycle with no comm records.
            yield f"omnistat_rccl_late_records{{{label_defaults}}} {self._late_records} {interval_bin}".encode()
            yield b"\n"

        for interval_bin, keys in comm_bins:
            for key, delta in keys.items():
                gpu_id, nranks = key
                total = comm_running[key]
                total[0] += delta[0]
                total[1] += delta[1]
                labels = f'{label_defaults},card="{gpu_id}",nranks="{nranks}"'
                yield f"omnistat_rccl_comm_created_count{{{labels}}} {total[0]} {interval_bin}".encode()
                yield b"\n"
                yield f"omnistat_rccl_comm_init_total_duration_ns{{{labels}}} {total[1]} {interval_bin}".encode()
                yield b"\n"
