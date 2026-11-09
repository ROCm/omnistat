#!/usr/bin/env python3
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
#
# gpu_indexing.py
#
# Translate the HIP_VISIBLE_DEVICES index Omnistat labels metrics with into the
# index a vendor tool expects.
#
# Omnistat deliberately labels metrics by HIP index because that is what users
# reason about. amd-smi enumerates independently, and on at least one MI250 node
# the two disagree: capping amd-smi GPU 2 slowed the device Omnistat reports as
# card 0. Any command carrying a device index has to be translated, or it
# silently throttles the wrong GPU.
#
# Matching is done on PCI BDF, which both sides expose, rather than assuming the
# orderings coincide.

import glob
import logging
import os
import re
import subprocess

_BDF_RE = re.compile(r"([0-9a-fA-F]{4}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.(\d)")


def _kfd_bdf_by_node():
    """{kfd_node_index: 'dddd:bb:dd.f'} for nodes that are GPUs."""
    out = {}
    for path in glob.glob("/sys/class/kfd/kfd/topology/nodes/*/properties"):
        try:
            node = int(os.path.basename(os.path.dirname(path)))
            props = {}
            with open(path) as fh:
                for line in fh:
                    parts = line.split()
                    if len(parts) == 2:
                        props[parts[0]] = parts[1]
            # simd_count 0 marks a CPU node
            if int(props.get("simd_count", 0)) <= 0:
                continue
            domain = int(props.get("domain", 0))
            loc = int(props.get("location_id", 0))
            bus = (loc >> 8) & 0xFF
            dev = (loc >> 3) & 0x1F
            fn = loc & 0x7
            out[node] = "%04x:%02x:%02x.%d" % (domain, bus, dev, fn)
        except (ValueError, OSError):
            continue
    return out


def _amdsmi_bdf_by_index(rocm_path="/opt/rocm"):
    """{amdsmi_index: 'dddd:bb:dd.f'} via `amd-smi static --bus`."""
    exe = os.path.join(rocm_path, "bin", "amd-smi")
    if not os.path.exists(exe):
        exe = "amd-smi"
    try:
        res = subprocess.run([exe, "static", "--bus"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        logging.warning("[gpu_indexing] amd-smi query failed: %s" % e)
        return {}
    out, cur = {}, None
    for line in res.stdout.splitlines():
        m = re.match(r"\s*GPU:\s*(\d+)", line)
        if m:
            cur = int(m.group(1))
            continue
        if cur is not None and "BDF:" in line:
            b = _BDF_RE.search(line)
            if b:
                out[cur] = b.group(0).lower()
                cur = None
    return out


def hip_to_amdsmi(hip_to_kfd=None, rocm_path="/opt/rocm"):
    """Map HIP_VISIBLE_DEVICES index -> amd-smi index.

    hip_to_kfd: {hip_index: kfd_node} if the caller already knows it (Omnistat
    computes the inverse in utils.gpu_index_mapping_based_on_guids). Used only
    to resolve HIP indices; BDF does the actual matching.

    Returns {} when the mapping cannot be determined, which callers should
    treat as "no translation" rather than guessing.
    """
    kfd_bdf = _kfd_bdf_by_node()
    smi_bdf = _amdsmi_bdf_by_index(rocm_path)
    if not kfd_bdf or not smi_bdf:
        logging.warning("[gpu_indexing] could not read BDFs (kfd=%d amd-smi=%d)" % (len(kfd_bdf), len(smi_bdf)))
        return {}

    bdf_to_smi = {v: k for k, v in smi_bdf.items()}

    # HIP ordering is the kfd nodes sorted ascending unless told otherwise
    if hip_to_kfd:
        pairs = sorted(hip_to_kfd.items())
    else:
        pairs = list(enumerate(sorted(kfd_bdf)))

    mapping = {}
    for hip, kfd in pairs:
        bdf = kfd_bdf.get(int(kfd))
        if bdf is None:
            continue
        smi = bdf_to_smi.get(bdf)
        if smi is None:
            logging.warning("[gpu_indexing] no amd-smi match for BDF %s (hip=%s)" % (bdf, hip))
            continue
        mapping[int(hip)] = int(smi)

    if len(mapping) != len(pairs):
        logging.warning("[gpu_indexing] partial mapping: %d of %d devices resolved" % (len(mapping), len(pairs)))
    return mapping
