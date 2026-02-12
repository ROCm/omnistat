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


### Annotations

The resource manager collector optionally allows users to add application-level context to Omnistat metrics using the `omnistat-annotate` tool. This is useful for marking specific events or phases within an application, such as the start and end of a computation, making it
easier to correlate performance data with application behavior.  To demonstrate creation of high-level markers from within a job script, the following snippet highlights annotation of repeated runs of an application with different command-line arguments (where the argument size included as text for the annotation).

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

## ROCprofiler

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

The ROCprofiler data collector requires [building the ROCprofiler
extension](./installation/extensions.md#rocprofiler).

To ensure all performance counters are collected correctly, the collector has
the following requirements depending on how Omnistat is executed:
- *System mode*: Run Omnistat with the `CAP_PERFMON` capability enabled.
- *User mode*: Set the `HSA_TOOLS_LIB` environment variable in the application's runtime environment.
  ```shell
  export HSA_TOOLS_LIB=/opt/rocm/lib/librocprofiler64.so
  ```

**Collector**: `enable_rocprofiler`
<br/>
**Collector options**: `profile`

| GPU Metric                                    | Description                          |
| :-------------------------------------------- | :----------------------------------- |
| `omnistat_hardware_counter`                   | GPU hardware counter value from ROCprofiler. Labels: `source`, `name`. |

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

### Slingshot (CXI) telemetry

On Slingshot systems, Omnistat can optionally expose additional Cassini/CXI
telemetry counters from `/sys/class/cxi/*/device/telemetry/`. These are exported
as raw counter values (use `rate()` / `increase()` in Prometheus to derive
rates).

| Node Metric                                  | Description                          |
| :------------------------------------------- | :----------------------------------- |
| `omnistat_network_cxi_telemetry`             | CXI telemetry counter value. Labels: `interface`, `counter`. |
| `omnistat_network_cxi_tx_ok_octets`          | Transmitted fabric bytes (OK). Labels: `interface`. |
| `omnistat_network_cxi_rx_ok_octets`          | Received fabric bytes (OK). Labels: `interface`. |
| `omnistat_network_cxi_tx_octets_multi`       | Transmitted multicast bytes. Labels: `interface`. |
| `omnistat_network_cxi_tx_octets_ieee`        | Transmitted IEEE-framed bytes. Labels: `interface`. |
| `omnistat_network_cxi_tx_octets_opt`         | Transmitted optimized-protocol bytes. Labels: `interface`. |
| `omnistat_network_cxi_pkts_sent_by_tc`       | Packets sent by traffic class. Labels: `interface`, `traffic_class`. |
| `omnistat_network_cxi_pkts_recv_by_tc`       | Packets received by traffic class. Labels: `interface`, `traffic_class`. |
| `omnistat_network_cxi_rx_ok_packets_bucket`  | OK receive packet counts by message-size bucket. Labels: `interface`, `bucket_min`, `bucket_max`. |
| `omnistat_network_cxi_tx_ok_packets_bucket`  | OK transmit packet counts by message-size bucket. Labels: `interface`, `bucket_min`, `bucket_max`. |
| `omnistat_network_cxi_pause_sent`            | Pause events sent. Labels: `interface`. |
| `omnistat_network_cxi_pause_recv`            | Pause events received by traffic class. Labels: `interface`, `traffic_class`. |
| `omnistat_network_cxi_pause_xoff_sent`       | XOFF events sent by traffic class. Labels: `interface`, `traffic_class`. |
| `omnistat_network_cxi_discard_cntr`          | Discard counters by traffic class. Labels: `interface`, `traffic_class`. |
| `omnistat_network_cxi_fgfc_discard`          | Flow-control discard counter. Labels: `interface`. |
| `omnistat_network_cxi_tx_bad_octets`         | Bad transmitted octets. Labels: `interface`. |
| `omnistat_network_cxi_rx_bad_octets`         | Bad received octets. Labels: `interface`. |
| `omnistat_network_cxi_pcs_corrected_cw`      | Corrected PCS codewords. Labels: `interface`. |
| `omnistat_network_cxi_pcs_uncorrected_cw`    | Uncorrected PCS codewords. Labels: `interface`. |

### Slingshot (CXI) derived metrics

The following CXI metrics are computed directly in Omnistat using the formulas
in `features.tex` (deltas between consecutive samples).

| Node Metric                                                      | Description |
| :--------------------------------------------------------------- | :---------- |
| `omnistat_network_cxi_tx_bandwidth_bytes_per_second`             | TX bandwidth (bytes/s). Labels: `interface`. |
| `omnistat_network_cxi_rx_bandwidth_bytes_per_second`             | RX bandwidth (bytes/s). Labels: `interface`. |
| `omnistat_network_cxi_bidirectional_bandwidth_bytes_per_second`  | TX+RX bandwidth (bytes/s). Labels: `interface`. |
| `omnistat_network_cxi_tx_to_rx_balance_ratio`                    | TX/RX bandwidth ratio. Labels: `interface`. |
| `omnistat_network_cxi_multicast_tx_share_fraction`               | Multicast TX share (fraction). Labels: `interface`. |
| `omnistat_network_cxi_ieee_tx_share_fraction`                    | IEEE TX share (fraction). Labels: `interface`. |
| `omnistat_network_cxi_optimized_tx_share_fraction`               | Optimized TX share (fraction). Labels: `interface`. |
| `omnistat_network_cxi_packet_send_rate_packets_per_second`       | Packet send rate (pkts/s). Labels: `interface`. |
| `omnistat_network_cxi_packet_receive_rate_packets_per_second`    | Packet receive rate (pkts/s). Labels: `interface`. |
| `omnistat_network_cxi_avg_bytes_per_tx_packet`                   | Average bytes per TX packet. Labels: `interface`. |
| `omnistat_network_cxi_small_packet_fraction_tx`                  | Small packet fraction (TX). Labels: `interface`. |
| `omnistat_network_cxi_large_packet_fraction_tx`                  | Large packet fraction (TX). Labels: `interface`. |
| `omnistat_network_cxi_link_busy_fraction`                        | Link busy fraction. Labels: `interface`. |
| `omnistat_network_cxi_link_stall_per_flit`                       | Link stalls per flit. Labels: `interface`. |
| `omnistat_network_cxi_cq_blocked_cycles_per_second`              | CQ blocked cycles (cycles/s). Labels: `interface`. |
| `omnistat_network_cxi_nic_no_work_cycles_per_second`             | NIC no-work proxy (cycles/s). Labels: `interface`. |
| `omnistat_network_cxi_pause_received_per_second`                 | Pause receive rate (events/s). Labels: `interface`. |
| `omnistat_network_cxi_pause_sent_per_second`                     | Pause send rate (events/s). Labels: `interface`. |
| `omnistat_network_cxi_xoff_sent_per_second`                      | XOFF send rate (events/s). Labels: `interface`. |
| `omnistat_network_cxi_ecn_marking_ratio_request_fraction`        | ECN marking ratio (request). Labels: `interface`. |
| `omnistat_network_cxi_ecn_marking_ratio_response_fraction`       | ECN marking ratio (response). Labels: `interface`. |
| `omnistat_network_cxi_congestion_discard_per_second`             | Congestion discard rate (events/s). Labels: `interface`. |
| `omnistat_network_cxi_command_credits_in_use_per_second`         | Command credits-in-use proxy (1/s). Labels: `interface`. |
| `omnistat_network_cxi_receive_fifo_credits_in_use_per_second`    | Receive FIFO credits-in-use proxy (1/s). Labels: `interface`. |
| `omnistat_network_cxi_pi_posted_credits_in_use_per_second`       | PI posted credits-in-use proxy (1/s). Labels: `interface`. |
| `omnistat_network_cxi_resource_busy_per_second`                  | Resource busy rate (events/s). Labels: `interface`. |
| `omnistat_network_cxi_endpoint_table_pressure_fraction`          | Endpoint table pressure proxy (fraction). Labels: `interface`. |
| `omnistat_network_cxi_ordered_to_unordered_ratio`                | Ordered/unordered ratio. Labels: `interface`. |
| `omnistat_network_cxi_unordered_fraction`                        | Unordered fraction. Labels: `interface`. |
| `omnistat_network_cxi_ordered_request_fraction`                  | Ordered request fraction. Labels: `interface`. |
| `omnistat_network_cxi_mean_rsp_latency_bin_index`                | Mean response latency proxy (bin index). Labels: `interface`. |
| `omnistat_network_cxi_tail_latency_fraction_top10pct_bins`       | Tail latency fraction (top bins). Labels: `interface`. |
| `omnistat_network_cxi_timeout_per_second`                        | Timeout rate (events/s). Labels: `interface`. |
| `omnistat_network_cxi_bad_tx_octets_per_second`                  | Bad TX octet rate (octets/s). Labels: `interface`. |
| `omnistat_network_cxi_bad_rx_octets_per_second`                  | Bad RX octet rate (octets/s). Labels: `interface`. |
| `omnistat_network_cxi_ecc_corrected_cw_per_second`               | ECC corrected codewords (cw/s). Labels: `interface`. |
| `omnistat_network_cxi_ecc_uncorrected_cw_per_second`             | ECC uncorrected codewords (cw/s). Labels: `interface`. |
