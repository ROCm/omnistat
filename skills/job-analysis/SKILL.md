---
name: job-analysis
description: Analyze an HPC job from an Omnistat database using hypothesis-driven exploration, driven by the omnistat-inspect tool. Use this to diagnose why a job behaved as it did — performance bottlenecks, hardware issues, anomalies, or comparing a degraded job against a healthy baseline. For a plain factual snapshot without investigation, use job-report instead.
allowedPrompts:
  - tool: Bash
    prompt: run omnistat-inspect commands
  - tool: Bash
    prompt: execute PromQL queries via curl
  - tool: Bash
    prompt: create temporary directory
  - tool: Bash
    prompt: read query results from file
  - tool: Bash
    prompt: list directory contents
  - tool: Bash
    prompt: check victoriametrics status
  - tool: Bash
    prompt: run curl commands
---

# Job Analysis

Analyze GPU telemetry data collected by Omnistat for HPC/AI workloads. This skill guides you through a top-down, hypothesis-driven analysis of job performance, driven primarily by the `omnistat-inspect` CLI tool.

**Target audience:** HPC engineers, AI/ML researchers, system administrators investigating job performance, GPU health, and resource utilization.

**What the analysis produces:** A structured report identifying performance bottlenecks, hardware issues, resource utilization patterns, and anomalies -- with all findings backed by data.

**When to use this vs `job-report`.** Use **job-analysis** when you need to understand *why* a job behaved as it did — diagnosing bottlenecks, throttling, stragglers, or regressions, or comparing a degraded job against a healthy baseline. It is hypothesis-driven and iterative. If you only need a quick factual snapshot of *what* a job did (stats, energy, health, data quality) without investigation, use **job-report** instead. A common pattern is to run job-report first, then reach for job-analysis when something looks off.

## Tooling: omnistat-inspect

This skill is built entirely around `omnistat-inspect`, the consolidated analysis CLI. It is your single entry point for every phase:

- **Baseline characterization** — `omnistat-inspect --tsdb-url $TSDB_URL job JOBID report` produces the structured report card (overview, stats, variance, data-collection, health). Start every analysis here: it is the fastest way to understand scale, runtime, utilization, variance, and health in a single call.
- **Deep-dive subcommands** — `job JOBID info` (metadata/topology), `stats` (gauges, counters, hardware counters, and per-node/per-GPU variance), `health` (data-collection coverage and health checks), `iterations` (iteration boundaries and per-iteration stats), `query` (arbitrary PromQL), and `timeseries` (raw series export).
- **Data-source inspection** — `omnistat-inspect --tsdb-url $TSDB_URL db info` lists the jobs and metrics available in the backend (no job context required).

For anything not covered by a subcommand, drop to raw PromQL via `query` (TSDB) or `curl` against the TSDB HTTP API.

### Job-context flexibility

Every `omnistat-inspect job JOBID` invocation resolves the job's time window in one of two ways:

- **Discovery (default):** omits `--start`/`--end`; the tool scans the database to discover the job's time range and topology. Add `--cache-dir DIR` to persist the discovery snapshot and per-section results so repeat calls are cheap (no re-scan, no re-query).
- **Direct window:** pass both `--start ISO8601` and `--end ISO8601` (optionally `--interval SECONDS`) to skip discovery entirely and analyze an exact window — useful for zooming into a single phase or iteration you found earlier.

```bash
# Discovery + cache (cheap repeat calls)
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID report

# Direct window (no discovery scan)
omnistat-inspect --tsdb-url $TSDB_URL job JOBID \
  --start 2026-01-01T12:00:00Z --end 2026-01-01T12:10:00Z report
```

#### Cache-dir reuse for late-pipeline `query` / `timeseries`

The `query` and `timeseries` subcommands are typically run late in the analysis,
well after discovery. **Always pass the same `--cache-dir` you used for the
initial `report`/`overview` call** so they rehydrate the cached discovery
snapshot instead of re-scanning:

```bash
# Early: discovery runs once and is cached (time range + sampling interval)
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID overview

# Later: query/timeseries reuse the snapshot — no re-scan, correct default step
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID \
  query --promql 'avg(rocm_utilization_percentage{$job, $jobstep})'
```

Because the rehydrated snapshot restores the discovered **sampling interval**,
the default query step is the sampling interval (`max(sampling_interval, 1s)`) —
exactly what you want for full-resolution queries. Notes:

- **Do not** reach for `--start`/`--end` just to "scope to the discovered
  window" — passing them *skips* discovery and drops the sampling interval, so
  the default step silently degrades to **1s** unless you also add `--interval`.
  Use `--start`/`--end` only when you genuinely want a narrower sub-window, and
  pair them with `--interval` to keep the step correct.
- For a deliberately coarser step on a single `query` (e.g. an overview of a
  long job), pass `--step SECONDS` directly; it overrides the default for that
  call only. (`timeseries` has no `--step`; control its resolution via the
  cached interval or the global `--interval`.)

## Prerequisites

1. **Data source** — one of:
   - **VictoriaMetrics running** with the Omnistat database loaded (use the `open-database` skill if needed), OR
   - **CSV exports** from `omnistat-query --export` (no TSDB required)
2. **Python virtual environment activated** with omnistat installed (`pip install ".[query]"` from the omnistat repo root) — this provides `omnistat-inspect`. Confirm with `which omnistat-inspect`.
3. **Job ID(s)** to analyze (discover available jobs with `omnistat-inspect --tsdb-url $TSDB_URL db info` or `omnistat-inspect --csv-dir /path/to/exports db info`)

## Setup

Before starting analysis, set up the working environment:

### TSDB Mode (default)

```bash
# 1. Create a cache directory for this analysis session (cheap repeat calls)
SCRATCH=$(mktemp -d /tmp/omnistat-inspect-XXXXXX)
echo "Cache directory: $SCRATCH/cache"

# 2. Set the TSDB URL (VictoriaMetrics or Prometheus)
TSDB_URL="http://localhost:8428"

# 3. Verify connectivity and discover available jobs
omnistat-inspect --tsdb-url $TSDB_URL db info
```

### CSV Mode

Use CSV mode when you have CSV exports from `omnistat-query --export` and no running TSDB. All subcommands except `job query` work in CSV mode — CSV mode uses whatever metrics were exported, so if a metric wasn't included in the export, it won't be available for analysis.

