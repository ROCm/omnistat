"""Minimal ``SeriesSet`` wrapper for the iteration logic.

Only the two extraction methods the iteration logic needs are kept:
:meth:`raw_signal` (first series as aligned numpy arrays, used for the
averaged-utilization signal) and :meth:`per_series` (per-series NaN-filtered
values, used for per-GPU integrals).
"""

from __future__ import annotations

import numpy as np

from omnistat.inspect.compute import extract_values


class SeriesSet:
    """Lightweight wrapper around raw TSDB/CSV query results."""

    __slots__ = ("_results",)

    def __init__(self, results):
        self._results = results if results else []

    def __len__(self):
        return len(self._results)

    def __bool__(self):
        return bool(self._results)

    def per_series(self):
        """Iterate ``(labels_dict, values_list)`` per series (NaN-filtered)."""
        for r in self._results:
            yield r.get("metric", {}), extract_values(r)

    def raw_signal(self):
        """First series as ``(timestamps, values)`` numpy arrays.

        NaN values are replaced with 0.0. Useful for aggregated signals
        (e.g. ``aggregate="avg"`` queries) where there is exactly one series.
        """
        if not self._results:
            return np.array([]), np.array([])
        raw = self._results[0].get("values", [])
        ts = np.array([float(v[0]) for v in raw])
        vals = np.array([float(v[1]) if v[1] != "NaN" else 0.0 for v in raw])
        return ts, vals
