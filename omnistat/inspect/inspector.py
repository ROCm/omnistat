#!/usr/bin/env python3
# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2023 - 2026 Advanced Micro Devices, Inc. All Rights Reserved.
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

"""QueryLedger and JobInspector — query engine for Omnistat job data."""

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from prometheus_api_client import PrometheusConnect

from omnistat.inspect.job import (
    HealthMixin,
    IterationsMixin,
    NodeSummaryMixin,
    StatsMixin,
    ValidationMixin,
)
from omnistat.inspect.constants import (
    CATEGORY_CONFIG,
    COUNTER_METRICS,
    HOST_COUNTER_METRICS,
    HOST_GAUGE_METRICS,
    METRIC_CATEGORIES,
    NETWORK_COUNTER_METRICS,
    SCAN_DAYS,
    SCAN_STEP,
    VENDOR_COUNTER_METRICS,
    VENDOR_GAUGE_METRICS,
)

# ---------------------------------------------------------------------------
# Query tracking
# ---------------------------------------------------------------------------


class QueryLedger:
    """Tracks all queries executed during an analysis session."""

    def __init__(self):
        self.queries = []
        self.start_time = time.monotonic()

    def record(self, promql, step, elapsed, datapoints):
        self.queries.append(
            {
                "promql": promql,
                "step": step,
                "time_seconds": round(elapsed, 4),
                "datapoints": datapoints,
            }
        )

    def summary(self):
        total_time = sum(q["time_seconds"] for q in self.queries)
        elapsed = round(time.monotonic() - self.start_time, 4)
        return {
            "num_queries": len(self.queries),
            "total_query_time_seconds": round(total_time, 4),
            "queries": self.queries,
            "analysis_elapsed_seconds": elapsed,
        }


# ---------------------------------------------------------------------------
# Core inspection class
# ---------------------------------------------------------------------------


