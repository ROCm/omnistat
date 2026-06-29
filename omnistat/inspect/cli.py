"""Command-line entry point for ``omnistat-inspect``.

A grouped, extensible CLI::

    omnistat-inspect [--tsdb-url URL | --csv-dir DIR] [--cache-dir DIR]
                   job <JOBID> [--start TS --end TS] [--interval N] [--refresh]
                       {report, info, stats, health, iterations, query, timeseries} [opts]
    omnistat-inspect [--tsdb-url URL | --csv-dir DIR] db info

The ``job`` group resolves a :class:`~omnistat.inspect.job.core.Job` once (either by
discovery, by cached snapshot, or directly from ``--start``/``--end``), then
builds the :class:`~omnistat.inspect.job.core.Module` registered for the chosen
subcommand and wraps its output in a standard envelope. The ``db`` group is a
data-source-level sibling that needs no job context (``db info`` lists the jobs
and metrics available in the backend).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any

from omnistat.inspect import constants
from omnistat.inspect.backend.csv import CsvDataSource
from omnistat.inspect.backend.tsdb import TsdbDataSource
from omnistat.inspect.cache import JsonStore
from omnistat.inspect.job.context import JobContext
from omnistat.inspect.job.core import Job, Module
from omnistat.inspect.job.health import Health
from omnistat.inspect.job.info import Info
from omnistat.inspect.job.iterations import Iterations
from omnistat.inspect.job.query import Query
from omnistat.inspect.job.report import Report
from omnistat.inspect.job.stats import Stats
from omnistat.inspect.job.timeseries import Timeseries

# Subcommand registry: (group, command) -> Module class. The CLI builds the
# selected module from ``ds``/``store``, extracting its knobs from the parsed
# args via the module's ``param_defaults`` (argparse dests match the param names).
MODULES: dict[tuple[str, str], type[Module]] = {
    ("job", "report"): Report,
    ("job", "info"): Info,
    ("job", "stats"): Stats,
    ("job", "health"): Health,
    ("job", "iterations"): Iterations,
    ("job", "query"): Query,
    ("job", "timeseries"): Timeseries,
}

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def _emit(obj: Any) -> None:
    """Encode a builtin object to indented JSON on stdout."""
    sys.stdout.buffer.write((json.dumps(obj, indent=2) + "\n").encode())


def _fail(payload: dict, code: int = 2) -> None:
    _emit(payload)
    sys.exit(code)


def _parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp; assume UTC when no tz is given."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="omnistat-inspect",
        description="Analyze Omnistat jobs: one-shot report card plus deep-dive analysis commands.",
    )

    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--tsdb-url", help="VictoriaMetrics URL (e.g. http://localhost:8428)")
    src.add_argument("--csv-dir", help="Directory of CSV exports from omnistat-query --export")
    p.add_argument(
        "--cache-dir",
        default=None,
        help="Directory for caches: discovery snapshots, module outputs, and the CSV parsed-series cache.",
    )

    groups = p.add_subparsers(dest="group")

    job = groups.add_parser("job", help="Analyze a single job")
    job.add_argument("jobid", help="Job ID to analyze")
    job.add_argument("--start", default=None, help="Job start (ISO-8601); with --end skips discovery")
    job.add_argument("--end", default=None, help="Job end (ISO-8601); with --start skips discovery")
    job.add_argument("--interval", type=float, default=None, help="Override discovered sampling interval (seconds)")
    job.add_argument("--refresh", action="store_true", help="Force fresh job discovery, ignoring any cached snapshot.")

    job_subs = job.add_subparsers(dest="command")

    p_report = job_subs.add_parser("report", help="Generate the one-shot JSON report card")
    p_report.add_argument(
        "--cv-threshold",
        type=float,
        default=constants.DEFAULT_CV_THRESHOLD,
        dest="cv_threshold",
        help="Coefficient-of-variation threshold to trigger variance drill-down",
    )
    p_report.add_argument("--verbose", action="store_true", help="Include full per-node / per-GPU arrays")

    p_iter = job_subs.add_parser("iterations", help="Detect iteration boundaries and per-iteration statistics")
    p_iter.add_argument("--metric", default=constants.DEFAULT_ITER_METRIC, help="Metric for iteration detection")
    p_iter.add_argument(
        "--low-threshold", type=float, default=constants.DEFAULT_ITER_LOW_THRESHOLD, dest="low_threshold"
    )
    p_iter.add_argument(
        "--high-threshold", type=float, default=constants.DEFAULT_ITER_HIGH_THRESHOLD, dest="high_threshold"
    )
    p_iter.add_argument(
        "--min-idle-seconds", type=float, default=constants.DEFAULT_ITER_MIN_IDLE_SECONDS, dest="min_idle_seconds"
    )
    p_iter.add_argument(
        "--min-iteration-seconds",
        type=float,
        default=constants.DEFAULT_ITER_MIN_ITERATION_SECONDS,
        dest="min_iteration_seconds",
    )

    job_subs.add_parser("info", help="Job info: nodes, GPUs, duration, and run metadata")

    p_stats = job_subs.add_parser("stats", help="Stats: gauges, counters, hardware counters, and variance")
    p_stats.add_argument(
        "--cv-threshold",
        type=float,
        default=constants.DEFAULT_CV_THRESHOLD,
        dest="cv_threshold",
        help="Coefficient-of-variation threshold to trigger variance drill-down",
    )
    p_stats.add_argument("--verbose", action="store_true", help="Include full per-node / per-GPU arrays")

    job_subs.add_parser("health", help="Health: data-collection coverage and health checks")

    p_query = job_subs.add_parser("query", help="Run arbitrary PromQL over the job window (TSDB only)")
    p_query.add_argument(
        "--promql",
        required=True,
        dest="promql",
        help="PromQL expression; $job and $jobstep are substituted with the job selector",
    )
    p_query.add_argument("--step", default=None, dest="step", help="Query step (default: auto)")

    p_ts = job_subs.add_parser("timeseries", help="Export raw time-series for a single metric")
    p_ts.add_argument("--metric", required=True, dest="metric", help="Metric name to export")
    p_ts.add_argument("--node", default=None, dest="node", help="Filter by node (instance)")
    p_ts.add_argument("--card", default=None, dest="card", help="Filter by GPU card")
    p_ts.add_argument(
        "--label",
        action="append",
        default=None,
        dest="label",
        metavar="KEY=VALUE",
        help="Filter by an arbitrary label (repeatable); value may be a regex (e.g. name=FETCH_SIZE|WRITE_SIZE)",
    )

    db = groups.add_parser("db", help="Inspect the data source (no job context)")
    db_subs = db.add_subparsers(dest="command")
    db_subs.add_parser("info", help="List available jobs and Omnistat metrics in the data source")

    return p


# ---------------------------------------------------------------------------
# Job resolution
# ---------------------------------------------------------------------------


def _resolve_job(args, ds, store) -> Job | None:
    """Build a Job via direct --start/--end args, or cache-aware discovery."""
    if args.start and args.end:
        ctx = JobContext(
            jobid=args.jobid,
            start_time=_parse_ts(args.start),
            end_time=_parse_ts(args.end),
            sampling_interval=args.interval,
        )
        return Job.from_context(ds, ctx, store=store)

    return Job.open(ds, args.jobid, store=store, refresh=args.refresh)


# ---------------------------------------------------------------------------
# Output envelope
# ---------------------------------------------------------------------------


def _data_source(ds) -> dict:
    """Describe the backing data source (type plus url/dir if present)."""
    db_info = ds.get_db_info()
    out = {"type": db_info.get("type", "unknown")}
    if db_info.get("url") is not None:
        out["url"] = db_info["url"]
    if db_info.get("dir") is not None:
        out["dir"] = db_info["dir"]
    return out


def _emit_result(ds, payload: dict) -> None:
    """Wrap a module's payload in the standard envelope and emit it."""
    _emit(
        {
            "jobid": ds.jobid,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_source": _data_source(ds),
            **payload,
            "query_stats": ds.get_query_stats(),
        }
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _build_data_source(args):
    """Construct the data source for the chosen backend (interval is job-only)."""
    interval = getattr(args, "interval", None)
    if args.csv_dir:
        return CsvDataSource(args.csv_dir, cache_dir=args.cache_dir, sampling_interval=interval)
    return TsdbDataSource(args.tsdb_url, sampling_interval=interval)


def _run_db(args, ds) -> None:
    """Handle the ``db`` group: data-source-level queries with no job context."""
    if args.command != "info":
        _fail({"error": f"Unknown command: db {args.command}"}, code=1)
    try:
        summary = ds.db_summary()
    except Exception as e:  # noqa: BLE001 - surface as structured error
        _fail({"error": str(e), "error_type": type(e).__name__, "query_stats": ds.get_query_stats()})
    _emit(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "data_source": _data_source(ds),
            **summary,
            "query_stats": ds.get_query_stats(),
        }
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.group:
        parser.print_help()
        sys.exit(1)

    if not getattr(args, "command", None):
        _fail({"error": f"No subcommand specified for '{args.group}'. Use --help for available subcommands."}, code=1)

    ds = _build_data_source(args)

    if args.group == "db":
        _run_db(args, ds)
        return

    module_cls = MODULES.get((args.group, args.command))
    if module_cls is None:
        _fail({"error": f"Unknown command: {args.group} {args.command}"}, code=1)

    store = JsonStore(args.cache_dir) if args.cache_dir else None

    job = _resolve_job(args, ds, store)
    if job is None:
        _fail(
            {
                "error": f"Job {args.jobid} not found in the data source",
                "jobid": args.jobid,
                "query_stats": ds.get_query_stats(),
            }
        )

    try:
        knobs = {name: getattr(args, name) for name in module_cls.param_defaults}
        _emit_result(ds, module_cls(ds, store, **knobs).get())
    except Exception as e:  # noqa: BLE001 - surface as structured error
        _fail(
            {
                "error": str(e),
                "error_type": type(e).__name__,
                "jobid": args.jobid,
                "query_stats": ds.get_query_stats(),
            }
        )


if __name__ == "__main__":
    main()
