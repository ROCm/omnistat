"""Stats module: gauges, counters, hardware counters, and variance."""

from __future__ import annotations

from typing import Any

import numpy as np

from omnistat.inspect import compute, constants
from omnistat.inspect.job.core import Module


class Stats(Module):
    name = "stats_v2"
    param_defaults = {"cv_threshold": constants.DEFAULT_CV_THRESHOLD, "verbose": False}

    def build(self) -> dict:
        gauges, gauge_cvs = self._collect_gauges()
        counters = self._collect_counters()
        hw_counters = self._hardware_counters()
        kernels = self._kernels()
        variance = self._variance(gauge_cvs)
        return {
            "gauges": gauges,
            "counters": counters,
            "hardware_counters": hw_counters,
            "kernels": kernels,
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

    # Hardware counters are keyed per GCD (one Omnistat "card" on one host).
    _HW_KEY_LABELS = ("instance", "card")

    def _hardware_counters(self) -> dict | None:
        ds = self.ds
        # Reset-aware totals are computed by the backend (server-side
        # increase()/resets() on the TSDB, full-series despike + per_key_increase
        # on CSV). The fine step matters only for the client-side/fallback path:
        # coarse truncation would drop the first ~300 s plus a trailing partial
        # window of accumulation (a job-dependent 30-100% undercount), and the
        # full range also captures GCDs that only report mid-job (activation
        # stagger) where a boundary-window query would miss them.
        effective_duration = ds.job_duration
        # Group per GCD *and* counter name in one call; the backend returns one
        # (delta, span, monotonic) tuple per (instance, card, name) key.
        per_key = ds.counter_increase("omnistat_hardware_counter", self._HW_KEY_LABELS + ("name",))
        if not per_key:
            return None

        by_name: dict[str, list[tuple[float, float, bool]]] = {}
        for key, value in per_key.items():
            by_name.setdefault(key[-1], []).append(value)

        rows: list[dict] = []
        totals: dict[str, float] = {}
        all_spans: list[float] = []
        for name in sorted(by_name):
            entries = by_name[name]
            total = float(sum(d for d, _, _ in entries))
            spans = [s for _, s, _ in entries]
            active_duration = float(np.mean(spans)) if spans else 0.0
            monotonic = all(m for _, _, m in entries)
            active_rate = total / active_duration if active_duration > 0 else 0.0
            effective_rate = total / effective_duration if effective_duration > 0 else 0.0
            rows.append(
                {
                    "counter": name,
                    "total": round(total, 6),
                    "active_rate": round(active_rate, 6),
                    "effective_rate": round(effective_rate, 6),
                    "observed_span_seconds": round(active_duration, 4),
                    "monotonic": monotonic,
                    "num_series": len(entries),
                }
            )
            totals[name] = total
            all_spans.extend(spans)

        active_duration = float(np.mean(all_spans)) if all_spans else 0.0
        flops_dicts = compute.flops(totals, active_duration, effective_duration)
        flops = [dict(**f) for f in flops_dicts] if flops_dicts else None
        return {"rows": rows, "flops": flops}

    # ------------------------------------------------------------------
    # Kernel tracing (optional collector)
    # ------------------------------------------------------------------

    # Meta block reused for every kernel variance entry — the comparison
    # quantity is per-dispatch mean duration (Δduration_ns / Δdispatch_count).
    _KERNEL_VAR_META = {
        "source": "GPU",
        "label": "Mean dispatch duration",
        "name": "mean_dispatch_duration_ns",
        "unit": "ns",
    }
    _KERNEL_KEY_LABELS = ("instance", "card", "kernel")

    def _kernels(self) -> dict | None:
        ds = self.ds
        step = ds.coarse_step()
        dur = ds.job_query(constants.KERNEL_DURATION_METRIC, step)
        if not dur:
            return None
        cnt = ds.job_query(constants.KERNEL_COUNT_METRIC, step)

        dur_deltas = compute.per_key_counter_deltas(dur, self._KERNEL_KEY_LABELS)
        cnt_deltas = compute.per_key_counter_deltas(cnt, self._KERNEL_KEY_LABELS)

        per_kernel_dur: dict[str, float] = {}
        per_kernel_cnt: dict[str, float] = {}
        for (_inst, _card, kernel), d in dur_deltas.items():
            per_kernel_dur[kernel] = per_kernel_dur.get(kernel, 0.0) + d
        for (_inst, _card, kernel), c in cnt_deltas.items():
            per_kernel_cnt[kernel] = per_kernel_cnt.get(kernel, 0.0) + c

        if not per_kernel_dur:
            return None

        total_duration_ns = float(sum(per_kernel_dur.values()))
        total_dispatches = int(round(sum(per_kernel_cnt.values())))

        dropped_results = ds.job_query(constants.KERNEL_DROPPED_METRIC, step)
        dropped = int(round(sum(compute.counter_deltas(dropped_results))))

        rows: list[dict] = []
        for kernel, d in per_kernel_dur.items():
            disp = per_kernel_cnt.get(kernel, 0.0)
            mean = d / disp if disp > 0 else 0.0
            rows.append(
                {
                    "kernel": kernel,
                    "total_duration_ns": round(d, 4),
                    "dispatches": int(round(disp)),
                    "mean_duration_ns": round(mean, 4),
                }
            )
        rows.sort(key=lambda r: r["total_duration_ns"], reverse=True)
        top = rows if self.p.verbose else rows[: constants.TOP_KERNELS_LIMIT]

        top_names = [r["kernel"] for r in top]
        variance = self._kernel_variance(top_names, dur_deltas, cnt_deltas, step)

        return {
            "num_kernels": len(per_kernel_dur),
            "total_dispatches": total_dispatches,
            "total_duration_ns": round(total_duration_ns, 4),
            "dropped_dispatches": dropped,
            "top": top,
            "variance": variance,
        }

    def _kernel_variance(self, top, dur_deltas, cnt_deltas, step) -> dict:
        meta = self._KERNEL_VAR_META
        name = meta["name"]
        by_node, by_gpu_id, by_gpu = [], [], []
        for kernel in top:
            extra = {"kernel": kernel}
            per_gpu: dict[tuple, float] = {}
            node_dur: dict[str, float] = {}
            node_cnt: dict[str, float] = {}
            for (inst, card, k), d in dur_deltas.items():
                if k != kernel:
                    continue
                c = cnt_deltas.get((inst, card, k), 0.0)
                if c <= 0:
                    continue
                per_gpu[(inst, card)] = d / c
                node_dur[inst] = node_dur.get(inst, 0.0) + d
                node_cnt[inst] = node_cnt.get(inst, 0.0) + c
            if not per_gpu:
                continue

            entry = self._variance_entry(name, ("instance", "card"), per_gpu, meta=meta, extra=extra)
            if entry:
                by_gpu.append(entry)

            node_means = {inst: node_dur[inst] / node_cnt[inst] for inst in node_dur if node_cnt[inst] > 0}
            entry = self._variance_entry(name, ("instance",), node_means, meta=meta, extra=extra)
            if entry:
                by_node.append(entry)

            by_card: dict[str, list[float]] = {}
            for (_inst, card), v in per_gpu.items():
                by_card.setdefault(card, []).append(v)
            slot = {card: float(np.mean(by_card[card])) for card in sorted(by_card)}
            entry = self._variance_entry(name, ("card",), slot, meta=meta, extra=extra)
            if entry:
                by_gpu_id.append(entry)

        return {
            "cv_threshold": self.p.cv_threshold,
            "metric": name,
            "by_node": by_node,
            "by_gpu_id": by_gpu_id,
            "by_gpu": by_gpu,
        }

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

    def _metric_entry_extremes_of_means(
        self,
        name: str,
        key_fields: tuple,
        key_to_value: dict[Any, float],
        meta: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict:
        """Wrap a ``{key: value}`` dict in the unified variance-entry shape.

        ``meta`` (``{"source","label","name","unit"}``) overrides the default
        ``constants.GAUGE_BY_METRIC[name]`` lookup so non-gauge entries (e.g.
        kernels) can reuse this shape without a ``GAUGE_BY_METRIC`` row.
        ``extra`` is merged into the entry (e.g. ``{"kernel": <full name>}``).
        """
        items = list(key_to_value.items())
        items.sort(key=lambda kv: kv[1])
        min_k, min_v = items[0]
        max_k, max_v = items[-1]
        n = len(key_to_value)

        if meta is None:
            metric = constants.GAUGE_BY_METRIC[name]
            meta = {"source": metric.source, "label": metric.label, "name": name, "unit": metric.unit}

        entry: dict[str, Any] = dict(extra or {})
        entry.update(
            {
                "source": meta["source"],
                "label": meta["label"],
                "name": meta["name"],
                "unit": meta["unit"],
                "n": n,
                "cv": round(compute.cv_of(key_to_value.values()), 4),
                "min_mean": self._extreme(key_fields, min_k, min_v),
                "max_mean": self._extreme(key_fields, max_k, max_v),
            }
        )

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

    def _variance_entry(self, name, key_fields, means, meta=None, extra=None) -> dict | None:
        """Gate a {key: mean} dict on between-key CV; wrap it, or None if absent/uniform."""
        if not means or compute.cv_of(means.values()) <= self.p.cv_threshold:
            return None
        return self._metric_entry_extremes_of_means(name, key_fields, means, meta=meta, extra=extra)

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
