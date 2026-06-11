"""Prometheus/VictoriaMetrics backend for omnistat-inspect.

Range-queries a Prometheus-compatible endpoint. :meth:`job_query` accepts
``aggregate`` / ``start`` / ``end`` parameters for iteration detection.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from prometheus_api_client import PrometheusConnect

from omnistat.inspect.backend.base import DataSource
from omnistat.inspect.constants import SCAN_DAYS, SCAN_STEP
from omnistat.inspect.helpers import build_jobs_summary


def _filter_str(filters: dict[str, str]) -> str:
    parts = []
    for k, v in filters.items():
        op = "=~" if "|" in v or ".*" in v else "="
        parts.append(f'{k}{op}"{v}"')
    return ", ".join(parts)


class TsdbDataSource(DataSource):
    """Range-query a Prometheus/VictoriaMetrics endpoint."""

    supports_context_cache = True
    backend_kind = "tsdb"

    def __init__(self, url: str, sampling_interval: float | None = None) -> None:
        super().__init__(sampling_interval=sampling_interval)
        self.url = url.rstrip("/")
        self._prom = PrometheusConnect(url=self.url)

    # ------------------------------------------------------------------
    # Low-level query helpers
    # ------------------------------------------------------------------

    def _query_range(self, promql: str, start, end, step) -> list[dict]:
        t0 = time.monotonic()
        results = self._prom.custom_query_range(promql, start, end, step=step)
        elapsed = time.monotonic() - t0
        points = sum(len(r.get("values", [])) for r in results)
        self.ledger.record(promql, str(step), elapsed, points)
        return results

    def raw_query_range(self, promql, step) -> list[dict]:
        return self._query_range(promql, self.start_time, self.end_time, step)

    def _job_selector(self) -> str:
        return f'rmsjob_info{{jobid="{self.jobid}", jobstep=~".*"}}'

    def _scoped_selector(self, metric: str, filters: dict[str, str] | None = None, join: bool = True) -> str:
        selector = metric
        if filters:
            selector = f"{metric}{{{_filter_str(filters)}}}"
        if join:
            join_expr = f"max by (instance) ({self._job_selector()})"
            return f"{selector} * on (instance) group_left() ({join_expr})"
        return f'{metric}{{jobid="{self.jobid}", jobstep=~".*"' + (f", {_filter_str(filters)}" if filters else "") + "}"

    # ------------------------------------------------------------------
    # DataSource API
    # ------------------------------------------------------------------

    def job_query(self, metric, step, filters=None, join=True, aggregate=None, start=None, end=None):
        promql = self._scoped_selector(metric, filters, join)
        if aggregate:
            promql = f"{aggregate}({promql})"
        q_start = start if start is not None else self.start_time
        q_end = end if end is not None else self.end_time
        return self._query_range(promql, q_start, q_end, step)

    def label_values(self, label, metric=None, match_filters=None, start=None, end=None):
        url = f"{self.url}/api/v1/label/{urllib.parse.quote(label, safe='')}/values"
        params = []
        if metric or match_filters:
            parts = []
            if metric:
                if ".*" in metric:
                    parts.append(f'__name__=~"{metric}"')
                else:
                    parts.append(f'__name__="{metric}"')
            if match_filters:
                parts.extend(f'{k}="{v}"' for k, v in match_filters.items())
            params.append(("match[]", "{" + ", ".join(parts) + "}"))
        if start and end:
            params.append(("start", str(int(start.timestamp()))))
            params.append(("end", str(int(end.timestamp()))))
        elif self.start_time and self.end_time:
            ps, pe = self.padded_range()
            params.append(("start", str(int(ps.timestamp()))))
            params.append(("end", str(int(pe.timestamp()))))
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"

        t0 = time.monotonic()
        with urllib.request.urlopen(url, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        elapsed = time.monotonic() - t0
        data = payload.get("data", []) or []
        self.ledger.record(f"label_values({label})", "n/a", elapsed, len(data))
        if payload.get("status") != "success":
            return []
        return data

    def agg_by_label(self, metric, label, step, filters=None):
        selector = metric
        if filters:
            selector = f"{metric}{{{_filter_str(filters)}}}"
        join_expr = f"max by (instance) ({self._job_selector()})"
        promql = f"avg by ({label}) ({selector} * on (instance) group_left() ({join_expr}))"
        results = self._query_range(promql, self.start_time, self.end_time, step)
        out: dict[str, float] = {}
        for r in results:
            key = r.get("metric", {}).get(label, "unknown")
            values = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
            if values:
                out[str(key)] = sum(values) / len(values)
        return out

    def get_label_for_series(self, metric: str) -> str:
        return "instance"

    # ------------------------------------------------------------------
    # Job discovery
    # ------------------------------------------------------------------

    def discover_job(self, jobid: str) -> bool:
        self.jobid = jobid
        now = datetime.now(timezone.utc) + timedelta(minutes=1)
        promql = f'max(rmsjob_info{{jobid="{jobid}", jobstep=~".*"}})'

        start_time = end_time = None
        for day in range(SCAN_DAYS):
            scan_end = now - timedelta(days=day)
            scan_start = scan_end - timedelta(days=1)
            results = self._query_range(promql, scan_start, scan_end, SCAN_STEP)
            if not end_time and results:
                end_time = datetime.fromtimestamp(results[0]["values"][-1][0], tz=timezone.utc)
                start_time = datetime.fromtimestamp(results[0]["values"][0][0], tz=timezone.utc)
                continue
            if end_time and results:
                start_time = datetime.fromtimestamp(results[0]["values"][0][0], tz=timezone.utc)
                continue
            if end_time and not results:
                break

        if start_time is None:
            return False

        self.start_time = start_time
        self.end_time = end_time
        self._discover_sampling_interval()
        if self.sampling_interval and self.sampling_interval < SCAN_STEP:
            self._refine_range(self.sampling_interval)
        self._populate_metadata()
        return True

    def _refine_range(self, interval: float) -> None:
        """Refine job start/end times to actual sample timestamps."""
        range_secs = int(SCAN_STEP * 4)
        selector = f'rmsjob_info{{jobid="{self.jobid}", jobstep=~".*"}}'

        eval_at = self.start_time + timedelta(seconds=SCAN_STEP)
        promql = f"min(tfirst_over_time({selector}[{range_secs}s]))"
        results = self._query_range(promql, eval_at, eval_at, 1)
        if results and results[0].get("values"):
            ts = float(results[0]["values"][0][1])
            self.start_time = datetime.fromtimestamp(ts, tz=timezone.utc)

        eval_at = self.end_time + timedelta(seconds=SCAN_STEP)
        promql = f"max(tlast_over_time({selector}[{range_secs}s]))"
        results = self._query_range(promql, eval_at, eval_at, 1)
        if results and results[0].get("values"):
            ts = float(results[0]["values"][0][1])
            self.end_time = datetime.fromtimestamp(ts, tz=timezone.utc)

    def _discover_sampling_interval(self) -> None:
        step = self.coarse_step()
        info = self._query_range(
            f"omnistat_info * on (instance) group_left() (max by (instance) ({self._job_selector()}))",
            self.start_time,
            self.end_time,
            step,
        )
        intervals = []
        for r in info:
            m = r.get("metric", {})
            self.omnistat_version = self.omnistat_version or m.get("version")
            try:
                if m.get("interval_secs") is not None:
                    intervals.append(float(m["interval_secs"]))
            except (ValueError, TypeError):
                pass
        if intervals and self.sampling_interval is None:
            self.sampling_interval = min(intervals)

    def ensure_metadata(self) -> None:
        """Lazily fetch descriptive metadata a rehydrated context lacks."""
        if not self.hosts:
            self._populate_metadata()

    def _populate_metadata(self) -> None:
        if self.hosts:
            return
        step = self.coarse_step()
        rms = self._query_range(self._job_selector(), self.start_time, self.end_time, step)
        hosts = set()
        for r in rms:
            m = r.get("metric", {})
            host = m.get("instance")
            if host:
                hosts.add(host)
            self.user = self.user or m.get("user")
            self.partition = self.partition or m.get("partition")
            self.nodes_label = self.nodes_label or m.get("nodes")
        self.hosts = sorted(hosts)

        gpus_results = self._query_range(
            f"rocm_num_gpus * on (instance) group_left() (max by (instance) ({self._job_selector()}))",
            self.start_time,
            self.end_time,
            step,
        )
        for r in gpus_results:
            values = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
            if values:
                self.gpus_per_node = int(max(values))
                break

        version = self._query_range(
            f"rocm_version_info * on (instance) group_left() (max by (instance) ({self._job_selector()}))",
            self.start_time,
            self.end_time,
            step,
        )
        for r in version:
            m = r.get("metric", {})
            gtype = m.get("type", "")
            if gtype and not self.gpu_type:
                self.gpu_type = gtype
            self.vbios_version = self.vbios_version or m.get("vbios")
            self.driver_version = self.driver_version or m.get("driver_ver")
            if ("MI250" in gtype) or ("MI200" in gtype):
                self.gpu_arch = "mi250x"
            elif "MI300" in gtype:
                self.gpu_arch = "mi300x"

    def get_db_info(self):
        return {"type": "tsdb", "url": self.url}

    def db_summary(self):
        """List the database's jobs and Omnistat metrics (no job context needed)."""
        now = datetime.now(timezone.utc)
        wide_start = now - timedelta(days=730)
        wide_end = now + timedelta(days=1)

        results = self._query_range("rmsjob_info", wide_start, wide_end, "1h")
        jobs = build_jobs_summary(results)

        # Scope the metric listing to the span covered by the jobs found above,
        # so it reflects what those jobs actually collected rather than the
        # full retention window.
        relevant: list[str] = []
        if jobs:
            span_start = min(datetime.fromisoformat(j["start_time"]) for j in jobs)
            span_end = max(datetime.fromisoformat(j["end_time"]) for j in jobs)
            all_metrics = self.label_values("__name__", start=span_start, end=span_end)
            relevant = sorted(n for n in all_metrics if n.startswith(("rocm_", "omnistat_", "rmsjob_")))

        return {
            "num_jobs": len(jobs),
            "jobs": jobs,
            "num_metrics": len(relevant),
            "metrics": relevant,
        }
