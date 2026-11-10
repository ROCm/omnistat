# ROCm Omnistat documentation

ROCm Omnistat is a set of Python utilities and data
collectors to support scale-out cluster telemetry targeting AMD Instinct™ GPUs.
Omnistat operates in two primary modes: **system mode** for permanent, cluster-wide collection managed by a system administrator, and **user mode** for temporary collection scoped to a single user's batch job. Collected telemetry can be visualized in Grafana, exported for custom analysis, or explored interactively by an AI coding agent.

::::{grid} 1 2 2 2
:gutter: 3

:::{grid-item-card} Conceptual

- [Omnistat metrics](metrics.md)
- [Advanced profiling](advanced-profiling.md)
:::

:::{grid-item-card} Install

- [System mode installation](system-mode/system-install.md)
- [User mode installation](user-mode/user-install.md)
:::

:::{grid-item-card} How to

- [Grafana integration in system mode](system-mode/grafana.md) 
- [Running jobs in user mode](user-mode/jobs.md)
- [Analyzing results collected in user mode](user-mode/analysis.md)
- [Running in user mode on ORNL Frontier](site.frontier.md)
- [Running in user mode on ORNL Lux](site.lux.md)
- [Running in user mode on LLNL Tuolumne](site.tuo.md)
:::

:::{grid-item-card} Reference

- [AI Agent skills](agents.md) 
- [Developer guide](developer/developers.md) 
:::

::::
