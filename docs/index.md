# ROCm Omnistat documentation

ROCm Omnistat is a set of Python utilities and data
collectors to support scale-out cluster telemetry targeting AMD Instinct™ GPUs.
Omnistat operates in two primary modes: **system mode** for permanent, cluster-wide collection managed by a system administrator, and **user mode** for temporary collection scoped to a single user's batch job. Collected telemetry can be visualized in Grafana, exported for custom analysis, or explored interactively by an AI coding agent.

::::{grid} 1 2 2 2
:gutter: 3

:::{grid-item-card} Introduction

- [What is omnistat?](introduction.md)
- [Supported metrics](metrics.md)
- [Advanced profiling](advanced-profiling.md) with hardware counters and tracing.
:::

:::{grid-item-card} Installation

- [System mode installation](system-install.md)
- [User mode installation](user-install.md)
:::

:::{grid-item-card} How to

- [Grafana integration in system mode](grafana.md) 
- [Running jobs in user mode](jobs.md)
- [Running in user mode on ORNL Frontier](site.frontier.md)
- [Running in user mode on ORNL Lux](site.lux.md)
- [Running in user mode on ORNL Lux](site.lux.md)
- [Running in user mode on LLNL](site.tuo.md)
:::

:::{grid-item-card} Reference

- [AI Agent skills](agents.md) 
- [Developer guide](developer/developers.md) 
:::

::::