class JobInspector(StatsMixin, HealthMixin, IterationsMixin, ValidationMixin, NodeSummaryMixin):
    """Query engine for Omnistat job data."""

    SCAN_STEP = SCAN_STEP
    SCAN_DAYS = SCAN_DAYS
    METRIC_CATEGORIES = METRIC_CATEGORIES
    COUNTER_METRICS = COUNTER_METRICS
    HOST_GAUGE_METRICS = HOST_GAUGE_METRICS
    NETWORK_COUNTER_METRICS = NETWORK_COUNTER_METRICS
    VENDOR_GAUGE_METRICS = VENDOR_GAUGE_METRICS
    VENDOR_COUNTER_METRICS = VENDOR_COUNTER_METRICS
    HOST_COUNTER_METRICS = HOST_COUNTER_METRICS
    CATEGORY_CONFIG = CATEGORY_CONFIG

    def __init__(self, prometheus_url):
        self.prometheus_url = prometheus_url.rstrip("/")
        self.prometheus = PrometheusConnect(url=self.prometheus_url)
        self.ledger = QueryLedger()

        # Job state (populated by discover_job)
        self.jobid = None
        self.start_time = None
        self.end_time = None
        self.gpu_arch = None
        self.sampling_interval = None  # seconds, from omnistat_info interval_secs

    # ------------------------------------------------------------------
    # Database discovery
    # ------------------------------------------------------------------

    def get_db_info(self):
        """Discover database contents: available jobs, time ranges, and metrics."""
        wide_start = datetime.now() - timedelta(days=730)
        wide_end = datetime.now() + timedelta(days=1)

        # Available job IDs
        job_ids = self.label_values("jobid", start=wide_start, end=wide_end)

        # Available metrics (filtered to omnistat/rocm)
        all_metrics = self.label_values("__name__", start=wide_start, end=wide_end)
        relevant = sorted(n for n in all_metrics if n.startswith(("rocm_", "omnistat_", "rmsjob_")))

        # Query rmsjob_info at coarse resolution for per-job time ranges and metadata
        promql = "rmsjob_info"
        results = self.query_range(promql, wide_start, wide_end, "1h")

        # Group by jobid
        jobs_data = {}
        for r in results:
            m = r.get("metric", {})
            jobid = m.get("jobid", "unknown")
            vals = r.get("values", [])
            if not vals:
                continue
            if jobid not in jobs_data:
                jobs_data[jobid] = {
                    "user": m.get("user", ""),
                    "partition": m.get("partition", ""),
                    "nodes_label": m.get("nodes", ""),
                    "hosts": set(),
                    "min_ts": float("inf"),
                    "max_ts": float("-inf"),
                }
            host = m.get("instance", "unknown")
            jobs_data[jobid]["hosts"].add(host)

            ts_first = vals[0][0]
            ts_last = vals[-1][0]
            if ts_first < jobs_data[jobid]["min_ts"]:
                jobs_data[jobid]["min_ts"] = ts_first
            if ts_last > jobs_data[jobid]["max_ts"]:
                jobs_data[jobid]["max_ts"] = ts_last

        # Build per-job summaries
        jobs = []
        for jobid in sorted(jobs_data):
            jd = jobs_data[jobid]
            start_dt = datetime.fromtimestamp(jd["min_ts"])
            end_dt = datetime.fromtimestamp(jd["max_ts"])
            duration_h = (jd["max_ts"] - jd["min_ts"]) / 3600
            jobs.append(
                {
                    "jobid": jobid,
                    "start_time": start_dt.isoformat(),
                    "end_time": end_dt.isoformat(),
                    "approximate_duration_hours": round(duration_h, 1),
                    "num_nodes": len(jd["hosts"]),
                    "user": jd["user"],
                    "partition": jd["partition"],
                }
            )

        return {
            "tsdb_url": self.prometheus_url,
            "num_jobs": len(jobs),
            "jobs": jobs,
            "num_metrics": len(relevant),
            "metrics": relevant,
        }

    # ------------------------------------------------------------------
    # Low-level query methods
    # ------------------------------------------------------------------

    def query_range(self, promql, start, end, step):
        """Execute a PromQL range query with timing instrumentation."""
        step_str = str(step)
        t0 = time.monotonic()
        results = self.prometheus.custom_query_range(promql, start, end, step=step)
        elapsed = time.monotonic() - t0
        datapoints = sum(len(r.get("values", [])) for r in results)
        self.ledger.record(promql, step_str, elapsed, datapoints)
        return results

    def query_instant(self, promql):
        """Execute a PromQL instant query with timing instrumentation."""
        t0 = time.monotonic()
        results = self.prometheus.custom_query(promql)
        elapsed = time.monotonic() - t0
        datapoints = len(results)
        self.ledger.record(promql, "instant", elapsed, datapoints)
        return results

    def label_values(self, label, match=None, start=None, end=None):
        """Get label values via VictoriaMetrics HTTP API."""
        url = f"{self.prometheus_url}/api/v1/label/{urllib.parse.quote(label, safe='')}/values"
        params = []
        if match:
            params.append(("match[]", match))
        if start:
            params.append(("start", str(int(start.timestamp()))))
        if end:
            params.append(("end", str(int(end.timestamp()))))
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

    # ------------------------------------------------------------------
    # Job discovery
    # ------------------------------------------------------------------

    def discover_job(self, jobid):
        """Find job time range by scanning rmsjob_info."""
        self.jobid = jobid
        now = datetime.now() + timedelta(minutes=1)
        start_time = None
        end_time = None

        for day in range(self.SCAN_DAYS):
            scan_end = now - timedelta(days=day)
            scan_start = scan_end - timedelta(days=1)

            promql = f'max(rmsjob_info{{jobid="{jobid}", jobstep=~".*"}})'
            results = self.query_range(promql, scan_start, scan_end, self.SCAN_STEP)

            if not end_time and len(results) > 0:
                end_time = datetime.fromtimestamp(results[0]["values"][-1][0])
                start_time = datetime.fromtimestamp(results[0]["values"][0][0])
                continue
            elif end_time and len(results) > 0:
                start_time = datetime.fromtimestamp(results[0]["values"][0][0])
                continue
            elif end_time and len(results) == 0:
                break

        if start_time is None:
            return False

        self.start_time = start_time
        self.end_time = end_time
        self._detect_gpu_arch()
        self._discover_sampling_interval()
        return True

    def _detect_gpu_arch(self):
        """Detect GPU architecture from rocm_version_info type label."""
        job_filter = f'jobid="{self.jobid}", jobstep=~".*"'
        join = f"max by (instance) (rmsjob_info{{{job_filter}}})"
        promql = f"rocm_version_info * on (instance) group_left() ({join})"
        results = self.query_range(promql, self.start_time, self.end_time, self._coarse_step())
        for r in results:
            gpu_type = r.get("metric", {}).get("type", "")
            if "MI250" in gpu_type or "MI200" in gpu_type:
                self.gpu_arch = "mi250x"
                return

    def _discover_sampling_interval(self):
        """Discover the sampling interval from omnistat_info's interval_secs label.

        Scoped to this job's nodes via rmsjob_info join. Uses the minimum
        interval found (finest resolution available). Different nodes or
        phases within a job may have different intervals.
        """
        job_filter = f'jobid="{self.jobid}", jobstep=~".*"'
        join = f"max by (instance) (rmsjob_info{{{job_filter}}})"
        promql = f"omnistat_info * on (instance) group_left() ({join})"
        results = self.query_range(promql, self.start_time, self.end_time, self._coarse_step())
        intervals = set()
        for r in results:
            interval_secs = r.get("metric", {}).get("interval_secs")
            if interval_secs is not None:
                try:
                    intervals.add(float(interval_secs))
                except (ValueError, TypeError):
                    pass
        if intervals:
            self.sampling_interval = min(intervals)

    def _metric_selector(self, metric):
        """Build a PromQL metric selector, applying architecture-specific filters.

        On MI250X, odd-numbered cards always report 0W for socket power.
        Excluding them avoids skewing stats (the even card reports combined
        package power for both GCDs).
        """
        if metric == "rocm_average_socket_power_watts" and self.gpu_arch == "mi250x":
            return f'{metric}{{card=~"0|2|4|6"}}'
        return metric

    def _refine_range(self, interval):
        """Refine job start/end times with finer resolution."""
        delta = timedelta(seconds=self.SCAN_STEP * 2)
        promql = f'max(rmsjob_info{{jobid="{self.jobid}", jobstep=~".*"}})'

        results = self.query_range(
            promql,
            self.start_time - delta,
            self.start_time + delta,
            interval,
        )
        if len(results) > 0:
            self.start_time = datetime.fromtimestamp(results[0]["values"][0][0])

        results = self.query_range(
            promql,
            self.end_time - delta,
            self.end_time + delta,
            interval,
        )
        if len(results) > 0:
            self.end_time = datetime.fromtimestamp(results[0]["values"][-1][0])

    # ------------------------------------------------------------------
    # Metadata retrieval
    # ------------------------------------------------------------------

    def get_job_metadata(self, interval):
        """Retrieve node count, GPU count, hosts, and sampling interval."""
        job_filter = f'jobid="{self.jobid}", jobstep=~".*"'
        join = f"max by (instance) (rmsjob_info{{{job_filter}}})"

        # Get host list from rmsjob_info
        promql = f"rmsjob_info{{{job_filter}}}"
        step = self._coarse_step()
        results = self.query_range(promql, self.start_time, self.end_time, step)

        hosts = set()
        job_metadata = {}
        for r in results:
            m = r.get("metric", {})
            host = m.get("instance", "unknown")
            hosts.add(host)
            # Capture metadata from first result per host
            if host not in job_metadata:
                job_metadata[host] = {
                    "user": m.get("user", ""),
                    "partition": m.get("partition", ""),
                    "nodes": m.get("nodes", ""),
                    "jobstep": m.get("jobstep", ""),
                }

        hosts = sorted(hosts)
        num_nodes = len(hosts)

        # Get GPU count
        promql_gpus = f"rocm_num_gpus * on (instance) group_left() ({join})"
        results_gpus = self.query_range(promql_gpus, self.start_time, self.end_time, step)
        gpus_per_node = None
        total_gpus = 0
        if results_gpus:
            for r in results_gpus:
                vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
                if vals:
                    gpus_per_node = int(max(vals))
            total_gpus = (gpus_per_node or 0) * num_nodes

        # Get omnistat_info for database type and sampling intervals
        db_type = None
        sampling_intervals = set()
        mode = None
        version = None
        promql_info = f"omnistat_info * on (instance) group_left() ({join})"
        results_info = self.query_range(promql_info, self.start_time, self.end_time, step)
        if results_info:
            for r in results_info:
                m = r.get("metric", {})
                if db_type is None:
                    db_type = m.get("type", m.get("schema", None))
                if mode is None:
                    mode = m.get("mode", None)
                if version is None:
                    version = m.get("version", None)
                interval_secs = m.get("interval_secs")
                if interval_secs is not None:
                    try:
                        sampling_intervals.add(float(interval_secs))
                    except (ValueError, TypeError):
                        pass

        sampling_intervals = sorted(sampling_intervals)

        # Extract metadata from first host
        first_host_meta = job_metadata.get(hosts[0], {}) if hosts else {}

        runtime_seconds = (self.end_time - self.start_time).total_seconds()

        return {
            "jobid": self.jobid,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "runtime_seconds": round(runtime_seconds, 1),
            "num_nodes": num_nodes,
            "gpus_per_node": gpus_per_node,
            "total_gpus": total_gpus,
            "hosts": hosts,
            "sampling_intervals": sampling_intervals,
            "min_interval": sampling_intervals[0] if sampling_intervals else None,
            "max_interval": sampling_intervals[-1] if sampling_intervals else None,
            "mode": mode,
            "version": version,
            "user": first_host_meta.get("user", ""),
            "partition": first_host_meta.get("partition", ""),
            "db_type": db_type,
        }

    def _coarse_step(self):
        """Select a coarse step for metadata queries."""
        if self.start_time is None or self.end_time is None:
            return self.SCAN_STEP
        duration = (self.end_time - self.start_time).total_seconds()
        step = min(self.SCAN_STEP, duration)
        duration_minutes = duration / 60
        if duration_minutes > 60:
            step = "1h"
        elif duration_minutes > 10:
            step = "5m"
        return step

    # ------------------------------------------------------------------
    # Annotations and Figure of Merit
    # ------------------------------------------------------------------

    def get_annotations(self):
        """Fetch rmsjob_annotations for the job."""
        job_filter = f'jobid="{self.jobid}", jobstep=~".*"'
        join = f"max by (instance) (rmsjob_info{{{job_filter}}})"
        promql = f"rmsjob_annotations * on (instance) group_left() ({join})"
        results = self.query_range(promql, self.start_time, self.end_time, self._coarse_step())

        annotations = []
        seen = set()
        for r in results:
            m = r.get("metric", {})
            text = m.get("marker", m.get("annotation", ""))
            if text and text not in seen:
                seen.add(text)
                annotations.append(text)

        return annotations

    def get_fom(self):
        """Fetch omnistat_fom (figure of merit) for the job."""
        job_filter = f'jobid="{self.jobid}", jobstep=~".*"'
        join = f"max by (instance) (rmsjob_info{{{job_filter}}})"
        promql = f"omnistat_fom * on (instance) group_left() ({join})"
        results = self.query_range(promql, self.start_time, self.end_time, self._coarse_step())

        if not results:
            return None

        fom_entries = []
        for r in results:
            m = r.get("metric", {})
            vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
            if not vals:
                continue
            fom_entries.append(
                {
                    "name": m.get("name", "unknown"),
                    "instance": m.get("instance", "unknown"),
                    "min": round(min(vals), 4),
                    "max": round(max(vals), 4),
                    "last": round(vals[-1], 4),
                    "num_points": len(vals),
                }
            )

        return fom_entries if fom_entries else None

    # ------------------------------------------------------------------
    # Metrics discovery
    # ------------------------------------------------------------------

    def _detect_category(self, metric):
        """Detect the category of a metric from METRIC_CATEGORIES."""
        for cat, metrics in self.METRIC_CATEGORIES.items():
            if metric in metrics:
                return cat
        # Fallback for rocm_ras_* prefix
        if metric.startswith("rocm_ras_"):
            return "ras"
        return None

    def get_available_metrics(self, categorize=False):
        """Enumerate metrics present in the job's time window."""
        all_names = self.label_values(
            "__name__",
            start=self.start_time - timedelta(seconds=60),
            end=self.end_time + timedelta(seconds=60),
        )

        # Filter to omnistat/rocm metrics
        relevant = [n for n in all_names if n.startswith(("rocm_", "omnistat_", "rmsjob_"))]
        relevant.sort()

        if not categorize:
            return {"metrics": relevant}

        categorized = {}
        uncategorized = []

        # Build reverse lookup
        metric_to_cat = {}
        for cat, metrics in self.METRIC_CATEGORIES.items():
            for m in metrics:
                metric_to_cat[m] = cat

        for m in relevant:
            cat = metric_to_cat.get(m)
            if cat is None:
                # Check RAS pattern
                if m.startswith("rocm_ras_"):
                    cat = "ras"
                else:
                    cat = None

            if cat:
                categorized.setdefault(cat, []).append(m)
            else:
                uncategorized.append(m)

        if uncategorized:
            categorized["other"] = uncategorized

        return {"metrics_by_category": categorized, "total_metrics": len(relevant)}

    # ------------------------------------------------------------------
    # Time series fetching
    # ------------------------------------------------------------------

    def get_timeseries(self, metric, interval, filters=None):
        """Fetch raw time series with rmsjob_info join."""
        job_filter = f'jobid="{self.jobid}", jobstep=~".*"'
        join = f"max by (instance) (rmsjob_info{{{job_filter}}})"

        metric_filter = metric
        if filters:
            filter_str = ", ".join(f'{k}="{v}"' for k, v in filters.items())
            metric_filter = f"{metric}{{{filter_str}}}"

        promql = f"{metric_filter} * on (instance) group_left() ({join})"
        results = self.query_range(promql, self.start_time, self.end_time, interval)

        series = []
        for r in results:
            m = r.get("metric", {})
            values = r.get("values", [])
            timestamps = [v[0] for v in values]
            data = [v[1] for v in values]
            series.append(
                {
                    "labels": m,
                    "timestamps": timestamps,
                    "values": data,
                    "num_points": len(values),
                }
            )

        return {
            "metric": metric,
            "num_series": len(series),
            "series": series,
        }

    # ------------------------------------------------------------------
    # Auto step
    # ------------------------------------------------------------------

    def _auto_step(self):
        """Compute the finest safe query step.

        Returns the finest step that is both meaningful (not finer than
        the actual sampling interval) and within VictoriaMetrics limits
        (runtime / step <= maxPointsPerTimeseries = 90,000).

        No arbitrary floor — sub-second sampling intervals are preserved
        for short jobs where VM limits allow it.
        """
        duration = (self.end_time - self.start_time).total_seconds()
        max_points = 90000
        vm_limit = duration / max_points  # finest step VM will accept
        if self.sampling_interval is not None:
            return max(self.sampling_interval, vm_limit)
        return max(vm_limit, 1.0)  # fallback if interval unknown

    # ------------------------------------------------------------------
    # Query stats
    # ------------------------------------------------------------------

    def get_query_stats(self):
        return self.ledger.summary()
