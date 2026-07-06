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

"""Iterations module: training-iteration detection and per-iteration stats.

Built on the :class:`~omnistat.inspect.job.core.Module` base so it
self-serializes and caches like the report modules. Distinct threshold
parameterizations cache separately via the declared ``param_defaults``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from omnistat.inspect.constants import (
    DEFAULT_ITER_HIGH_THRESHOLD,
    DEFAULT_ITER_LOW_THRESHOLD,
    DEFAULT_ITER_METRIC,
    DEFAULT_ITER_MIN_IDLE_SECONDS,
    DEFAULT_ITER_MIN_ITERATION_SECONDS,
)
from omnistat.inspect.helpers import compute_step
from omnistat.inspect.job.core import Module
from omnistat.inspect.series import SeriesSet


class Iterations(Module):
    name = "iterations"
    param_defaults = {
        "metric": DEFAULT_ITER_METRIC,
        "low_threshold": DEFAULT_ITER_LOW_THRESHOLD,
        "high_threshold": DEFAULT_ITER_HIGH_THRESHOLD,
        "min_idle_seconds": DEFAULT_ITER_MIN_IDLE_SECONDS,
        "min_iteration_seconds": DEFAULT_ITER_MIN_ITERATION_SECONDS,
    }

    def build(self) -> dict:
        ds = self.ds
        cfg = self.p
        metric = cfg.metric
        low_threshold = cfg.low_threshold
        high_threshold = cfg.high_threshold
        min_idle_seconds = cfg.min_idle_seconds
        min_iteration_seconds = cfg.min_iteration_seconds

        step = ds.iteration_auto_step()
        thresholds = {
            "low": low_threshold,
            "high": high_threshold,
            "min_idle_seconds": min_idle_seconds,
        }

        # 1. Fetch averaged utilization across all GPUs
        series = SeriesSet(ds.job_query(metric, step, aggregate="avg"))
        timestamps, avg_util = series.raw_signal()

        if len(timestamps) == 0:
            return {
                "metric": metric,
                "step": str(step),
                "thresholds": thresholds,
                "num_iterations": 0,
                "iterations": [],
                "summary": None,
                "error": "No data returned for averaged utilization query",
            }

        # 2. Detect idle regions: contiguous spans where avg_util < low_threshold
        is_idle = avg_util < low_threshold
        idle_regions = self._idle_regions(is_idle, timestamps, min_idle_seconds)

        # 3. Derive iteration boundaries from gaps between idle regions
        iteration_bounds = self._iteration_bounds(idle_regions, len(timestamps))
        iteration_bounds = [
            (s, e) for s, e in iteration_bounds if (timestamps[e] - timestamps[s]) >= min_iteration_seconds
        ]

        # 4. Compute per-iteration statistics
        iterations: list[dict] = []
        for iter_num, (start_idx, end_idx) in enumerate(iteration_bounds, 1):
            window = self._iteration_window(
                ds, metric, step, timestamps, avg_util, start_idx, end_idx, iter_num, low_threshold, high_threshold
            )
            if window is not None:
                iterations.append(window)

        # 5. Compute summary
        summary = self._summary(iterations)

        return {
            "metric": metric,
            "step": str(step),
            "thresholds": thresholds,
            "num_iterations": len(iterations),
            "iterations": iterations,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # Detection helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _idle_regions(is_idle, timestamps, min_idle_seconds):
        idle_regions = []
        in_idle = False
        idle_start_idx = 0
        for i, idle in enumerate(is_idle):
            if idle and not in_idle:
                idle_start_idx = i
                in_idle = True
            elif not idle and in_idle:
                idle_end_idx = i - 1
                idle_duration = timestamps[idle_end_idx] - timestamps[idle_start_idx]
                if idle_duration >= min_idle_seconds:
                    idle_regions.append((idle_start_idx, idle_end_idx))
                in_idle = False
        if in_idle:
            idle_end_idx = len(timestamps) - 1
            idle_duration = timestamps[idle_end_idx] - timestamps[idle_start_idx]
            if idle_duration >= min_idle_seconds:
                idle_regions.append((idle_start_idx, idle_end_idx))
        return idle_regions

    @staticmethod
    def _iteration_bounds(idle_regions, n):
        iteration_bounds = []
        if not idle_regions:
            iteration_bounds.append((0, n - 1))
            return iteration_bounds
        if idle_regions[0][0] > 0:
            iteration_bounds.append((0, idle_regions[0][0] - 1))
        for i in range(len(idle_regions) - 1):
            start_idx = idle_regions[i][1] + 1
            end_idx = idle_regions[i + 1][0] - 1
            if start_idx <= end_idx:
                iteration_bounds.append((start_idx, end_idx))
        if idle_regions[-1][1] < n - 1:
            iteration_bounds.append((idle_regions[-1][1] + 1, n - 1))
        return iteration_bounds

    def _iteration_window(
        self, ds, metric, step, timestamps, avg_util, start_idx, end_idx, iter_num, low_threshold, high_threshold
    ) -> dict | None:
        iter_start_ts = float(timestamps[start_idx])
        iter_end_ts = float(timestamps[end_idx])
        duration = iter_end_ts - iter_start_ts
        if duration <= 0:
            return None

        iter_avg = avg_util[start_idx : end_idx + 1]

        iter_start_dt = datetime.fromtimestamp(iter_start_ts, tz=timezone.utc)
        iter_end_dt = datetime.fromtimestamp(iter_end_ts, tz=timezone.utc)
        iter_step = compute_step(duration, ds.sampling_interval)
        iter_series = SeriesSet(ds.job_query(metric, iter_step, start=iter_start_dt, end=iter_end_dt))

        gpu_integrals = []
        for _, vals in iter_series.per_series():
            if vals:
                gpu_integrals.append(sum(vals) * iter_step)
        utilization_integral = round(sum(gpu_integrals) / len(gpu_integrals), 1) if gpu_integrals else 0.0

        mean_utilization = round(utilization_integral / duration, 1) if duration > 0 else 0.0

        dips = 0
        above_high = False
        for val in iter_avg:
            if val > high_threshold:
                above_high = True
            elif val < low_threshold and above_high:
                dips += 1
                above_high = False

        below_20 = int(np.sum(iter_avg < 20))
        below_50 = int(np.sum(iter_avg < 50))
        above_80 = int(np.sum(iter_avg > 80))

        below_20_secs = round(below_20 * step, 1)
        below_50_secs = round(below_50 * step, 1)
        above_80_secs = round(above_80 * step, 1)

        return {
            "iteration": iter_num,
            "start_time": iter_start_dt.isoformat(),
            "end_time": iter_end_dt.isoformat(),
            "duration_seconds": round(duration, 1),
            "utilization_integral": utilization_integral,
            "mean_utilization": mean_utilization,
            "dips": dips,
            "time_below_20pct": {
                "seconds": below_20_secs,
                "percent": round(below_20_secs / duration * 100, 1) if duration > 0 else 0,
            },
            "time_below_50pct": {
                "seconds": below_50_secs,
                "percent": round(below_50_secs / duration * 100, 1) if duration > 0 else 0,
            },
            "time_above_80pct": {
                "seconds": above_80_secs,
                "percent": round(above_80_secs / duration * 100, 1) if duration > 0 else 0,
            },
        }

    @staticmethod
    def _summary(iterations: list[dict]) -> dict | None:
        if not iterations:
            return None
        durations = [it["duration_seconds"] for it in iterations]
        integrals = [it["utilization_integral"] for it in iterations]
        return {
            "mean_duration": round(float(np.mean(durations)), 1),
            "stddev_duration": round(float(np.std(durations)), 1),
            "mean_integral": round(float(np.mean(integrals)), 1),
            "duration_range": [round(min(durations), 1), round(max(durations), 1)],
        }
