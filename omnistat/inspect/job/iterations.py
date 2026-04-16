"""IterationsMixin — training iteration detection and per-iteration stats."""

from datetime import datetime

import numpy as np


class IterationsMixin:
    """Mixin providing detect_iterations."""

    def detect_iterations(
        self,
        metric="rocm_utilization_percentage",
        low_threshold=20.0,
        high_threshold=70.0,
        min_idle_seconds=30.0,
        min_iteration_seconds=60.0,
    ):
        """Detect iteration boundaries and compute per-iteration statistics.

        Uses the averaged GPU utilization signal to find sustained idle periods
        (below low_threshold for at least min_idle_seconds), then computes
        per-iteration stats from the per-GPU data within each iteration window.

        Step selection uses _auto_step() for the averaged signal query.
        Per-iteration per-GPU queries use a finer step when the iteration
        window allows it.
        """
        job_filter = f'jobid="{self.jobid}", jobstep=~".*"'
        join = f"max by (instance) (rmsjob_info{{{job_filter}}})"
        step = self._auto_step()

        # 1. Fetch averaged utilization across all GPUs
        promql_avg = f"avg({metric} * on (instance) group_left() ({join}))"
        results = self.query_range(promql_avg, self.start_time, self.end_time, step)

        if not results or not results[0].get("values"):
            return {
                "metric": metric,
                "step": str(step),
                "thresholds": {
                    "low": low_threshold,
                    "high": high_threshold,
                    "min_idle_seconds": min_idle_seconds,
                },
                "num_iterations": 0,
                "iterations": [],
                "summary": None,
                "error": "No data returned for averaged utilization query",
            }

        # Parse the averaged signal
        raw_values = results[0]["values"]
        timestamps = np.array([float(v[0]) for v in raw_values])
        avg_util = np.array([float(v[1]) if v[1] != "NaN" else 0.0 for v in raw_values])

        # 2. Detect idle regions: contiguous spans where avg_util < low_threshold
        is_idle = avg_util < low_threshold

        # Find contiguous idle regions
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
        # Handle trailing idle region
        if in_idle:
            idle_end_idx = len(timestamps) - 1
            idle_duration = timestamps[idle_end_idx] - timestamps[idle_start_idx]
            if idle_duration >= min_idle_seconds:
                idle_regions.append((idle_start_idx, idle_end_idx))

        # 3. Derive iteration boundaries from gaps between idle regions
        # Iterations are the active regions between idle gaps
        iteration_bounds = []

        if not idle_regions:
            # No idle regions found — entire job is one iteration
            iteration_bounds.append((0, len(timestamps) - 1))
        else:
            # Before first idle region
            if idle_regions[0][0] > 0:
                iteration_bounds.append((0, idle_regions[0][0] - 1))
            # Between idle regions
            for i in range(len(idle_regions) - 1):
                start_idx = idle_regions[i][1] + 1
                end_idx = idle_regions[i + 1][0] - 1
                if start_idx <= end_idx:
                    iteration_bounds.append((start_idx, end_idx))
            # After last idle region
            if idle_regions[-1][1] < len(timestamps) - 1:
                iteration_bounds.append((idle_regions[-1][1] + 1, len(timestamps) - 1))

        # Filter out short segments
        iteration_bounds = [
            (s, e) for s, e in iteration_bounds if (timestamps[e] - timestamps[s]) >= min_iteration_seconds
        ]

        # 4. Compute per-iteration statistics
        iterations = []
        for iter_num, (start_idx, end_idx) in enumerate(iteration_bounds, 1):
            iter_start_ts = timestamps[start_idx]
            iter_end_ts = timestamps[end_idx]
            duration = iter_end_ts - iter_start_ts

            if duration <= 0:
                continue

            # Slice the averaged signal for this iteration
            iter_avg = avg_util[start_idx : end_idx + 1]

            # Fetch per-GPU data for this iteration window only.
            # Per-iteration queries can use a finer step since the window
            # is shorter than the full job (duration / 90000 is more lenient).
            iter_start_dt = datetime.fromtimestamp(iter_start_ts)
            iter_end_dt = datetime.fromtimestamp(iter_end_ts)
            max_points = 90000
            vm_iter_limit = duration / max_points
            if self.sampling_interval is not None:
                iter_step = max(self.sampling_interval, vm_iter_limit)
            else:
                iter_step = step
            promql_raw = f"{metric} * on (instance) group_left() ({join})"
            raw_results = self.query_range(promql_raw, iter_start_dt, iter_end_dt, iter_step)

            # Compute utilization integral from per-GPU data
            # integral = sum(values) * step / num_gpus
            if raw_results:
                gpu_integrals = []
                for r in raw_results:
                    vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
                    if vals:
                        gpu_integrals.append(sum(vals) * iter_step)
                if gpu_integrals:
                    utilization_integral = round(sum(gpu_integrals) / len(gpu_integrals), 1)
                else:
                    utilization_integral = 0.0
            else:
                utilization_integral = 0.0

            mean_utilization = round(utilization_integral / duration, 1) if duration > 0 else 0.0

            # Dip count: transitions from >high_threshold to <low_threshold
            dips = 0
            above_high = False
            for val in iter_avg:
                if val > high_threshold:
                    above_high = True
                elif val < low_threshold and above_high:
                    dips += 1
                    above_high = False

            # Time in utilization bands (from averaged signal)
            n_samples = len(iter_avg)
            below_20 = int(np.sum(iter_avg < 20))
            below_50 = int(np.sum(iter_avg < 50))
            above_80 = int(np.sum(iter_avg > 80))

            below_20_secs = round(below_20 * step, 1)
            below_50_secs = round(below_50 * step, 1)
            above_80_secs = round(above_80 * step, 1)

            iterations.append(
                {
                    "iteration": iter_num,
                    "start_time": datetime.utcfromtimestamp(iter_start_ts).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_time": datetime.utcfromtimestamp(iter_end_ts).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
            )

        # 5. Compute summary
        summary = None
        if iterations:
            durations = [it["duration_seconds"] for it in iterations]
            integrals = [it["utilization_integral"] for it in iterations]
            summary = {
                "mean_duration": round(float(np.mean(durations)), 1),
                "stddev_duration": round(float(np.std(durations)), 1),
                "mean_integral": round(float(np.mean(integrals)), 1),
                "duration_range": [round(min(durations), 1), round(max(durations), 1)],
            }

        return {
            "metric": metric,
            "step": str(step),
            "thresholds": {
                "low": low_threshold,
                "high": high_threshold,
                "min_idle_seconds": min_idle_seconds,
            },
            "num_iterations": len(iterations),
            "iterations": iterations,
            "summary": summary,
        }
