"""Pure computation functions for statistical analysis (no instance state)."""

import numpy as np


def stats_block(vals, percentiles=None):
    """Compute count/min/max/mean/stddev/percentiles for an array of values."""
    if percentiles is None:
        percentiles = [25, 50, 75, 95, 99]
    if len(vals) == 0:
        return None
    return {
        "count": len(vals),
        "min": round(float(np.min(vals)), 4),
        "max": round(float(np.max(vals)), 4),
        "mean": round(float(np.mean(vals)), 4),
        "stddev": round(float(np.std(vals)), 4),
        "percentiles": {f"p{p}": round(float(np.percentile(vals, p)), 4) for p in percentiles},
    }


def distribution_summary(values):
    """Compute min/max/mean/stddev/cv/percentiles for an array of floats."""
    arr = np.array(values, dtype=float)
    mean = float(np.mean(arr))
    stddev = float(np.std(arr))
    return {
        "min": round(float(np.min(arr)), 4),
        "max": round(float(np.max(arr)), 4),
        "mean": round(mean, 4),
        "stddev": round(stddev, 4),
        "cv": round(stddev / abs(mean), 4) if mean != 0 else 0.0,
        "percentiles": {
            "p5": round(float(np.percentile(arr, 5)), 4),
            "p25": round(float(np.percentile(arr, 25)), 4),
            "p50": round(float(np.percentile(arr, 50)), 4),
            "p75": round(float(np.percentile(arr, 75)), 4),
            "p95": round(float(np.percentile(arr, 95)), 4),
        },
    }


def detect_outliers(stats, field, group_by):
    """Detect outliers at 3-sigma on the given field. Returns list of outlier dicts."""
    values = [s[field] for s in stats if field in s]
    if len(values) < 3:
        return []
    arr = np.array(values, dtype=float)
    mean = float(np.mean(arr))
    stddev = float(np.std(arr))
    if stddev == 0:
        return []
    outliers = []
    for s in stats:
        if field not in s:
            continue
        z = (s[field] - mean) / stddev
        if abs(z) >= 3.0:
            entry = {"z_score": round(z, 2), field: s[field]}
            for label in group_by:
                if label in s:
                    entry[label] = s[label]
            outliers.append(entry)
    outliers.sort(key=lambda o: abs(o["z_score"]), reverse=True)
    return outliers


def summarize_groups(stats, group_by):
    """Summarize gauge per-group stats into a compact distribution + outliers."""
    if not stats:
        return {"num_groups": 0, "summary": {}, "outliers": []}

    summary = {}
    for field in ("mean", "min", "max"):
        values = [s[field] for s in stats if field in s]
        if values:
            summary[field] = distribution_summary(values)

    outliers = detect_outliers(stats, "mean", group_by)

    return {
        "num_groups": len(stats),
        "summary": summary,
        "outliers": outliers,
    }


def summarize_counter_groups(stats, group_by):
    """Summarize counter per-group stats into a compact distribution + outliers."""
    if not stats:
        return {"num_groups": 0, "aggregate_total_delta": 0, "summary": {}, "outliers": []}

    aggregate_total_delta = round(sum(s.get("total_delta", 0) for s in stats), 4)

    summary = {}
    for field in ("total_delta", "rate_per_second"):
        values = [s[field] for s in stats if field in s]
        if values:
            summary[field] = distribution_summary(values)

    outliers = detect_outliers(stats, "total_delta", group_by)

    return {
        "num_groups": len(stats),
        "aggregate_total_delta": aggregate_total_delta,
        "summary": summary,
        "outliers": outliers,
    }


def make_group_key(metric_labels, group_by):
    """Extract a group key tuple from series labels."""
    return tuple(metric_labels.get(label, "unknown") for label in group_by)
