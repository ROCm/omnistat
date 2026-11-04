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
# optimizer_signals.py
#
# Self-describing telemetry payload and clock-decision types shared by the
# optimizer framework.
#
# Replaces ingest_fom(name, value, ts) -> int, which carried one scalar and
# returned one frequency for all GPUs. A BYO optimizer may want utilization,
# counters or power, and decisions need a device dimension. Plain dicts so the
# payload round-trips through JSON.

import time

# An optimizer selects the kinds it understands and ignores the rest
KIND_FOM = "fom"
KIND_UTILIZATION = "utilization"
KIND_POWER = "power"
KIND_COUNTER = "counter"


def make_sample(kind, name, value, device=None, units=None, direction=None):
    """Build one self-describing sample.

    Args:
        kind (str): semantic class -- see KIND_* above.
        name (str): metric name, e.g. "gpu_busy" or "wallclock_secs_per_step".
        value (float): the measurement.
        device (int, optional): GPU index.  Omit for node- or job-scoped values.
        units (str, optional): e.g. "percent", "s", "W".
        direction (str, optional): "higher" or "lower" -- which way is better.
                                   Only meaningful for FOM-like kinds.
    """
    s = {"kind": kind, "name": name, "value": float(value)}
    if device is not None:
        s["device"] = int(device)
    if units:
        s["units"] = units
    if direction:
        s["direction"] = direction
    return s


def make_batch(samples, source="app", host=None, timestamp_msecs=None):
    """Wrap samples with provenance.

    Args:
        samples (list): list of make_sample() dicts.
        source (str): "app", "collector", ... -- who produced these.
        host (str, optional): originating hostname.
        timestamp_msecs (int, optional): defaults to now.
    """
    if timestamp_msecs is None:
        timestamp_msecs = int(time.time() * 1000.0)
    batch = {"source": source, "timestamp_msecs": int(timestamp_msecs), "samples": list(samples)}
    if host:
        batch["host"] = host
    return batch


def batch_from_legacy_fom(name, value, timestamp_msecs, host=None):
    """Adapt the original /fom payload into a batch.

    Keeps the existing Cholla patch and any other app posting {name, value}
    working unchanged.
    """
    return make_batch(
        [make_sample(KIND_FOM, name or "fom", value)],
        source="app",
        host=host,
        timestamp_msecs=timestamp_msecs,
    )


def select(batch, kind, device=None):
    """Return samples of a given kind, optionally for one device."""
    out = []
    for s in batch.get("samples", []):
        if s.get("kind") != kind:
            continue
        if device is not None and s.get("device") != device:
            continue
        out.append(s)
    return out


def devices_in(batch, kind=None):
    """Sorted list of device indices present in the batch."""
    devs = set()
    for s in batch.get("samples", []):
        if kind is not None and s.get("kind") != kind:
            continue
        if "device" in s:
            devs.add(int(s["device"]))
    return sorted(devs)


def clock_decision(value_mhz, device=None, clock="sclk", limit="max"):
    """Build one clock decision.

    Args:
        value_mhz (int): target frequency limit in MHz.
        device (int, optional): GPU index.  None means all GPUs on every host,
                                which reproduces the original broadcast
                                behaviour.
        clock (str): "sclk" or "mclk".
        limit (str): "min" or "max".
    """
    d = {"clock": clock, "type": limit, "value": int(value_mhz)}
    if device is not None:
        d["gpu_index"] = int(device)
    return d


def normalize_decisions(result):
    """Accept what an optimizer returned and normalize to a decision list.

    Tolerates the legacy scalar return (an int frequency meaning "all GPUs")
    so existing plugins such as StepDownOptimizer keep working untouched.
    """
    if result is None:
        return []
    if isinstance(result, (int, float)):
        return [clock_decision(int(result))]
    if isinstance(result, dict):
        return [result]
    return list(result)
