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
| `rocm_average_socket_power_watts` | Average socket power (W). |
| `rocm_sclk_clock_mhz`             | GPU clock speed (MHz). |
| `rocm_mclk_clock_mhz`             | Memory clock speed (MHz). |
| `rocm_temperature_celsius`        | GPU temperature (°C). Labels: `location`. |
| `rocm_temperature_memory_celsius` | Memory temperature (°C). Labels: `location`. |


## Resource Manager

The resource manager data collector links system-level monitoring data with specific
jobs running on the system. This is essential for attributing resource usage
to individual users or applications.

**Collector**: `enable_rms`

| Node Metric             | Description                          |
| :---------------------- | :----------------------------------- |
| `rmsjob_info`           | Resource manager info metric tracking running jobs. When a job is running, the `jobid` label is different than the empty string. Labels: `jobid`, `user`, `partition`, `nodes`, `batchflag`, `jobstep`, `type`. |


## Annotations

The resource manager collector optionally allows users to add application-level context to Omnistat metrics using the
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


## Occupancy

The occupancy collection mechanism is another optional capability of the ROCm data collectors that provides insight to help understand how the GPU's compute units (CUs)
are being utilized. It represents the ratio of active wavefronts to the
maximum number of wavefronts that a CU can handle simultaneously.

**Collectors**: `enable_rocm_smi` or `enable_amd_smi`, `enable_cu_occupancy`

| GPU Metric                    | Description                          |
| :---------------------------- | :----------------------------------- |
| `rocm_num_compute_units`      | Number of compute units. |
| `rocm_compute_unit_occupancy` | Number of used compute units. |


## VCN

The VCN (Video Core Next) collection mechanism is an optional capability of
the AMD SMI data collector that provides metrics for monitoring video decoding
operations on AMD GPUs. GPUs may contain multiple VCN engines to handle
parallel video decoding workloads, which can be identified with the `engine`
label.

```{note}
The VCN collector requires enabling the AMD SMI collector (`enable_amd_smi`).
It is **not** supported by the ROCm SMI collector (`enable_rocm_smi`).
```

**Collectors**: `enable_amd_smi`, `enable_vcn`

| GPU Metric                            | Description                          |
| :------------------------------------ | :----------------------------------- |
| `rocm_decoder_utilization_percentage` | Video decoder engine utilization (%). Labels: `engine`. |


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
