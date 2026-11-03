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

"""Stateless step-selection helpers.

:func:`compute_step` (finest safe step for an arbitrary window),
:func:`auto_step` (backend-aware finest step for the whole job), and
:func:`build_jobs_summary` (per-job rollup of ``rmsjob_info`` results, used by the
``db info`` command).
"""

from __future__ import annotations

from datetime import datetime, timezone

from omnistat.inspect.constants import VM_MAX_POINTS


def compute_step(duration: float, sampling_interval: float | None, max_points: int = VM_MAX_POINTS) -> float:
    """Compute the finest safe query step for a given window duration.

    Uses the sampling interval when known, otherwise falls back to
    ``max(vm_limit, 1.0)``. ``vm_limit`` keeps a range query under the
    VictoriaMetrics point cap.
    """
    vm_limit = duration / max_points
    if sampling_interval is not None:
        return max(sampling_interval, vm_limit)
    return max(vm_limit, 1.0)


def auto_step(duration: float, sampling_interval: float | None, backend: str = "tsdb") -> float:
    """Return the finest safe query step in seconds.

    For TSDB, respects VictoriaMetrics point limits. For CSV, uses the
    sampling interval directly (no point limits).
    """
    if backend == "csv":
        if sampling_interval is not None:
            return sampling_interval
        return 1.0
    return compute_step(duration, sampling_interval)


def build_jobs_summary(rmsjob_results: list[dict]) -> list[dict]:
    """Roll up ``rmsjob_info`` range-query results into per-job summary dicts.

    Each entry carries the job id, observed time range, approximate duration,
    node count, user, and partition. Returned sorted by job id.
    """
    jobs_data: dict = {}
    for r in rmsjob_results:
        m = r.get("metric", {})
        jobid = m.get("jobid", "unknown")
        vals = r.get("values", [])
        if not vals:
            continue
        if jobid not in jobs_data:
            jobs_data[jobid] = {
                "user": m.get("user", ""),
                "partition": m.get("partition", ""),
                "nodes_label": m.get("nodes", ""),
                "hosts": set(),
                "min_ts": float("inf"),
                "max_ts": float("-inf"),
            }
        host = m.get("instance", "unknown")
        jobs_data[jobid]["hosts"].add(host)

        ts_first = vals[0][0]
        ts_last = vals[-1][0]
        if ts_first < jobs_data[jobid]["min_ts"]:
            jobs_data[jobid]["min_ts"] = ts_first
        if ts_last > jobs_data[jobid]["max_ts"]:
            jobs_data[jobid]["max_ts"] = ts_last

    jobs = []
    for jobid in sorted(jobs_data):
        jd = jobs_data[jobid]
        start_dt = datetime.fromtimestamp(jd["min_ts"], tz=timezone.utc)
        end_dt = datetime.fromtimestamp(jd["max_ts"], tz=timezone.utc)
        duration_h = (jd["max_ts"] - jd["min_ts"]) / 3600
        jobs.append(
            {
                "jobid": jobid,
                "start_time": start_dt.isoformat(),
                "end_time": end_dt.isoformat(),
                "approximate_duration_hours": round(duration_h, 1),
                "num_nodes": len(jd["hosts"]),
                "user": jd["user"],
                "partition": jd["partition"],
            }
        )
    return jobs
