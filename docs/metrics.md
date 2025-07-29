# Metrics

```eval_rst
.. toctree::
   :glob:
   :maxdepth: 4
```

## ROCm

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

**Collector**: `enable_rms`


| Node Metric             | Description                          |
| :---------------------- | :----------------------------------- |
| `rmsjob_info`           | Resource manager information about running jobs. When a job is running, the `jobid` label is different than the empty string. Labels: `jobid`, `user`, `partition`, `nodes`, `batchflag`, `jobstep`, `type`. |


## Annotations

**Collector**: `enable_rms`
<br/>
**Collector options**: `enable_annotations = True`

| Node Metric             | Description                          |
| :---------------------- | :----------------------------------- |
| `rmsjob_annotations`    | User-provided annotations. Labels: `jobid`, `marker`. |


## Network

**Collector**: `enable_network`

| Node Metric                 | Description                          |
| :-------------------------- | :----------------------------------- |
| `omnistat_network_tx_bytes` | Total bytes transmitted by network interface. Labels: `device_class`, `interface`. |
| `omnistat_network_rx_bytes` | Total bytes received by network interface. Labels: `device_class`, `interface`. |
