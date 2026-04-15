# Future Improvements from Investigation Lessons

## 9. Tool Gaps: `omnistat-inspect` Needs New Subcommands

The degraded performance investigation exposed several operations that were done via raw Python scripts instead of the tool, because the tool lacked the needed subcommands:

- **`compare`** subcommand — compare stats across multiple jobs side-by-side. Currently requires running `stats` on each job separately and manually comparing the output.
- **`fom`** subcommand — analyze FOM at multiple resolutions automatically. The investigation discovered that 60s resolution was misleading and 5s resolution told the true story. A dedicated subcommand could query at multiple steps and flag resolution-dependent discrepancies.
- **`network`** subcommand — per-interface analysis with phase binning (network throughput bucketed by GPU utilization phase), total data transferred per iteration, temporal pattern analysis. All of this was done manually with raw PromQL.
- ~~**Counter delta analysis**~~ — **DONE**: `omnistat-inspect counters --job JOBID` discovers hardware counter names and computes per-counter delta statistics automatically.
- ~~**Multi-category stats with drill-down**~~ — **DONE**: `stats --category {host,network,vendor,xgmi}` supports multi-level drill-down for all metric categories, with automatic counter/gauge detection. Counter delta analysis at multiple levels is now supported via `stats --category`.

## 10. Query Tracking Gap

During the investigation, most analysis was done outside `omnistat-inspect` because the tool lacked the needed subcommands (see point 9). This means query tracking — which was designed to measure analysis efficiency — captured only a fraction of the actual work:

- Only 6 `omnistat-inspect` invocations were tracked (103 queries, 10.14s query time)
- The bulk of investigation queries (network analysis, per-iteration analysis, CPU analysis, remaining metrics sweep) were executed via raw Python `requests` scripts, completely bypassing the tracking system

The SKILL.md should emphasize routing ALL queries through `omnistat-inspect` for tracking. The `query` subcommand exists for ad-hoc PromQL, so there is no reason to use raw `curl`/`requests` — but the agent defaults to raw scripts when the built-in subcommands don't cover the use case.

**Root cause:** This is a consequence of point 9. If the tool had `compare`, `iterations`, `network`, and `fom` subcommands, the agent would have used them instead of writing ad-hoc scripts, and all queries would have been tracked automatically. Fixing point 9 largely fixes point 10.
