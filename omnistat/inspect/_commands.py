"""Subcommand handlers for omnistat-inspect."""

import json
import os

from omnistat.inspect._analyzer import AnalyzeJob


def cmd_db_info(analyzer, args):
    result = analyzer.get_db_info()
    result["query_stats"] = analyzer.get_query_stats()
    return result


def _ensure_job(analyzer, args):
    """Discover job and optionally refine with interval. Returns False on failure."""
    if not analyzer.discover_job(args.job):
        return {"error": f"Job {args.job} not found in the database"}
    # Use explicit --interval if provided, otherwise use auto-discovered sampling interval
    interval = getattr(args, "interval", None)
    if interval is None:
        interval = analyzer.sampling_interval
    if interval and interval < AnalyzeJob.SCAN_STEP:
        analyzer._refine_range(interval)
    return None


def cmd_job_info(analyzer, args):
    err = _ensure_job(analyzer, args)
    if err:
        return err

    interval = getattr(args, "interval", None)
    metadata = analyzer.get_job_metadata(interval)

    # Always include annotations and figure of merit
    annotations = analyzer.get_annotations()
    if annotations:
        metadata["annotations"] = annotations

    fom = analyzer.get_fom()
    if fom:
        metadata["figure_of_merit"] = fom

    # Always include host, network, and vendor summary if present
    available = analyzer.get_available_metrics()
    available_set = set(available.get("metrics", []))
    node_summary = analyzer.get_node_summary(available_set)
    if node_summary:
        metadata["node_summary"] = node_summary

    metadata["query_stats"] = analyzer.get_query_stats()
    return metadata


def cmd_metrics(analyzer, args):
    err = _ensure_job(analyzer, args)
    if err:
        return err

    result = analyzer.get_available_metrics(categorize=args.categorize)
    result["jobid"] = args.job
    result["query_stats"] = analyzer.get_query_stats()
    return result


def cmd_stats(analyzer, args):
    err = _ensure_job(analyzer, args)
    if err:
        return err

    category = getattr(args, "category", None)
    level = getattr(args, "level", None)
    # interval is informational only — step is auto-computed by compute_stats
    interval = args.interval if args.interval is not None else analyzer.sampling_interval

    # --metric narrows to a single metric (category auto-detected)
    if args.metric:
        if category is None:
            category = analyzer._detect_category(args.metric)
        if level is None:
            level = "global"

        # Validate level
        if category and category in AnalyzeJob.CATEGORY_CONFIG:
            valid_levels = AnalyzeJob.CATEGORY_CONFIG[category]["levels"]
            if level not in valid_levels:
                valid = ", ".join(sorted(valid_levels.keys()))
                return {
                    "error": f"Invalid level '{level}' for category '{category}'. Valid levels: {valid}",
                    "category": category,
                }

        stats = analyzer.compute_stats(args.metric, interval, level=level, category=category)
        return {
            "jobid": args.job,
            "category": category,
            "level": level,
            "sampling_interval": interval,
            "results": [stats],
            "query_stats": analyzer.get_query_stats(),
        }

    # Determine which categories to iterate
    if category:
        config = AnalyzeJob.CATEGORY_CONFIG.get(category)
        if config is None:
            return {"error": f"Unknown category: {category}"}
        categories = {category: config}
    else:
        categories = AnalyzeJob.CATEGORY_CONFIG

    # Build results: each category × its levels (filtered by --level if given)
    results_by_category = {}
    for cat, config in categories.items():
        if level is not None:
            # --level filters to a specific level; skip categories that don't support it
            if level not in config["levels"]:
                continue
            levels_to_run = [level]
        else:
            levels_to_run = list(config["levels"].keys())

        cat_results = {}
        for lvl in levels_to_run:
            level_stats = []
            for metric in config["default_metrics"]:
                stats = analyzer.compute_stats(
                    metric,
                    interval,
                    level=lvl,
                    category=cat,
                )
                level_stats.append(stats)
            cat_results[lvl] = level_stats
        results_by_category[cat] = cat_results

    return {
        "jobid": args.job,
        "category": category,
        "level": level,
        "sampling_interval": interval,
        "results_by_category": results_by_category,
        "query_stats": analyzer.get_query_stats(),
    }


def cmd_data_check(analyzer, args):
    err = _ensure_job(analyzer, args)
    if err:
        return err

    interval = args.interval if args.interval is not None else (analyzer.sampling_interval or 5.0)
    result = analyzer.check_data_collection(interval)
    result["jobid"] = args.job
    result["query_stats"] = analyzer.get_query_stats()
    return result


def cmd_health(analyzer, args):
    err = _ensure_job(analyzer, args)
    if err:
        return err

    interval = args.interval if args.interval is not None else (analyzer.sampling_interval or 5.0)
    result = analyzer.check_health(interval)
    result["jobid"] = args.job
    result["query_stats"] = analyzer.get_query_stats()
    return result


def cmd_timeseries(analyzer, args):
    err = _ensure_job(analyzer, args)
    if err:
        return err

    filters = {}
    if args.node:
        filters["instance"] = args.node
    if args.card:
        filters["card"] = args.card

    result = analyzer.get_timeseries(args.metric, args.interval, filters=filters if filters else None)
    result["jobid"] = args.job
    result["query_stats"] = analyzer.get_query_stats()

    # If --output specified, write to file instead of stdout
    if args.output:
        filepath = args.output
        os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else ".", exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(result, f, indent=2, default=str)
        return {
            "jobid": args.job,
            "metric": args.metric,
            "num_series": result["num_series"],
            "output_file": filepath,
            "query_stats": result["query_stats"],
        }

    return result


def cmd_query(analyzer, args):
    err = _ensure_job(analyzer, args)
    if err:
        return err

    # Template substitution for $job and $step
    promql = args.promql
    promql = promql.replace("$job", f'jobid="{args.job}"')
    promql = promql.replace("$step", 'jobstep=~".*"')

    step = args.step if args.step else args.interval
    results = analyzer.query_range(promql, analyzer.start_time, analyzer.end_time, step)

    series = []
    for r in results:
        m = r.get("metric", {})
        values = r.get("values", [])
        series.append(
            {
                "labels": m,
                "timestamps": [v[0] for v in values],
                "values": [v[1] for v in values],
                "num_points": len(values),
            }
        )

    result = {
        "jobid": args.job,
        "promql": promql,
        "step": str(step),
        "num_series": len(series),
        "series": series,
        "query_stats": analyzer.get_query_stats(),
    }

    if args.output:
        filepath = args.output
        os.makedirs(os.path.dirname(filepath) if os.path.dirname(filepath) else ".", exist_ok=True)
        with open(filepath, "w") as f:
            json.dump(result, f, indent=2, default=str)
        return {
            "jobid": args.job,
            "promql": promql,
            "num_series": len(series),
            "output_file": filepath,
            "query_stats": result["query_stats"],
        }

    return result


def cmd_iterations(analyzer, args):
    err = _ensure_job(analyzer, args)
    if err:
        return err

    result = analyzer.detect_iterations(
        metric=args.metric,
        low_threshold=args.low_threshold,
        high_threshold=args.high_threshold,
        min_idle_seconds=args.min_idle_seconds,
        min_iteration_seconds=args.min_iteration_seconds,
    )
    result["jobid"] = args.job
    result["query_stats"] = analyzer.get_query_stats()
    return result


def cmd_counters(analyzer, args):
    err = _ensure_job(analyzer, args)
    if err:
        return err
    result = analyzer.get_counter_summary()
    result["jobid"] = args.job
    result["query_stats"] = analyzer.get_query_stats()
    return result