```bash
# 1. Create a cache directory for this analysis session
SCRATCH=$(mktemp -d /tmp/omnistat-inspect-XXXXXX)
echo "Cache directory: $SCRATCH/cache"

# 2. Set the CSV directory path
CSV_DIR="/path/to/csv/exports"

# 3. Discover available data
omnistat-inspect --csv-dir $CSV_DIR db info

# 4. Run analysis (same subcommands as TSDB mode)
omnistat-inspect --csv-dir $CSV_DIR --cache-dir $SCRATCH/cache job JOBID info
omnistat-inspect --csv-dir $CSV_DIR --cache-dir $SCRATCH/cache job JOBID stats
omnistat-inspect --csv-dir $CSV_DIR --cache-dir $SCRATCH/cache job JOBID health
```

**Note:** The `job query` subcommand (arbitrary PromQL) is not available in CSV mode — it requires a TSDB backend.

The `db info` subcommand verifies database connectivity and reports all available jobs with their time ranges, node counts, users, and partitions, plus the full list of available metrics. Use this output to select a job ID and confirm you are looking at the right database.

## Analysis Workflow

Follow this top-down, hypothesis-driven workflow. Each phase builds on the previous one. You have freedom to explore and investigate -- this is a methodology guide, not a rigid script.

### Epistemic Discipline

**Do not assume what the workload is.** Unless the user tells you the application name, or annotations/metadata explicitly identify it, treat the workload as unknown. Describe what the telemetry shows (e.g., "the GPUs spend ~40% of wall-clock idle between compute phases, each phase ~90s long") rather than what you think it means (e.g., "this is a training workload doing forward/backward passes"). Note: GPU utilization naturally sits near 0% or near 100%, so simply calling it "bimodal" is not an insight — quantify the idle fraction or phase structure instead. If you need to speculate, label it clearly as a hypothesis.

**Do not assume the workload is homogeneous.** A single HPC job may run different tasks on different nodes or GPUs. Some nodes may run data loading, others may run compute, others may handle communication. VRAM differences across GPUs, utilization variance across nodes, or non-uniform network traffic are signals of heterogeneity, not necessarily problems. Before reporting "imbalance" as a finding, consider whether the workload is intentionally heterogeneous.

**Report what you observe, not what you expect.** If a metric looks unusual, describe the observation and its magnitude. Do not assume it is a problem unless you have evidence of impact (e.g., on runtime, throughput, or health). An observation like "5% of GPUs use 10x more VRAM than the rest" is a fact; "there is a memory imbalance problem" is an interpretation that may be wrong.

### Job Discovery and Characterization

**The first step of every analysis is the one-shot report.** It is the factual baseline the rest of the workflow builds on — a single call that returns the job overview, the full `stats` block (gauges, counters, hardware counters, variance), and the `health` block (data-collection coverage + hardware health). Save it and reuse its embedded blocks; the downstream sections below consume this output rather than re-fetching the same data.

```bash
# First step — factual baseline. Embeds overview + stats + health in one JSON.
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID report > $SCRATCH/report_JOBID.json

# List all metrics available in the data source, plus all jobs and time ranges
omnistat-inspect --tsdb-url $TSDB_URL db info
```

The `report` JSON has top-level keys `overview`, `stats`, and `health`. Everything the "Data Collection and Hardware Health Validation" and "Statistical Analysis" sections need is already in this one document — you only issue additional `stats` / `health` / `info` calls to **drill down** (finer grouping), **refresh** (after `--interval` changes), or when you deliberately **skipped** the baseline report. `job info` on its own returns just the overview subset if you ever need it in isolation.

Key information to extract:
- **Runtime**: How long did the job run?
- **Scale**: How many nodes and GPUs?
- **Sampling interval**: What time resolution is available?
- **Available metrics**: Which collectors were active? (GPU, host, network, RAS, xGMI, rocprofiler) — `db info` lists every metric present in the backend.
- **Annotations**: `rmsjob_annotations` markers (e.g., application phases, benchmark identifiers)
- **Figure of Merit**: `omnistat_fom` values (e.g., GFLOPS achieved)

The `job info` subcommand automatically includes `annotations` and `figure_of_merit` when the corresponding metrics are present in the database.

