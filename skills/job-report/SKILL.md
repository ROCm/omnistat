---
name: job-report
description: Generate a factual summary report card for an HPC job from an Omnistat database using the single-shot omnistat-inspect JSON command. Use this for a quick, comprehensive snapshot of what a job did (stats, energy, health, data quality) without diagnosing why. For root-cause investigation, performance debugging, or comparing jobs, use job-analysis instead.
allowedPrompts:
  - tool: Bash
    prompt: run omnistat-inspect
  - tool: Bash
    prompt: create temporary directory
  - tool: Bash
    prompt: read query results from file
  - tool: Bash
    prompt: write report to file
---

# Job Report

Generate a factual summary **report card** for an HPC job using GPU telemetry collected by Omnistat. A **single invocation** of `omnistat-inspect ... job <ID> report` returns one JSON document containing every datum needed to render the report. It supports flexible job-context resolution (`--start`/`--end` to skip discovery, `--cache-dir` for cheap repeat calls) and a home for deeper analysis subcommands (e.g. `iterations`).

**Target audience:** HPC engineers, AI/ML researchers, system administrators who need a quick, comprehensive snapshot of a job's behavior.

**When to use this vs `job-analysis`.** Use **job-report** when you want a quick, factual snapshot of *what* a job did — global statistics, energy, health findings, and data quality, presented as-is. This is NOT an investigation tool: it does not form hypotheses, find root causes, or compare jobs. When you need to understand *why* a job behaved as it did (bottlenecks, throttling, stragglers, regressions, healthy-vs-degraded comparison), use **job-analysis** instead. A common pattern is to run job-report first for the snapshot, then job-analysis if something looks off.

## Bash Tool Description Convention

When calling the Bash tool, phrase the `description` field to match the `allowedPrompts` declared in this skill's frontmatter so commands are auto-approved:

- `omnistat-inspect` invocation → **"run omnistat-inspect"**
- `mktemp` → **"create temporary directory"**
- `cat` / reading JSON output files → **"read query results from file"**
- Writing the final report → **"write report to file"**

## Prerequisites

1. **Data source** — one of:
   - **VictoriaMetrics running** with the Omnistat database loaded (use the `open-database` skill if needed), OR
   - **CSV exports** from `omnistat-query --export`
2. **Python virtual environment activated** with omnistat installed (`pip install ".[query]"` from the omnistat repo root). Confirm `which omnistat-inspect` resolves inside the venv.
3. **Job ID** to report on.

## One-Shot Data Collection

`omnistat-inspect ... job <ID> report` writes the JSON document to stdout — redirect it to a file. The data source (`--tsdb-url` / `--csv-dir`) and `--cache-dir` are **global** flags (before `job`); the job ID is a positional argument of the `job` group; `report` is the subcommand.

```bash
# 1. Create a scratch directory
SCRATCH=$(mktemp -d /tmp/omnistat-report-XXXXXX)
echo "Scratch directory: $SCRATCH"

# 2a. TSDB mode
TSDB_URL="http://localhost:8428"
omnistat-inspect --tsdb-url $TSDB_URL job JOBID report > $SCRATCH/report.json

# 2b. CSV mode (alternative)
omnistat-inspect --csv-dir /path/to/csv/exports job JOBID report > $SCRATCH/report.json
```

Useful flags:

| Flag | Position | Purpose |
|------|----------|---------|
| `--cache-dir DIR` | global (before `job`) | Persist the discovery snapshot and per-module results so repeat calls skip the day-scan and re-computation. |
| `--start ISO --end ISO` | `job` group (after JOBID) | Provide the job window directly to **skip job discovery** entirely (much fewer queries). Use with `--interval` when the sampling interval is known. |
| `--interval SECONDS` | `job` group | Override discovered sampling interval. |
| `--refresh` | `job` group | Force fresh discovery, ignoring any cached snapshot. |
| `--cv-threshold 0.05` | `report` subcommand | CV value above which a variance drill-down is reported (default 0.05). |
| `--verbose` | `report` subcommand | Include full per-node / per-GPU arrays under `stats.variance.by_node[*].all` and `stats.variance.by_gpu[*].all`, and expand `stats.kernels.top` from the top 10 to **all** `num_kernels` kernels. |

That single invocation produces everything the report card needs.

### Job-context flexibility

`omnistat-inspect` resolves the job window in one of two ways:

