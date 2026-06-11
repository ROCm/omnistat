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