The `job info` subcommand reports the discovered sampling interval (auto-detected from the `omnistat_info` metric's `interval_secs` label). The sampling interval is also used internally by `stats`, `health`, and `iterations` to auto-compute the finest safe query step — you do not need to pass `--interval` to these subcommands.

### GPU Architecture Detection

After discovering the job, identify the GPU architecture from the available metrics and load the corresponding architecture profile for GPU-specific domain knowledge (power reporting quirks, thermal limits, memory characteristics, RAS error blocks, hardware counter formulas).

Architecture profiles are located in `skills/job-analysis/gpus/`. Read the matching profile before proceeding to data-collection and health validation.

**Detection:** Use `overview.gpu_type` from the `job info` output and apply the same substring rules as the job-report skill's "GPU Architecture Handling" table (`MI250` or `MI200 (MCM)` → MI250X; `MI300` → MI300X). When `gpu_type` is a list, apply the rule to each element.

The architecture profile contains critical information for correct interpretation of the data (e.g., which GPU cards report power, thermal throttling thresholds, RAS error block meanings).

### Resolution Sensitivity

Step resolution significantly affects observed statistics. Coarse steps (e.g., 60s) average over intervals, smearing peaks and troughs together. This can be seriously misleading:

- **Peak metrics are underestimated** at coarse resolution (e.g., peak FOM at 60s may be 10-25% lower than at 5s)
- **Mean metrics are mostly unaffected** by resolution (averaging preserves the mean)
- **Iteration boundaries blur** at coarse resolution, making it impossible to distinguish per-iteration behavior

**Always verify critical findings at the finest feasible resolution.** The finest meaningful resolution is the sampling interval reported by `job info` (from `omnistat_info`'s `interval_secs` label) — querying at a finer step than this adds no real data.

#### Step Selection

The `stats`, `health`, and `iterations` subcommands **auto-compute the finest safe query step**. The step is `max(sampling_interval, runtime / 90000)` — never finer than the actual data, never exceeding VictoriaMetrics' `search.maxPointsPerTimeseries` limit (90,000). There is no arbitrary floor: sub-second sampling intervals are preserved for short jobs where VM limits allow it.

The `iterations` subcommand does **not** accept `--interval` — it always uses auto-computed steps. The `stats` and `health` subcommands accept an optional `--interval` for time-range refinement only (the query step is still auto-computed).

For `timeseries` and `query`, the default step is the discovered sampling interval (`max(sampling_interval, 1s)`) when you reuse the cached discovery snapshot via `--cache-dir` — full resolution with no extra flags. For a coarser overview on a long job, `query` accepts an explicit `--step SECONDS`; `timeseries` has no `--step`, so adjust its resolution via the cached interval or the global `--interval`.

**When the auto-computed step is much coarser than the sampling interval** (which happens on very long jobs), state the resolution gap explicitly in the report and note which findings may be affected (especially peaks and percentiles).

**Critical rule for peak metrics:** If peak FOM, peak utilization, or peak throughput appears degraded, **always re-verify at the finest feasible step** (using `query` with an explicit `--step`) before concluding there is a peak performance difference. Apparent peak degradation is frequently an artifact of temporal averaging — the true peaks may be identical across jobs. Do not claim peak performance differs without checking at fine resolution.

### Data Collection and Hardware Health Validation

Before analyzing performance, verify that data collection was complete and reliable, and check for hardware issues. **This data is already in the baseline report's `health` block — read it from there; do not re-fetch.** Only run `health` standalone if you skipped the baseline report or need to refresh after changing `--interval`:

```bash
# Standalone health (only if you skipped the baseline report or are refreshing)
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID health
```

The `health` block covers both data-collection coverage (completeness, timing stagger, gaps) and hardware health (RAS errors, thermals, power).

#### Data-collection coverage (part of `health`)

Review the coverage portion of the health report for:
- **Missing nodes**: `expected_nodes` vs `reporting_nodes` — any gap means some nodes never reported
- **Activation stagger**: `activation.spread_seconds` — how long it took for all nodes to start reporting. A spread >5% of total job duration is significant and means early-job statistics are skewed by partial participation
- **Deactivation stagger**: `deactivation.spread_seconds` — same for shutdown. Large spread means late-job statistics are unreliable
- **Sampling gaps**: `sampling_gaps.nodes_with_gaps` and `sampling_gaps.total_gaps` — check `gap_timing` to see if gaps are clustered (systemic event, e.g., network outage) or distributed (per-node issues). Clustered gaps at the same offset suggest a single event affecting all nodes simultaneously
- **Reporting duration**: `reporting_duration.stats` — nodes with significantly shorter reporting durations may have crashed or been evicted mid-job
- **Timing source**: `timing_source` — indicates which metric was used to derive per-node timing (`rmsjob_info` in TSDB mode; in CSV mode, falls back to GPU/host metrics like `rocm_utilization_percentage` when `rmsjob_info` is absent)

#### Hardware health (`health`)

Review the health report for:
- **RAS errors**: Any hardware errors during the job
- **Thermal issues**: GPUs running hot
- **Power anomalies**: Unexpected zero-power readings
- **Push health**: Whether monitoring push duration exceeded the push interval (indicates monitoring overhead)

If critical issues are found, note them -- they may explain performance anomalies found later.

### Statistical Analysis

Follow these steps in order. **Do not skip steps or move to iteration analysis until all steps are complete.**

#### Step 1: Read the baseline stats

The job's `stats` are **already in the baseline report** (`report_JOBID.json` → `stats`): global gauge/counter summaries, hardware counters, and per-node / per-GPU variance, all in one block — no `--category` or `--level` flags exist or are needed. Read them from the baseline; only run `stats` standalone if you skipped the report or are refreshing after an `--interval` change:

```bash
# Standalone stats (only if you skipped the baseline report or are refreshing)
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID stats > $SCRATCH/stats_JOBID.json
```

The `stats` block has keys `gauges`, `counters`, `hardware_counters`, and `variance`. Counter metrics (cumulative values like bytes transferred, energy consumed) are automatically detected and produce delta-based totals. Gauge metrics produce mean/min/max/cv/percentiles. The `cv` (coefficient of variation) field measures relative dispersion — high CV indicates non-uniform distribution across GPUs or nodes.

#### Step 2: Identify anomalous metrics

Review the gauge and counter stats. For each metric, check for:
- High `cv` (uneven distribution across nodes/GPUs)
- Unexpected values (rates, totals, or distributions that differ from expectation)
- Large gaps between percentiles (for GPU utilization specifically, a near-0/near-100 split is the expected norm — not a finding; quantify idle time or phase structure rather than labeling it "bimodal")

**In comparative analysis:** compare each metric between the healthy and degraded jobs. Identify which show significant differences (>10% in rates or totals, >5 percentage points in gauge means).

#### Step 3: Inspect the variance breakdown

The `variance` block in the same `stats` output already drills into any metric whose between-node or between-GPU CV exceeds the threshold (default `cv_threshold=0.05`; override with `--cv-threshold`). Each entry first collapses every spatial key's whole-job series to **one scalar** — the `reduction` field names which one: `temporal_mean` (time-average of samples; plain gauges), `rate` (Δtotal ÷ active duration; counter-derived gauges like network RX/TX), `ratio` (Δa ÷ Δb between two counters; kernel mean dispatch duration = Δduration ÷ Δdispatches), or `total` (a single reset-aware cumulative-counter delta per key). Counter metrics (`COUNTER_LIST`: IO, network, vendor energy, xGMI) now participate in `stats.variance` with `reduction: "total"`, grouped by `source` like gauges — every counter gets a `by_node` entry; GPU-source counters (xGMI) also get `by_gpu`/`by_gpu_id`. It then carries the **between-key `cv`** (dispersion across those per-key reduced values), `min`/`max` (the lowest/highest per-key reduced value and the key that owns it — extremes of per-key reductions, **not** absolute sample minima/maxima, which live in `stats.gauges[].min`/`max`), and either an `all` map listing every key (when `n ≤ 16`) or `percentiles` over the per-key values (when `n > 16`; `--verbose` adds `all` too). The three groupings:
- `by_node` — one reduced value per node (`{instance}`); straggler nodes, systemic vs. node-local effects
- `by_gpu_id` — one per card slot (`{card}`); card-position effects
- `by_gpu` — one per (node, card) (`{instance, card}`); individual GPU stragglers

Use `--verbose` to force full per-entity arrays even for large jobs. To raise sensitivity, lower `--cv-threshold`:

```bash
# More sensitive variance drill-down: run for BOTH jobs
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID stats --cv-threshold 0.02 --verbose > $SCRATCH/stats_verbose_JOBID.json
```

The step is auto-computed to stay within `maxPointsPerTimeseries` limits, so queries should not fail due to point limits. If a query does fail, the `--interval` flag can be used as an override to force a coarser step.

The variance breakdown answers critical questions that the global summary cannot:
- Is the anomaly systemic (all nodes/GPUs equally affected) or localized?
- Is there a straggler node or a systematic card-position effect?

**In comparative analysis:** examine the `variance` block for **both** the healthy and degraded jobs so you can compare at each grouping.

The `variance` block exposes three groupings, from coarse to fine:

| Grouping | Key | What it reveals |
|----------|-----|-----------------|
| Per-node | `by_node` | Straggler nodes; systemic vs. node-local effects |
| Per-card-slot | `by_gpu_id` | Systematic card-position effects (e.g., all card-0s behaving differently) |
| Per-GPU | `by_gpu` | Individual GPU outliers masked by node/slot averages |

**GPU-specific guidance:**
- Use **`by_gpu_id`** to check for systematic card-position effects (e.g., all card-0s behaving differently)
- Use **`by_gpu`** to catch individual GPU outliers — a single underperforming GPU is masked by node-level averages and invisible at the card-slot grouping
- High CV in utilization may indicate load imbalance, but may also reflect intentionally heterogeneous workloads (e.g., data-parallel workers with unequal partition sizes). Do not assume imbalance is a problem without further evidence
- VRAM near 100% = high memory usage (may or may not indicate pressure — some workloads intentionally fill VRAM)
- Non-uniform VRAM across `by_gpu_id` = different GPUs may be doing different work; investigate before labeling as imbalance

**Network/host guidance:**
- Network and host gauges appear in `by_node`; compare per-node uniformity (CV) — low CV with all nodes equally affected points to systemic causes (topology, congestion); high CV points to node-specific issues
- If a counter total (e.g., network bytes) differs between jobs but `by_node` CV is low, the difference is systemic rather than localized to a few nodes

#### Gate check before proceeding

Before moving to iteration analysis or time-series analysis, verify:
- [ ] For every metric that shows a significant anomaly or cross-job difference in the global summary, have you examined the `variance` groupings (`by_node`, `by_gpu_id`, `by_gpu`) to determine whether the issue is systemic or localized?
- [ ] If a metric is uniform in the global summary but you expect variance, have you lowered `--cv-threshold` to confirm it is genuinely uniform rather than below the default gate?
- [ ] If GPU utilization differs, have you checked `by_node` to see whether all nodes are equally affected or if there are outliers?

If the answer to any of these is no, go back and analyze the relevant variance data before proceeding.

### Iteration-Level Analysis

Some workloads have repetitive phases that produce visible idle gaps in the averaged GPU utilization signal. The `iterations` subcommand attempts to detect these boundaries automatically. **However, iteration detection is not always meaningful** — it depends on the workload having a clear, repetitive structure visible in the GPU-averaged utilization signal. Only include iteration analysis in your report if the results are conclusive and consistent.

```bash
# Detect iterations and compute per-iteration stats
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID iterations

# With custom thresholds
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID iterations \
  --low-threshold 15 --high-threshold 75 --min-idle-seconds 20 --min-iteration-seconds 45
```

The `iterations` subcommand:
1. **Identifies iteration boundaries** from averaged GPU utilization — finds sustained idle periods (below `--low-threshold` for at least `--min-idle-seconds`) that separate iterations
2. **Computes per-iteration duration** — often the single most informative metric for detecting performance degradation
3. **Computes the utilization integral** — total GPU-%-seconds per iteration, measuring actual GPU compute work delivered independent of how long it took:
   - If utilization integral is constant across iterations but duration varies → the GPUs do the same work but something else (communication, I/O) takes longer
   - If utilization integral varies → the GPUs are doing different amounts of work per iteration
4. **Counts idle dips** — transitions from high utilization (>`--high-threshold`) to low utilization (<`--low-threshold`) within an iteration
5. **Computes time in utilization bands** — percentage of iteration spent below 20%, below 50%, above 80%, characterizing the balance between compute and communication phases

The `--min-idle-seconds` parameter prevents brief utilization dips within an iteration from being misidentified as iteration boundaries. The `--min-iteration-seconds` parameter filters out spurious short segments at job start/end.

#### When to Report Iteration Results

**Include** iteration analysis when:
- Iterations have consistent durations (low coefficient of variation)
- The number of detected iterations matches what you'd expect from the job's structure
- Per-iteration metrics tell a clear story (e.g., steady-state behavior, or a clear trend)

**Omit or flag as inconclusive** when:
- The detector finds an unexpected or irregular number of iterations
- Iteration durations vary wildly with no clear pattern
- The averaged signal doesn't show clean idle separations (the workload may not be iteration-based)
- Results are more confusing than informative

#### Validating with Per-GPU Analysis

The default iteration detection uses the **averaged** GPU utilization signal — the mean across all GPUs at each time step. This implicitly assumes the workload is roughly homogeneous across GPUs. If the workload is heterogeneous (different GPUs doing different things), the averaged signal may produce misleading iteration boundaries.

To validate, sample a few individual GPUs and compare their iteration structure to the global result:

```bash
# Iteration detection on a single GPU (node + card)
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID query \
  --promql 'rocm_utilization_percentage{instance="HOSTNAME",card="0"} * on (instance) group_left() (max by (instance) (rmsjob_info{$job,$jobstep}))' \
  > $SCRATCH/single_gpu_util.json
```

If individual GPUs show a different iteration pattern than the global average, the workload is likely heterogeneous and the global iteration analysis should not be reported as definitive. Instead, note the heterogeneity as an observation.

**Key insight:** Iteration duration is often a more reliable indicator of performance than mean utilization. Two jobs can have different mean utilization (due to different amounts of idle time) but identical peak performance and compute work — the difference is entirely in how long the communication/idle phases last.

### Annotation-Based Analysis

When `job info` reports annotations (`rmsjob_annotations` markers), analyze each annotated region separately. Annotations mark application phases (e.g., "training", "validation", "checkpoint") or benchmark stages, and different regions often have very different GPU behavior — job-level statistics average over them and can be misleading.

**Discovering annotation time ranges:** Use `timestamp()` to find when each annotation marker was active:

```promql
timestamp(count by (marker) (rmsjob_annotations{$job} > 0))
```

This returns a time series per marker. The first and last timestamps define the region where that annotation was active. Use the `query` subcommand:

```bash
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID query \
  --promql 'timestamp(count by (marker) (rmsjob_annotations{$job} > 0))' \
  > $SCRATCH/annotation_timestamps.json
```

**Per-annotation metrics:** To compute a metric scoped to a specific annotation, join through `rmsjob_info` and `rmsjob_annotations` to propagate the `marker` label. For example, average GPU utilization per annotation region:

```promql
avg by (marker) (
  avg by (instance) (rocm_utilization_percentage)
  * on (instance) group_left(jobid,marker)
    rmsjob_info{$job}
  * on (jobid) group_left(marker)
    count by (jobid,marker) (rmsjob_annotations{$job} > 0)
)
```

This pattern works with any per-node or per-GPU metric. Replace `avg by (instance) (rocm_utilization_percentage)` with the metric of interest.

**What to look for:**
- Do all annotated regions have similar utilization, or do some phases show dramatically different behavior?
- Is FOM concentrated in specific regions?
- Do idle periods between annotations explain overall low utilization?

### Time Series Analysis

For metrics or GPUs that show anomalies in the statistical analysis, fetch the raw time series.

```bash
# Export time series to file (avoids flooding context with large data)
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID timeseries --metric rocm_utilization_percentage > $SCRATCH/util_timeseries.json

# Filter to a specific node or GPU
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID timeseries --metric rocm_utilization_percentage --node hostname1 --card 0 > $SCRATCH/node1_card0.json
```

For ad-hoc investigation, use the `query` subcommand with raw PromQL:

```bash
# Custom aggregation -- average utilization across all GPUs over time
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID query \
  --promql 'avg(rocm_utilization_percentage * on (instance) group_left() (max by (instance) (rmsjob_info{$job,$jobstep})))' \
  > $SCRATCH/avg_util.json

# Max temperature per node over time
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID query \
  --promql 'max by (instance) (rocm_temperature_celsius * on (instance) group_left() (max by (instance) (rmsjob_info{$job,$jobstep})))' \
  > $SCRATCH/max_temp_per_node.json
```

### Cross-Metric Reasoning

Move beyond simple correlation to form and test hypotheses about job behavior.

#### Causal Direction

When two metrics are correlated, always ask: **which is cause and which is consequence?** Or are both caused by a third factor?

Template for every correlation:
1. "X is low and Y is low. Does low X cause low Y?"
2. "Or does low Y cause low X?"
3. "Or does some Z cause both low X and low Y?"

**Example from practice:** An investigation found lower network throughput correlated with longer HPL iterations. The initial conclusion — "network bandwidth is the bottleneck" — was challenged: lower throughput could equally be a *consequence* of MPI desynchronization (nodes not ready to receive, so effective throughput drops) rather than a *cause* (fabric unable to deliver bandwidth). The telemetry alone could not distinguish the two. This distinction matters because the remediation is completely different.

**Confidence calibration:** Your reported confidence must match the strength of evidence:
- **High confidence** requires: the data unambiguously points to a single cause, alternative hypotheses have been tested and ruled out, and the finding has been verified at fine resolution
- **Moderate confidence** is appropriate when: the data is consistent with a hypothesis but the causal direction is ambiguous, or the analysis has not been performed at all available granularities
- **Low confidence** is appropriate when: multiple hypotheses are equally consistent with the data

When causal direction is ambiguous — as it often is with correlated metrics like network throughput and GPU idle time — the report **must** present the alternative hypotheses explicitly rather than asserting one as the root cause. Stating "network degradation caused longer iterations" when the data equally supports "something caused MPI desynchronization, which manifests as both lower throughput and longer iterations" is overconfident and potentially misleading.

#### Consequence Chains

Multiple metrics moving together often indicate a single root cause propagating through a chain of effects, not multiple independent problems:

```
More idle time → lower mean utilization → lower mean power → lower mean clocks → lower mean temperature
```

This looks like four separate problems (utilization, power, clocks, temperature) but is actually one (more idle time). **Before listing multiple degraded metrics as separate findings, check whether they are all consequences of a single upstream cause.**

Indicators of a consequence chain:
- All metrics move in the same direction
- The magnitudes are proportional (e.g., 10% less utilization → ~10% less power)
- One metric logically depends on another (GPUs clock down when idle → lower power is a physical consequence, not an independent problem)

Many GPU metrics are physically linked this way: when GPUs idle (waiting for MPI, I/O, or data), utilization drops → GPU clocks reduce (DVFS) → power drops → temperature drops. Mean power, clock speed, and temperature are all **consequences** of utilization, not independent indicators. **When comparing jobs**, do not count lower mean power, lower mean clocks, and lower mean temperature as separate problems if utilization is also lower — they are all effects of the same cause (more idle time).

#### Correlation Patterns

Use these patterns as starting hypotheses, but always verify the causal direction:

| Observation | Possible Interpretation | What to check |
|---|---|---|
| Same peak performance, longer iterations | Communication/I/O bottleneck | Utilization integral (should be constant), network throughput during idle phases |
| Lower peak performance, same iteration duration | GPU compute degradation | Temperature (throttling?), clock speeds, RAS errors |
| High utilization + low FOM | Inefficient compute | Hardware counters if available, memory bandwidth |
| Low utilization + normal power | Memory-bound workload | VRAM usage, HBM bandwidth counters |
| All nodes equally degraded (low CV) | Systemic issue (topology, config) | Node placement, runtime configuration |
| One or few outlier nodes (high CV) | Node-specific issue (hardware, OS) | RAS errors, temperature, per-node stats |

Use the `query` subcommand or `timeseries` exports to examine metrics side-by-side during the same time windows.

## Comparative Analysis Across Jobs

When investigating performance differences between jobs (e.g., healthy vs degraded), single-job analysis is insufficient. You need structured cross-job comparison.

### When to Use Comparative Analysis

- A job is reported as degraded relative to a known baseline
- Multiple jobs run the same workload but achieve different FOM
- You need to determine whether a job's behavior is normal or anomalous

### Establishing a Baseline

1. Identify one or more **healthy reference jobs** running the same workload on the same system
2. Analyze the reference job first (all steps above) to understand normal behavior
3. Use the reference job's statistics as the baseline for comparison

**Critical: Compare at the finest available resolution.** Coarse-step comparisons can be misleading — a job that appears 23% degraded at 60s resolution may have identical peak performance at 5s resolution, with the difference being entirely in iteration duration.

### Systematic Elimination

When comparing healthy and degraded jobs, systematically check each potential cause and either confirm or rule it out:

| Check | What to compare | Rules out |
|-------|----------------|-----------|
| **Peak GPU performance** | Peak FOM or peak utilization at fine resolution | GPU hardware capability |
| **GPU compute work** | Utilization integral per iteration | Workload differences |
| **Iteration duration** | Per-iteration wall-clock time | Communication/I/O overhead |
| **Node balance** | Per-node metric CV and min/max spread | Straggler nodes |
| **GPU-ID balance** | Per-card statistics within nodes | Specific GPU failures |
| **CPU utilization** | Mean active cores, load1, temporal profiles | CPU contention |
| **Memory** | VRAM usage, HBM clocks (MCLK), HBM temperature | Memory issues |
| **Network throughput** | Per-NIC peak rates, sustained rates, total data | Network bottleneck |
| **Hardware health** | RAS errors, thermal throttling, power | Hardware failures |
| **Data collection** | Sampling interval, gaps, monitoring overhead | Measurement artifacts |

For each check, document whether the factor is **the same** (ruled out) or **different** (potential cause). At the end, you should have a short list of factors that actually differ, plus confidence about what does NOT explain the problem.

### Cross-Job Comparison Techniques

When comparing healthy and degraded jobs, apply the variance approach (statistical analysis) to both jobs and compare at each grouping. Key techniques for network and other metrics:

**Per-node comparison:** Compare the `by_node` variance for network metrics across both jobs. If all nodes show proportionally lower throughput in the degraded job, the issue is systemic (topology, congestion). If only specific nodes are degraded, it's localized. Use `timeseries --metric <name> --node <host>` to pull the raw series for any node that stands out, and `db info` to discover available metrics.

**Total data transferred:** Compare cumulative counter deltas (total TX bytes per iteration) rather than just rates. If the same workload transfers the same total data but at lower throughput, the network is delivering the same work more slowly.

**Temporal pattern analysis:** Some workloads have characteristic traffic patterns (e.g., increasing throughput as matrix factorization progresses in HPL). Compare the temporal shape, not just the average — a flattened pattern indicates disrupted communication phases.

**Per-node uniformity:** Compute the coefficient of variation (CV) of per-node metrics for any category. Low CV with all nodes equally affected points to systemic causes. High CV with outlier nodes points to node-specific issues.

## Domain Knowledge

GPU-specific details (power reporting quirks, thermal limits, memory characteristics, RAS error blocks, hardware counter formulas) are documented in the architecture profiles under `gpus/`. The sections below cover concepts that apply universally across GPU architectures.

### RAS Error Interpretation

> **Keep in sync:** the general thresholds below (uncorrectable > 0 → critical; correctable Δ > 1000 → warning) are duplicated in the job-report skill's "Deriving health severity" table. Update both together.

RAS (Reliability, Availability, Serviceability) error counters are **cumulative** -- they increase monotonically during GPU operation. To determine errors during a job, compare the start and end values (delta).

**General thresholds:**
- **Uncorrectable errors > 0**: Critical. Any uncorrectable ECC error indicates data corruption risk.
- **Correctable errors > 1000 (delta)**: Degrading. High correctable error rates suggest failing memory.
- **Correctable errors < 1000 (delta)**: Normal. Small numbers of correctable errors are routine.

Consult the GPU architecture profile for platform-specific error block names and their meanings.

### Hardware Counters

If `omnistat_hardware_counter` metrics are present, the `stats` subcommand discovers and summarizes them automatically under the `hardware_counters` key:

```bash
# hardware_counters (and derived FLOPS) are part of the stats output
omnistat-inspect --tsdb-url $TSDB_URL --cache-dir $SCRATCH/cache job JOBID stats > $SCRATCH/stats_JOBID.json
```

Hardware counters are **cumulative** — values grow monotonically within a session. The delta represents total work done during the job. `stats` computes this per GCD (`(instance, card)`) over the full job range, using reset-aware **`increase()`** summation (server-side on the TSDB; despike + client-side `increase()` on CSV). This spans the **fine-step** range (not a coarse step, which would truncate the first ~300 s plus a trailing partial window and undercount by 30-100 %) and tolerates the ROCm spurious-zero glitch (`100, 0, 100`). It emits totals, two rates, observed spans, a `monotonic` flag, per-series counts, and architecture-specific FLOPS for every counter present.

**Time-multiplexing is not yet handled.** When the profiler rotates through counter sets (more counters than hardware slots), a **time-multiplexed counter behaves like a *rate*, not a cumulative counter**: it resets on every counter-set change (possibly every sample), so the TSDB series is bounded/churning (e.g. `50, 50, 60, 60, 55, 55`) rather than monotonically growing. The `increase()`-based totals here (and the default `stats` output) are **not valid** for such counters and are **not yet supported**. If a counter's totals/FLOPS look implausible (e.g. far above the architecture's per-GCD peak), suspect time-multiplexing and corroborate with `num_series` and the raw `omnistat_hardware_counter` series shape (a churning, non-monotonic series confirms it).

**Active vs effective.** Each `rows[]` entry (and each `flops[]` entry) carries two rates:
- **active** (`active_rate` / `active_rate_flops_per_s`) = total ÷ each GCD's own observed span — *how fast it computed while accumulating*.
- **effective** (`effective_rate` / `effective_rate_flops_per_s`) = total ÷ full job wall time — *includes startup/activation idle*.

`active > effective` by roughly the startup-idle fraction; a low `effective` with a healthy `active` means the GCDs were idle for much of the wall clock. Use `observed_span_seconds` to see how short the accumulating window was. Note activation stagger: on large jobs only a fraction of GCDs report at the job's first/last instants, so check `num_series` reflects the full GCD count (the fine-step range query captures staggered series that a boundary query would miss).

**Reevaluation procedure.** When a row's `monotonic` is `false`, or the numbers look inconsistent with the workload (e.g. FLOPS far above the architecture's per-GCD peak, or a scenario with heavy counter multiplexing), cross-check the default against a VictoriaMetrics `increase()` query over the job range:

