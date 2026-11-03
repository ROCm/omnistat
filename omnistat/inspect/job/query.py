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

"""Query module: arbitrary PromQL over the discovered job window (TSDB-only)."""

from __future__ import annotations

from omnistat.inspect.job.core import Module


class Query(Module):
    name = "query"
    param_defaults = {"promql": None, "step": None}

    def build(self) -> dict:
        if self.ds.backend_kind != "tsdb":
            return {
                "error": "The 'query' command requires a TSDB (--tsdb-url). "
                "Arbitrary PromQL is not supported in CSV mode."
            }
        # Substitute $jobstep before $job: $job is a prefix of $jobstep, so the
        # reverse order would corrupt $jobstep into 'jobid="..."step'.
        promql = self.p.promql.replace("$jobstep", 'jobstep=~".*"').replace("$job", f'jobid="{self.ds.jobid}"')
        step = self.p.step or self.ds.auto_step()
        results = self.ds.raw_query_range(promql, step)
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
            "promql": promql,
            "step": str(step),
            "num_series": len(series),
            "series": series,
        }
