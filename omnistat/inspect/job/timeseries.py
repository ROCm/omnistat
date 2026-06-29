"""Timeseries module: raw time-series export for a single metric."""

from __future__ import annotations

from omnistat.inspect.job.core import Module


class Timeseries(Module):
    name = "timeseries"
    param_defaults = {"metric": None, "node": None, "card": None, "label": None}

    def build(self) -> dict:
        filters = {}
        if self.p.node:
            filters["instance"] = self.p.node
        if self.p.card:
            filters["card"] = self.p.card
        for item in self.p.label or []:
            key, sep, value = item.partition("=")
            if not sep or not key:
                raise ValueError(f"Malformed --label '{item}'; expected KEY=VALUE")
            filters[key] = value
        step = self.ds.auto_step()
        results = self.ds.job_query(self.p.metric, step, filters=filters or None)
        series = [
            {
                "labels": r.get("metric", {}),
                "timestamps": [v[0] for v in r.get("values", [])],
                "values": [v[1] for v in r.get("values", [])],
                "num_points": len(r.get("values", [])),
            }
            for r in results
        ]
        return {
            "metric": self.p.metric,
            "num_series": len(series),
            "series": series,
        }
