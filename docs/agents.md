# Agent Support

```eval_rst
.. toctree::
   :glob:
   :maxdepth: 4
```

In addition to the [Grafana](./grafana.md) dashboards and command-line
[report card](query_report_card) already available, Omnistat
provides a set of *skills* that let an AI coding agent explore, summarize, and
analyze collected telemetry. Whether data was gathered in {ref}`user-mode or system-mode
<user-vs-system>`, an agent can load an Omnistat database, produce a factual
report card for a job, or run a hypothesis-driven investigation into why a job
behaved as it did. This complements the interactive dashboards and standalone
query utilities documented elsewhere.

The short recording below demonstrates the workflow end to end: a Claude Code
session that produces a job report and analysis, driven entirely by
natural-language prompts.

<video width="640" controls>
  <source src="https://github.com/user-attachments/assets/26fdc2fa-25c0-4b56-8cc1-df0f0647c7c8" type="video/mp4">
</video>
<p><em>Generating a job report and analysis with an agent.</em></p>

## The skills

The skills are markdown playbooks (`SKILL.md`) that guide an agent through
loading, summarizing, or analyzing collected telemetry from an Omnistat data
source, using shell commands and Omnistat's tooling. Three skills are
available:

- **open-database**: load an Omnistat database with VictoriaMetrics so it can
  be queried, then verify the data is present and ready.
- **job-report**: produce a one-shot, factual *report card* describing what a
  job did: global statistics, energy, health findings, and data quality. It can
  also flag statistically significant differences between GPUs or nodes (spatial
  outliers), but each metric is reduced over the whole run, so the report does
  not examine behavior along the time axis. This is a snapshot, not an
  investigation.
- **job-analysis**: run a hypothesis-driven investigation into *why* a job
  behaved as it did: performance bottlenecks, stragglers, hardware/health
  issues, or comparing a degraded job against a healthy baseline. Unlike the
  report, it also works along the time axis, describing what happened over the
  course of the run and using those temporal patterns as evidence.

A common workflow is to run **job-report** first for the snapshot, then reach
for **job-analysis** when something looks off. The job-analysis skill also
carries per-architecture profiles (`gpus/mi250x.md`, `gpus/mi300x.md`) that
supply additional context and architecture-specific rendering notes. These
two job-related skills drive `omnistat-inspect`, Omnistat's consolidated
analysis CLI for agents.

## Setup

### Prerequisites

Before invoking a skill, ensure the following are in place:

1. **A Python environment with omnistat installed**, providing the
   `omnistat-inspect` command. See the [installation](installation/index.md)
   guide.
2. **A data source**, either a running VictoriaMetrics instance with the
   Omnistat database loaded (the **open-database** skill automates this), or
   CSV exports produced by `omnistat-query --export`.
