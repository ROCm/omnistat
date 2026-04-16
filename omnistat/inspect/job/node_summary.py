"""NodeSummaryMixin — host, network, vendor, and hardware counter summaries."""

from datetime import timedelta

import numpy as np


class NodeSummaryMixin:
    """Mixin providing get_node_summary and get_counter_summary."""

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
