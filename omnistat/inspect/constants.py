"""Constants for omnistat-inspect metric categories and configuration."""

SCAN_STEP = 60.0
SCAN_DAYS = 365

METRIC_CATEGORIES = {
    "gpu": [
        "rocm_utilization_percentage",
        "rocm_vram_used_percentage",
        "rocm_vram_total_bytes",
        "rocm_average_socket_power_watts",
        "rocm_sclk_clock_mhz",
        "rocm_mclk_clock_mhz",
        "rocm_temperature_celsius",
        "rocm_temperature_memory_celsius",
        "rocm_version_info",
        "rocm_num_compute_units",
        "rocm_compute_unit_occupancy",
        "rocm_average_decoder_utilization_percentage",
    ],
    "host": [
        "omnistat_host_boot_time_seconds",
        "omnistat_host_mem_total_bytes",
        "omnistat_host_mem_available_bytes",
        "omnistat_host_mem_free_bytes",
        "omnistat_host_cpu_num_physical_cores",
        "omnistat_host_cpu_num_logical_cores",
        "omnistat_host_cpu_aggregate_core_utilization",
        "omnistat_host_cpu_load1",
        "omnistat_host_io_read_local_total_bytes",
        "omnistat_host_io_write_local_total_bytes",
        "omnistat_io_read_total_bytes",
        "omnistat_io_write_total_bytes",
    ],
    "network": [
        "omnistat_network_tx_bytes",
        "omnistat_network_rx_bytes",
    ],
    "vendor": [
        "omnistat_vendor_power_watts",
        "omnistat_vendor_accel_power_watts",
        "omnistat_vendor_cpu_power_watts",
        "omnistat_vendor_memory_power_watts",
        "omnistat_vendor_energy_joules",
        "omnistat_vendor_accel_energy_joules",
        "omnistat_vendor_cpu_energy_joules",
        "omnistat_vendor_memory_energy_joules",
        "omnistat_vendor_samples_total",
        "omnistat_vendor_samples_skipped_total",
    ],
    "ras": [],  # populated dynamically (rocm_ras_*)
    "xgmi": [
        "rocm_xgmi_total_read_kilobytes",
        "rocm_xgmi_total_write_kilobytes",
    ],
    "rms": [
        "rmsjob_info",
        "rmsjob_annotations",
        "rocm_num_gpus",
    ],
    "rocprofiler": [
        "omnistat_hardware_counter",
    ],
    "system": [
        "omnistat_info",
    ],
}

# Counters need rate/delta computation, not raw min/max/mean
COUNTER_METRICS = {
    "omnistat_host_io_read_local_total_bytes",
    "omnistat_host_io_write_local_total_bytes",
    "omnistat_io_read_total_bytes",
    "omnistat_io_write_total_bytes",
    "omnistat_network_tx_bytes",
    "omnistat_network_rx_bytes",
    "omnistat_vendor_energy_joules",
    "omnistat_vendor_accel_energy_joules",
    "omnistat_vendor_cpu_energy_joules",
    "omnistat_vendor_memory_energy_joules",
    "omnistat_vendor_samples_total",
    "omnistat_vendor_samples_skipped_total",
    "rocm_xgmi_total_read_kilobytes",
    "rocm_xgmi_total_write_kilobytes",
}

# Gauge metrics worth summarizing per category
HOST_GAUGE_METRICS = [
    "omnistat_host_cpu_aggregate_core_utilization",
    "omnistat_host_cpu_load1",
    "omnistat_host_mem_available_bytes",
]

NETWORK_COUNTER_METRICS = [
    "omnistat_network_tx_bytes",
    "omnistat_network_rx_bytes",
]

VENDOR_GAUGE_METRICS = [
    "omnistat_vendor_power_watts",
    "omnistat_vendor_accel_power_watts",
    "omnistat_vendor_cpu_power_watts",
    "omnistat_vendor_memory_power_watts",
]

VENDOR_COUNTER_METRICS = [
    "omnistat_vendor_energy_joules",
    "omnistat_vendor_accel_energy_joules",
    "omnistat_vendor_cpu_energy_joules",
    "omnistat_vendor_memory_energy_joules",
]

HOST_COUNTER_METRICS = [
    "omnistat_host_io_read_local_total_bytes",
    "omnistat_host_io_write_local_total_bytes",
]

CATEGORY_CONFIG = {
    "gpu": {
        "levels": {
            "global": [],
            "node": ["instance"],
            "gpu-id": ["card"],
            "gpu": ["instance", "card"],
        },
        "default_metrics": [
            "rocm_utilization_percentage",
            "rocm_vram_used_percentage",
            "rocm_temperature_celsius",
            "rocm_average_socket_power_watts",
            "rocm_sclk_clock_mhz",
        ],
    },
    "host": {
        "levels": {
            "global": [],
            "node": ["instance"],
        },
        "default_metrics": [
            "omnistat_host_cpu_aggregate_core_utilization",
            "omnistat_host_cpu_load1",
            "omnistat_host_mem_available_bytes",
            "omnistat_host_io_read_local_total_bytes",
            "omnistat_host_io_write_local_total_bytes",
        ],
    },
    "network": {
        "levels": {
            "global": [],
            "node": ["instance"],
            "interface-id": ["interface"],
            "interface": ["instance", "interface"],
        },
        "default_metrics": [
            "omnistat_network_tx_bytes",
            "omnistat_network_rx_bytes",
        ],
    },
    "vendor": {
        "levels": {
            "global": [],
            "node": ["instance"],
        },
        "default_metrics": [
            "omnistat_vendor_power_watts",
            "omnistat_vendor_accel_power_watts",
            "omnistat_vendor_cpu_power_watts",
            "omnistat_vendor_memory_power_watts",
            "omnistat_vendor_energy_joules",
            "omnistat_vendor_accel_energy_joules",
            "omnistat_vendor_cpu_energy_joules",
            "omnistat_vendor_memory_energy_joules",
        ],
    },
    "xgmi": {
        "levels": {
            "global": [],
            "node": ["instance"],
            "gpu-id": ["card"],
            "gpu": ["instance", "card"],
        },
        "default_metrics": [
            "rocm_xgmi_total_read_kilobytes",
            "rocm_xgmi_total_write_kilobytes",
        ],
    },
}
