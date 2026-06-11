"""Stats module: gauges, counters, hardware counters, and variance."""

from __future__ import annotations

from typing import Any

import numpy as np

from omnistat.inspect import compute, constants
from omnistat.inspect.job.core import Module


class Stats(Module):
    name = "stats"
    param_defaults = {"cv_threshold": constants.DEFAULT_CV_THRESHOLD, "verbose": False}

    def build(self) -> dict:
        gauges, gauge_cvs = self._collect_gauges()
        counters = self._collect_counters()
        hw_counters = self._hardware_counters()
        variance = self._variance(gauge_cvs)
        return {
            "gauges": gauges,
            "counters": counters,
            "hardware_counters": hw_counters,
            "variance": variance,
        }

    # ------------------------------------------------------------------
    # Gauges + counters
    # ------------------------------------------------------------------

    def _collect_gauges(self) -> tuple[list[dict], dict[str, float]]:
        step = self.ds.auto_step()
        out: list[dict] = []
        gauge_cvs: dict[str, float] = {}
        for row in constants.GAUGE_LIST:
            results = self._fetch_series(row.name, step)
            if not results:
                continue
            if row.name in constants.COUNTER_METRICS:
                per_node = compute.per_node_counter_deltas(results)
                if not per_node:
                    continue
                _total, mean, min_value, max_value, cv, percentiles, n = compute.rate_summary(
                    per_node, min_duration=self._counter_min_duration(), qs=constants.PERCENTILES
                )
            else:
                arr = compute.pool_values(results)
                mean, min_value, max_value, cv, percentiles, n = compute.gauge_stats(arr, qs=constants.PERCENTILES)
                if mean is None:
                    continue
            gauge_cvs[row.name] = cv
            out.append(
                {
                    "source": row.source,
                    "label": row.label,
                    "name": row.name,
                    "mean": round(mean, 4),
                    "min": round(min_value, 4),
                    "max": round(max_value, 4),
                    "unit": row.unit,
                    "n": n,
                    "cv": round(cv, 4),
                    "percentiles": percentiles,
                }
            )
        return out, gauge_cvs

    def _collect_counters(self) -> list[dict]:
        step = self.ds.auto_step()
        out: list[dict] = []
        for row in constants.COUNTER_LIST:
            results = self.ds.job_query(row.name, step)
            deltas = compute.counter_deltas(results)
            if not deltas:
                continue
            out.append(
                {
                    "source": row.source,
                    "label": row.label,
                    "name": row.name,
                    "total": round(sum(deltas), 6),
                    "unit": row.unit,
                }
            )
        return out

    # ------------------------------------------------------------------
    # Hardware counters + FLOPS
    # ------------------------------------------------------------------

    def _hardware_counters(self) -> dict | None:
        ds = self.ds
        step = ds.coarse_step()
        duration = ds.job_duration
        names = self._try_label_values("name", metric="omnistat_hardware_counter")
        if not names:
            return None

        rows: list[dict] = []
        totals: dict[str, float] = {}
        for name in sorted(names):
            results = ds.job_query("omnistat_hardware_counter", step, filters={"name": name})
            if not results:
                continue
            deltas = compute.counter_deltas(results)
            if not deltas:
                continue
            total = float(np.sum(deltas))
            rate = total / duration if duration > 0 else 0.0
            rows.append(
                {
                    "counter": name,
                    "total": round(total, 6),
                    "rate": round(rate, 6),
                    "num_series": len(deltas),
                }
            )
            totals[name] = total

        flops_dicts = compute.flops(totals, duration)
        flops = [dict(**f) for f in flops_dicts] if flops_dicts else None
        return {"rows": rows, "flops": flops}

    # ------------------------------------------------------------------
    # Variance
    # ------------------------------------------------------------------

    def _variance(self, gauge_cvs: dict[str, float]) -> dict:
        step = self.ds.auto_step()
        gpu_raw: dict[str, list[dict]] = {}
        for name in constants.GPU_VARIANCE_METRICS:
            if gauge_cvs.get(name, 0.0) <= self.p.cv_threshold:
                continue
            gpu_raw[name] = self._fetch_series(name, step)
        by_id, by_gpu = self._gpu_variance(gpu_raw)
        return {
            "cv_threshold": self.p.cv_threshold,
            "verbose": self.p.verbose,
            "by_node": self._per_node_variance(step, gauge_cvs),
            "by_gpu_id": by_id,
            "by_gpu": by_gpu,
        }

    @staticmethod
    def _extreme(key_fields: tuple, k: Any, v: float) -> dict:
        """Build an extreme entry dict from the key fields, key, and value."""
        kw: dict[str, Any] = {"value": round(float(v), 4)}
        if len(key_fields) == 1:
            kw[key_fields[0]] = str(k)
        else:
            for field, part in zip(key_fields, k):
                kw[field] = str(part)
        return kw

    def _metric_entry_extremes_of_means(self, name: str, key_fields: tuple, key_to_value: dict[Any, float]) -> dict:
        """Wrap a ``{key: value}`` dict in the unified variance-entry shape."""
        items = list(key_to_value.items())
        items.sort(key=lambda kv: kv[1])
        min_k, min_v = items[0]
        max_k, max_v = items[-1]
        n = len(key_to_value)

        metric = constants.GAUGE_BY_METRIC[name]
        entry: dict[str, Any] = {
            "source": metric.source,
            "label": metric.label,
            "name": name,
            "unit": metric.unit,
            "n": n,
            "cv": round(compute.cv_of(key_to_value.values()), 4),
            "min_mean": self._extreme(key_fields, min_k, min_v),
            "max_mean": self._extreme(key_fields, max_k, max_v),
        }

        def all_block():
            if len(key_fields) == 1:
                return {str(k): round(float(v), 4) for k, v in key_to_value.items()}
            return [self._extreme(key_fields, k, v) for k, v in key_to_value.items()]

        if n <= constants.INLINE_ALL_THRESHOLD:
            entry["all"] = all_block()
        else:
            entry["percentiles"] = compute.percentiles_of(key_to_value.values(), constants.PERCENTILES)
            if self.p.verbose:
                entry["all"] = all_block()
        return entry

    def _variance_entry(self, name, key_fields, means) -> dict | None:
        """Gate a {key: mean} dict on between-key CV; wrap it, or None if absent/uniform."""
        if not means or compute.cv_of(means.values()) <= self.p.cv_threshold:
            return None
        return self._metric_entry_extremes_of_means(name, key_fields, means)

    def _counter_min_duration(self) -> float:
        return float(self.ds.sampling_interval or 0.0)

    def _per_node_variance(self, step: float, gauge_cvs: dict[str, float]) -> list[dict]:
        out: list[dict] = []
        for name, cv in gauge_cvs.items():
            if cv <= self.p.cv_threshold:
                continue
            if name not in constants.GAUGE_BY_METRIC:
                continue
            if name in constants.COUNTER_METRICS:
                results = self._fetch_series(name, step)
                per_node_totals = compute.per_node_counter_deltas(results)
                min_dur = self._counter_min_duration()
                means = {n: d / dur for n, (d, dur) in per_node_totals.items() if d > 0 and dur >= min_dur and dur > 0}
            elif name in constants.DROP_ZERO_SERIES_METRICS:
                results = self._fetch_series(name, step)
                means = compute.per_label_means(results, "instance")
            else:
                means = dict(self.ds.agg_by_label(name, "instance", step))
            entry = self._variance_entry(name, ("instance",), means)
            if entry:
                out.append(entry)
        return out

    def _gpu_variance(self, gpu_raw):
        """Per-(instance,card) temporal means → (by_gpu_id, by_gpu) in one pass."""
        by_id, by_gpu = [], []
        for name, results in gpu_raw.items():
            assert name not in constants.COUNTER_METRICS, (
                f"GPU_VARIANCE_METRICS contains counter {name!r}; per-card aggregation "
                "of cumulative counters is meaningless."
            )
            means = {}
            for m, values in self._iter_series(results):
                means[(m.get("instance", "unknown"), m.get("card", "?"))] = float(np.mean(values))
            if not means:
                continue
            by_card = {}
            for (_inst, card), v in means.items():
                by_card.setdefault(card, []).append(v)
            slot = {card: float(np.mean(by_card[card])) for card in sorted(by_card)}

            gpu_entry = self._variance_entry(name, ("instance", "card"), means)
            if gpu_entry:
                by_gpu.append(gpu_entry)
            id_entry = self._variance_entry(name, ("card",), slot)
            if id_entry:
                by_id.append(id_entry)
        return by_id, by_gpu
