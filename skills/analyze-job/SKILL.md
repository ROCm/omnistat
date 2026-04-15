---
name: analyze-job
description: Analyze an HPC job from an Omnistat database using hypothesis-driven exploration.
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

# Analyze HPC Job

Analyze GPU telemetry data collected by Omnistat for HPC/AI workloads. This skill guides you through a top-down, hypothesis-driven analysis of job performance using the `omnistat-inspect` CLI tool.

**Target audience:** HPC engineers, AI/ML researchers, system administrators investigating job performance, GPU health, and resource utilization.

**What the analysis produces:** A structured report identifying performance bottlenecks, hardware issues, resource utilization patterns, and anomalies -- with all findings backed by data.

## Prerequisites

1. **VictoriaMetrics running** with the Omnistat database loaded (use the `load-database` skill if needed)
2. **Python virtual environment activated** with omnistat installed (`pip install ".[query]"` from the omnistat repo root)
3. **Job ID(s)** to analyze (discover available jobs with `omnistat-inspect --tsdb-url $TSDB_URL db-info`)

## Setup

Before starting analysis, set up the working environment:

```bash
# 1. Create a scratch directory for this analysis session
SCRATCH=$(mktemp -d /tmp/omnistat-inspect-XXXXXX)
echo "Scratch directory: $SCRATCH"

# 2. Set the TSDB URL (VictoriaMetrics or Prometheus)
TSDB_URL="http://localhost:8428"

# 3. Verify connectivity and discover available jobs
omnistat-inspect --tsdb-url $TSDB_URL db-info
```

The `db-info` subcommand verifies database connectivity and reports all available jobs with their time ranges, node counts, users, and partitions, plus the full list of available metrics. Use this output to select a job ID and confirm you are looking at the right database.

## Analysis Workflow

Follow this top-down, hypothesis-driven workflow. Each phase builds on the previous one. You have freedom to explore and investigate -- this is a methodology guide, not a rigid script.

### Epistemic Discipline

**Do not assume what the workload is.** Unless the user tells you the application name, or annotations/metadata explicitly identify it, treat the workload as unknown. Describe what the telemetry shows (e.g., "GPU utilization is bimodal with sustained 100% phases separated by idle dips") rather than what you think it means (e.g., "this is a training workload doing forward/backward passes"). If you need to speculate, label it clearly as a hypothesis.

**Do not assume the workload is homogeneous.** A single HPC job may run different tasks on different nodes or GPUs. Some nodes may run data loading, others may run compute, others may handle communication. VRAM differences across GPUs, utilization variance across nodes, or non-uniform network traffic are signals of heterogeneity, not necessarily problems. Before reporting "imbalance" as a finding, consider whether the workload is intentionally heterogeneous.

**Report what you observe, not what you expect.** If a metric looks unusual, describe the observation and its magnitude. Do not assume it is a problem unless you have evidence of impact (e.g., on runtime, throughput, or health). An observation like "5% of GPUs use 10x more VRAM than the rest" is a fact; "there is a memory imbalance problem" is an interpretation that may be wrong.

### Phase 1: Job Discovery and Characterization

Start by understanding what the job is and what resources it used.

```bash
# Discover job time range, topology, and metadata
omnistat-inspect --tsdb-url $TSDB_URL --scratch-dir $SCRATCH job-info --job JOBID

# List all available metrics for this job, grouped by category
omnistat-inspect --tsdb-url $TSDB_URL metrics --job JOBID --categorize
```

Key information to extract:
- **Runtime**: How long did the job run?
- **Scale**: How many nodes and GPUs?
- **Sampling interval**: What time resolution is available?
- **Available metrics**: Which collectors were active? (GPU, host, network, RAS, xGMI, rocprofiler)
- **Annotations**: `rmsjob_annotations` markers (e.g., application phases, benchmark identifiers)
- **Figure of Merit**: `omnistat_fom` values (e.g., GFLOPS achieved)

The `job-info` subcommand automatically includes `annotations` and `figure_of_merit` when the corresponding metrics are present in the database.

The `job-info` subcommand automatically discovers the sampling interval from the `omnistat_info` metric (via the `interval_secs` label) and reports it as `sampling_intervals`, `min_interval`, and `max_interval` in its output. The sampling interval is also auto-discovered during job discovery and used internally by `stats`, `health`, and `iterations` to auto-compute the finest safe query step — you do not need to pass `--interval` to these subcommands.

### GPU Architecture Detection

After discovering the job, identify the GPU architecture from the available metrics and load the corresponding architecture profile for GPU-specific domain knowledge (power reporting quirks, thermal limits, memory characteristics, RAS error blocks, hardware counter formulas).

