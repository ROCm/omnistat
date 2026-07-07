# Metrics Available

```eval_rst
.. toctree::
   :glob:
   :maxdepth: 4
```

Omnistat supports multiple embedded data collectors to aggregate a large
collection of metrics from a variety of system sources.  Many of the available
data collectors are optional and can be enabled via runtime configuration
settings (e.g. via [omnistat.default](https://github.com/ROCm/omnistat/blob/main/omnistat/config/omnistat.default)).  The sections and tables that follow serve to outline major data
collector variants, their associated runtime configuration control options, and
a comprehensive list of specific metric names defined for each collector.

Note that Omnistat metrics generally fall into one of the two following types:

- **Node-level metrics**: These are reported once per node and are designated
 with a *Node Metric* heading.
- **GPU-level metrics**: These are reported for each individual GPU on a node
  and include a `card` label to distinguish between them. These metric types are denoted
  with a *GPU Metric* heading.

In addition, an optional [External](#external) data collector is available to
ingest additional site-specific metrics not included directly in Omnistat.

<hr style="border: 1px solid black;">

## ROCm

This core data collector provides essential metrics for monitoring AMD Instinct(tm) GPUs covering utilization, memory usage,
power consumption, frequencies, and temperature.  These metrics can be
collected using the ROCm System Management Interface (ROCm SMI) or the AMD
System Management Interface (AMD SMI) and are fundamental for assessing GPU
health and performance.

**Collector**: `enable_rocm_smi` or `enable_amd_smi`

| Node Metric             | Description                          |
| :---------------------- | :----------------------------------- |
| `rocm_num_gpus`         | Number of GPUs in the node.          |

| GPU Metric                        | Description                          |
| :-------------------------------- | :----------------------------------- |
| `rocm_version_info`               | GPU model and versioning information for GPU driver and VBIOS. Labels: `driver_ver`, `vbios`, `type`, `schema`. |
| `rocm_utilization_percentage`     | GPU utilization (%). |
| `rocm_vram_used_percentage`       | Memory utilization (%). |
| `rocm_vram_total_bytes`           | Total GPU memory (bytes). |
| `rocm_average_socket_power_watts` | Average socket power (W). |
| `rocm_energy_joules`              | Cumulative energy consumption (J) - accumulated since GPU driver load. |
| `rocm_sclk_clock_mhz`             | GPU clock speed (MHz). |
| `rocm_mclk_clock_mhz`             | Memory clock speed (MHz). |
| `rocm_temperature_celsius`        | GPU temperature (°C). Labels: `location`. |
| `rocm_temperature_memory_celsius` | Memory temperature (°C). Labels: `location`. |

<hr style="border: 1px solid black;">

## Resource Manager

The resource manager data collector links system-level monitoring data with specific
jobs running on the system. This is essential for attributing resource usage
to individual users or applications.

**Collector**: `enable_rms`

| Node Metric             | Description                          |
| :---------------------- | :----------------------------------- |
| `rmsjob_info`           | Resource manager info metric tracking running jobs. When a job is running, the `jobid` label is different than the empty string. Labels: `jobid`, `user`, `partition`, `nodes`, `batchflag`, `jobstep`, `type`. |

<hr style="border: 1px solid black;">

## Host

The host data collector optionally gathers host-oriented data including CPU and
memory utilization statistics along with general I/O metrics.

**Collector**: `enable_host_metrics`

| Node Metric             | Description                          |
| :---------------------- | :----------------------------------- |
| `omnistat_host_boot_time_seconds` | Node boot time (seconds since epoch). |
| `omnistat_mem_total_bytes`| Total host memory available (bytes). |
| `omnistat_mem_available_bytes` | Currently available host memory (bytes). This is typically the amount of memory available for allocation to new processes.|
| `omnistat_mem_free_bytes` | Free host memory available (bytes). This represents the amount of physical RAM that is currently unused - it is generally smaller than `omnistat_mem_available_bytes` due to caching. |
| `omnistat_host_cpu_num_physical_cores` | Number of physical CPU cores. |
| `omnistat_host_cpu_num_logical_cores` | Number of logical CPU cores. |
| `omnistat_host_cpu_aggregate_core_utilization` | Instantaneous number of busy CPU cores. Typical range varies from 0 (no load) to num_logical_cores (max load). |
| `omnistat_host_cpu_load1` | 1-minute CPU load average. This is identical to 1-minute load reported by `uptime`. |
| `omnistat_io_read_local_total_bytes` | Total block-level data read from **local** physical disks (bytes).|
| `omnistat_io_write_local_total_bytes` | Total bock-level data written to **local** physical disk (bytes). |

### Process-based I/O 

**Collector**: `enable_host_metrics`
<br/>
**Collector options**: `enable_proc_io_stats`

The default I/O tracking mechanism above tracks node-local I/O to physical
disks. Consequently, it does not have visibility to I/O directed at
network-based file systems (e.g NFS, Lustre, Vast) that are
common in large production clusters.  To enable tracking of all I/O (**including
network-based**), the host collector includes an optional mechanism to track I/O
of individual processes at the syscall level.  This requires access to scan
relevant files in `/proc` and is generally appropriate for use in {ref}`User-mode <user-vs-system>`
execution where Omnistat is running under the same user ID as the application.

| Node Metric             | Description                          |
| :---------------------- | :----------------------------------- |
| `omnistat_io_read_total_bytes` | Total data read by visible processes (bytes). This metric tracks I/O at the syscall level and includes both local and network I/O. Labels: `pid`, `cmd`.|
| `omnistat_io_write_total_bytes` | Total data written by visible processes (bytes). This metric tracks I/O at the syscall level and includes both local and network I/O. Labels: `pid`, `cmd`.|

<hr style="border: 1px solid black;">

## RAS

The RAS (Reliability, Availability, Serviceability) collection mechanism is an
optional capability of the ROCm data collectors and provides information
about ECC errors in different GPU blocks. There are three types of ECC errors
available for tracking:
- Correctable: Single-bit errors that are automatically corrected by the
  hardware. These do not cause data corruption or affect functionality.
- Uncorrectable: Multi-bit errors that cannot be corrected by the hardware.
  These can lead to data corruption and system instability.
- Deferred: Multi-bit errors that cannot be corrected by the hardware but can
  be flagged or isolated. These need to be handled to ensure data integrity
  and system stability.

**Collectors**: `enable_rocm_smi` or `enable_amd_smi`, `enable_ras_ecc`

| GPU Metric                               | Description                          |
| :--------------------------------------- | :----------------------------------- |
| `rocm_ras_umc_correctable_count`         | Correctable errors in the Unified Memory Controller block. |
| `rocm_ras_sdma_correctable_count`        | Correctable errors in the System Direct Memory Access block. |
| `rocm_ras_gfx_correctable_count`         | Correctable errors in the Graphics Processing Unit block. |
| `rocm_ras_mmhub_correctable_count`       | Correctable errors in the Multi Media Hub block. |
| `rocm_ras_pcie_bif_correctable_count`    | Correctable errors in the PCIe Bifurcation block. |
| `rocm_ras_hdp_correctable_count`         | Correctable errors in the Host Data Path block. |
| `rocm_ras_xgmi_wafl_correctable_count`   | Correctable errors in the External Global Memory Interconnect block. |
| `rocm_ras_umc_uncorrectable_count`       | Uncorrectable errors in the Unified Memory Controller block. |
| `rocm_ras_sdma_uncorrectable_count`      | Uncorrectable errors in the System Direct Memory Access block. |
| `rocm_ras_gfx_uncorrectable_count`       | Uncorrectable errors in the Graphics Processing Unit block. |
| `rocm_ras_mmhub_uncorrectable_count`     | Uncorrectable errors in the Multi Media Hub block. |
| `rocm_ras_pcie_bif_uncorrectable_count`  | Uncorrectable errors in the PCIe Bifurcation block. |
| `rocm_ras_hdp_uncorrectable_count`       | Uncorrectable errors in the Host Data Path block. |
| `rocm_ras_xgmi_wafl_uncorrectable_count` | Uncorrectable errors in the External Global Memory Interconnect block. |
| `rocm_ras_umc_deferred_count`            | Deferred[^deferred] errors in the Unified Memory Controller block. |
| `rocm_ras_sdma_deferred_count`           | Deferred[^deferred] errors in the System Direct Memory Access block.  |
| `rocm_ras_gfx_deferred_count`            | Deferred[^deferred] errors in the Graphics Processing Unit block. |
| `rocm_ras_mmhub_deferred_count`          | Deferred[^deferred] errors in the Multi Media Hub block. |
| `rocm_ras_pcie_bif_deferred_count`       | Deferred[^deferred] errors in the PCIe Bifurcation block. |
| `rocm_ras_hdp_deferred_count`            | Deferred[^deferred] errors in the Host Data Path block. |
| `rocm_ras_xgmi_wafl_deferred_count`      | Deferred[^deferred] errors in the External Global Memory Interconnect block. |

[^deferred]: Deferred RAS ECC counts are only available with `enable_amd_smi`,
  and not with `enable_rocm_smi`.

<hr style="border: 1px solid black;">

## Occupancy

The occupancy collection mechanism is another optional capability of the ROCm data collectors that provides insight to help understand how the GPU's compute units (CUs)
are being utilized. It represents the ratio of active wavefronts to the
maximum number of wavefronts that a CU can handle simultaneously.

**Collectors**: `enable_rocm_smi` or `enable_amd_smi`, `enable_cu_occupancy`

| GPU Metric                    | Description                          |
| :---------------------------- | :----------------------------------- |
| `rocm_num_compute_units`      | Number of compute units. |
| `rocm_compute_unit_occupancy` | Number of used compute units. |

<hr style="border: 1px solid black;">

## xGMI

The xGMI (External Global Memory Interconnect) data collector provides metrics
for monitoring the total data transferred over the GPU-to-GPU high-speed
interconnect. These metrics accumulate over time and are reset upon driver
load.

**Collectors**: `enable_rocm_smi` or `enable_amd_smi`, `enable_xgmi`

| GPU Metric                            | Description                           |
| :------------------------------------ | :------------------------------------ |
| `rocm_xgmi_total_read_kilobytes`      | Total data read from all xGMI links (KB).   |
| `rocm_xgmi_total_write_kilobytes`     | Total data written to all xGMI links (KB).  |

<hr style="border: 1px solid black;">

## VCN

The VCN (Video Core Next) collection mechanism is an optional capability of
the AMD SMI data collector that provides metrics for monitoring video decoding
operations on AMD GPUs. GPUs may contain multiple VCN engines to handle
parallel video decoding workloads.

```{note}
The VCN collector requires enabling the AMD SMI collector (`enable_amd_smi`).
It is **not** supported by the ROCm SMI collector (`enable_rocm_smi`).
```

**Collectors**: `enable_amd_smi`, `enable_vcn`

| GPU Metric                                    | Description                          |
| :-------------------------------------------- | :----------------------------------- |
| `rocm_average_decoder_utilization_percentage` | Decoder utilization averaged across all engines in the GPU (%). |

<hr style="border: 1px solid black;">

## Hardware Counters

The ROCprofiler data collector provides access to low-level GPU hardware
counters for in-depth performance analysis. Counters are collected by sampling
the GPUs at the device level with minimal impact on application performance.
The collection is configured through the `profile` option in the configuration
file.

Each profile defines a sampling mode and a set of counters to be collected:
- `sampling_mode`: This option controls how counter sets are distributed
  across the available GPUs:
    - `constant`: Assigns one set of counters to all GPUs.
    - `gpu-id`: Cyclically assigns sets of counters to GPU IDs in all nodes.
       The number of sets of counters must not exceed the number of GPUs per
       node.
    - `periodic`: Rotates all GPUs through multiple counter sets, changing the
      active counter set after every sample. When this mode is enabled, counter
      values are reset at each sampling interval and not accumulated.
- `counters`: This option accepts one or more sets of counters formatted as a
  flat or nested JSON list. For a complete list of supported counters, see the
  [ROCm documentation](https://rocm.docs.amd.com/en/latest/conceptual/gpu-arch/mi300-mi200-performance-counters.html).

```eval_rst
.. code-block:: ini
   :caption: Example profile to collect free-running and active cycles on all GPUs

    [omnistat.collectors.rocprofiler.cycles]
    sampling_mode = constant
    counters = ["GRBM_COUNT", "GRBM_GUI_ACTIVE"]
  ```

```eval_rst
.. code-block:: ini
   :caption: Example profile to collect HBM reads and writes from different GPU IDs

    [omnistat.collectors.rocprofiler.hbm]
    sampling_mode = gpu-id
    counters = [["FETCH_SIZE"], ["WRITE_SIZE"]]
  ```

The ROCprofiler data collector requires [building the hardware counters
extension](./installation/extensions.md#hardware-counters).

To ensure all performance counters are collected correctly, the collector has
the following requirements depending on how Omnistat is executed:
- *System mode*: Run Omnistat with the `CAP_PERFMON` capability enabled.
- *User mode*: Set the `HSA_TOOLS_LIB` environment variable in the application's runtime environment.
  ```shell
  export HSA_TOOLS_LIB=/opt/rocm/lib/librocprofiler64.so
  export HSA_TOOLS_ROCPROFILER_V1_TOOLS=1
  ```

**Collector**: `enable_rocprofiler`
<br/>
**Collector options**: `profile`

| GPU Metric                                    | Description                          |
| :-------------------------------------------- | :----------------------------------- |
| `omnistat_hardware_counter`                   | GPU hardware counter value from ROCprofiler. Labels: `source`, `name`. |

<hr style="border: 1px solid black;">

## Kernel Tracing

The kernel tracing data collector traces individual GPU kernel dispatches,
recording kernel names, execution durations, and GPU IDs. It produces
per-kernel time series metrics that enable detailed analysis of GPU workload
composition over time.

The collector requires [building the kernel tracing
library](./installation/extensions.md#kernel-tracing). To intercept kernel
dispatches, the `ROCP_TOOL_LIBRARIES` environment variable must be set in the
GPU application's runtime environment pointing to the built library:

```shell
export ROCP_TOOL_LIBRARIES=/path/to/build-trace/libomnistat_trace.so
```

**Collector**: `enable_kernel_trace`

| GPU Metric | Description |
| :--- | :--- |
| `omnistat_kernel_dispatch_count` | Cumulative number of kernel dispatches. Labels: `kernel`. |
| `omnistat_kernel_total_duration_ns` | Cumulative kernel execution time (ns). Labels: `kernel`. |

| Node Metric | Description |
| :--- | :--- |
| `omnistat_kernel_dropped_dispatches` | Cumulative number of dispatches excluded from metrics collection because their timestamps fell outside the valid time range. This is an Omnistat bookkeeping metric and does not affect GPU execution. |

<hr style="border: 1px solid black;">

## Network

The network data collector enables metrics providing information about data
transfers for each network interface detected in the host platform. Currently
supported network types include Ethernet, Infiniband, and
Slingshot.

**Collector**: `enable_network`

| Node Metric                 | Description                          |
| :-------------------------- | :----------------------------------- |
| `omnistat_network_tx_bytes` | Total bytes transmitted by network interface. Labels: `device_class`, `interface`. |
| `omnistat_network_rx_bytes` | Total bytes received by network interface. Labels: `device_class`, `interface`. |

<hr style="border: 1px solid black;">

## External

The external data collector provides a mechanism to incorporate custom,
site-specific metrics into Omnistat by executing a user-provided script at each
collection interval. The script is expected to write metrics to stdout in
[Prometheus text exposition
format](https://prometheus.io/docs/instrumenting/exposition_formats/#text-based-format)
(one metric per line). Metric names and labels are not fixed in advance --
they are discovered dynamically from the script output at runtime.

All metrics produced by the external script are automatically tagged with an
`omnistat_external="1"` label to distinguish them from native Omnistat metrics.
Any metric that is **not** present in the script output on a given invocation
will be automatically removed from Omnistat tracking so metrics can be dynamically added/deleted via this method.

**Collector**: `enable_external`
<br/>
**Collector options**: `script`, `timeout_secs`

### Configuration

The external collector is enabled by setting `enable_external = True` in the
`[omnistat.collectors]` section. Collector options are configured in a
separate `[omnistat.collectors.external]` section where `script` specifies
the path to the executable and `timeout_secs` (default: 10 seconds) controls how
long Omnistat will wait for the script to complete before discarding its
output.

```eval_rst
.. code-block:: ini
   :caption: Example configuration

    [omnistat.collectors]
    enable_external = True

    [omnistat.collectors.external]
    script = /path/to/my_metrics.sh
    timeout_secs = 10
```

### Script output format

The script must print metrics to stdout using the following format:

```
metric_name value
metric_name{label1="val1",label2="val2"} value
```

Lines beginning with `#` and empty lines are ignored.

### Example

The following example script emits free disk space metrics for multiple
filesystems, using a label to distinguish between them.

```eval_rst
.. code-block:: bash
   :caption: my_metrics.sh

    #!/usr/bin/env bash
    # Emit free space (bytes) for monitored filesystems
    for fs in /home /scratch; do
        free_bytes=$(df --output=avail -B1 "${fs}" | tail -1)
        echo "site_disk_free_bytes{fs=\"${fs}\"} ${free_bytes}"
    done
```

Running the script produces output that Omnistat parses directly:

```eval_rst
.. code-block:: console

    $ ./my_metrics.sh
    site_disk_free_bytes{fs="/home"} 524288000000
    site_disk_free_bytes{fs="/scratch"} 1932735283200
```
<hr style="border: 1px solid black;">

## User-supplied

In addition to the collection mechanisms highlighted above, Omnistat has several options for
incorporating user-supplied data for overlay with existing telemetry data.  The following
subsections highlight available options including *annotations* and *figures of merit*.

### Annotations

Omnistat allows users to add application-level context to telemetry data using the
`omnistat-annotate` tool. Annotations are managed by the [resource manager](#resource-manager)
collector and can be used to mark specific events or phases within an application, such as the start
and end of a computation, making it easier to correlate performance data with application behavior.
To demonstrate creation of high-level markers from within a job script, the following snippet
highlights annotation of repeated runs of an application with different command-line arguments (where
the argument size is included as text for the annotation).

```eval_rst
.. code-block:: bash
   :caption: Example use of high-level annotations in a job script

    for SIZE in 102400 358400 768000; do
        ${OMNISTAT_DIR}/omnistat-annotate --mode start --text  "Size=${SIZE}"
        ./my_app --size ${SIZE}
        ${OMNISTAT_DIR}/omnistat-annotate --mode stop
        sleep 5
    done
```

**Collector**: `enable_rms`
<br/>
**Collector options**: `enable_annotations`

| Node Metric             | Description                          |
| :---------------------- | :----------------------------------- |
| `rmsjob_annotations`    | User-provided annotations. Labels: `jobid`, `marker`. |

### Figure of Merit

Many iterative applications have a natural notion of progress (e.g., time per iteration, GFLOPS
achieved, number of samples processed, number of epochs completed) that can be used to quantify
application performance. Omnistat user-mode supports collection of these figures of merit (FOM)
allowing users to inject custom application performance metrics into the telemetry data stream while
an application is running.  By correlating FOM values with system telemetry, users can gain insights
into how specific application performance relates to observed hardware behavior, including power and
energy consumption.

To support this feature, Omnistat exposes a `/fom` REST endpoint that accepts a JSON payload with a
user-supplied FOM name and value; the timestamp is encoded automatically at time of receipt.  The
following highlights a CLI example using `curl` to report a GFLOPS measurement:

```eval_rst
.. code-block:: bash
   :caption: Example FOM submission using curl

    curl -X POST http://localhost:8001/fom \
      -H "Content-Type: application/json" \
      -d '{"name": "gflops", "value": 511.264069}'
```

For C++ applications, a more efficient approach is to use a header-only HTTP
client such as [cpp-httplib](https://github.com/yhirose/cpp-httplib) to issue
the POST request directly from within the application code:

```eval_rst
.. code-block:: cpp
   :caption: Example FOM submission from C++ using cpp-httplib

    #include "httplib.h"
    #include <iostream>
    #include <sstream>

    // Initialize connection to local Omnistat server
    httplib::Client cli("http://localhost:8001");

    // application FOM value (e.g., GFLOPS for current iteration)
    double step_gflops = 134.45;

    // build payload to send FOM to Omnistat endpoint
    std::ostringstream data;
    data << "{\"name\":\"step_gflops\",\"value\":" << step_gflops << "}";

    auto res = cli.Post("/fom", data.str(), "application/json");
    if (!res || res->status < 200 || res->status >= 300) {
        std::cerr << "FOM POST failed\n";
    }
```

Python applications can use the `requests` library to report FOM values natively:

```eval_rst
.. code-block:: python
   :caption: Example FOM submission from Python using requests

    import requests

    # application FOM value (e.g., GFLOPS for current iteration)
    step_gflops = 134.45

    # build payload and send FOM to Omnistat endpoint
    payload = {"name": "step_gflops", "value": step_gflops}
    res = requests.post("http://localhost:8001/fom", json=payload)
    if not res.ok:
        print(f"FOM POST failed: {res.status_code}")
```

**Collector**: user-mode only (`omnistat-usermode`)

| Node Metric             | Description                          |
| :---------------------- | :----------------------------------- |
| `omnistat_fom`          | Application-supplied figure of merit value. Labels: `instance`, `name`. |