3. **The Omnistat skills**, obtained from a checkout of the [source
   repository](https://github.com/ROCm/omnistat) and enabled for your agent
   (see *Enabling the skills with an agent* below).

### Enabling the skills with an agent

The skills live in the Omnistat [source
repository](https://github.com/ROCm/omnistat) under the `skills/` directory.
They are not part of the installed Python package, so you need a checkout of
the repository to use them. The `SKILL.md` format is agent-agnostic, and the
same directories work with any agent that can read the playbooks and run shell
commands. To enable them, symlink (or copy) each skill directory into the
location your agent scans; symlinking keeps the skills in sync with your
repository checkout:

```
<agent-skills-dir>/
├── open-database -> <omnistat-dir>/skills/open-database
├── job-report    -> <omnistat-dir>/skills/job-report
└── job-analysis  -> <omnistat-dir>/skills/job-analysis
```

Here `<omnistat-dir>` is your Omnistat repository checkout, which contains the
`skills/` directory.

Replace `<agent-skills-dir>` with the path for your agent below. Most agents
support a *project* scope (a directory in the working tree, shared with the
project) and a *personal* scope (a directory under your home, available across
all sessions). Once the skills are discovered, the agent selects one implicitly
by matching your request against its `description`, or you can invoke one explicitly with
`/skills`. Restart the agent if a newly added skill does not appear.

- **[Claude Code](https://www.claude.com/product/claude-code)**: scans
  `.claude/skills/` (project) or `~/.claude/skills/` (personal).
- **[OpenAI Codex](https://developers.openai.com/codex/)**: scans
  `.agents/skills/` from the working directory up to the repository root
  (project) or `~/.agents/skills/` (personal); skills can also be mentioned
  explicitly with a `$` prefix.
- **[OpenCode](https://opencode.ai/)**: scans its native `.opencode/skills/`
  (project) or `~/.config/opencode/skills/` (personal), and also reads the
  `.claude/skills/` and `.agents/skills/` locations above, so skills already
  enabled for Claude Code or Codex are picked up with no extra setup.

## Examples

### Prompts

Once the skills are enabled, describe what you want in natural language and let
the agent select and drive the appropriate skill. For example:

- *"Give me a report card for the last job under the `/path/to/omnistat/db`
  database."*
- *"Analyze why job 51820 ran slower than 51774. Keep it to the top few
  findings, max 15 lines."*
- *"The `/path/to/omnistat/db` database has runs of the same workload from 1
  up to 1024 nodes: do a scalability analysis and identify any obvious
  bottleneck."*

### Sessions

The two sessions below show the kind of end-to-end interaction the skills
enable. The agent decides which skill to invoke and which `omnistat-inspect`
commands to run, and the user only supplies the natural-language request.

#### Example session 1: a factual report card

> **You:** *"Open the database at `/scratch/omnistat/run-2026-05-17` and give me a
> report card for job 44092."*

The agent uses the **open-database** skill to launch VictoriaMetrics against
the database directory, verifies the job is present, then applies the
**job-report** skill. Under the hood it makes a single `omnistat-inspect` call
and renders the JSON output into a report card summarizing what the job did.
For example:

```text
Job 44092 — 1 node, 4× MI250X, ran 5m 00s (2024-05-17 10:14–10:19 UTC)

Metrics Stats
  Source | Metric              | Mean  | Max
  GPU    | Utilization (%)     |  52.8 | 100.0
  GPU    | Power (W)           | 174.8 | 387.0
  GPU    | Temperature (°C)    |  51.8 |  64.0
  GPU    | Memory used (%)     |  63.3 |  95.1

Data Collection Quality and Hardware Health
  Overall status: ok — no hardware-health indicators were flagged.
```

This is a snapshot, not a diagnosis: the report states the numbers as-is
without speculating about causes.

#### Example session 2: hypothesis-driven analysis

> **You:** *"Our 64-node training run 51820 hit lower throughput than usual.
> Job 51774 was a good run on the same nodes. Can you figure out what went
> wrong?"*

The agent reaches for the **job-analysis** skill and works top-down: it
compares the slow job against the healthy baseline, forms hypotheses from
where they diverge, and confirms each one against the data before reporting
it. Rather than dumping statistics, it produces a structured write-up that
names a likely cause and shows the evidence behind it. For example:

```text
Job 51820 — why it underperformed 51774

Summary
  Throughput tracked the baseline for the first ~40% of the run, then dropped
  ~18%. The regression is isolated to one node; the other 63 stayed healthy.

Evidence
  - frontier04313:card2 reached 96 °C and began clock-throttling ~40% in;
    its kernel dispatch times then ran ~1.9× the fleet median.
  - Every other GPU stayed near 61 °C and tracked the baseline closely.
  - Per-iteration time rose from 4.1 s to 4.9 s at the same moment, so the
    whole job paced to that one straggler.

Most likely cause
  A single overheating/throttling GPU is gating the collective; the rest of
  the fleet is fine. Suggested next step: drain frontier04313 and re-check.
```

Unlike the one-shot report, an analysis tends to be more **conversational**.
It may take a few back-and-forth turns rather than a single reply, and you can
steer it as you go:

- **Control the length.** The default write-up can be detailed; ask for a
  shorter form when you just want the headline, e.g. *"keep it to the top 10
  findings"* or *"max 15 lines."*
- **Follow up and visualize.** Drill in with further questions, or ask the
  agent to chart a metric over time; it can export the underlying time series
  and render a plot, e.g. *"plot GPU temperature for frontier04313 across the
  run."*