Architecture profiles are located in `skills/analyze-job/gpus/`. Read the matching profile before proceeding to Phase 2.

**Detection:** Query `rocm_version_info` for the job — the `type` label identifies the GPU architecture (e.g., `"Aldebaran/MI200 [Instinct MI250X]"` or `"AMD INSTINCT MI200 (MCM) OAM ..."`). Match on substring:
- `type` contains `MI250` or `MI200` → **MI250X** (`gpus/mi250x.md`)

The architecture profile contains critical information for correct interpretation of the data (e.g., which GPU cards report power, thermal throttling thresholds, RAS error block meanings).

### Resolution Sensitivity

Step resolution significantly affects observed statistics. Coarse steps (e.g., 60s) average over intervals, smearing peaks and troughs together. This can be seriously misleading:

- **Peak metrics are underestimated** at coarse resolution (e.g., peak FOM at 60s may be 10-25% lower than at 5s)
- **Mean metrics are mostly unaffected** by resolution (averaging preserves the mean)
- **Iteration boundaries blur** at coarse resolution, making it impossible to distinguish per-iteration behavior

**Always verify critical findings at the finest feasible resolution.** The finest meaningful resolution is the sampling interval reported by `job-info` (from `omnistat_info`'s `interval_secs` label) — querying at a finer step than this adds no real data.

#### Step Selection

The `stats`, `health`, and `iterations` subcommands **auto-compute the finest safe query step**. The step is `max(sampling_interval, runtime / 90000)` — never finer than the actual data, never exceeding VictoriaMetrics' `search.maxPointsPerTimeseries` limit (90,000). There is no arbitrary floor: sub-second sampling intervals are preserved for short jobs where VM limits allow it.

You do **not** need to pass `--interval` to these subcommands — the sampling interval is auto-discovered from `omnistat_info` during job discovery. If you do pass `--interval`, it is used only for time-range refinement, not for the query step.

For `timeseries` and `query` subcommands, you control the step explicitly via `--interval` or `--step`. Use the sampling interval reported by `job-info` for full resolution, or a coarser value for overview queries on long jobs.

**When the auto-computed step is much coarser than the sampling interval** (which happens on very long jobs), state the resolution gap explicitly in the report and note which findings may be affected (especially peaks and percentiles).

**Critical rule for peak metrics:** If peak FOM, peak utilization, or peak throughput appears degraded, **always re-verify at the finest feasible step** (using `query` with an explicit `--step`) before concluding there is a peak performance difference. Apparent peak degradation is frequently an artifact of temporal averaging — the true peaks may be identical across jobs. Do not claim peak performance differs without checking at fine resolution.

### Phase 2: Data Collection and Hardware Health Validation

Before analyzing performance, verify that data collection was complete and reliable, and check for hardware issues.

```bash
# Validate data collection completeness, timing stagger, and gaps
omnistat-inspect --tsdb-url $TSDB_URL --scratch-dir $SCRATCH data-check --job JOBID

# Run hardware health checks (RAS errors, thermals, power)
omnistat-inspect --tsdb-url $TSDB_URL --scratch-dir $SCRATCH health --job JOBID
```

#### Data collection (`data-check`)

Review the data-check report for:
- **Missing nodes**: `expected_nodes` vs `reporting_nodes` — any gap means some nodes never reported
- **Activation stagger**: `activation.spread_seconds` — how long it took for all nodes to start reporting. A spread >5% of total job duration is significant and means early-job statistics are skewed by partial participation
- **Deactivation stagger**: `deactivation.spread_seconds` — same for shutdown. Large spread means late-job statistics are unreliable
- **Sampling gaps**: `sampling_gaps.nodes_with_gaps` and `sampling_gaps.total_gaps` — check `gap_timing` to see if gaps are clustered (systemic event, e.g., network outage) or distributed (per-node issues). Clustered gaps at the same offset suggest a single event affecting all nodes simultaneously
- **Reporting duration**: `reporting_duration.stats` — nodes with significantly shorter reporting durations may have crashed or been evicted mid-job

#### Hardware health (`health`)

Review the health report for:
- **RAS errors**: Any hardware errors during the job
- **Thermal issues**: GPUs running hot
- **Power anomalies**: Unexpected zero-power readings
- **Push health**: Whether monitoring push duration exceeded the push interval (indicates monitoring overhead)

If critical issues are found, note them -- they may explain performance anomalies found later.

### Phase 3: Statistical Analysis

Follow these steps in order. **Do not skip steps or move to iteration analysis until all steps are complete.**

#### Step 1: Collect global-level stats

Run global-level stats for each job.

```bash
omnistat-inspect --tsdb-url $TSDB_URL --scratch-dir $SCRATCH stats --job JOBID --level global
```

The output is nested as `results_by_category → {category} → {level} → [metric stats]`. Counter metrics (cumulative values like bytes transferred, energy consumed) are automatically detected and produce delta-based stats (total_delta, rate_per_second, per-series mean/min/max/stddev). Gauge metrics produce the standard count/min/max/mean/stddev/percentiles distribution.

#### Step 2: Identify anomalous categories

Review the global stats. For each category, check for:
- High stddev relative to mean (uneven distribution)
- Unexpected values (rates, totals, or distributions that differ from expectation)
- Bimodal distributions or large gaps between percentiles

**In comparative analysis:** compare each category's global stats between the healthy and degraded jobs. Identify which categories show significant differences (>10% in rates or totals, >5 percentage points in gauge means).

#### Step 3: Drill down (required)

For every category identified in Step 2 as anomalous or significantly different between jobs, **run stats at finer levels now.** Do not defer this to recommendations — do it before writing the report.

```bash
# Network drill-down: run for BOTH jobs
omnistat-inspect --tsdb-url $TSDB_URL stats --job JOBID --category network --level interface-id
omnistat-inspect --tsdb-url $TSDB_URL stats --job JOBID --category network --level node

# GPU drill-down: run for BOTH jobs
omnistat-inspect --tsdb-url $TSDB_URL stats --job JOBID --category gpu --level node
omnistat-inspect --tsdb-url $TSDB_URL stats --job JOBID --category gpu --level gpu-id
```

The step is auto-computed to stay within `maxPointsPerTimeseries` limits, so queries should not fail due to point limits. If a query does fail, the `--interval` flag can be used as an override to force a coarser step.

The drill-down answers critical questions that global stats cannot:
- Is the anomaly systemic (all nodes/interfaces equally affected) or localized?
- For network: are all interface types proportionally affected, or is a specific NIC position degraded?
- For GPU: is there a straggler node or a systematic card-position effect?

**In comparative analysis:** run the drill-down for **both** the healthy and degraded jobs so you can compare at each level.

The available levels per category:

| Category | Levels (coarse → fine) |
|----------|----------------|
| `gpu` | global → node → gpu-id → gpu |
| `network` | global → node → interface-id → interface |
| `xgmi` | global → node → gpu-id → gpu |
| `host` | global → node |
| `vendor` | global → node |

**GPU-specific guidance:**
- Use **gpu-id** level to check for systematic card-position effects (e.g., all card-0s behaving differently)
- Always run **gpu** level to catch individual GPU outliers — a single underperforming GPU is masked by node-level averages and invisible at gpu-id level
- High stddev in utilization may indicate load imbalance, but may also reflect intentionally heterogeneous workloads (e.g., data-parallel workers with unequal partition sizes). Do not assume imbalance is a problem without further evidence
- VRAM near 100% = high memory usage (may or may not indicate pressure — some workloads intentionally fill VRAM)
- Non-uniform VRAM across gpu-id = different GPUs may be doing different work; investigate before labeling as imbalance

**Network-specific guidance:**
- Use **interface-id** level to check whether all interfaces of the same type behave similarly, or if specific interface positions are degraded
- Use **interface** level to identify specific NICs with anomalous throughput
- If all interfaces show proportionally lower throughput, the issue is systemic (topology, congestion); if only some are degraded, it's interface-specific
- Compare per-node network uniformity (CV) — low CV with all nodes equally affected points to systemic causes; high CV points to node-specific issues

#### Gate check before proceeding

Before moving to iteration analysis or Phase 4, verify:
- [ ] For every category that shows a significant anomaly or cross-job difference at the global level, have you examined the finer-level data (node, interface-id, gpu-id) to determine whether the issue is systemic or localized?
- [ ] If network throughput differs between jobs, have you checked the interface-id level data to see whether all interfaces are proportionally affected?
- [ ] If GPU utilization differs, have you checked the node level to see whether all nodes are equally affected or if there are outliers?

If the answer to any of these is no, go back and analyze the relevant finer-level data before proceeding.

#### Category and Level Reference

| Category | Valid Levels | Description |
|----------|-------------|-------------|
| `gpu` | global, node, gpu-id, gpu | GPU metrics grouped by instance/card |
| `host` | global, node | Host CPU/memory/IO grouped by instance |
| `network` | global, node, interface-id, interface | Network TX/RX grouped by instance/interface |
| `vendor` | global, node | Vendor power/energy grouped by instance |
| `xgmi` | global, node, gpu-id, gpu | xGMI data transfer grouped by instance/card |

### Iteration-Level Analysis

Some workloads have repetitive phases that produce visible idle gaps in the averaged GPU utilization signal. The `iterations` subcommand attempts to detect these boundaries automatically. **However, iteration detection is not always meaningful** — it depends on the workload having a clear, repetitive structure visible in the GPU-averaged utilization signal. Only include iteration analysis in your report if the results are conclusive and consistent.

```bash
# Detect iterations and compute per-iteration stats
omnistat-inspect --tsdb-url $TSDB_URL --scratch-dir $SCRATCH iterations --job JOBID

# With custom thresholds
omnistat-inspect --tsdb-url $TSDB_URL iterations --job JOBID \
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
omnistat-inspect --tsdb-url $TSDB_URL query --job JOBID --interval INTERVAL \
  --promql 'rocm_utilization_percentage{instance="HOSTNAME",card="0"} * on (instance) group_left() (max by (instance) (rmsjob_info{$job,$step}))' \
  --output $SCRATCH/single_gpu_util.json
```

If individual GPUs show a different iteration pattern than the global average, the workload is likely heterogeneous and the global iteration analysis should not be reported as definitive. Instead, note the heterogeneity as an observation.

**Key insight:** Iteration duration is often a more reliable indicator of performance than mean utilization. Two jobs can have different mean utilization (due to different amounts of idle time) but identical peak performance and compute work — the difference is entirely in how long the communication/idle phases last.

### Annotation-Based Analysis

When `job-info` reports annotations (`rmsjob_annotations` markers), analyze each annotated region separately. Annotations mark application phases (e.g., "training", "validation", "checkpoint") or benchmark stages, and different regions often have very different GPU behavior — job-level statistics average over them and can be misleading.

**Discovering annotation time ranges:** Use `timestamp()` to find when each annotation marker was active:

```promql
timestamp(count by (marker) (rmsjob_annotations{$job} > 0))
```

This returns a time series per marker. The first and last timestamps define the region where that annotation was active. Use the `query` subcommand:

```bash
omnistat-inspect --tsdb-url $TSDB_URL query --job JOBID --interval INTERVAL \
  --promql 'timestamp(count by (marker) (rmsjob_annotations{$job} > 0))' \
  --output $SCRATCH/annotation_timestamps.json
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

### Phase 4: Time Series Analysis

For metrics or GPUs that show anomalies in Phase 3, fetch the raw time series.

```bash
# Export time series to file (avoids flooding context with large data)
omnistat-inspect --tsdb-url $TSDB_URL timeseries --job JOBID --interval INTERVAL --metric rocm_utilization_percentage --output $SCRATCH/util_timeseries.json

# Filter to a specific node or GPU
omnistat-inspect --tsdb-url $TSDB_URL timeseries --job JOBID --interval INTERVAL --metric rocm_utilization_percentage --node hostname1 --card 0 --output $SCRATCH/node1_card0.json
```

For ad-hoc investigation, use the `query` subcommand with raw PromQL:

```bash
# Custom aggregation -- average utilization across all GPUs over time
omnistat-inspect --tsdb-url $TSDB_URL query --job JOBID --interval INTERVAL \
  --promql 'avg(rocm_utilization_percentage * on (instance) group_left() (max by (instance) (rmsjob_info{$job,$step})))' \
  --output $SCRATCH/avg_util.json

# Max temperature per node over time
omnistat-inspect --tsdb-url $TSDB_URL query --job JOBID --interval INTERVAL \
  --promql 'max by (instance) (rocm_temperature_celsius * on (instance) group_left() (max by (instance) (rmsjob_info{$job,$step})))' \
  --output $SCRATCH/max_temp_per_node.json
```

### Phase 5: Cross-Metric Reasoning

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
2. Analyze the reference job first (Phases 1-5) to understand normal behavior
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

When comparing healthy and degraded jobs, apply the drill-down approach (Phase 3) to both jobs and compare at each level. Key techniques for network and other categories:

**Per-interface comparison:** Run `stats --category network --level interface-id` for both jobs. If all interfaces show proportionally lower throughput in the degraded job, the issue is systemic (topology, congestion). If only specific interfaces are degraded, it's interface-specific. Use the `metrics` subcommand to discover available interfaces and determine which carry application traffic vs management traffic.

**Total data transferred:** Compare cumulative counter deltas (total TX bytes per iteration) rather than just rates. If the same workload transfers the same total data but at lower throughput, the network is delivering the same work more slowly.

**Temporal pattern analysis:** Some workloads have characteristic traffic patterns (e.g., increasing throughput as matrix factorization progresses in HPL). Compare the temporal shape, not just the average — a flattened pattern indicates disrupted communication phases.

**Per-node uniformity:** Compute the coefficient of variation (CV) of per-node metrics for any category. Low CV with all nodes equally affected points to systemic causes. High CV with outlier nodes points to node-specific issues.

## Domain Knowledge

GPU-specific details (power reporting quirks, thermal limits, memory characteristics, RAS error blocks, hardware counter formulas) are documented in the architecture profiles under `gpus/`. The sections below cover concepts that apply universally across GPU architectures.

### RAS Error Interpretation

RAS (Reliability, Availability, Serviceability) error counters are **cumulative** -- they increase monotonically during GPU operation. To determine errors during a job, compare the start and end values (delta).

**General thresholds:**
- **Uncorrectable errors > 0**: Critical. Any uncorrectable ECC error indicates data corruption risk.
- **Correctable errors > 1000 (delta)**: Degrading. High correctable error rates suggest failing memory.
- **Correctable errors < 1000 (delta)**: Normal. Small numbers of correctable errors are routine.

Consult the GPU architecture profile for platform-specific error block names and their meanings.

### Consequence Chains in GPU Metrics

Many GPU metrics are physically linked. When GPUs are idle (waiting for MPI, I/O, or data):

- Utilization drops → GPU clocks reduce (DVFS) → power consumption drops → temperature drops

Mean power, clock speed, and temperature are all **consequences** of utilization, not independent indicators. When comparing jobs, do not count lower mean power, lower mean clocks, and lower mean temperature as separate problems if utilization is also lower — they are all effects of the same cause (more idle time).

### Hardware Counters

If `omnistat_hardware_counter` metrics are present, use the `counters` subcommand to discover and summarize them:

```bash
# Discover which counters are present and compute per-counter statistics
omnistat-inspect --tsdb-url $TSDB_URL --scratch-dir $SCRATCH counters --job JOBID
```

Hardware counters are **cumulative** — values grow monotonically within a session. The delta (last - first) represents total work done during the job. The `counters` subcommand automatically computes these deltas, rates, and per-series statistics for every counter present.

The set of counters varies by job configuration (e.g., one job may have F32 VALU counters while another has F64). The subcommand discovers which counters are actually present.

Consult the GPU architecture profile (`gpus/`) for platform-specific counter names, FLOPS formulas, and bandwidth interpretation.

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

For `timeseries` and `query`, the `--interval` parameter determines the query step:
- Use the sampling interval (from `job-info`) for full-resolution data
- Use a coarser step (e.g., `--step 60`) for overview queries on long jobs

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
2. **At the end of analysis**, summarize:
   - Total number of queries across all subcommands
   - Total query time
   - Total analysis elapsed time
   - Step resolutions used

When using `--scratch-dir`, a cumulative `query_log.json` is automatically maintained across all invocations. Review it at the end of the analysis session.

## Reporting Guidelines

When presenting analysis results:

1. **Lead with findings** -- start with what's unusual or noteworthy, not with raw numbers
2. **Be concise** -- summarize statistics, don't dump tables of numbers
3. **Quantify claims** -- always cite the metric, value, and context (e.g., "GPU utilization averaged 23% across all 64 GPUs, with node abc123 at 8% mean (p5=2%, p95=15%)")
4. **Flag severity** -- use the health check severity levels (critical/warning/info/ok)
5. **Separate observations from interpretations** -- "VRAM usage varies from 5% to 95% across GPUs" is an observation; "there is a memory imbalance problem" is an interpretation. Present observations first; interpretations should be labeled as hypotheses unless confirmed by additional evidence
6. **Do not name the workload unless you know it** -- if the user hasn't told you what application is running, do not guess. Describe the behavior patterns you observe without attributing them to a specific application or algorithm
7. **Omit inconclusive analysis** -- if iteration detection produces confusing or inconsistent results, omit it from the report rather than presenting misleading data. Note that iteration analysis was attempted and was inconclusive, and explain why
8. **Include query resolution and resolution gap** -- always state the step/resolution used for queries in the job summary (e.g., "Query step: 15s, Sampling interval: 0.01s"). When the query step is much coarser than the sampling interval, explicitly note the resolution gap and which findings may be affected (especially peaks and high-percentile values)
9. **Include tracking summary** -- end the report with total queries executed, total query time, and total analysis time
10. **Save to scratch dir** -- write the final report to the scratch directory for reference