```promql
sum(increase(omnistat_hardware_counter{name="SQ_INSTS_VALU_MFMA_MOPS_BF16"}[<job-range>]))
```

Apply the same FLOPS formula to the `increase()` result and compare to `total_flops`. For a genuinely cumulative counter the two should agree (the default *is* a reset-aware `increase()` over the same range). Caveat: this cross-check only validates the cumulative case — it does **not** rescue a time-multiplexed counter, whose series is rate-like rather than cumulative (see "Time-multiplexing is not yet handled" above); `increase()` over a churning series is meaningless, so neither the default nor this query is valid there.

The set of counters varies by job configuration (e.g., one job may have F32 VALU counters while another has F64). The `hardware_counters` block reflects whichever counters are actually present.

**Per-GCD imbalance (FLOPS-imbalance proxy).** `hardware_counters.variance` carries per-counter-name spatial variance of the per-GCD counter totals (`reduction: "total"`, `metric: "counter_total"`), with the same `by_node`/`by_gpu_id`/`by_gpu` shape as `stats.kernels.variance` plus a `counter` field. Because FLOPS scale linearly with the hardware-counter total, the between-GCD `cv` and `min`/`max` here are a **direct FLOPS-imbalance proxy** — a single straggler GCD (and the FLOPS imbalance it implies) surfaces as a `by_gpu` entry that the scalar `num_series`/`total` in `rows[]` cannot show. CV-gated like all variance, so uniform counters are absent.

