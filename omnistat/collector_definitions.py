COLLECTORS = [
    {
        "runtime_option": "enable_rocm_smi",
        "enabled_by_default": True,
        "file": "omnistat.collector_smi",
        "className": "ROCMSMI",
    },
    {
        "runtime_option": "amd_smi",
        "enabled_by_default": False,
        "file": "omnistat.collector_smi_v2",
        "className": "AMDSMI",
    },
    {
        "runtime_option": "rms",
        "enabled_by_default": True,
        "file": "omnistat.collector_rms",
        "className": "RMSJob",
    },
    {
        "runtime_option": "network",
        "enabled_by_default": True,
        "file": "omnistat.collector_network",
        "className": "NETWORK",
    },
    {
        "runtime_option": "events",
        "enabled_by_default": True,
        "file": "omnistat.collector_events",
        "className": "ROCMEvents",
    },
    {
        "runtime_option": "rocprofiler",
        "enabled_by_default": False,
        "file": "omnistat.collector_rocprofiler",
        "className": "rocprofiler",
    },
    {
        "runtime_option": "kmsg",
        "enabled_by_default": True,
        "file": "omnistat.contrib.collector_kmsg",
        "className": "KmsgCollector",
    },
    {
        "runtime_option": "vendor_counters",
        "enabled_by_default": True,
        "file": "omnistat.collector_pm_counters",
        "className": "PM_COUNTERS",
    },
]