- **Discovery (default)** — `omnistat-inspect --tsdb-url $URL job JOBID report` scans the TSDB for the job's time range. Add `--cache-dir $SCRATCH/cache` so a second call (e.g. an `iterations` follow-up) rehydrates from the cached snapshot instead of re-scanning.
- **Direct window** — when you already know the job's start/end (e.g. from the scheduler), pass them to skip discovery:

  ```bash
  omnistat-inspect --tsdb-url $TSDB_URL job JOBID \
      --start 2026-01-01T12:00:00Z --end 2026-01-01T14:30:00Z --interval 10 \
      report > $SCRATCH/report.json
  ```

  The `query_stats.total_queries` field will be noticeably lower than a discovery run. (On the TSDB backend the overview still fills hosts/versions live; on CSV the direct path reports only what the window contains.)

## JSON Schema Reference

`report.json` is an **envelope** wrapping the report-card payload. The envelope keys are:

- `jobid` — the analyzed job id
- `generated_at` — ISO 8601 UTC timestamp of when the report was produced
- `data_source` — `{type: "tsdb"|"csv", url?, dir?}`; `url` is present for TSDB, `dir` for CSV. This is the source of the "Database" line in Report Metadata.
- `overview` — job identity/topology (below)
- `stats` — gauges, counters, hardware counters, **and `variance`** (below)
- `health` — data-collection coverage **and** health indicators (below)
- `query_stats` — `{total_queries, total_query_time_seconds, elapsed_seconds}`; the source of the Report Metadata counts.

Note the nesting: `variance` lives under `stats`, and both the data-collection table and the health indicators live under `health`. There is no top-level `variance`, `data_collection`, or `metadata` key.

### `overview`
- `jobid`, `user`, `partition`
- `start_time`, `end_time` (ISO 8601 UTC)
- `duration_seconds`, `duration_human` (e.g. `"2h 34m 12s"`)
- `num_nodes`, `num_gpus`, `hosts[]`
- `gpu_type`, `driver_version`, `vbios_version` — each is always a sorted list of distinct strings (single-element in the common case), or `null` if the field was not present in the data.
- `omnistat_version`, `sampling_interval` (seconds)
- `annotations[]` — strings; empty list if none
- `figure_of_merit` — list of `{name, instance, min, max, last, num_points}` or `null`

### `stats.gauges[]`
Rows in display order. Each entry: `{source, label, name, mean, min, max, unit, n, cv, percentiles}`. `n` is the size of the population behind `cv` / `percentiles` — pooled sample count for plain gauges, per-node-rate count for counter-derived rate gauges (Network RX/TX rate). `min`/`max` are absolute sample extremes over that same population (compare with `stats.variance.by_*[].min_mean`/`max_mean`, which are extremes of *per-key temporal means*). `percentiles` is `{p5, p25, p50, p75, p95}` over the pooled population. Only metrics that were present in the data are emitted — absent metrics are simply omitted from the list.

**Units in the JSON are each metric's native unit** (`W`, `MHz`, `°C`, `%`, `bytes`, `KiB`, `B/s`, `KiB/s`, `J`); labels are unit-free. The renderer is responsible for picking a sensible display unit per row (see "Unit selection" below).

### `stats.counters[]`
Each entry: `{source, label, name, total, unit}`. Same base-unit convention as gauges. Same emit-only-if-present rule.

### `stats.hardware_counters`
`null` or `{rows: [...], flops: [...] | null}`.

Each `rows[]` entry: `{counter, total, active_rate, effective_rate, observed_span_seconds, monotonic, num_series}`. Counters are summed per GCD (`(instance, card)`) at the fine step using despike + reset-aware (`increase()`) semantics, so `total` is robust to ROCm spurious-zero glitches and genuine counter resets. `active_rate` = `total ÷ observed_span_seconds` (mean per-GCD span actually accumulating); `effective_rate` = `total ÷ job duration` (charges startup/activation idle). `monotonic` is `false` when a sustained counter reset/multiplex was detected (figures are then increase-summed across the break — cross-check advisable).

Each `flops[]` entry: `{precision, kind, total_flops, active_rate_flops_per_s, effective_rate_flops_per_s}` (or the whole `flops` is `null`). `active_rate_flops_per_s` divides by the mean per-GCD active span, `effective_rate_flops_per_s` by full wall time.

### `stats.kernels`
`null` when kernel tracing was off / no kernel data exists (the report omits the Top kernels table and the folded kernel-dispatch-duration variance rows). Otherwise:

