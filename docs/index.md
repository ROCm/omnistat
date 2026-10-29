# Omnistat documentation

Welcome to the documentation area for **Omnistat**, a set of Python utilities and data
collectors to support scale-out cluster telemetry targeting AMD Instinct™ MI accelerators.
Omnistat operates in two primary modes: permanent, cluster-wide collection managed by a
system administrator, and temporary collection scoped to a single user's batch job.
Collected telemetry can be visualized in Grafana, exported for custom analysis, or explored
interactively by an AI coding agent.

```{figure} images/omnistat_intro.png
---
width: 950px
alt: Grafana dashboard showing per-GPU utilization gauges and utilization heatmaps across a job.
---
```

::::{grid} 1 2 2 2
:gutter: 3

:::{grid-item-card} Introduction
:link: introduction
:link-type: doc

Project overview, supported [metrics](metrics.md), and [advanced
profiling](advanced-profiling.md) with hardware counters and tracing.
:::

:::{grid-item-card} System-mode
:link: system-mode/system-install
:link-type: doc

Cluster-wide deployment by a system administrator, plus [Grafana
integration](system-mode/grafana.md) for visualization.
:::

:::{grid-item-card} User-mode
:link: user-mode/user-install
:link-type: doc

Per-job collection without elevated credentials, including [job
execution](user-mode/jobs.md) and [analysis](user-mode/analysis.md).
:::

:::{grid-item-card} Miscellaneous

[Agent skills](agents.md) for exploring collected telemetry with an AI coding
agent, and the [developer guide](developer/developers.md) for adding new data
collection mechanisms.
:::

::::
