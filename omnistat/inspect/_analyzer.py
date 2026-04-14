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

"""QueryLedger and AnalyzeJob — core analysis engine for Omnistat job data."""

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import numpy as np
from prometheus_api_client import PrometheusConnect

from omnistat.inspect._constants import (
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
# Core analysis class
# ---------------------------------------------------------------------------


class AnalyzeJob:
    """Core analysis engine for Omnistat job data."""

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
    # Host, network, and vendor summary
    # ------------------------------------------------------------------

    def get_node_summary(self, available_metrics):
        """Compute summary stats for host, network, and vendor metrics.

        For gauge metrics: global min/max/mean/stddev.
        For counter metrics: per-node total delta, then global stats on deltas.
        Only includes metrics that are present in available_metrics.
        """
        job_filter = f'jobid="{self.jobid}", jobstep=~".*"'
        join = f"max by (instance) (rmsjob_info{{{job_filter}}})"
        step = self._coarse_step()
        categories = {}

        def _gauge_stats(metric):
            promql = f"{self._metric_selector(metric)} * on (instance) group_left() ({join})"
            results = self.query_range(promql, self.start_time, self.end_time, step)
            if not results:
                return None
            all_vals = []
            for r in results:
                vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
                all_vals.extend(vals)
            if not all_vals:
                return None
            arr = np.array(all_vals)
            return {
                "metric": metric,
                "type": "gauge",
                "min": round(float(np.min(arr)), 4),
                "max": round(float(np.max(arr)), 4),
                "mean": round(float(np.mean(arr)), 4),
                "stddev": round(float(np.std(arr)), 4),
            }

        def _counter_stats(metric):
            promql = f"{self._metric_selector(metric)} * on (instance) group_left() ({join})"
            results = self.query_range(promql, self.start_time, self.end_time, step)
            if not results:
                return None
            # For counters, compute per-series delta (last - first)
            deltas = []
            for r in results:
                vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
                if len(vals) >= 2:
                    deltas.append(vals[-1] - vals[0])
            if not deltas:
                return None
            arr = np.array(deltas)
            total = round(float(np.sum(arr)), 4)
            duration = (self.end_time - self.start_time).total_seconds()
            return {
                "metric": metric,
                "type": "counter",
                "total_delta": total,
                "rate_per_second": round(total / duration, 4) if duration > 0 else 0,
                "num_series": len(deltas),
                "per_series_mean_delta": round(float(np.mean(arr)), 4),
                "per_series_min_delta": round(float(np.min(arr)), 4),
                "per_series_max_delta": round(float(np.max(arr)), 4),
            }

        # Host
        host_results = []
        for m in self.HOST_GAUGE_METRICS:
            if m in available_metrics:
                s = _gauge_stats(m)
                if s:
                    host_results.append(s)
        for m in self.HOST_COUNTER_METRICS:
            if m in available_metrics:
                s = _counter_stats(m)
                if s:
                    host_results.append(s)
        if host_results:
            categories["host"] = host_results

        # Network
        net_results = []
        for m in self.NETWORK_COUNTER_METRICS:
            if m in available_metrics:
                s = _counter_stats(m)
                if s:
                    net_results.append(s)
        if net_results:
            categories["network"] = net_results

        # Vendor
        vendor_results = []
        for m in self.VENDOR_GAUGE_METRICS:
            if m in available_metrics:
                s = _gauge_stats(m)
                if s:
                    vendor_results.append(s)
        for m in self.VENDOR_COUNTER_METRICS:
            if m in available_metrics:
                s = _counter_stats(m)
                if s:
                    vendor_results.append(s)
        if vendor_results:
            categories["vendor"] = vendor_results

        return categories if categories else None

    def get_counter_summary(self):
        """Discover hardware counter names and compute per-counter statistics.

        Queries omnistat_hardware_counter metrics, discovers which counter
        names exist for the job, and computes per-counter delta statistics.
        """
        job_filter = f'jobid="{self.jobid}", jobstep=~".*"'
        join = f"max by (instance) (rmsjob_info{{{job_filter}}})"
        step = self._coarse_step()

        # Discover counter names present in the job's time window
        counter_names = self.label_values(
            "name",
            match="omnistat_hardware_counter",
            start=self.start_time - timedelta(seconds=60),
            end=self.end_time + timedelta(seconds=60),
        )

        # Get expected node count from rmsjob_info "nodes" label
        expected_nodes = None
        promql_rms = f"rmsjob_info{{{job_filter}}}"
        results_rms = self.query_range(promql_rms, self.start_time, self.end_time, step)
        for r in results_rms:
            nodes_label = r.get("metric", {}).get("nodes")
            if nodes_label:
                try:
                    expected_nodes = int(nodes_label)
                except (ValueError, TypeError):
                    pass
                break

        if not counter_names:
            return {
                "counter_names": [],
                "num_counters": 0,
                "counters": {},
                "expected_nodes": expected_nodes,
            }

        counters = {}
        all_instances = set()
        duration = (self.end_time - self.start_time).total_seconds()

        for name in sorted(counter_names):
            promql = f'omnistat_hardware_counter{{name="{name}"}} ' f"* on (instance) group_left() ({join})"
            results = self.query_range(promql, self.start_time, self.end_time, step)
            if not results:
                continue

            deltas = []
            max_value = None
            counter_instances = set()
            for r in results:
                instance = r.get("metric", {}).get("instance")
                if instance:
                    counter_instances.add(instance)
                    all_instances.add(instance)
                vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
                if vals:
                    series_max = max(vals)
                    if max_value is None or series_max > max_value:
                        max_value = series_max
                if len(vals) >= 2:
                    deltas.append(vals[-1] - vals[0])

            if not deltas:
                continue

            arr = np.array(deltas)
            total = round(float(np.sum(arr)), 4)
            counters[name] = {
                "type": "counter",
                "total_delta": total,
                "rate_per_second": round(total / duration, 4) if duration > 0 else 0,
                "num_series": len(deltas),
                "num_nodes": len(counter_instances),
                "per_series_mean_delta": round(float(np.mean(arr)), 4),
                "per_series_min_delta": round(float(np.min(arr)), 4),
                "per_series_max_delta": round(float(np.max(arr)), 4),
                "max_value": round(float(max_value), 4) if max_value is not None else None,
            }

        # Validate node coverage
        actual_nodes = len(all_instances)
        validation = {
            "expected_nodes": expected_nodes,
            "actual_nodes": actual_nodes,
        }
        if expected_nodes is not None and actual_nodes < expected_nodes:
            validation["missing_nodes"] = expected_nodes - actual_nodes
            validation["warning"] = (
                f"Counter data from {actual_nodes} nodes but job allocated {expected_nodes} "
                f"(counters may be multiplexed across GPUs)"
            )

        return {
            "counter_names": sorted(counter_names),
            "num_counters": len(counter_names),
            "counters": counters,
            "data_validation": validation,
        }

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
    # Statistics computation
    # ------------------------------------------------------------------

    def _stats_block(self, vals, percentiles=None):
        """Compute count/min/max/mean/stddev/percentiles for an array of values."""
        if percentiles is None:
            percentiles = [25, 50, 75, 95, 99]
        if len(vals) == 0:
            return None
        return {
            "count": len(vals),
            "min": round(float(np.min(vals)), 4),
            "max": round(float(np.max(vals)), 4),
            "mean": round(float(np.mean(vals)), 4),
            "stddev": round(float(np.std(vals)), 4),
            "percentiles": {f"p{p}": round(float(np.percentile(vals, p)), 4) for p in percentiles},
        }

    def _distribution_summary(self, values):
        """Compute min/max/mean/stddev/cv/percentiles for an array of floats."""
        arr = np.array(values, dtype=float)
        mean = float(np.mean(arr))
        stddev = float(np.std(arr))
        return {
            "min": round(float(np.min(arr)), 4),
            "max": round(float(np.max(arr)), 4),
            "mean": round(mean, 4),
            "stddev": round(stddev, 4),
            "cv": round(stddev / abs(mean), 4) if mean != 0 else 0.0,
            "percentiles": {
                "p5": round(float(np.percentile(arr, 5)), 4),
                "p25": round(float(np.percentile(arr, 25)), 4),
                "p50": round(float(np.percentile(arr, 50)), 4),
                "p75": round(float(np.percentile(arr, 75)), 4),
                "p95": round(float(np.percentile(arr, 95)), 4),
            },
        }

    def _detect_outliers(self, stats, field, group_by):
        """Detect outliers at 3-sigma on the given field. Returns list of outlier dicts."""
        values = [s[field] for s in stats if field in s]
        if len(values) < 3:
            return []
        arr = np.array(values, dtype=float)
        mean = float(np.mean(arr))
        stddev = float(np.std(arr))
        if stddev == 0:
            return []
        outliers = []
        for s in stats:
            if field not in s:
                continue
            z = (s[field] - mean) / stddev
            if abs(z) >= 3.0:
                entry = {"z_score": round(z, 2), field: s[field]}
                for label in group_by:
                    if label in s:
                        entry[label] = s[label]
                outliers.append(entry)
        outliers.sort(key=lambda o: abs(o["z_score"]), reverse=True)
        return outliers

    def _summarize_groups(self, stats, group_by):
        """Summarize gauge per-group stats into a compact distribution + outliers."""
        if not stats:
            return {"num_groups": 0, "summary": {}, "outliers": []}

        summary = {}
        for field in ("mean", "min", "max"):
            values = [s[field] for s in stats if field in s]
            if values:
                summary[field] = self._distribution_summary(values)

        outliers = self._detect_outliers(stats, "mean", group_by)

        return {
            "num_groups": len(stats),
            "summary": summary,
            "outliers": outliers,
        }

    def _summarize_counter_groups(self, stats, group_by):
        """Summarize counter per-group stats into a compact distribution + outliers."""
        if not stats:
            return {"num_groups": 0, "aggregate_total_delta": 0, "summary": {}, "outliers": []}

        aggregate_total_delta = round(sum(s.get("total_delta", 0) for s in stats), 4)

        summary = {}
        for field in ("total_delta", "rate_per_second"):
            values = [s[field] for s in stats if field in s]
            if values:
                summary[field] = self._distribution_summary(values)

        outliers = self._detect_outliers(stats, "total_delta", group_by)

        return {
            "num_groups": len(stats),
            "aggregate_total_delta": aggregate_total_delta,
            "summary": summary,
            "outliers": outliers,
        }

    def compute_stats(self, metric, interval, level="global", category=None, percentiles=None):
        """Compute statistics at different aggregation levels.

        Uses CATEGORY_CONFIG for data-driven grouping. Automatically detects
        whether a metric is a counter (delta computation) or gauge (raw value
        aggregation).

        Args:
            metric: Metric name to compute stats for.
            interval: Query step interval in seconds.
            level: Aggregation level (valid levels depend on category).
            category: Metric category (auto-detected from metric if None).
            percentiles: List of percentile values to compute.
        """
        if percentiles is None:
            percentiles = [25, 50, 75, 95, 99]

        # Auto-detect category if not provided
        if category is None:
            category = self._detect_category(metric)
        if category is None or category not in self.CATEGORY_CONFIG:
            # Fallback to gpu category for unknown metrics
            category = "gpu"

        config = self.CATEGORY_CONFIG[category]
        valid_levels = config["levels"]

        if level not in valid_levels:
            valid = ", ".join(sorted(valid_levels.keys()))
            return {
                "error": f"Invalid level '{level}' for category '{category}'. Valid levels: {valid}",
                "metric": metric,
                "category": category,
            }

        group_by = valid_levels[level]
        is_counter = metric in self.COUNTER_METRICS

        job_filter = f'jobid="{self.jobid}", jobstep=~".*"'
        join = f"max by (instance) (rmsjob_info{{{job_filter}}})"
        promql = f"{self._metric_selector(metric)} * on (instance) group_left() ({join})"
        step = self._auto_step()
        results = self.query_range(promql, self.start_time, self.end_time, step)

        if not results:
            return {
                "metric": metric,
                "level": level,
                "category": category,
                "type": "counter" if is_counter else "gauge",
                "stats": [] if group_by else None,
                "step": str(step),
            }

        if is_counter:
            return self._compute_counter_stats(
                metric,
                results,
                level,
                category,
                group_by,
                step,
            )
        else:
            return self._compute_gauge_stats(
                metric,
                results,
                level,
                category,
                group_by,
                step,
                percentiles,
            )

    def _make_group_key(self, metric_labels, group_by):
        """Extract a group key tuple from series labels."""
        return tuple(metric_labels.get(label, "unknown") for label in group_by)

    def _compute_gauge_stats(self, metric, results, level, category, group_by, interval, percentiles):
        """Compute gauge-style statistics (pool values, compute distribution)."""
        if not group_by:
            # Global aggregation
            all_vals = []
            for r in results:
                vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
                all_vals.extend(vals)
            stats = self._stats_block(np.array(all_vals), percentiles) if all_vals else None
            return {
                "metric": metric,
                "level": level,
                "category": category,
                "type": "gauge",
                "step": str(interval),
                "stats": stats,
            }

        # Grouped aggregation
        grouped = {}
        for r in results:
            m = r.get("metric", {})
            key = self._make_group_key(m, group_by)
            vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
            grouped.setdefault(key, []).extend(vals)

        stats = []
        for key in sorted(grouped):
            s = self._stats_block(np.array(grouped[key]), percentiles)
            if s:
                for label, value in zip(group_by, key):
                    s[label] = value
                stats.append(s)

        stats_output = self._summarize_groups(stats, group_by)

        return {
            "metric": metric,
            "level": level,
            "category": category,
            "type": "gauge",
            "step": str(interval),
            "stats": stats_output,
        }

    def _compute_counter_stats(self, metric, results, level, category, group_by, interval):
        """Compute counter-style statistics (per-series delta, then aggregate)."""
        duration = (self.end_time - self.start_time).total_seconds()

        if not group_by:
            # Global aggregation — compute per-series deltas, then aggregate
            deltas = []
            for r in results:
                vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
                if len(vals) >= 2:
                    deltas.append(vals[-1] - vals[0])
            if not deltas:
                return {
                    "metric": metric,
                    "level": level,
                    "category": category,
                    "type": "counter",
                    "step": str(interval),
                    "stats": None,
                }
            arr = np.array(deltas)
            total = round(float(np.sum(arr)), 4)
            stats = {
                "total_delta": total,
                "rate_per_second": round(total / duration, 4) if duration > 0 else 0,
                "num_series": len(deltas),
                "per_series_mean_delta": round(float(np.mean(arr)), 4),
                "per_series_min_delta": round(float(np.min(arr)), 4),
                "per_series_max_delta": round(float(np.max(arr)), 4),
                "per_series_stddev_delta": round(float(np.std(arr)), 4),
            }
            return {
                "metric": metric,
                "level": level,
                "category": category,
                "type": "counter",
                "step": str(interval),
                "stats": stats,
            }

        # Grouped aggregation — compute per-series deltas, group, then aggregate per group
        grouped_deltas = {}
        for r in results:
            m = r.get("metric", {})
            key = self._make_group_key(m, group_by)
            vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
            if len(vals) >= 2:
                grouped_deltas.setdefault(key, []).append(vals[-1] - vals[0])

        stats = []
        for key in sorted(grouped_deltas):
            arr = np.array(grouped_deltas[key])
            total = round(float(np.sum(arr)), 4)
            s = {
                "total_delta": total,
                "rate_per_second": round(total / duration, 4) if duration > 0 else 0,
                "num_series": len(arr),
                "per_series_mean_delta": round(float(np.mean(arr)), 4),
                "per_series_min_delta": round(float(np.min(arr)), 4),
                "per_series_max_delta": round(float(np.max(arr)), 4),
                "per_series_stddev_delta": round(float(np.std(arr)), 4),
            }
            for label, value in zip(group_by, key):
                s[label] = value
            stats.append(s)

        stats_output = self._summarize_counter_groups(stats, group_by)

        return {
            "metric": metric,
            "level": level,
            "category": category,
            "type": "counter",
            "step": str(interval),
            "stats": stats_output,
        }

    # ------------------------------------------------------------------
    # Health checks
    # ------------------------------------------------------------------

    def _health_step(self, interval):
        """Select an appropriate step for health checks.

        Health checks don't need sub-second resolution. Use at least 5s,
        and for long jobs use an even coarser step. This avoids generating
        hundreds of millions of datapoints for jobs sampled at 10-50ms.
        """
        # Floor at 5s -- finer resolution adds query cost without health value
        step = max(float(interval), 5.0)
        duration = (self.end_time - self.start_time).total_seconds()
        # For jobs longer than 1h, use 15s; longer than 6h, use 60s
        if duration > 21600:
            step = max(step, 60.0)
        elif duration > 3600:
            step = max(step, 15.0)
        return step

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

    def check_health(self, interval):
        """Run health checks with severity levels."""
        job_filter = f'jobid="{self.jobid}", jobstep=~".*"'
        join = f"max by (instance) (rmsjob_info{{{job_filter}}})"
        checks = []
        health_step = self._health_step(interval)

        # --- RAS errors ---
        ras_metrics = self.label_values(
            "__name__",
            match='{__name__=~"rocm_ras_.*"}',
            start=self.start_time - timedelta(seconds=60),
            end=self.end_time + timedelta(seconds=60),
        )

        if ras_metrics:
            for ras_metric in ras_metrics:
                promql = f"{ras_metric} * on (instance) group_left() ({join})"
                results = self.query_range(promql, self.start_time, self.end_time, self._coarse_step())

                for r in results:
                    m = r.get("metric", {})
                    vals = r.get("values", [])
                    if not vals:
                        continue

                    start_val = float(vals[0][1]) if vals[0][1] != "NaN" else 0
                    end_val = float(vals[-1][1]) if vals[-1][1] != "NaN" else 0
                    delta = end_val - start_val

                    if delta > 0:
                        is_uncorrectable = "uncorrectable" in ras_metric
                        severity = "critical" if is_uncorrectable else ("warning" if delta > 1000 else "info")
                        checks.append(
                            {
                                "check": "ras_errors",
                                "metric": ras_metric,
                                "severity": severity,
                                "instance": m.get("instance", "unknown"),
                                "card": m.get("card", "unknown"),
                                "start_value": start_val,
                                "end_value": end_val,
                                "delta": delta,
                                "message": f"{ras_metric} increased by {delta:.0f} on {m.get('instance')} card {m.get('card')}",
                            }
                        )

        # --- Thermals ---
        promql_temp = f"rocm_temperature_celsius * on (instance) group_left() ({join})"
        results_temp = self.query_range(promql_temp, self.start_time, self.end_time, health_step)
        for r in results_temp:
            m = r.get("metric", {})
            vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
            if not vals:
                continue
            max_temp = max(vals)
            mean_temp = sum(vals) / len(vals)

            if max_temp >= 100:
                checks.append(
                    {
                        "check": "thermal",
                        "severity": "critical",
                        "instance": m.get("instance", "unknown"),
                        "card": m.get("card", "unknown"),
                        "max_celsius": round(max_temp, 1),
                        "mean_celsius": round(mean_temp, 1),
                        "message": f"GPU throttling temperature ({max_temp:.0f}C) on {m.get('instance')} card {m.get('card')}",
                    }
                )
            elif mean_temp >= 90:
                checks.append(
                    {
                        "check": "thermal",
                        "severity": "warning",
                        "instance": m.get("instance", "unknown"),
                        "card": m.get("card", "unknown"),
                        "max_celsius": round(max_temp, 1),
                        "mean_celsius": round(mean_temp, 1),
                        "message": f"Sustained high temperature ({mean_temp:.0f}C avg) on {m.get('instance')} card {m.get('card')}",
                    }
                )

        # --- Power (MI250 odd-card filter) ---
        promql_power = f"rocm_average_socket_power_watts * on (instance) group_left() ({join})"
        results_power = self.query_range(promql_power, self.start_time, self.end_time, health_step)
        for r in results_power:
            m = r.get("metric", {})
            card = m.get("card", "0")
            vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
            if not vals:
                continue

            # MI250: odd cards report 0W, this is expected
            try:
                card_num = int(card)
            except (ValueError, TypeError):
                card_num = 0

            mean_power = sum(vals) / len(vals)
            max_power = max(vals)

            if mean_power == 0 and card_num % 2 == 1:
                # Expected MI250 behavior -- odd cards report 0W
                continue

            if mean_power == 0:
                checks.append(
                    {
                        "check": "power",
                        "severity": "warning",
                        "instance": m.get("instance", "unknown"),
                        "card": card,
                        "mean_watts": 0,
                        "message": f"Zero power reported on even card {card} of {m.get('instance')} (unexpected)",
                    }
                )

        # --- Data collection completeness ---
        # Use health_step for gap detection; gaps smaller than health_step
        # are not operationally significant for health purposes
        promql_rms = f"rmsjob_info{{{job_filter}}}"
        results_rms = self.query_range(promql_rms, self.start_time, self.end_time, health_step)

        expected_hosts = set()
        actual_hosts = set()
        sampling_gaps = []

        for r in results_rms:
            m = r.get("metric", {})
            host = m.get("instance", "unknown")
            actual_hosts.add(host)

            # Check for gaps in time series
            timestamps = [v[0] for v in r.get("values", [])]
            if len(timestamps) > 1:
                diffs = np.diff(timestamps)
                expected_step = float(health_step)
                gap_threshold = expected_step * 3  # Allow 3x the step before flagging
                gaps = [(i, float(d)) for i, d in enumerate(diffs) if d > gap_threshold]
                if gaps:
                    sampling_gaps.append(
                        {
                            "instance": host,
                            "num_gaps": len(gaps),
                            "max_gap_seconds": round(max(g[1] for g in gaps), 1),
                        }
                    )

        # Check declared vs actual nodes
        nodes_label = None
        for r in results_rms:
            m = r.get("metric", {})
            nodes_label = m.get("nodes", None)
            if nodes_label:
                break

        if nodes_label:
            try:
                expected_count = int(nodes_label)
                if len(actual_hosts) < expected_count:
                    checks.append(
                        {
                            "check": "data_collection",
                            "severity": "warning",
                            "message": f"Missing nodes: expected {expected_count}, found {len(actual_hosts)}",
                            "expected_nodes": expected_count,
                            "actual_nodes": len(actual_hosts),
                            "hosts_found": sorted(actual_hosts),
                        }
                    )
            except ValueError:
                pass

        if sampling_gaps:
            for gap in sampling_gaps:
                checks.append(
                    {
                        "check": "data_collection",
                        "severity": "warning",
                        "instance": gap["instance"],
                        "message": f"Sampling gaps on {gap['instance']}: {gap['num_gaps']} gaps, max {gap['max_gap_seconds']}s",
                        "num_gaps": gap["num_gaps"],
                        "max_gap_seconds": gap["max_gap_seconds"],
                    }
                )

        # --- Push health ---
        # Get push_interval_secs from omnistat_info labels
        push_interval = None
        info_results = self.label_values(
            "push_interval_secs",
            match=f'omnistat_info{{jobid="{self.jobid}"}}',
            start=self.start_time - timedelta(seconds=60),
            end=self.end_time + timedelta(seconds=60),
        )
        if not info_results:
            # Try without jobid filter (omnistat_info may not have jobid label)
            info_results = self.label_values(
                "push_interval_secs",
                start=self.start_time - timedelta(seconds=60),
                end=self.end_time + timedelta(seconds=60),
            )
        if info_results:
            try:
                push_interval = float(info_results[0])
            except (ValueError, TypeError):
                pass

        if push_interval is not None:
            promql_push = f"omnistat_perf_push_background_seconds * on (instance) group_left() ({join})"
            results_push = self.query_range(promql_push, self.start_time, self.end_time, health_step)

            if results_push:
                # Aggregate push durations across all nodes: extract the
                # distinct values (push duration changes once per push cycle)
                all_push_durations = []
                per_node_exceeded = []

                for r in results_push:
                    m = r.get("metric", {})
                    instance = m.get("instance", "unknown")
                    raw_vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
                    if not raw_vals:
                        continue

                    # Extract distinct push durations (value changes at each push)
                    durations = [raw_vals[0]]
                    for v in raw_vals[1:]:
                        if v != durations[-1]:
                            durations.append(v)

                    max_push = max(durations)
                    if max_push > push_interval:
                        per_node_exceeded.append(
                            {
                                "instance": instance,
                                "max_push_seconds": round(max_push, 2),
                            }
                        )

                    all_push_durations.append(durations)

                # Report nodes where push exceeded push_interval
                if per_node_exceeded:
                    # Summarize: if many nodes are affected, report aggregate
                    worst = max(per_node_exceeded, key=lambda x: x["max_push_seconds"])
                    checks.append(
                        {
                            "check": "push_duration",
                            "severity": "critical",
                            "message": (
                                f"Push duration exceeded push_interval ({push_interval}s) "
                                f"on {len(per_node_exceeded)} node(s). "
                                f"Worst: {worst['instance']} at {worst['max_push_seconds']}s"
                            ),
                            "push_interval_secs": push_interval,
                            "nodes_exceeded": len(per_node_exceeded),
                            "worst_instance": worst["instance"],
                            "worst_push_seconds": worst["max_push_seconds"],
                        }
                    )

                # Check for increasing trend across all nodes.
                # Use the first node's duration sequence as representative
                # (pushes are coordinated so all nodes see the same pattern).
                if all_push_durations:
                    # Pick the longest sequence for trend analysis
                    representative = max(all_push_durations, key=len)
                    if len(representative) >= 3:
                        first_half = representative[: len(representative) // 2]
                        second_half = representative[len(representative) // 2 :]
                        mean_first = sum(first_half) / len(first_half)
                        mean_second = sum(second_half) / len(second_half)
                        increase_pct = ((mean_second - mean_first) / mean_first * 100) if mean_first > 0 else 0

                        if increase_pct > 25:
                            severity = "warning" if increase_pct < 100 else "critical"
                            checks.append(
                                {
                                    "check": "push_duration_trend",
                                    "severity": severity,
                                    "message": (
                                        f"Push duration increasing: "
                                        f"first half avg {mean_first:.1f}s, "
                                        f"second half avg {mean_second:.1f}s "
                                        f"(+{increase_pct:.0f}%)"
                                    ),
                                    "push_interval_secs": push_interval,
                                    "first_half_mean_seconds": round(mean_first, 2),
                                    "second_half_mean_seconds": round(mean_second, 2),
                                    "increase_percent": round(increase_pct, 1),
                                    "num_pushes": len(representative),
                                    "push_durations": [round(d, 2) for d in representative],
                                }
                            )

        # Summarize
        severity_counts = {"critical": 0, "warning": 0, "info": 0, "ok": 0}
        for c in checks:
            severity_counts[c["severity"]] = severity_counts.get(c["severity"], 0) + 1

        overall = "ok"
        if severity_counts["critical"] > 0:
            overall = "critical"
        elif severity_counts["warning"] > 0:
            overall = "warning"
        elif severity_counts["info"] > 0:
            overall = "info"

        return {
            "overall_status": overall,
            "severity_counts": severity_counts,
            "health_step_used": health_step,
            "checks": checks,
        }

    # ------------------------------------------------------------------
    # Iteration detection
    # ------------------------------------------------------------------

    def detect_iterations(
        self,
        metric="rocm_utilization_percentage",
        low_threshold=20.0,
        high_threshold=70.0,
        min_idle_seconds=30.0,
        min_iteration_seconds=60.0,
    ):
        """Detect iteration boundaries and compute per-iteration statistics.

        Uses the averaged GPU utilization signal to find sustained idle periods
        (below low_threshold for at least min_idle_seconds), then computes
        per-iteration stats from the per-GPU data within each iteration window.

        Step selection uses _auto_step() for the averaged signal query.
        Per-iteration per-GPU queries use a finer step when the iteration
        window allows it.
        """
        job_filter = f'jobid="{self.jobid}", jobstep=~".*"'
        join = f"max by (instance) (rmsjob_info{{{job_filter}}})"
        step = self._auto_step()

        # 1. Fetch averaged utilization across all GPUs
        promql_avg = f"avg({metric} * on (instance) group_left() ({join}))"
        results = self.query_range(promql_avg, self.start_time, self.end_time, step)

        if not results or not results[0].get("values"):
            return {
                "metric": metric,
                "step": str(step),
                "thresholds": {
                    "low": low_threshold,
                    "high": high_threshold,
                    "min_idle_seconds": min_idle_seconds,
                },
                "num_iterations": 0,
                "iterations": [],
                "summary": None,
                "error": "No data returned for averaged utilization query",
            }

        # Parse the averaged signal
        raw_values = results[0]["values"]
        timestamps = np.array([float(v[0]) for v in raw_values])
        avg_util = np.array([float(v[1]) if v[1] != "NaN" else 0.0 for v in raw_values])

        # 2. Detect idle regions: contiguous spans where avg_util < low_threshold
        is_idle = avg_util < low_threshold

        # Find contiguous idle regions
        idle_regions = []
        in_idle = False
        idle_start_idx = 0
        for i, idle in enumerate(is_idle):
            if idle and not in_idle:
                idle_start_idx = i
                in_idle = True
            elif not idle and in_idle:
                idle_end_idx = i - 1
                idle_duration = timestamps[idle_end_idx] - timestamps[idle_start_idx]
                if idle_duration >= min_idle_seconds:
                    idle_regions.append((idle_start_idx, idle_end_idx))
                in_idle = False
        # Handle trailing idle region
        if in_idle:
            idle_end_idx = len(timestamps) - 1
            idle_duration = timestamps[idle_end_idx] - timestamps[idle_start_idx]
            if idle_duration >= min_idle_seconds:
                idle_regions.append((idle_start_idx, idle_end_idx))

        # 3. Derive iteration boundaries from gaps between idle regions
        # Iterations are the active regions between idle gaps
        iteration_bounds = []

        if not idle_regions:
            # No idle regions found — entire job is one iteration
            iteration_bounds.append((0, len(timestamps) - 1))
        else:
            # Before first idle region
            if idle_regions[0][0] > 0:
                iteration_bounds.append((0, idle_regions[0][0] - 1))
            # Between idle regions
            for i in range(len(idle_regions) - 1):
                start_idx = idle_regions[i][1] + 1
                end_idx = idle_regions[i + 1][0] - 1
                if start_idx <= end_idx:
                    iteration_bounds.append((start_idx, end_idx))
            # After last idle region
            if idle_regions[-1][1] < len(timestamps) - 1:
                iteration_bounds.append((idle_regions[-1][1] + 1, len(timestamps) - 1))

        # Filter out short segments
        iteration_bounds = [
            (s, e) for s, e in iteration_bounds if (timestamps[e] - timestamps[s]) >= min_iteration_seconds
        ]

        # 4. Compute per-iteration statistics
        iterations = []
        for iter_num, (start_idx, end_idx) in enumerate(iteration_bounds, 1):
            iter_start_ts = timestamps[start_idx]
            iter_end_ts = timestamps[end_idx]
            duration = iter_end_ts - iter_start_ts

            if duration <= 0:
                continue

            # Slice the averaged signal for this iteration
            iter_avg = avg_util[start_idx : end_idx + 1]

            # Fetch per-GPU data for this iteration window only.
            # Per-iteration queries can use a finer step since the window
            # is shorter than the full job (duration / 90000 is more lenient).
            iter_start_dt = datetime.fromtimestamp(iter_start_ts)
            iter_end_dt = datetime.fromtimestamp(iter_end_ts)
            max_points = 90000
            vm_iter_limit = duration / max_points
            if self.sampling_interval is not None:
                iter_step = max(self.sampling_interval, vm_iter_limit)
            else:
                iter_step = step
            promql_raw = f"{metric} * on (instance) group_left() ({join})"
            raw_results = self.query_range(promql_raw, iter_start_dt, iter_end_dt, iter_step)

            # Compute utilization integral from per-GPU data
            # integral = sum(values) * step / num_gpus
            if raw_results:
                gpu_integrals = []
                for r in raw_results:
                    vals = [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]
                    if vals:
                        gpu_integrals.append(sum(vals) * iter_step)
                if gpu_integrals:
                    utilization_integral = round(sum(gpu_integrals) / len(gpu_integrals), 1)
                else:
                    utilization_integral = 0.0
            else:
                utilization_integral = 0.0

            mean_utilization = round(utilization_integral / duration, 1) if duration > 0 else 0.0

            # Dip count: transitions from >high_threshold to <low_threshold
            dips = 0
            above_high = False
            for val in iter_avg:
                if val > high_threshold:
                    above_high = True
                elif val < low_threshold and above_high:
                    dips += 1
                    above_high = False

            # Time in utilization bands (from averaged signal)
            n_samples = len(iter_avg)
            below_20 = int(np.sum(iter_avg < 20))
            below_50 = int(np.sum(iter_avg < 50))
            above_80 = int(np.sum(iter_avg > 80))

            below_20_secs = round(below_20 * step, 1)
            below_50_secs = round(below_50 * step, 1)
            above_80_secs = round(above_80 * step, 1)

            iterations.append(
                {
                    "iteration": iter_num,
                    "start_time": datetime.utcfromtimestamp(iter_start_ts).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end_time": datetime.utcfromtimestamp(iter_end_ts).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "duration_seconds": round(duration, 1),
                    "utilization_integral": utilization_integral,
                    "mean_utilization": mean_utilization,
                    "dips": dips,
                    "time_below_20pct": {
                        "seconds": below_20_secs,
                        "percent": round(below_20_secs / duration * 100, 1) if duration > 0 else 0,
                    },
                    "time_below_50pct": {
                        "seconds": below_50_secs,
                        "percent": round(below_50_secs / duration * 100, 1) if duration > 0 else 0,
                    },
                    "time_above_80pct": {
                        "seconds": above_80_secs,
                        "percent": round(above_80_secs / duration * 100, 1) if duration > 0 else 0,
                    },
                }
            )

        # 5. Compute summary
        summary = None
        if iterations:
            durations = [it["duration_seconds"] for it in iterations]
            integrals = [it["utilization_integral"] for it in iterations]
            summary = {
                "mean_duration": round(float(np.mean(durations)), 1),
                "stddev_duration": round(float(np.std(durations)), 1),
                "mean_integral": round(float(np.mean(integrals)), 1),
                "duration_range": [round(min(durations), 1), round(max(durations), 1)],
            }

        return {
            "metric": metric,
            "step": str(step),
            "thresholds": {
                "low": low_threshold,
                "high": high_threshold,
                "min_idle_seconds": min_idle_seconds,
            },
            "num_iterations": len(iterations),
            "iterations": iterations,
            "summary": summary,
        }

    # ------------------------------------------------------------------
    # Query stats
    # ------------------------------------------------------------------

    def get_query_stats(self):
        return self.ledger.summary()
