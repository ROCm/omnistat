# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2023 - 2026 Advanced Micro Devices, Inc. All Rights Reserved.
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

"""GPU properties detected from the host, used to adapt tests to the
hardware available in the test environment.
"""

from . import config
from omnistat.utils import load_amdsmi_interface


def get_gpu_asic_info(device=0):
    """Return GPU market name and graphics version of the given device."""
    smi = load_amdsmi_interface(config.rocm_path)

    smi.amdsmi_init()
    handles = smi.amdsmi_get_processor_handles()
    info = smi.amdsmi_get_gpu_asic_info(handles[device])
    smi.amdsmi_shut_down()

    return info["market_name"], info["target_graphics_version"]


gpu_type, gpu_arch = get_gpu_asic_info() if config.rocm_host else ("", "")

# Consumer GPUs (RDNA) lack RAS/ECC counters, energy accumulators, and
# hardware counters. Matching on "Radeon" rather than "Instinct" so that an
# unrecognized datacenter GPU is tested instead of silently skipped.
consumer_gpu = "Radeon" in gpu_type