```jsonc
{
  "num_kernels": <int>,            // distinct kernels seen across the job
  "total_dispatches": <int>,       // summed over all kernels/GPUs
  "total_duration_ns": <float>,    // summed GPU time over all kernels (ns)
  "dropped_dispatches": <int>,     // node-level dropped count (0 if none)
  "top": [                         // top TOP_KERNELS_LIMIT (10) by default, or all num_kernels kernels under --verbose; ranked by total_duration_ns desc
    { "kernel": "<full mangled name>", "total_duration_ns": <float>,
      "dispatches": <int>, "mean_duration_ns": <float> }
  ],
  "variance": {
    "cv_threshold": <float>,
    "metric": "mean_dispatch_duration_ns",   // the compared quantity = Δduration_ns / Δdispatch_count
    "by_node":   [ ... ],   // key fields: {instance}
    "by_gpu_id": [ ... ],   // key fields: {card}
    "by_gpu":    [ ... ]    // key fields: {instance, card}
  }
}
```

`kernel` names are long mangled C++ symbols (600+ bytes) — keep full names in JSON, **truncate only at render time**. `total_duration_ns` and `mean_duration_ns` are native nanoseconds (the renderer converts). A kernel's share of total GPU time is `total_duration_ns / stats.kernels.total_duration_ns` (compute it at render time — there is no stored percentage field).

The three **spatial** variance lists (`by_node`/`by_gpu_id`/`by_gpu`) carry exactly the same outer shape as `stats.variance.by_*` entries (`source`/`label`/`name`/`unit`/`n`/`cv`/`min_mean`/`max_mean`/`all`|`percentiles`) **plus a `kernel` field** holding the full kernel name; `source` is `"GPU"`, `label` is `"Mean dispatch duration"`, `unit` is `"ns"`. The compared quantity is each kernel's **mean dispatch duration** (ns/dispatch) — statistically the same kind of per-key temporal mean as a gauge entry, so these fold directly into the matching gauge variance subsection. All three variance lists are `[]` when nothing crossed `cv_threshold`. Only the **top kernels** participate in variance (under `--verbose`, "top kernels" is the full kernel set, so variance is computed for every kernel).

### `stats.variance`
Nested under `stats` (not a top-level key).
- `cv_threshold`, `verbose`
- `by_node`, `by_gpu_id`, `by_gpu`: flat lists of per-metric entries (empty list when nothing varied along that axis).

Every entry, in every section, carries the same outer shape:

```jsonc
{
  "source": "...", "label": "...", "name": "...", "unit": "...",
  "n": <int>, "cv": <float>,
  "min_mean": {<key_fields>, "value": <float>},
  "max_mean": {<key_fields>, "value": <float>},

  // exactly one of the following, based on n (both in --verbose):
  "all": {<key>: <mean>, ...}                 // when n <= 16
  "percentiles": {p5, p25, p50, p75, p95}     // when n > 16
}
```

- `n` is the per-key population size (number of nodes / card slots / GPUs that contributed). Same key name as `stats.gauges[].n`, but the underlying population differs (per-sample there, per-key here).
- `cv` is the **between-key** CV across per-key temporal means (not the same as `stats.gauges[].cv`, which is the pooled per-sample CV).
- `unit` is the metric's native base unit (same convention as `stats.gauges[].unit`); the renderer picks a display unit per entry without consulting the gauge row.
- `min_mean`/`max_mean` are the extremes across **per-key temporal means** — not absolute sample extrema (those live in `stats.gauges[].min`/`max`).
- `percentiles` is `{p5, p25, p50, p75, p95}` over the same per-key-means population that `cv` is computed from. Identical key shape to `stats.gauges[].percentiles`.
- `all` is emitted when `n <= 16` (every key explicitly — at this size percentiles essentially collapse to min/max and the reader can scan every key). Slots/instances whose series are exactly 0 throughout the job (e.g. MI250X odd-card power) are filtered out and simply do not appear.

Key fields per section:
- `by_node`: `{"instance": ...}` (one per hostname).
- `by_gpu_id`: `{"card": ...}` (one per card slot; n ≤ 8 on MI250X → always carries `all`).
- `by_gpu`: `{"instance": ..., "card": ...}` (one per individual GPU). `all`, when emitted, is a list of `{instance, card, value}` triples.

A metric appears in a section iff its **between-key CV** exceeds `cv_threshold`. Empty lists mean "nothing varied along that axis". Group entries by `source` at render time to reproduce the gpu/vendor/host/network tables for `by_node` and `by_gpu`.

### `health.data_collection`
Nested under the top-level `health` key.
- `reporting_nodes`, `expected_nodes`
- `activation_stagger_seconds`, `deactivation_stagger_seconds`
- `reporting_duration_per_node_seconds`: `{mean, min, max}` (or `null` if no data)
- `nodes_with_gaps`, `total_gaps`

