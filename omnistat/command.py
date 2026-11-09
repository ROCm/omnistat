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
# command.py
#
# Command/control module for GPU clock frequency adjustments.  Provides a
# CommandController class that executes system commands to change GPU clock
# frequencies based on instructions received via the /command REST endpoint
# in standalone.py.
#
# Prometheus metrics are registered here and collected automatically by the
# REGISTRY.collect() loop in standalone.py without any polling-loop changes.

import logging
import re
import subprocess
import time
from datetime import datetime, timezone

from prometheus_client import Gauge

from omnistat import gpu_indexing

# from omnistat.annotate import omnistat_annotate

# Prometheus gauge tracking the most recently applied clock frequency per
# (clock, limit, gpu) combination.
_clock_gauge = Gauge(
    "omnistat_optimize_clock_mhz",
    "Current GPU clock frequency limit set by the energy optimizer",
    ["clock", "limit", "gpu"],
)

_VALID_CLOCKS = {"sclk", "mclk"}
_VALID_LIMITS = {"min", "max"}

# Set when a clock command is issued but the hardware does not adopt it.
# On MI250 (job 499903) requesting an sclk max-limit of exactly 500 MHz -- the
# bottom DPM level, equal to the min limit -- was rejected silently: the helper
# exited 0, printed only a CSV header, and the GPUs stayed at 1040-1205 MHz.
# An optimizer that scores an uncapped window as if it were capped draws
# exactly the wrong conclusion, so surface the mismatch.
_clock_reject_gauge = Gauge(
    "omnistat_optimize_clock_rejected",
    "1 when the last clock command for this GPU was not adopted by the hardware",
    ["clock", "limit", "gpu"],
)


