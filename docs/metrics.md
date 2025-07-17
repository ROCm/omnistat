# Metrics

```eval_rst
.. toctree::
   :glob:
   :maxdepth: 4
```

## ROCm

| Node Metric             | Description                          |
| :---------------------- | :----------------------------------- |
| `rocm_num_gpus`         | Number of GPUs in the node.          |

| GPU Metric                        | Description                          |
| :-------------------------------- | :----------------------------------- |
| `rocm_version_info`               | GPU model and versioning information for GPU driver and VBIOS.<br/> Labels: `driver_ver`, `vbios`, `type`, `schema`. |
| `rocm_utilization_percentage`     |                                      |
| `rocm_vram_used_percentage`       |                                      |
| `rocm_average_socket_power_watts` |                                      |
| `rocm_sclk_clock_mhz`             |                                      |
| `rocm_mclk_clock_mhz`             |                                      |
| `rocm_temperature_celsius`        |                                      |
| `rocm_temperature_memory_celsius` |                                      |