### `health.health`
Nested under the top-level `health` key (i.e. `health.health.indicators`). This block carries **raw numeric indicators only** — there is **no** `status` field and **no** per-finding `severity`. The renderer derives both (see "Deriving health severity" below).

`indicators[]` — a flat list; each entry has a `category` plus category-specific numeric fields. Only "worth noticing" entries are emitted (e.g. thermal only when a GPU reached ≥ 90 °C, push_trend only when the increase exceeds 25 %), so an empty list means "nothing flagged".

| `category` | Fields | Emitted when |
|------------|--------|--------------|
| `ras` | `name`, `instance`, `card`, `delta` (int) | a `rocm_ras_*` counter increased over the job (`delta > 0`); one entry per series |
| `thermal` | `instance`, `card`, `max` (°C) | a GPU's max compute-die temperature ≥ 90 °C |
| `push_exceeded` | `push_interval`, `nodes_exceeded`, `worst_instance`, `worst_max_push` | any node's push duration exceeded the configured push interval |
| `push_trend` | `first_half_mean`, `second_half_mean` | mean push duration grew > 25 % from the first half of the run to the second |

### `query_stats` (envelope)
- `total_queries`, `total_query_time_seconds`, `elapsed_seconds`.

The "Database" value for Report Metadata comes from the envelope `data_source` (`url` for TSDB, `dir` for CSV), not from this block.

## Report Rendering

Read `$SCRATCH/report.json` once and produce a single markdown report at `$SCRATCH/report-JOBID.md`. The report has the fixed section structure below — each section maps to direct field lookups.

### Sections (in order)

1. **Info** — from `overview`
2. **Metrics Stats** — combined gauge table → counter totals → hardware counters → top kernels (from `stats`; top kernels only when `stats.kernels` is non-null). Hardware counters and top kernels are rendered as peer tables within this section (no `####` subsection headings).
3. **Variance** — node-level → GPU-ID-level → GPU-level subsections, each combining gauge variance with the matching axis of kernel mean-dispatch-duration variance (from `stats.variance` and `stats.kernels.variance`)
4. **Data Collection Quality and Hardware Health** — `health.data_collection` table + derived `health.health.indicators` findings (single combined section)
5. **Report Metadata** — from the envelope `data_source` and `query_stats`

### Info

Render the overview table. For the GPU architecture row, derive a display name from each element of `gpu_type` using the rules in "GPU Architecture Handling" below — show the deduplicated, sorted list of architecture names (e.g. `MI250X`), not the raw type strings. Include `driver_version`. Omit `vbios_version` when the list has exactly one element; include it only when multiple distinct versions are present. If `annotations` is non-empty, summarise them. If `figure_of_merit` is non-null, render a small FOM table.

### Metrics Stats

**Combined gauge table** — one row per entry in `stats.gauges[]`, columns `Source | Metric | Mean | Max`. Use the `label` from each entry and pick a display unit per "Unit selection" below; append the unit to the metric name (e.g. `Memory available (GiB)`, `RX rate (GB/s)`). Do **not** add `Min`, `n`, or percentile columns to this default table — those fields are available in the JSON and feed the annotation rules below when they carry signal.

**Counter totals table** — one row per entry in `stats.counters[]`, columns `Source | Metric | Total`. Same unit-selection rules.

#### Gauge distribution annotations (optional, agent discretion)

After the gauge table, the agent MAY add a short **bullet list** under a `**Distribution notes.**` header, calling out gauge rows whose distribution shape isn't captured by Mean and Max alone. One bullet per metric (or per cluster of metrics that share the same shape and explanation). Stay silent if nothing triggers.

Each bullet is **one short sentence**, ideally under 15 words. Group related metrics in a single bullet when they share an explanation (e.g. "GPU Power, Total power, and CPU power are all pulled down by …"). Do not write multi-clause sentences with three different observations.

The triggers are evaluated against the entry's `percentiles` and `mean / min / max`, with `range = max − min` (skip if 0). Use the trigger to decide whether to comment — **do not quote percentile values, the rule name, or symbols like p25 / p95 in the prose**. Describe the shape in plain language.

- **Saturated / pinned** — `(p95 − p25) < 0.01 × range`. Phrasing: *"Frequency sits at the boost clock most of the time; mean is dragged down by idle stretches."* **Do not** write this bullet for GPU Utilization: near-0/near-100 utilization is the expected norm, so "utilization is bimodal/pinned" is not a useful observation on its own. Only comment on utilization shape if it is genuinely surprising for the job (e.g. a steady mid-range plateau, or far more idle time than the runtime suggests) — and then say *what* it implies, not that it is bimodal.
- **Wide outer tail (upper or lower)** — `(max − p95) > (p95 − p5)` or `(p5 − min) > (p95 − p5)`. Phrasing: *"Memory power: most nodes draw 81–94 W, with a few spiking to 129 W."*
- **Skewed** — `|mean − p50| > 0.1 × (p95 − p5)`. Phrasing: *"GPU Power, Total power, CPU power: long lower tails (startup/teardown samples) drag the mean below the typical reading."*

