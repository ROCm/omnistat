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

"""Health module: data-collection quality and health indicators."""

from __future__ import annotations

import numpy as np

from omnistat.inspect import compute
from omnistat.inspect.job.core import Module


class Health(Module):
    name = "health"

    def build(self) -> dict:
        # A rehydrated context carries no descriptive metadata (hosts,
        # nodes_label); fetch it lazily so ``expected_nodes`` is accurate.
        self.ds.ensure_metadata()
        return {
            "data_collection": self._data_collection(),
            "health": self._health(),
        }

    # ------------------------------------------------------------------
    # Data collection
    # ------------------------------------------------------------------

    def _data_collection(self) -> dict:
        step = self.ds.auto_step()
        results = self._fetch_per_node_timestamps(step)

        if not results:
            return {
                "reporting_nodes": 0,
                "expected_nodes": self._expected_nodes(),
                "activation_stagger_seconds": None,
                "deactivation_stagger_seconds": None,
                "reporting_duration_per_node_seconds": None,
                "nodes_with_gaps": 0,
                "total_gaps": 0,
            }

        first_ts: list[float] = []
        last_ts: list[float] = []
        durations: list[float] = []
        nodes: set[str] = set()
        nodes_with_gaps = 0
        total_gaps = 0

        for r in results:
            host = r.get("metric", {}).get("instance", "unknown")
            nodes.add(host)
            ts = sorted({float(v[0]) for v in r.get("values", [])})
            if not ts:
                continue
            first_ts.append(ts[0])
            last_ts.append(ts[-1])
            durations.append(ts[-1] - ts[0])
            gaps = self._gaps(ts, step)
            if gaps:
                nodes_with_gaps += 1
                total_gaps += len(gaps)

        return {
            "reporting_nodes": len(nodes),
            "expected_nodes": self._expected_nodes(),
            "activation_stagger_seconds": round(max(first_ts) - min(first_ts), 2) if first_ts else None,
            "deactivation_stagger_seconds": round(max(last_ts) - min(last_ts), 2) if last_ts else None,
            "reporting_duration_per_node_seconds": self._stats(durations) if durations else None,
            "nodes_with_gaps": nodes_with_gaps,
            "total_gaps": total_gaps,
        }

    def _fetch_per_node_timestamps(self, step: float) -> list[dict]:
        results = self._try_query("rmsjob_info", step, join=False)
        if results and any(r.get("values") for r in results):
            return results
        for metric in (
            "rocm_utilization_percentage",
            "omnistat_host_cpu_aggregate_core_utilization",
        ):
            results = self._try_query(metric, step)
            if results:
                return results
        return []

    def _expected_nodes(self) -> int | None:
        if self.ds.nodes_label:
            try:
                return int(self.ds.nodes_label)
            except (ValueError, TypeError):
                pass
        return len(self.ds.hosts) or None

    @staticmethod
    def _gaps(timestamps: list[float], expected_step: float) -> list[float]:
        if len(timestamps) < 2:
            return []
        diffs = np.diff(np.asarray(timestamps))
        threshold = expected_step * 3
        return [float(d) for d in diffs if d > threshold]

    @staticmethod
    def _stats(values: list[float]) -> dict:
        arr = np.asarray(values, dtype=float)
        return {
            "mean": round(float(arr.mean()), 2),
            "min": round(float(arr.min()), 2),
            "max": round(float(arr.max()), 2),
        }

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def _health(self) -> dict:
        step = self._health_step()
        indicators: list[dict] = []
        indicators.extend(self._check_ras())
        indicators.extend(self._check_thermal(step))
        indicators.extend(self._check_push(step))
        return {"indicators": indicators}

    def _health_step(self) -> float:
        duration = self.ds.job_duration
        step = max(float(self.ds.sampling_interval or 5.0), 5.0)
        if duration > 21600:
            return max(step, 60.0)
        if duration > 3600:
            return max(step, 15.0)
        return step

    def _check_ras(self) -> list[dict]:
        out: list[dict] = []
        names = self._try_label_values("__name__", metric="rocm_ras_.*")
        for name in names:
            results = self._try_query(name, self.ds.coarse_step())
            for r in results:
                values = r.get("values", [])
                if not values:
                    continue
                s = float(values[0][1]) if values[0][1] != "NaN" else 0.0
                e = float(values[-1][1]) if values[-1][1] != "NaN" else 0.0
                delta = e - s
                if delta <= 0:
                    continue
                m = r.get("metric", {})
                out.append(
                    {
                        "category": "ras",
                        "name": name,
                        "instance": m.get("instance"),
                        "card": m.get("card"),
                        "delta": int(delta),
                    }
                )
        return out

    def _check_thermal(self, step: float) -> list[dict]:
        results = self._try_query("rocm_temperature_celsius", step)
        out: list[dict] = []
        for m, values in self._iter_series(results):
            max_value = max(values)
            # Emit only "worth noticing" entries; rendering layer decides severity.
            if max_value < 90:
                continue
            out.append(
                {
                    "category": "thermal",
                    "instance": m.get("instance"),
                    "card": m.get("card"),
                    "max": round(max_value, 2),
                }
            )
        return out

    def _check_push(self, step: float) -> list[dict]:
        push_interval = self._discover_push_interval()
        if push_interval is None:
            return []
        results = self._try_query("omnistat_perf_push_background_seconds", step)
        if not results:
            return []

        deduped_by_node = [
            (m.get("instance", "unknown"), compute.dedup_consecutive(values))
            for m, values in self._iter_series(results)
        ]
        out: list[dict] = []
        for indicator in (
            self._push_exceeded_indicator(deduped_by_node, push_interval),
            self._push_trend_indicator(deduped_by_node),
        ):
            if indicator is not None:
                out.append(indicator)
        return out

    @staticmethod
    def _push_exceeded_indicator(deduped_by_node, push_interval) -> dict | None:
        """Per-node "push duration exceeded interval" indicator, or ``None``."""
        exceeded = [
            {"instance": instance, "max_push": round(max(d), 2)}
            for instance, d in deduped_by_node
            if max(d) > push_interval
        ]
        if not exceeded:
            return None
        worst = max(exceeded, key=lambda x: x["max_push"])
        return {
            "category": "push_exceeded",
            "push_interval": float(push_interval),
            "nodes_exceeded": len(exceeded),
            "worst_instance": worst["instance"],
            "worst_max_push": round(worst["max_push"], 2),
        }

    @staticmethod
    def _push_trend_indicator(deduped_by_node) -> dict | None:
        """Drift "push duration increasing" indicator from the longest series, or ``None``."""
        if not deduped_by_node:
            return None
        rep = max((d for _instance, d in deduped_by_node), key=len)
        if len(rep) < 3:
            return None
        mid = len(rep) // 2
        first = sum(rep[:mid]) / mid
        second = sum(rep[mid:]) / (len(rep) - mid)
        increase = ((second - first) / first * 100) if first > 0 else 0.0
        if increase <= 25:
            return None
        return {
            "category": "push_trend",
            "first_half_mean": round(first, 2),
            "second_half_mean": round(second, 2),
        }

    def _discover_push_interval(self) -> float | None:
        results = self._try_label_values("push_interval_secs", metric="omnistat_info")
        for v in results:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
        return None
