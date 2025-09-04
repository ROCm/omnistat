# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2025 Advanced Micro Devices, Inc. All Rights Reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# -------------------------------------------------------------------------------
# Omnistat data collector definitions (metadata to support dynamic loading)

COLLECTORS = [
    {
        "runtime_option": "enable_rocm_smi",
        "enabled_by_default": True,
        "file": "omnistat.collector_smi",
        "className": "ROCMSMI",
    },
    {
        "runtime_option": "enable_amd_smi",
        "enabled_by_default": False,
        "file": "omnistat.collector_smi_v2",
        "className": "AMDSMI",
    },
    {
        "runtime_option": "enable_rms",
        "enabled_by_default": True,
        "file": "omnistat.collector_rms",
        "className": "RMSJob",
    },
    {
        "runtime_option": "enable_network",
        "enabled_by_default": True,
        "file": "omnistat.collector_network",
        "className": "NETWORK",
    },
    {
        "runtime_option": "enable_events",
        "enabled_by_default": False,
        "file": "omnistat.collector_events",
        "className": "ROCMEvents",
    },
    {
        "runtime_option": "enable_rocprofiler",
        "enabled_by_default": False,
        "file": "omnistat.collector_rocprofiler",
        "className": "rocprofiler",
    },
    {
        "runtime_option": "enable_kmsg",
        "enabled_by_default": False,
        "file": "omnistat.contrib.collector_kmsg",
        "className": "KmsgCollector",
    },
    {
        "runtime_option": "enable_vendor_counters",
        "enabled_by_default": True,
        "file": "omnistat.collector_pm_counters",
        "className": "PM_COUNTERS",
    },
]
