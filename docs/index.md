# ROCm Omnistat documentation

ROCm Omnistat is a set of Python utilities and data
collectors to support scale-out cluster telemetry targeting AMD Instinct™ GPUs.
Omnistat operates in two primary modes: **system mode** for permanent, cluster-wide collection managed by a system administrator, and **user mode** for temporary collection scoped to a single user's batch job. Collected telemetry can be visualized in Grafana, exported for custom analysis, or explored interactively by an AI coding agent.

The ROCm Omnistat public repository is located within the rocm-systems repo at [ROCm/omnistat](https://github.com/ROCm/omnistat).

::::{grid} 1 2 2 2
:gutter: 3

:::{grid-item-card} Conceptual

- [Omnistat metrics](metrics.md)
- [Advanced profiling](advanced-profiling.md)
:::

:::{grid-item-card} Install

- [System-mode installation](system-mode/system-install.md)
- [User-mode installation](user-mode/user-install.md)
:::

:::{grid-item-card} How to

- [Grafana integration in system mode](system-mode/grafana.md) 
- [Running jobs in user mode](user-mode/jobs.md)
- [Analyzing results collected in user mode](user-mode/analysis.md)
- [Running jobs on ORNL Frontier](site.frontier.md)
- [Running jobs on ORNL Lux](site.lux.md)
- [Running jobs on LLNL Tuolumne](site.tuo.md)
:::

:::{grid-item-card} Reference

- [AI agent skills](agents.md) 
- [Developer guide](developer/developers.md) 
:::

::::

To contribute to the documentation, see
[Contributing to ROCm](https://rocm.docs.amd.com/en/latest/contribute/contributing.html).

You can find licensing information on the
[Licensing](https://rocm.docs.amd.com/en/latest/about/license.html) page.