Consult the GPU architecture profile (`gpus/`) for platform-specific counter names, FLOPS formulas, per-GCD peaks, and bandwidth interpretation.

### Metric Reference

**GPU Metrics** (per-GPU, include `card` label):
| Metric | Description |
|--------|-------------|
| `rocm_utilization_percentage` | GPU compute utilization (%) |
| `rocm_vram_used_percentage` | GPU memory utilization (%) |
| `rocm_vram_total_bytes` | Total GPU memory (bytes) |
| `rocm_average_socket_power_watts` | Average socket power (W) |
| `rocm_sclk_clock_mhz` | GPU clock speed (MHz) |
| `rocm_mclk_clock_mhz` | Memory clock speed (MHz) |
| `rocm_temperature_celsius` | GPU temperature (C) |
| `rocm_temperature_memory_celsius` | Memory temperature (C) |

**Host Metrics** (per-node):
| Metric | Type | Description |
|--------|------|-------------|
| `rocm_num_gpus` | gauge | Number of GPUs in the node |
| `omnistat_host_cpu_aggregate_core_utilization` | gauge | Instantaneous busy CPU cores (0 to num_logical_cores) |
| `omnistat_host_cpu_load1` | gauge | 1-minute CPU load average |
| `omnistat_host_mem_available_bytes` | gauge | Available host memory (bytes) |
| `omnistat_host_mem_free_bytes` | gauge | Free host memory (bytes) |
| `omnistat_host_mem_total_bytes` | gauge | Total host memory (bytes) |
| `omnistat_host_io_read_local_total_bytes` | counter | Local disk reads (bytes, cumulative) |
| `omnistat_host_io_write_local_total_bytes` | counter | Local disk writes (bytes, cumulative) |