Numbers in bullets come from `mean`, `min`, `max`, or are described informally as "typical" / "most of the time" — the reader never sees percentile syntax. Aim for ≤4 bullets total; collapse same-shape metrics into one bullet rather than enumerating each.

**Hardware counters table** (if `stats.hardware_counters` is non-null) — columns `Counter | Total` from `rows[]`. If `flops[]` is non-null, follow with a FLOPS line per precision showing **both** rates from each entry: `active_rate_flops_per_s` ("active", compute speed while accumulating) and `effective_rate_flops_per_s` ("effective", over full wall time including startup idle) alongside `total_flops`. When any contributing row has `monotonic == false`, append a note that a counter reset was detected and the figures are increase-summed across the break (so they may warrant a cross-check).

xGMI read/write appears as additional rows in the combined gauge table (rates) and counter totals table (totals) when present — no separate section.

**Top kernels** (if `stats.kernels` is non-null) — rendered as a peer table within Metrics Stats, at the **same level as the hardware counters table** (a bold lead-in, not a `####` subsection heading). Omit entirely when `stats.kernels` is null.

Lead with one summary line from the top-level fields: distinct kernels (`num_kernels`), total dispatches (`total_dispatches`), and — **only when `dropped_dispatches > 0`** — call out the dropped count. Then a table, one row per entry in `stats.kernels.top`:

| Kernel | Total time | Dispatches | Mean | % time |
|--------|-----------:|-----------:|-----:|-------:|

- **Kernel** — truncate the full `kernel` name to ~60 chars with a trailing ellipsis; keep it inline-code formatted.
- **Total time** — convert `total_duration_ns` to s / ms (pick a readable unit per the Unit Selection philosophy: ns → µs ÷ 1e3 → ms ÷ 1e6 → s ÷ 1e9).
- **Dispatches** — `dispatches`.
- **Mean** — convert `mean_duration_ns` to µs / ms.
- **% time** — `total_duration_ns / stats.kernels.total_duration_ns × 100`, one decimal.

Render **one row per entry in `stats.kernels.top`**. By default that is the top 10 by total GPU time; under `--verbose` the array holds **all `num_kernels` kernels**, so the verbose report lists every kernel (still ranked by total time descending).

Stay factual — do not label any kernel "slow", "hot", or a "straggler".

### Variance

A dedicated top-level section with three fixed subsections — Node-level, GPU-ID, GPU-level. Each subsection **combines gauge variance** (`stats.variance.by_*`) **with the matching axis of kernel mean-dispatch-duration variance** (`stats.kernels.variance.by_*`); there is no standalone "Kernel variance" subsection. Rendering rules below apply uniformly per section, gated only by `n`:

