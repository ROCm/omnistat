"""HealthMixin — hardware health checks (thermals, RAS, power, push)."""

from datetime import timedelta


class HealthMixin:
    """Mixin providing check_health and _health_step."""

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