**Network Metrics** (per-node, per-interface via `interface` label):
| Metric | Type | Description |
|--------|------|-------------|
| `omnistat_network_tx_bytes` | counter | Bytes transmitted (cumulative) |
| `omnistat_network_rx_bytes` | counter | Bytes received (cumulative) |

**Vendor Metrics** (per-node, node-level power from platform BMC, `vendor` label):
| Metric | Type | Description |
|--------|------|-------------|
| `omnistat_vendor_power_watts` | gauge | Total node power (W) |
| `omnistat_vendor_accel_power_watts` | gauge | Accelerator (GPU) power (W) |
| `omnistat_vendor_cpu_power_watts` | gauge | CPU power (W) |
| `omnistat_vendor_memory_power_watts` | gauge | Memory power (W) |
| `omnistat_vendor_energy_joules` | counter | Total node energy (J, cumulative) |
| `omnistat_vendor_accel_energy_joules` | counter | Accelerator energy (J, cumulative) |
| `omnistat_vendor_cpu_energy_joules` | counter | CPU energy (J, cumulative) |
| `omnistat_vendor_memory_energy_joules` | counter | Memory energy (J, cumulative) |

**GPU Cumulative Counters** (compute delta for rates):
| Metric | Description |
|--------|-------------|
| `rocm_xgmi_total_read_kilobytes` | xGMI data read (KB, cumulative) |
| `rocm_xgmi_total_write_kilobytes` | xGMI data written (KB, cumulative) |

