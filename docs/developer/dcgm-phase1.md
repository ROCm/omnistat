Title: NVIDIA DCGM Integration — Phase 1 Specification

Objective
- Expose NVIDIA GPU metrics from dcgm-exporter into Prometheus via Omnistat without modifying existing Grafana dashboards.
- Keep metric names vendor-specific using the nvidia_ prefix.
- Do not alias or re-label to rocm_* in Phase 1.
- Document configuration, metric set, behavior, and testing.

Architecture Overview
- DCGM collection: dcgm-exporter runs as a service on NVIDIA nodes and exposes Prometheus metrics on a local port (default :9400).
- Omnistat collector: omnistat/collector_nvidia_dcgm.py scrapes dcgm-exporter and re-exports selected metrics under Omnistat’s /metrics endpoint using nvidia_* names.
- Prometheus: continues to scrape the Omnistat endpoint (default port :8001). Optionally, Prometheus may also scrape dcgm-exporter directly, but the default flow uses Omnistat.

Runtime Configuration (omnistat/config/omnistat.default)
[omnistat.collectors]
- enable_nvidia_dcgm = False
  - Enable/disable the NVIDIA DCGM collector (default: off).
- nvidia_dcgm_exporter_url = http://localhost:9400/metrics
  - URL to dcgm-exporter Prometheus endpoint.
- enable_nvidia_uuid_label = False
  - Include per-GPU UUID label if present (default: off). Turning this on increases series cardinality.

Collector Registration (omnistat/collector_definitions.json)
- runtime_option: "enable_nvidia_dcgm"
- file: "omnistat.collector_nvidia_dcgm"
- class_name: "NvidiaDCGMCollector"
- enabled_by_default: false

Metric Set (Phase 1)
Exposed under nvidia_* with labels as noted. The collector derives and standardizes certain values:

Node-level:
- nvidia_num_gpus (gauge)
  - Count of GPUs discovered on the node.
  - Labels: none.

Per-GPU core metrics:
- nvidia_version_info (gauge; value=1)
  - Labels: card, driver_ver, vbios, type, schema [, uuid if enabled].
  - driver_ver sourced from DCGM_FI_DRIVER_VERSION; vbios and type if available in dcgm-exporter CSV; otherwise “N/A”.
- nvidia_utilization_percentage (gauge, %)
  - DCGM_FI_DEV_GPU_UTIL.
  - Labels: card [, uuid].
- nvidia_vram_total_bytes (gauge, bytes)
  - Derived: (FB_USED + FB_FREE [+ FB_RESERVED]) MiB × 1048576.
  - Labels: card [, uuid].
- nvidia_vram_used_percentage (gauge, %)
  - Derived: FB_USED / (FB_USED + FB_FREE [+ FB_RESERVED]) × 100.
  - Labels: card [, uuid].
- nvidia_sclk_clock_mhz (gauge, MHz)
  - DCGM_FI_DEV_SM_CLOCK.
  - Labels: card [, uuid].
- nvidia_mclk_clock_mhz (gauge, MHz)
  - DCGM_FI_DEV_MEM_CLOCK.
  - Labels: card [, uuid].
- nvidia_temperature_celsius (gauge, °C)
  - DCGM_FI_DEV_GPU_TEMP.
  - Labels: card, location="gpu" [, uuid].
- nvidia_temperature_memory_celsius (gauge, °C)
  - DCGM_FI_DEV_MEMORY_TEMP.
  - Labels: card, location="memory" [, uuid].
- nvidia_power_usage_watts (gauge, W)
  - DCGM_FI_DEV_POWER_USAGE.
  - Labels: card [, uuid].

Optional/Available (registered when present; not required for ROCm parity in Phase 1):
- nvidia_total_energy_mj (counter, mJ)
  - DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION.
- nvidia_pcie_replay_counter (counter)
  - DCGM_FI_DEV_PCIE_REPLAY_COUNTER.
- nvidia_mem_copy_utilization_percentage (gauge, %)
  - DCGM_FI_DEV_MEM_COPY_UTIL.
- nvidia_enc_utilization_percentage (gauge, %)
  - DCGM_FI_DEV_ENC_UTIL.
- nvidia_dec_utilization_percentage (gauge, %)
  - DCGM_FI_DEV_DEC_UTIL.
- nvidia_fb_free_mib / nvidia_fb_used_mib / nvidia_fb_reserved_mib (gauges, MiB)
  - DCGM framebuffer memory fields.

Label Policy
- All per-GPU metrics carry card (string of DCGM gpu index).
- Temperatures include standardized location labels ("gpu" and "memory").
- UUID label is optional and off by default; enable via enable_nvidia_uuid_label if needed.

Behavior and Caveats
- Grafana: No dashboard changes in Phase 1. Panels hardcoded to rocm_* will not display NVIDIA data; this is expected for Phase 1 testing.
- Semantics:
  - Power usage (DCGM) is instantaneous/short-average; differs from ROCm “average socket power”.
  - Memory busy on ROCm (UMC activity) ≠ DCGM mem copy util; any comparison must note the difference.
- Error handling: If dcgm-exporter is unavailable, the collector logs an error and skips updates; existing metric values remain until the next successful scrape.
- Cardinality: Keeping UUID disabled by default reduces series explosion. Enabling UUID increases label cardinality and query costs.

Service Setup (dcgm-exporter)
Native systemd:
- ExecStart=/usr/bin/dcgm-exporter --address=:9400 --collectors=/etc/dcgm-exporter/default-counters.csv
- Restart=always; After=nvidia-driver.service; User/Group as appropriate.

Container:
- docker run -d --gpus all --cap-add SYS_ADMIN -p 9400:9400 nvcr.io/nvidia/k8s/dcgm-exporter:<version>

Security (optional):
- TLS/basic auth via --web-config-file (Prometheus exporter-toolkit). If enabled, set Omnistat collector URL to HTTPS accordingly.

Testing Plan
Unit tests:
- Parse fixtures from dcgm-exporter /metrics and validate:
  - GPU discovery
  - Label mapping (card, location)
  - Derived memory totals and percentages
  - Version info label population and fallbacks

Integration:
- Enable dcgm-exporter locally.
- Set enable_nvidia_dcgm=True in omnistat.config.
- Run ./omnistat-monitor, then:
  - curl localhost:8001/metrics | grep -E "^nvidia_" to confirm presence and labels.
- Prometheus: ensure scraping Omnistat /metrics ingests nvidia_* series.

Operational Notes
- Phase 1 does not attempt aliasing to rocm_*; dashboards remain unchanged.
- Where parity gaps exist, they are documented (power averaging, memory busy).
- In a later phase, optional Prometheus recording rules can provide a “ROCm view” without Grafana edits, or vendor-agnostic omnistat_vendor_* rollups can be added.

Rollout Steps
1) Ensure dcgm-exporter is running on NVIDIA nodes.
2) Set enable_nvidia_dcgm=True and adjust nvidia_dcgm_exporter_url if needed.
3) Run Omnistat monitor and verify nvidia_* metrics.
4) Prometheus scrapes Omnistat (default port :8001) and ingests nvidia_*.
5) Provide results to Prometheus/Grafana expert for feedback; defer dashboard changes to later phases.

Files and Paths
- Collector: omnistat/collector_nvidia_dcgm.py
- Registration: omnistat/collector_definitions.json
- Config: omnistat/config/omnistat.default

Change History
- Phase 1: Initial collector added; vendor-specific metrics exposed; UUID toggle disabled by default.
