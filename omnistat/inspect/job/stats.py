"""StatsMixin — compute_stats and supporting gauge/counter computation."""

import numpy as np

from .stats_utils import make_group_key, stats_block, summarize_counter_groups, summarize_groups


class StatsMixin:
    """Mixin providing compute_stats, _compute_gauge_stats, _compute_counter_stats."""

    def compute_stats(self, metric, interval, level="global", category=None, percentiles=None):
        """Compute statistics at different aggregation levels.

        Uses CATEGORY_CONFIG for data-driven grouping. Automatically detects
        whether a metric is a counter (delta computation) or gauge (raw value
        aggregation).

        Args:
            metric: Metric name to compute stats for.
            interval: Query step interval in seconds.
            level: Aggregation level (valid levels depend on category).
            category: Metric category (auto-detected from metric if None).
            percentiles: List of percentile values to compute.
        """
        if percentiles is None:
            percentiles = [25, 50, 75, 95, 99]

        # Auto-detect category if not provided
        if category is None:
            category = self._detect_category(metric)
        if category is None or category not in self.CATEGORY_CONFIG:
            # Fallback to gpu category for unknown metrics
            category = "gpu"

        config = self.CATEGORY_CONFIG[category]
        valid_levels = config["levels"]

        if level not in valid_levels:
            valid = ", ".join(sorted(valid_levels.keys()))
            return {
                "error": f"Invalid level '{level}' for category '{category}'. Valid levels: {valid}",
                "metric": metric,
                "category": category,
            }

        group_by = valid_levels[level]
        is_counter = metric in self.COUNTER_METRICS

        job_filter = f'jobid="{self.jobid}", jobstep=~".*"'
        join = f"max by (instance) (rmsjob_info{{{job_filter}}})"
        promql = f"{self._metric_selector(metric)} * on (instance) group_left() ({join})"
        step = self._auto_step()
        results = self.query_range(promql, self.start_time, self.end_time, step)

        if not results:
            return {
                "metric": metric,
                "level": level,
                "category": category,
                "type": "counter" if is_counter else "gauge",
                "stats": [] if group_by else None,
                "step": str(step),
            }

        if is_counter:
            return self._compute_counter_stats(
                metric,
                results,
                level,
                category,
                group_by,
                step,
            )
        else:
            return self._compute_gauge_stats(
                metric,
                results,
                level,
                category,
                group_by,
                step,
                percentiles,
            )

    def _compute_gauge_stats(self, metric, results, level, category, group_by, interval, percentiles):
        """Compute gauge-style statistics (pool values, compute distribution)."""
        if not group_by:
            # Global aggregation
            all_vals = []
            for r in results:
                vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
                all_vals.extend(vals)
            stats = stats_block(np.array(all_vals), percentiles) if all_vals else None
            return {
                "metric": metric,
                "level": level,
                "category": category,
                "type": "gauge",
                "step": str(interval),
                "stats": stats,
            }

        # Grouped aggregation
        grouped = {}
        for r in results:
            m = r.get("metric", {})
            key = make_group_key(m, group_by)
            vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
            grouped.setdefault(key, []).extend(vals)

        stats = []
        for key in sorted(grouped):
            s = stats_block(np.array(grouped[key]), percentiles)
            if s:
                for label, value in zip(group_by, key):
                    s[label] = value
                stats.append(s)

        stats_output = summarize_groups(stats, group_by)

        return {
            "metric": metric,
            "level": level,
            "category": category,
            "type": "gauge",
            "step": str(interval),
            "stats": stats_output,
        }

    def _compute_counter_stats(self, metric, results, level, category, group_by, interval):
        """Compute counter-style statistics (per-series delta, then aggregate)."""
        duration = (self.end_time - self.start_time).total_seconds()

        if not group_by:
            # Global aggregation — compute per-series deltas, then aggregate
            deltas = []
            for r in results:
                vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
                if len(vals) >= 2:
                    deltas.append(vals[-1] - vals[0])
            if not deltas:
                return {
                    "metric": metric,
                    "level": level,
                    "category": category,
                    "type": "counter",
                    "step": str(interval),
                    "stats": None,
                }
            arr = np.array(deltas)
            total = round(float(np.sum(arr)), 4)
            stats = {
                "total_delta": total,
                "rate_per_second": round(total / duration, 4) if duration > 0 else 0,
                "num_series": len(deltas),
                "per_series_mean_delta": round(float(np.mean(arr)), 4),
                "per_series_min_delta": round(float(np.min(arr)), 4),
                "per_series_max_delta": round(float(np.max(arr)), 4),
                "per_series_stddev_delta": round(float(np.std(arr)), 4),
            }
            return {
                "metric": metric,
                "level": level,
                "category": category,
                "type": "counter",
                "step": str(interval),
                "stats": stats,
            }

        # Grouped aggregation — compute per-series deltas, group, then aggregate per group
        grouped_deltas = {}
        for r in results:
            m = r.get("metric", {})
            key = make_group_key(m, group_by)
            vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
            if len(vals) >= 2:
                grouped_deltas.setdefault(key, []).append(vals[-1] - vals[0])

        stats = []
        for key in sorted(grouped_deltas):
            arr = np.array(grouped_deltas[key])
            total = round(float(np.sum(arr)), 4)
            s = {
                "total_delta": total,
                "rate_per_second": round(total / duration, 4) if duration > 0 else 0,
                "num_series": len(arr),
                "per_series_mean_delta": round(float(np.mean(arr)), 4),
                "per_series_min_delta": round(float(np.min(arr)), 4),
                "per_series_max_delta": round(float(np.max(arr)), 4),
                "per_series_stddev_delta": round(float(np.std(arr)), 4),
            }
            for label, value in zip(group_by, key):
                s[label] = value
            stats.append(s)

        stats_output = summarize_counter_groups(stats, group_by)

        return {
            "metric": metric,
            "level": level,
            "category": category,
            "type": "counter",
            "step": str(interval),
            "stats": stats_output,
        }