## Query Patterns

### The rmsjob_info Join

All GPU/node metrics must be joined with `rmsjob_info` to filter data to a specific job's time range and nodes:

```promql
metric_name * on (instance) group_left() (max by (instance) (rmsjob_info{jobid="JOBID", jobstep=~".*"}))
```

This pattern:
1. Selects `rmsjob_info` entries matching the job ID
2. Takes the `max by (instance)` to get one series per node
3. Multiplies (`*`) with the target metric, joining on the `instance` label
4. This effectively filters the metric to only the nodes and time range where the job was running

The `omnistat-inspect` tool applies this join automatically in all subcommands.

### Step Selection

For `stats`, `health`, and `iterations`, the query step is **auto-computed** as `max(sampling_interval, runtime / 90000)` — no `--interval` required. This ensures the finest resolution that is both meaningful (not finer than the data) and within VictoriaMetrics' `search.maxPointsPerTimeseries` limit.

For `timeseries` and `query`, reuse the cached discovery snapshot via `--cache-dir` so the step defaults to the discovered sampling interval (full resolution):
- `query`: pass `--step 60` for a coarser overview on long jobs; omit it for full resolution
- `timeseries`: no `--step` flag — control resolution via the cached interval or the global `--interval`

### Instant vs Range Queries

- **Range queries** (`query_range`): Return time series data over a time window. Used for trends, statistics, and time series export.
- **Instant queries** (`query_instant`): Return a single value at the current time. Less useful for historical data analysis.
- **Label values API**: Returns available label values. Used for metric discovery.

