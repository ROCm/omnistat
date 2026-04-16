"""ValidationMixin — data collection completeness checks."""

import numpy as np

from .stats_utils import distribution_summary


class ValidationMixin:
    """Mixin providing check_data_collection."""

    def check_data_collection(self, interval):
        """Validate data collection completeness and timing.

        Analyzes node activation/deactivation stagger, sampling gaps,
        missing nodes, and per-node reporting duration.

        Uses _auto_step (finest safe resolution) rather than _health_step
        so that short gaps and sub-minute stagger are not masked by a
        coarse query step.
        """
        job_filter = f'jobid="{self.jobid}", jobstep=~".*"'
        step = self._auto_step()

        promql_rms = f"rmsjob_info{{{job_filter}}}"
        results_rms = self.query_range(promql_rms, self.start_time, self.end_time, step)

        # Extract per-node timing information
        first_timestamps = []
        last_timestamps = []
        durations = []
        actual_hosts = set()
        all_gap_durations = []
        all_gap_offsets = []  # offset from earliest first_timestamp
        per_node_gaps = []

        for r in results_rms:
            m = r.get("metric", {})
            host = m.get("instance", "unknown")
            actual_hosts.add(host)

            timestamps = [v[0] for v in r.get("values", [])]
            if not timestamps:
                continue

            first_ts = timestamps[0]
            last_ts = timestamps[-1]
            first_timestamps.append(first_ts)
            last_timestamps.append(last_ts)
            durations.append(last_ts - first_ts)

            # Detect gaps (intervals > 3x the step)
            if len(timestamps) > 1:
                diffs = np.diff(timestamps)
                expected_step = float(step)
                gap_threshold = expected_step * 3
                gaps = [(i, float(d)) for i, d in enumerate(diffs) if d > gap_threshold]
                if gaps:
                    gap_durs = [g[1] for g in gaps]
                    gap_offsets = [timestamps[g[0]] for g in gaps]
                    all_gap_durations.extend(gap_durs)
                    all_gap_offsets.extend(gap_offsets)
                    per_node_gaps.append(
                        {
                            "instance": host,
                            "num_gaps": len(gaps),
                            "max_gap_seconds": round(max(gap_durs), 1),
                        }
                    )

        result = {"step_used": step}

        if not first_timestamps:
            result["reporting_nodes"] = 0
            return result

        result["reporting_nodes"] = len(actual_hosts)

        # --- Expected vs actual nodes ---
        nodes_label = None
        for r in results_rms:
            m = r.get("metric", {})
            nodes_label = m.get("nodes", None)
            if nodes_label:
                break

        expected_count = None
        if nodes_label:
            try:
                expected_count = int(nodes_label)
            except ValueError:
                pass

        result["expected_nodes"] = expected_count

        if expected_count is not None and len(actual_hosts) < expected_count:
            missing_hosts = []
            # Only list missing hosts if the count is manageable
            if expected_count - len(actual_hosts) <= 100:
                # We can't enumerate missing hosts without knowing the full set
                pass
            result["missing_nodes"] = {
                "expected": expected_count,
                "actual": len(actual_hosts),
                "missing_count": expected_count - len(actual_hosts),
            }

        # --- Activation stagger ---
        earliest = min(first_timestamps)
        activation_offsets = [ts - earliest for ts in first_timestamps]
        spread = max(first_timestamps) - earliest
        result["activation"] = {
            "spread_seconds": round(spread, 2),
            "stats": distribution_summary(activation_offsets),
        }

        # --- Deactivation stagger ---
        latest = max(last_timestamps)
        deactivation_offsets = [latest - ts for ts in last_timestamps]
        deact_spread = latest - min(last_timestamps)
        result["deactivation"] = {
            "spread_seconds": round(deact_spread, 2),
            "stats": distribution_summary(deactivation_offsets),
        }

        # --- Reporting duration ---
        result["reporting_duration"] = {
            "stats": distribution_summary(durations),
        }

        # --- Sampling gaps summary ---
        per_node_gaps.sort(key=lambda x: x["max_gap_seconds"], reverse=True)

        gap_result = {
            "nodes_with_gaps": len(per_node_gaps),
            "total_gaps": len(all_gap_durations),
        }

        if all_gap_durations:
            gap_result["gap_duration_stats"] = distribution_summary(all_gap_durations)
            # Compute gap timing relative to earliest first_timestamp
            gap_offsets_relative = [t - earliest for t in all_gap_offsets]
            gap_result["gap_timing"] = {
                "earliest_offset_seconds": round(min(gap_offsets_relative), 2),
                "latest_offset_seconds": round(max(gap_offsets_relative), 2),
                "median_offset_seconds": round(float(np.median(gap_offsets_relative)), 2),
            }
        else:
            gap_result["gap_duration_stats"] = None
            gap_result["gap_timing"] = None

        gap_result["per_node"] = per_node_gaps
        result["sampling_gaps"] = gap_result

        return result
