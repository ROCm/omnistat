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