## Analysis Tracking

Every `omnistat-inspect` subcommand includes a `query_stats` block in its output:

```json
"query_stats": {
  "num_queries": 12,
  "total_query_time_seconds": 3.45,
  "queries": [...],
  "analysis_elapsed_seconds": 5.12
}
```

### How to Use Tracking Data

1. **Record** the `query_stats` from each subcommand invocation during the analysis
2. **At the end of analysis**, summarize in the single consolidated Analysis Metadata table:
   - Database and report-generated date (merged in from the former Report Metadata table)
   - Number of turns (agentic tool-executing turns taken during the analysis)
   - Total number of queries across all subcommands
   - Total query time
   - Total analysis elapsed time
   - Step resolutions used and the sampling interval

Each invocation emits its own `query_stats` block; sum the `num_queries` and `total_query_time_seconds` across the JSON outputs you saved to `$SCRATCH` to get session totals. Using `--cache-dir` avoids re-running discovery and cached module queries across invocations, so repeated runs add few or no new queries.

## Reporting Guidelines

When presenting analysis results:

1. **Lead with findings** -- start with what's unusual or noteworthy, not with raw numbers. This is satisfied by the short Findings Summary at the top of the document; the full factual report card follows it, then the detailed analysis
2. **Be concise** -- summarize statistics, don't dump tables of numbers
3. **Quantify claims** -- always cite the metric, value, and context (e.g., "GPU utilization averaged 23% across all 64 GPUs, with node abc123 at 8% mean (p5=2%, p95=15%)")
4. **Flag severity** -- use the health check severity levels (critical/warning/info/ok)
5. **Separate observations from interpretations** -- "VRAM usage varies from 5% to 95% across GPUs" is an observation; "there is a memory imbalance problem" is an interpretation. Present observations first; interpretations should be labeled as hypotheses unless confirmed by additional evidence
6. **Do not name the workload unless you know it** -- if the user hasn't told you what application is running, do not guess. Describe the behavior patterns you observe without attributing them to a specific application or algorithm
7. **Omit inconclusive analysis** -- if iteration detection produces confusing or inconsistent results, omit it from the report rather than presenting misleading data. Note that iteration analysis was attempted and was inconclusive, and explain why
8. **Include query resolution and resolution gap** -- always state the step/resolution used for queries in the Detailed Findings and Analysis section (e.g., "Query step: 15s, Sampling interval: 0.01s"). When the query step is much coarser than the sampling interval, explicitly note the resolution gap and which findings may be affected (especially peaks and high-percentile values)
9. **Include a consolidated Analysis Metadata table** -- end the report with a single `Field | Value` table (titled **Analysis Metadata**, paralleling job-report's **Report Metadata**) that merges the metadata fields (Database, Report generated) with the tracking stats (number of turns, total queries, total query time, subcommands used, query resolution, sampling interval). Do not also render a separate metadata table inside the card -- there should be exactly one such table in the document
10. **Save to scratch dir** -- write the final report to the scratch directory for reference

### Document Structure (self-contained)

The analysis is a **single self-contained document** so a reader needs nothing else. Order the sections as follows:

1. **Findings Summary.** A short executive summary at the very top — lead with what is unusual or noteworthy (guideline 1). Render it as a single uniform bullet list of **at most 5 items**, each a short, scannable sentence (one observation per bullet; avoid long multi-clause sentences). **Do not** add a separate "Health status: OK" lead line above the list; instead fold health in as the **first bullet** (e.g. "**Health is clean** — no RAS errors, no thermal throttling, complete data collection"). When the job has health issues, that first bullet states them and is usually the most important finding. Keep it brief — full detail lives in the Detailed Findings and Analysis section below. This is what preserves "lead with findings" now that the factual card sits above the deep analysis.
2. **Report Card.** Render the full factual report card **exactly as the job-report skill would**, from the baseline `report_JOBID.json` you already collected (do **not** re-query). Use job-report's fixed sections verbatim: Info; Metrics Stats (the combined `Source | Metric | Mean | Max` gauge table, counter totals, hardware counters, plus job-report's optional plain-language *Distribution notes* bullets); Variance (its own section); Data Collection Quality and Hardware Health. **Do not render a separate "Report Metadata" table inside the card** — its fields (Database, Report generated) are merged into the single consolidated Analysis Metadata table at the very end (section 4) to avoid two near-duplicate metadata tables. **Keep this section strictly factual and self-contained — it must read like a stand-alone job-report report**, with **no interpretation, hypotheses, or cross-metric reasoning here** (all job-analysis interpretation belongs in Detailed Findings below, never folded into the card). For the rendering mechanics, follow the correspondingly-named sections of the **job-report** skill rather than reproducing them: **Unit Selection** (display-unit conversions), the gauge **Distribution notes** triggers, **Variance** table shapes (transposed `Min | Typical | Max`, small-n vs large-n, the Notable nodes/GPUs outlier rule), and **Deriving health severity**. If the job-report skill is not already loaded, read its `SKILL.md` for these rules before rendering. This is the factual reference, presented up front (not an appendix).
3. **Detailed Findings and Analysis.** The heart of the job-analysis value-add — observations, hypotheses (clearly labeled), cross-metric reasoning, comparative analysis, and conclusions. This is where any interpretation that the report card deliberately omits belongs: dispersion (CV), distribution shape, per-node/per-GPU outliers, and what they imply. Include the query resolution / resolution-gap note here (guideline 8): state the step/resolution used and, when the query step is much coarser than the sampling interval, which findings may be affected.
4. **Analysis Metadata** at the very end (guideline 9). This is the **single consolidated metadata + tracking table** for the whole document — it absorbs the report-card metadata fields (Database, Report generated) so there is exactly one such table, not two. (The name parallels job-report's **Report Metadata**: each skill titles the table after its output — *Report* Metadata for the factual card, *Analysis* Metadata for the analysis.) Render it as one `Field | Value` table containing: Database; Report generated; Number of turns; Total queries; Total query time; Subcommands used; Query resolution; Sampling interval.

This is report-first: a brief Findings Summary on top for quick orientation, then the full factual report card, then the deep interpretive analysis below the card, with the consolidated Analysis Metadata table last. It preserves the factual-vs-interpretive separation — the report card is the as-is facts, the Detailed Findings and Analysis section is the analysis.
