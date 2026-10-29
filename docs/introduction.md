# Overview

## What's Omnistat

Omnistat provides utilities for cluster administrators and application developers to aggregate scale-out system metrics via low-overhead sampling across all hosts in a cluster, or a subset of hosts associated with a specific user job. At its core, Omnistat collects key telemetry from AMD Instinct™ accelerators on a per-GPU basis. Relevant target metrics include:

* GPU utilization
* High-bandwidth memory (HBM) usage
* GPU power
* GPU temperature
* GPU clock frequency
* GPU memory clock frequency
* Inventory information:
  * ROCm driver version
  * GPU type
  * GPU vBIOS version

**Additional optional metrics:**

* Reliability, Availability, Serviceability (RAS) information (error counts per GPU block)
* GPU power caps
* GPU throttling events
* Host network traffic (received/transmitted)
* Host resource utilization (CPU, memory, and I/O)


To enable scalable collection of these metrics, Omnistat provides a python-based [Prometheus](https://prometheus.io) client that supplies instantaneous metric values on-demand for periodic polling by a companion Prometheus server (or a [VictoriaMetrics](https://github.com/VictoriaMetrics/VictoriaMetrics) server).

(user-vs-system)=
## User-mode vs system-level monitoring

Omnistat supports two deployment modes, depending on whether the user has administrative rights:

1. __System-wide monitoring__: requires administrative rights and is typically used to monitor all GPU hosts within a given cluster in a 24x7 mode of operation. Use this approach to support system-wide telemetry collection for all user workloads and optionally, provide job-level insights for systems running the [SLURM](https://slurm.schedmd.com), [Flux](https://flux-framework.org), or [PBS](https://altair.com/pbs-professional) workload managers.
1. __User-mode monitoring__: doesn't require administrative rights and can be run entirely within user-space. This case is typically exercised by end application users running on production clusters under the auspices of a resource manager who want to gather telemetry data within a single job allocation. Frequently, this approach is performed entirely within a command-line `ssh` environment but Omnistat includes support for downloading data after a job for visualization with a dockerized Grafana environment. Alternatively, standalone query utilities can be used to summarize collected metrics at the conclusion of a job. Resource managers supported by user-mode Omnistat include [SLURM](https://github.com/SchedMD/slurm), [Flux](https://flux-framework.org), and [PBS](https://altair.com/pbs-professional).

The following diagrams show the data collector layout and lifecycle for each mode.

```{figure} images/architecture_system-mode.png
---
name: fig-system-mode
width: 800px
---
System-wide monitoring
```

```{figure} images/architecture_user-mode.png
---
name: fig-user-mode
width: 800px
---
User-mode monitoring
```

In the __system-wide monitoring__ case, a system administrator enables data collectors permanently on all relevant hosts within the cluster and configures a Prometheus server to periodically poll these nodes (e.g. at 1 minute or 5 minute intervals). The Prometheus server typically runs on the cluster head node (or separate administrative host) and doesn't require GPU resources locally. For real-time and historical queries, the system administrator also enables a Grafana instance that queries the Prometheus datastore to provide a variety of visualizations with collected data. Example visualization panels are shown in the [Grafana](./system-mode/grafana.md) section.

In the __user-mode__ case, Omnistat data collectors and a companion VictoriaMetrics server are deployed temporarily on hosts assigned to a user's job. At the end of the job, Omnistat can query cached telemetry to summarize GPU utilization or visualize it offline. An example command-line summary:

(query_report_card)=
```{eval-rst}
.. _report_card:

.. code-block:: none
   :caption: Example telemetry summary report card in user-mode.

    ----------------------------------------
    Omnistat Report Card for Job # 44092
    ----------------------------------------

    Job Overview (Num Nodes = 1, Machine = Snazzy Cluster)
    --> Start time = 2024-05-17 10:14:00
    --> End   time = 2024-05-17 10:19:00

    GPU Statistics:

           | Utilization (%)  |  Memory Use (%)  | Temperature (C)  |    Power (W)     |
     GPU # |    Max     Mean  |    Max     Mean  |    Max     Mean  |    Max     Mean  |
    ------------------------------------------------------------------------------------
         0 |  100.00   55.56  |   95.09   63.40  |   61.00   52.89  |  300.00  177.67  |
         1 |  100.00   44.33  |   94.97   63.32  |   64.00   54.11  |  387.00  160.22  |
         2 |  100.00   55.56  |   94.92   63.28  |   60.00   51.11  |  304.00  176.78  |
         3 |  100.00   55.56  |   94.78   63.20  |   58.00   48.89  |  354.00  184.67  |

    --
    Query execution time = 0.1 secs
    Version = {__VERSION__}
```

## Software dependencies

The basic minimum dependencies to enable data collection via Omnistat tools in user-mode are as follows:

* [ROCm](https://rocm.docs.amd.com/en/latest) (v{__ROCM_MIN_VERSION__} or newer )
* Python dependencies (see top-level [requirements.txt](https://github.com/ROCm/omnistat/blob/main/requirements.txt))

System administrators wishing to deploy a system-wide GPU monitoring capability with near real-time visualization will also need one or more servers to host two additional services:

* [Grafana](https://github.com/grafana/grafana) - either local instance or can also use cloud-based infrastructure
* [Prometheus server](https://prometheus.io/docs/prometheus/latest/getting_started/) - used to periodically poll and aggregate data from multiple compute nodes


## Resource manager integration

Omnistat can be _optionally_ configured to map telemetry tracking to specific job Ids when using the popular [SLURM](https://github.com/SchedMD/slurm) resource manager or the new [Flux](https://flux-framework.org) framework. This works by enabling a Prometheus info metric that tracks node-level job assignments and exposes the following metadata:

* job id
* username
* partition name
* number of nodes allocated
* batch vs interactive job

Additional details on enabling this integration are discussed in the system-mode [Installation](./system-mode/system-install.md#slurm-integration) section. In addition, job-oriented dashboards using this feature are included in the companion [Grafana](./system-mode/grafana.md) discussion.