- **Small section (`n ≤ 16`, entry carries `all`)** — render every key in a table. Today's `by_gpu_id` pattern.
- **Large section (`n > 16`, entry carries `percentiles`)** — render a table **transposed so each metric is a row**, with columns `Min mean | Typical | Max mean` (`min_mean.value`, the entry's `p50`, `max_mean.value`, with units, no other annotations). The outer percentiles (`p5`, `p25`, `p75`, `p95`), `cv`, and `n` are available in the JSON and feed the optional annotations below; they do not appear in the table.

A small section emerges naturally in `by_gpu_id` (n ≤ 8 on MI250X), and also in `by_node`/`by_gpu` on small-cluster jobs (1–16 nodes).

**Do not write an intro paragraph for the Variance section.** Go straight from the `## Variance` heading to the first subsection heading. The column names (`Min mean`, `Typical`, `Max mean`) and the subsection headers (`Node-level variance (3 of 14 gauges varied)`) carry the necessary context; an explanatory paragraph that defines them is redundant for the intended reader.

**Folded kernel-variance table (shared rule for every subsection).** When the kernel-variance list for a subsection's axis is non-empty (`stats.kernels.variance.by_node` for Node-level, `by_gpu_id` for GPU-ID, `by_gpu` for GPU-level), append a kernel table **after** that subsection's gauge content, under a `**Kernel mean dispatch duration**` bold lead-in (no extra heading). The compared quantity is each kernel's mean dispatch duration (ns/dispatch). Render it **transposed** — one row per kernel, columns `Min mean | Typical | Max mean` (`min_mean.value`; `p50` for large-n or the median of `all` for small-n; `max_mean.value`). Convert ns → µs/ms per Unit Selection, truncate kernel names (~60 chars + ellipsis), and add no `cv`/`n`/spread columns. Factual tone only — never call a kernel slow or a straggler.

#### Node-level variance
If `stats.variance.by_node` **and** `stats.kernels.variance.by_node` are both empty (`[]`), write: **"All nodes behaved uniformly."** Otherwise render whichever parts have signal: the gauge table(s) below when `stats.variance.by_node` is non-empty, then the folded `**Kernel mean dispatch duration**` table (shared rule above) when `stats.kernels.variance.by_node` is non-empty.

For the gauge content, group entries by their `source` field (e.g. `GPU`, `Vendor`, `Host`, `Network`).

For the large-n case (most jobs), render one table per source group, **transposed** so each metric is a row and the columns are pure numeric:

| Metric            | Min mean | Typical | Max mean |
|-------------------|---------:|--------:|---------:|
| `<entry.label>`   | `<min_mean.value>` | `<p50>` | `<max_mean.value>` |
| ...               | ...      | ...     | ...      |

All three value columns are pure numbers in the row's display unit (no parentheticals, no node identifiers — those move to the consolidated "Notable nodes / GPUs" paragraph at the end of the Variance section). Do **not** add a `spread`, `cv`, `n`, or `p5 / p50 / p95` row, and do **not** name nodes in cells — the population, dispersion, and outlier identities live in the JSON and (for outliers) in the consolidated paragraph below.

For the small-n case (`n ≤ 16`), keep the existing per-key table (one row per key, one column per entry) — that's the GPU-ID pattern below. No transposition needed because there's no key-vs-key cross-comparison to lose.

#### GPU-ID variance
If `stats.variance.by_gpu_id` **and** `stats.kernels.variance.by_gpu_id` are both empty (`[]`), write: **"All card slots behaved uniformly."** Otherwise render the gauge per-card table below when `stats.variance.by_gpu_id` is non-empty, then the folded `**Kernel mean dispatch duration**` table when `stats.kernels.variance.by_gpu_id` is non-empty.

For the gauge content (always small-n on current hardware), render a single per-card table — one row per card present in any entry's `all`, one column per entry (column header = `entry.label`). Each cell shows the slot's mean (the value from `all[card]`). Slots absent from an entry's `all` (e.g. MI250X odd cards filtered out of Power) render as a dash. Pick display units per the Unit Selection rules.

Do not write an intro paragraph above the table — the section header and table column carry the metric name, and the table itself shows the per-slot values. After the table, write at most one short line of context that adds something the table cannot show, but only when it is backed by the loaded architecture profile (e.g. a note that MI250X odd cards are filtered out of Power per the profile's power-reporting quirk). Do not assert causal mechanisms that the profile does not document. Skip the line if there's nothing architecture-specific and documented to say.

#### GPU variance
If `stats.variance.by_gpu` **and** `stats.kernels.variance.by_gpu` are both empty (`[]`), write: **"All GPUs behaved uniformly."** Otherwise render the gauge table(s) below when `stats.variance.by_gpu` is non-empty, then the folded `**Kernel mean dispatch duration**` table when `stats.kernels.variance.by_gpu` is non-empty.

For the gauge content, group entries by `source`. Large-n (typical): one table per source group, **transposed so each metric is a row**, with columns `Min mean | Typical | Max mean` (`min_mean.value`, `p50`, `max_mean.value`) — same shape as Node-level, no `cv`/`n`/spread row. Small-n (`n ≤ 16`, rare): full per-GPU table from `all`.

#### Notable nodes / GPUs (consolidated outlier callout)

After **all** the variance subsections (Node-level, GPU-ID, Per-GPU) have been rendered, append a single combined paragraph titled **"Notable nodes / GPUs"** that lists each outlier key once with all the metrics it triggered on. This is the one place node / GPU identifiers appear in the entire Variance section — the tables stay purely numeric.

Outlier detection (same rule as before, applied per-entry per-axis):

- **Upper outlier** — `max_mean.value > p75 + 1.5 × IQR` (with `IQR = p75 − p25`).
- **Lower outlier** — `min_mean.value < p25 − 1.5 × IQR`.

For each axis (`by_node`, `by_gpu`) iterate entries; for each entry that fires either trigger, attribute the outlier to the identified key (`min_mean.instance` or `max_mean.instance`, plus `card` for `by_gpu`). Aggregate across entries and axes by key, so a single node that's a lower outlier on four different metrics appears once with its four metrics enumerated.

Rendering rules for the paragraph:

- Order keys by **trigger count, descending** — the host that fires the most outliers leads. Ties broken alphabetically by key.
- For each key, one bullet (or one sentence) listing the metrics it triggered on, with direction in parens (`low` / `high`).
- When a key also appears in a `health.health.indicators[]` entry (e.g. it is a `push_exceeded` `worst_instance` or a `thermal`/`ras` `instance`), append a brief tie-in (e.g. *"; also worst push-duration node"*).
- When a key appears on both `by_node` and `by_gpu` (e.g. a node that's a low-power outlier at the node level AND has the per-GPU `node:card` echo), surface that as a single line ("`frontier04313` — low on GPU Power, Total power, Accelerator power (node), per-GPU Power on card 2; also worst push-duration node.").
- If no outliers fired anywhere in the Variance section, omit the paragraph entirely. Do **not** write "no outliers detected."

The paragraph header is **bold "Notable nodes / GPUs."** (no `###`); it sits at the bottom of the Variance section after the last subsection's "Uniform across …" sentence (if present).

Skew, kurtosis, the literal percentile vocabulary, Tukey/IQR rule names, and the bounds themselves never appear in the rendered report — they're tools the agent uses to gate, not language for the reader.

### Data Collection Quality and Hardware Health

Render `health.data_collection` as a single table (reporting/expected nodes, activation/deactivation stagger, reporting duration, nodes with gaps, total gaps). Immediately after (no section break), render `**Overall status: <derived status>**` followed by a findings table with columns `Check | Severity | Details`, one row per entry in `health.health.indicators[]`.

#### Deriving health severity

> **Keep in sync:** the RAS thresholds in the `ras` row below (uncorrectable Δ > 0 → critical; correctable Δ > 1000 → warning) are duplicated in the job-analysis skill's "RAS Error Interpretation" section. Update both together.

The JSON carries **raw indicators with no severity or status** — you assign both at render time. For each indicator, map to a severity and write the `Details` prose from its numeric fields:

| `category` | Severity heuristic | Details prose (weave in the numbers) |
|------------|--------------------|--------------------------------------|
| `ras` | `delta > 0` on any `rocm_ras_uncorrectable_*` → **critical**; `delta > 1000` on a correctable counter → **warning**; else **info** | "`<name>` rose by `<delta>` on `<instance>`:card`<card>`." |
| `thermal` | `max ≥ 100` → **critical** (throttling range); `90 ≤ max < 100` → **warning** | "`<instance>`:card`<card>` reached `<max>` °C." |
| `push_exceeded` | `nodes_exceeded ≥ 0.5 × expected_nodes` **or** `worst_max_push ≥ 5 × push_interval` → **critical**; else **warning** | "`<nodes_exceeded>` nodes exceeded the `<push_interval>` s push interval; worst `<worst_instance>` at `<worst_max_push>` s." |
| `push_trend` | `ratio = second_half_mean / first_half_mean`; `ratio ≥ 2` → **critical**; `1.25 ≤ ratio < 2` → **warning** | "Mean push duration rose from `<first_half_mean>` s to `<second_half_mean>` s (≈`<ratio>`×)." |

**Overall status** = the worst severity across all rendered rows, ordered `ok < info < warning < critical`. If `indicators[]` is empty, the status is **ok** and you write a single line (e.g. "No hardware-health indicators were flagged.") instead of a findings table.

Do not surface an indicator's raw JSON field names as table columns — fold them into the `Details` sentence.

### Report Metadata

From the envelope `data_source` and `query_stats`:

| Field | Value |
|-------|-------|
| **Database** | `data_source.url` (TSDB) or `data_source.dir` (CSV) |
| **Total queries** | `query_stats.total_queries` |
| **Total query time** | `query_stats.total_query_time_seconds` s |
| **Report generated** | `generated_at` (or today's date) |

## Unit Selection

The JSON carries values in base/SI units. The renderer picks a sensible display unit per row, balancing precision (~3–4 significant digits) and familiarity. Apply the same conversion to a row's `mean`, `max`, and `total` and to the corresponding variance entries.

Each row keeps the source metric's native unit (no upscaling to a common base) — bytes stays `bytes`, kilobytes stays `KiB`, etc. The renderer only converts *upward* from there.

| `unit` field | Conversions (divide by) |
|--------------|-------------------------|
| `bytes` | KiB ÷ 1024, MiB ÷ 1024², GiB ÷ 1024³, TiB ÷ 1024⁴, PiB ÷ 1024⁵ |
| `KiB` | MiB ÷ 1024, GiB ÷ 1024², TiB ÷ 1024³, PiB ÷ 1024⁴ |
| `B/s` | KB/s ÷ 1e3, MB/s ÷ 1e6, GB/s ÷ 1e9 (decimal prefixes are conventional for throughput) |
| `KiB/s` | MiB/s ÷ 1024, GiB/s ÷ 1024², TiB/s ÷ 1024³ (use binary prefixes since the base is binary) |
| `J` | Wh ÷ 3,600, kWh ÷ 3,600,000, MWh ÷ 3,600,000,000 |
| `W`, `MHz`, `°C`, `%` | no conversion |

Pick the largest unit that yields a value ≥ 1 (with sensible rounding). For a gauge row, choose the unit using `max` so all rows in the table share scale; for counter rows, use `total`.

Examples of typical choices on this kind of job:

- Memory available (`bytes`, max ≈ 4.5e11) → **GiB**
- Network RX rate (`B/s`, max ≈ 1e8) → **MB/s** (or GB/s if larger)
- Network RX total (`bytes`, total ≈ 1e13) → **TiB**
- Disk write (`bytes`, total ≈ 1.4e9) → **GiB**
- Disk read (`bytes`, total = 0) → render as `0 bytes`
- Vendor total energy (`J`, total ≈ 2e8) → **kWh**
- xGMI Read rate (`KiB/s`, max ≈ 1e6) → **GiB/s** on MI250X-class workloads
- xGMI Read total (`KiB`, total ≈ 1e9) → **TiB**

For variance tables, use the **same display unit** chosen for the corresponding gauge/counter row so the numbers are directly comparable.

## GPU Architecture Handling

Map `overview.gpu_type` to a canonical architecture name using these substring rules (apply to each string when `gpu_type` is a list):

| Match (substring) | Architecture name | Profile |
|---|---|---|
| `MI250` or `MI200 (MCM)` | `MI250X` | `mi250x.md` |
| `MI300` | `MI300X` | `mi300x.md` |

(`MI200 (MCM)` is used instead of bare `MI200` to avoid false-matching MI210 type strings, which also contain `MI200`.)

Load the matching profile from `skills/job-analysis/gpus/` for FLOPS formula context and architecture-specific rendering notes:

- **MI250X** — odd cards report 0 W; `omnistat-inspect` already filters all-zero odd-card series, so Power appears with `n=256` rather than `512` in `by_gpu`. Render `GPU Power (W)` rows as-is without a "(even cards only)" qualifier.
- **MI300X** — no power-reporting quirk; each OAM is a single card, so Power `by_gpu` series count matches the full GPU count.

## Verbose-Mode Rendering Rule

In `--verbose` mode every entry carries `all` in addition to whatever it would carry by default (`percentiles` for large-n entries; `all` is already the default for small-n). For large-n verbose runs:
- `by_node`: append a full per-node table per source group.
- `by_gpu`: append a full per-GPU table per metric.
- `by_gpu_id`: no change (already shows `all` in default mode).

Verbose mode also expands `stats.kernels.top` from the top 10 to **all `num_kernels` kernels**. The Top kernels table therefore lists every kernel (still ranked by total GPU time) in a verbose report, instead of just the top 10.

## Tone and Style Rules

- **Factual and descriptive** — present numbers, do not interpret them or draw conclusions about performance.
- **Architecture-specific behaviors are facts, not anomalies** — note them matter-of-factly.
- **Health findings** — derive severity at render time per the "Deriving health severity" table; the JSON carries raw indicators only.
- **Human-readable units** — the JSON carries base units; the renderer converts (kWh, GiB/TiB, GB/s, etc.) per the Unit Selection rules above.
- **Markdown tables** for multi-value statistics — keep them scannable.
- **One combined gauge table** — all Mean/Max gauges in a single table with a Source column.
- **Variance is its own section** — never embed variability tables in Metrics Stats.
- **No recommendations, root cause analysis, or hypothesis testing.**

## Conciseness Rules

- **No table preamble that restates the overview.**
- **Minimize table notes** — only include notes for non-obvious things.
- **Uniformity notes in Variance** — single sentence when nothing to report. Do not render `cv`, `n`, or percentile values in the variance tables or the gauge table; they are gating tools only.
- **GPU table: omit MCLK, CU occupancy.**
- **Host metrics: CPU utilization and memory only** — omit load average.
- **Data collection quality: omit gap percentiles and onset range.**

## Report Metadata source

Read `query_stats.total_queries` and `query_stats.total_query_time_seconds` directly from the `report.json` envelope — there is no separate `query_log.json` to parse, and no `metadata` block.
