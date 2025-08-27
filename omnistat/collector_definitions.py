COLLECTORS = [
    {
        "name": "rocm_smi",
        "enabled_by_default": True,
        "file": "omnistat.collector_smi",
        "className": "ROCMSMI",
    },
    {
        "name": "amd_smi",
        "enabled_by_default": False,
        "file": "omnistat.collector_smi_v2",
        "className": "AMDSMI",
    },
    {
        "name": "rms",
        "enabled_by_default": True,
        "file": "omnistat.collector_rms",
        "className": "RMSJob",
    },
    {
        "name": "network",
        "enabled_by_default": True,
        "file": "omnistat.collector_network",
        "className": "NETWORK",
    },
    {
        "name": "events",
        "enabled_by_default": True,
        "file": "omnistat.collector_events",
        "className": "ROCMEvents",
    },
    {
        "name": "rocprofiler",
        "enabled_by_default": False,
        "file": "omnistat.collector_rocprofiler",
        "className": "rocprofiler",
    },
    {
        "name": "kmsg",
        "enabled_by_default": True,
        "file": "omnistat.contrib.collector_kmsg",
        "className": "KmsgCollector",
    },
    {
        "name": "vendor_counters",
        "enabled_by_default": True,
        "file": "omnistat.collector_pm_counters",
        "className": "PM_COUNTERS",
    },
]
