"""Timeseries module: raw time-series export for a single metric."""

from __future__ import annotations

from omnistat.inspect.job.core import Module


class Timeseries(Module):
    name = "timeseries"
    param_defaults = {"metric": None, "node": None, "card": None, "label": None}

    def build(self) -> dict:
        # --node / --card are exact identifiers (literal); --label values may be
        # a regex (documented), so they go through the regex path.
        literal_filters = {}
        if self.p.node:
            literal_filters["instance"] = self.p.node
        if self.p.card:
            literal_filters["card"] = self.p.card
        regex_filters = {}
        for item in self.p.label or []:
            key, sep, value = item.partition("=")
            if not sep or not key:
                raise ValueError(f"Malformed --label '{item}'; expected KEY=VALUE")
            regex_filters[key] = value
        step = self.ds.auto_step()
        results = self.ds.job_query(
            self.p.metric, step, literal_filters=literal_filters or None, regex_filters=regex_filters or None
        )
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
