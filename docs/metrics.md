# Metrics

This document provides a comprehensive list of the metrics collected by
Omnistat. The metrics are organized into two main categories:

- **Node-level metrics**: These are reported once per node and are presented
  in tables with the heading "Node Metric".
- **GPU-level metrics**: These are reported for each individual GPU on a node
  and include a `card` label to distinguish between them. They are presented
  in tables with the heading "GPU Metric".

```eval_rst
.. toctree::
   :glob:
   :maxdepth: 4
```

## ROCm

Essential metrics for monitoring AMD GPUs, covering utilization, memory usage,
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
| `rocm_average_socket_power_watts` | Average socket power (W). |
| `rocm_sclk_clock_mhz`             | GPU clock speed (MHz). |
| `rocm_mclk_clock_mhz`             | Memory clock speed (MHz). |
| `rocm_temperature_celsius`        | GPU temperature (°C). Labels: `location`. |
| `rocm_temperature_memory_celsius` | Memory temperature (°C). Labels: `location`. |


## Resource Manager

The resource manager metrics link system-level monitoring data with specific
jobs running on the system. This is essential for attributing resource usage
to individual users or applications.

**Collector**: `enable_rms`

| Node Metric             | Description                          |
| :---------------------- | :----------------------------------- |
| `rmsjob_info`           | Resource manager information about running jobs. When a job is running, the `jobid` label is different than the empty string. Labels: `jobid`, `user`, `partition`, `nodes`, `batchflag`, `jobstep`, `type`. |


## Annotations

Users can add application-level context to Omnistat data using the
`omnistat-annotate` tool. This is useful for marking specific events or phases
within an application, such as the start and end of a computation, making it
easier to correlate performance data with application behavior.

**Collector**: `enable_rms`
<br/>
**Collector options**: `enable_annotations`

| Node Metric             | Description                          |
| :---------------------- | :----------------------------------- |
| `rmsjob_annotations`    | User-provided annotations. Labels: `jobid`, `marker`. |


## RAS

RAS (Reliability, Availability, Serviceability) metrics provide information
about ECC errors in different GPU blocks. There are three types of ECC errors:
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
| `rocm_ras_umc_correctable_count`         | Correctable errors in the Unified Memory Controller. |
| `rocm_ras_sdma_correctable_count`        | Correctable errors in the Synchronous Data Memory Access. |
| `rocm_ras_gfx_correctable_count`         | Correctable errors in the Graphics Processing Unit. |
| `rocm_ras_mmhub_correctable_count`       | Correctable errors in the Memory Management Hub. |
| `rocm_ras_pcie_bif_correctable_count`    | Correctable errors in the PCIe Bifurcation. |
| `rocm_ras_hdp_correctable_count`         | Correctable errors in the Host Data Path. |
| `rocm_ras_xgmi_wafl_correctable_count`   | Correctable errors in the External Global Memory Interconnect. |
| `rocm_ras_umc_uncorrectable_count`       | Uncorrectable errors in the Unified Memory Controller. |
| `rocm_ras_sdma_uncorrectable_count`      | Uncorrectable errors in the Synchronous Data Memory Access. |
| `rocm_ras_gfx_uncorrectable_count`       | Uncorrectable errors in the Graphics Processing Unit. |
| `rocm_ras_mmhub_uncorrectable_count`     | Uncorrectable errors in the Memory Management Hub. |
| `rocm_ras_pcie_bif_uncorrectable_count`  | Uncorrectable errors in the PCIe Bifurcation. |
| `rocm_ras_hdp_uncorrectable_count`       | Uncorrectable errors in the Host Data Path. |
| `rocm_ras_xgmi_wafl_uncorrectable_count` | Uncorrectable errors in the External Global Memory Interconnect. |
| `rocm_ras_umc_deferred_count`            | Deferred[^deferred] errors in the Unified Memory Controller. |
| `rocm_ras_sdma_deferred_count`           | Deferred[^deferred] errors in the Synchronous Data Memory Access.  |
| `rocm_ras_gfx_deferred_count`            | Deferred[^deferred] errors in the Graphics Processing Unit. |
| `rocm_ras_mmhub_deferred_count`          | Deferred[^deferred] errors in the Memory Management Hub. |
| `rocm_ras_pcie_bif_deferred_count`       | Deferred[^deferred] errors in the PCIe Bifurcation. |
| `rocm_ras_hdp_deferred_count`            | Deferred[^deferred] errors in the Host Data Path. |
| `rocm_ras_xgmi_wafl_deferred_count`      | Deferred[^deferred] errors in the External Global Memory Interconnect. |

[^deferred]: Deferred RAS ECC counts are only available with `enable_amd_smi`,
  and not with `enable_rocm_smi`.


## Occupancy

Occupancy is a measure to help understand how the GPU's compute units (CUs)
are being utilized. It represents the ratio of active wavefronts to the
maximum number of wavefronts that a CU can handle simultaneously.

**Collectors**: `enable_rocm_smi` or `enable_amd_smi`, `enable_cu_occupancy`

| GPU Metric                    | Description                          |
| :---------------------------- | :----------------------------------- |
| `rocm_num_compute_units`      | Number of compute units. |
| `rocm_compute_unit_occupancy` | Number of used compute units. |


## Network

Network metrics provide information about data transfers for each network
interface. Supported network types include Ethernet, Infiniband, and
Slingshot.

**Collector**: `enable_network`

| Node Metric                 | Description                          |
| :-------------------------- | :----------------------------------- |
| `omnistat_network_tx_bytes` | Total bytes transmitted by network interface. Labels: `device_class`, `interface`. |
| `omnistat_network_rx_bytes` | Total bytes received by network interface. Labels: `device_class`, `interface`. |