class CommandController:
    def __init__(self, config):
        """Initialize from an already-parsed ConfigParser object.

        Reads [omnistat.optimize] for clock_cmd and sclk_frequencies.
        Raises ValueError if required options are missing or invalid.
        """
        section = "omnistat.optimize"

        self._clock_cmd = config[section].get("clock_cmd", "").strip()
        if not self._clock_cmd:
            raise ValueError("[omnistat.optimize] clock_cmd is required but not set.")

        # Commands arrive carrying the HIP_VISIBLE_DEVICES index Omnistat labels
        # metrics with. If clock_cmd ultimately feeds amd-smi, that index must be
        # translated -- the two enumerations disagree, so an untranslated command
        # throttles a different GPU than the one the optimizer measured.
        self._indexing = config[section].get("clock_cmd_indexing", "hip").strip().lower()
        self._gpu_map = {}
        if self._indexing == "amdsmi":
            rocm_path = "/opt/rocm"
            if config.has_section("omnistat.collectors"):
                rocm_path = config["omnistat.collectors"].get("rocm_path", rocm_path)
            self._gpu_map = gpu_indexing.hip_to_amdsmi(rocm_path=rocm_path)
            if self._gpu_map:
                logging.info("[CommandController] hip->amdsmi gpu map: %s" % dict(sorted(self._gpu_map.items())))
            else:
                logging.error(
                    "[CommandController] clock_cmd_indexing=amdsmi but the mapping could not be "
                    "determined; commands will NOT be translated and may target the wrong GPU."
                )
        elif self._indexing != "hip":
            raise ValueError("[omnistat.optimize] clock_cmd_indexing must be 'hip' or 'amdsmi'")

        # self._annotate = omnistat_annotate()
        logging.info("[CommandController] Initialized. clock_cmd=%s indexing=%s" % (self._clock_cmd, self._indexing))

    def _translate_gpu(self, gpu_index):
        """HIP index -> whatever clock_cmd expects."""
        if gpu_index is None or not self._gpu_map:
            return gpu_index
        mapped = self._gpu_map.get(int(gpu_index))
        if mapped is None:
            logging.warning("[CommandController] no mapping for hip gpu %s; passing through" % gpu_index)
            return gpu_index
        return mapped

    def apply_command(self, cmd_dict):
        """Validate and execute a clock-frequency command.

        Expected keys in cmd_dict:
            clock       (str)  "sclk" or "mclk"  — required
            type        (str)  "min"  or "max"    — required  (named "type" to match REST schema)
            value       (int)  frequency in MHz   — required
            gpu_index   (int)  GPU index          — optional; omit to apply to all GPUs
            time_begin  (int)  Unix timestamp     — optional; logged but applied immediately in Phase I
            time_end    (int)  Unix timestamp     — optional; logged but not enforced in Phase I

        Returns:
            dict with "status": "ok" on success, or raises ValueError on invalid input.
        """
        clock = cmd_dict.get("clock")
        limit = cmd_dict.get("type")
        value = cmd_dict.get("value")
        gpu_index = cmd_dict.get("gpu_index", None)
        time_begin = cmd_dict.get("time_begin", None)
        time_end = cmd_dict.get("time_end", None)

        # --- input validation ---
        if clock not in _VALID_CLOCKS:
            raise ValueError("Invalid 'clock': must be one of %s" % sorted(_VALID_CLOCKS))
        if limit not in _VALID_LIMITS:
            raise ValueError("Invalid 'type': must be one of %s" % sorted(_VALID_LIMITS))
        if not isinstance(value, int):
            raise ValueError("'value' must be an integer (MHz).")
        if gpu_index is not None and (not isinstance(gpu_index, int) or gpu_index < 0):
            raise ValueError("'gpu_index' must be a non-negative integer.")

        # Phase I: log time window fields but apply immediately
        if time_begin is not None:
            logging.info("[CommandController] time_begin=%s (applied immediately in Phase I)" % time_begin)
        if time_end is not None:
            logging.info("[CommandController] time_end=%s (not enforced in Phase I)" % time_end)

        # --- determine GPU targets ---
        gpu_targets = [gpu_index] if gpu_index is not None else [None]

        results = []
        rejected = []
        for gpu in gpu_targets:
            # gauge stays labelled by the HIP index users see in metrics;
            # only the command itself gets the translated index
            tgt = self._translate_gpu(gpu)
            self._run_clock_command(clock, limit, value, tgt)
            gpu_label = str(gpu) if gpu is not None else "all"
            _clock_gauge.labels(clock=clock, limit=limit, gpu=gpu_label).set(value)
            ok = self._verify_clock(clock, limit, value, tgt)
            _clock_reject_gauge.labels(clock=clock, limit=limit, gpu=gpu_label).set(0 if ok else 1)
            if not ok:
                rejected.append(gpu_label)
            results.append(gpu_label)

        annotation_text = "omnistat-optimize: %s %s -> %d MHz (gpu: %s)" % (
            clock,
            limit,
            value,
            ", ".join(results),
        )
        # self._annotate.start(annotation_text)
        logging.info("[CommandController] Applied: %s" % annotation_text)
        if rejected:
            logging.error(
                "[CommandController] NOT ADOPTED by hardware: %s %s -> %d MHz on gpu(s) %s "
                "(command exited 0). Treat these as uncapped." % (clock, limit, value, ", ".join(rejected))
            )

        return {
            "status": "ok" if not rejected else "partial",
            "clock": clock,
            "type": limit,
            "value": value,
            "gpus": results,
            "rejected": rejected,
        }

    def _verify_clock(self, clock, limit, value, gpu_index):
        """Read the limit back; True if adopted, True if undeterminable.

        Only ever reports a rejection actually observed -- an unparseable
        readback returns True rather than inventing a failure. Readback uses
        `rocm-smi`, which enumerates in kfd/HIP order, so it is keyed by the
        *untranslated* index; `clock_cmd` may use a different enumeration.
        """
        if clock != "sclk" or limit != "max" or gpu_index is None:
            return True
        hip = None
        if self._gpu_map:
            for h, s in self._gpu_map.items():
                if s == gpu_index:
                    hip = h
                    break
        if hip is None:
            hip = gpu_index
        try:
            res = subprocess.run(
                ["rocm-smi", "-d", str(hip), "--showsclkrange"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return True
        m = re.findall(r"(\d+)\s*Mhz\s*$", res.stdout, re.IGNORECASE | re.MULTILINE)
        if not m:
            return True
        applied = max(int(x) for x in m)
        # tolerance absorbs rounding to a hardware step
        return applied <= value + 100

    def _run_clock_command(self, clock, limit, value, gpu_index):
        """Execute the system clock command as a subprocess.

        Uses list form (never shell=True) to prevent shell injection.
        """
        cmd = self._clock_cmd.split() + ["-clock", clock, "-limit", limit, "-value", str(value)]
        if gpu_index is not None:
            cmd += ["-gpu", str(gpu_index)]

        logging.info("[CommandController] Running: %s" % " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.stdout.strip():
                logging.info("[CommandController] stdout: %s" % result.stdout.strip())
            if result.stderr.strip():
                logging.info("[CommandController] stderr: %s" % result.stderr.strip())
            if result.returncode != 0:
                logging.error("[CommandController] Command failed (rc=%d)" % result.returncode)
        except subprocess.TimeoutExpired:
            logging.error("[CommandController] Command timed out: %s" % cmd)
        except FileNotFoundError:
            logging.error("[CommandController] clock_cmd not found: %s" % self._clock_cmd)
        except Exception as e:
            logging.error("[CommandController] Unexpected error running command: %s" % e)
