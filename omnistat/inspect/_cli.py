"""CLI parser and entry point for omnistat-inspect."""

import argparse
import json
import sys

from omnistat.inspect._analyzer import AnalyzeJob
from omnistat.inspect._commands import (
    cmd_counters,
    cmd_db_info,
    cmd_health,
    cmd_iterations,
    cmd_job_info,
    cmd_metrics,
    cmd_query,
    cmd_stats,
    cmd_timeseries,
)
from omnistat.inspect._output import _append_query_log, _output_json, _write_scratch


def build_parser():
    parser = argparse.ArgumentParser(
        prog="omnistat-inspect",
        description="Agentic-first CLI tool for Omnistat HPC job analysis. " "Outputs structured JSON by default.",
    )
    parser.add_argument(
        "--tsdb-url",
        required=True,
        help="Time series database URL — VictoriaMetrics or Prometheus (e.g., http://localhost:8428)",
    )
    parser.add_argument(
        "--scratch-dir",
        default=None,
        help="Directory for dumping results to files",
    )
    subparsers = parser.add_subparsers(dest="command", help="Analysis subcommands")

    # db-info
    subparsers.add_parser("db-info", help="Show database contents: available jobs, time ranges, and metrics")

    # job-info
    p_info = subparsers.add_parser("job-info", help="Discover job time range and topology")
    p_info.add_argument("--job", required=True, help="Job ID")
    p_info.add_argument("--interval", type=float, default=None, help="Sampling interval (seconds) for time refinement")

    # metrics
    p_metrics = subparsers.add_parser("metrics", help="List available metrics for a job")
    p_metrics.add_argument("--job", required=True, help="Job ID")
    p_metrics.add_argument("--categorize", action="store_true", help="Group metrics by category")

    # stats
    p_stats = subparsers.add_parser("stats", help="Compute statistics for job metrics")
    p_stats.add_argument("--job", required=True, help="Job ID")
    p_stats.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Sampling interval (seconds) — used for time-range refinement; query step is auto-computed",
    )
    p_stats.add_argument("--metric", default=None, help="Filter to a specific metric (category auto-detected)")
    p_stats.add_argument(
        "--category",
        choices=["gpu", "host", "network", "vendor", "xgmi"],
        default=None,
        help="Filter to a specific category",
    )
    p_stats.add_argument(
        "--level",
        default=None,
        help="Filter to a specific aggregation level (valid levels depend on category)",
    )

    # health
    p_health = subparsers.add_parser("health", help="Run health checks on a job")
    p_health.add_argument("--job", required=True, help="Job ID")
    p_health.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Sampling interval (seconds) — used for time-range refinement and health step floor",
    )

    # timeseries
    p_ts = subparsers.add_parser("timeseries", help="Export raw time series data")
    p_ts.add_argument("--job", required=True, help="Job ID")
    p_ts.add_argument("--interval", type=float, required=True, help="Sampling interval (seconds)")
    p_ts.add_argument("--metric", required=True, help="Metric name")
    p_ts.add_argument("--node", default=None, help="Filter by node (instance)")
    p_ts.add_argument("--card", default=None, help="Filter by GPU card")
    p_ts.add_argument("--output", default=None, help="Write output to file instead of stdout")

    # query
    p_query = subparsers.add_parser("query", help="Execute arbitrary PromQL with job context")
    p_query.add_argument("--job", required=True, help="Job ID")
    p_query.add_argument("--interval", type=float, required=True, help="Sampling interval (seconds)")
    p_query.add_argument("--promql", required=True, help="PromQL query ($job and $step are substituted)")
    p_query.add_argument("--step", default=None, help="Query step override (e.g., '5s', '1m')")
    p_query.add_argument("--output", default=None, help="Write output to file instead of stdout")

    # iterations
    p_iter = subparsers.add_parser(
        "iterations", help="Detect iteration boundaries and compute per-iteration statistics"
    )
    p_iter.add_argument("--job", required=True, help="Job ID")
    p_iter.add_argument(
        "--interval",
        type=float,
        default=None,
        help="Sampling interval (seconds) — used for time-range refinement; query step is auto-computed",
    )
    p_iter.add_argument(
        "--metric",
        default="rocm_utilization_percentage",
        help="Metric for iteration detection (default: rocm_utilization_percentage)",
    )
    p_iter.add_argument(
        "--low-threshold",
        type=float,
        default=20.0,
        dest="low_threshold",
        help="Low utilization threshold for idle detection (default: 20%%)",
    )
    p_iter.add_argument(
        "--high-threshold",
        type=float,
        default=70.0,
        dest="high_threshold",
        help="High utilization threshold for dip counting (default: 70%%)",
    )
    p_iter.add_argument(
        "--min-idle-seconds",
        type=float,
        default=30.0,
        dest="min_idle_seconds",
        help="Minimum idle duration to count as iteration boundary (default: 30s)",
    )
    p_iter.add_argument(
        "--min-iteration-seconds",
        type=float,
        default=60.0,
        dest="min_iteration_seconds",
        help="Minimum iteration duration to include (default: 60s)",
    )

    # counters
    p_counters = subparsers.add_parser("counters", help="Discover and summarize hardware performance counters")
    p_counters.add_argument("--job", required=True, help="Job ID")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    analyzer = AnalyzeJob(args.tsdb_url)

    handlers = {
        "db-info": cmd_db_info,
        "job-info": cmd_job_info,
        "metrics": cmd_metrics,
        "stats": cmd_stats,
        "health": cmd_health,
        "timeseries": cmd_timeseries,
        "query": cmd_query,
        "iterations": cmd_iterations,
        "counters": cmd_counters,
    }

    handler = handlers.get(args.command)
    if not handler:
        print(json.dumps({"error": f"Unknown command: {args.command}"}))
        sys.exit(1)

    try:
        result = handler(analyzer, args)
    except Exception as e:
        result = {
            "error": str(e),
            "error_type": type(e).__name__,
            "query_stats": analyzer.get_query_stats(),
        }
        _output_json(result)
        _append_query_log(args.scratch_dir, analyzer.get_query_stats())
        sys.exit(2)

    # Write to scratch dir if specified
    if args.scratch_dir:
        job = getattr(args, "job", None)
        filename = f"{args.command}_{job}.json" if job else f"{args.command}.json"
        filepath = _write_scratch(args.scratch_dir, filename, result)
        _append_query_log(args.scratch_dir, analyzer.get_query_stats())
        print(
            json.dumps(
                {
                    "status": "ok",
                    "output_file": filepath,
                    "query_stats": result.get("query_stats", {}),
                },
                indent=2,
                default=str,
            )
        )
    else:
        is_error = "error" in result
        _output_json(result)
        if is_error:
            sys.exit(2)


if __name__ == "__main__":
    main()
