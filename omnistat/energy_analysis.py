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
# energy_analysis.py
#
# Orchestrator for the GPU energy/performance sweet-spot optimizer.
#
# EnergyAnalyzer receives FOM (figure-of-merit) samples, maintains a bounded
# history, and delegates optimization decisions to a pluggable optimizer
# (see optimizer_base.BaseOptimizer).  When the optimizer recommends a clock
# frequency change, the orchestrator dispatches commands to all job-host
# /command endpoints in background threads.

import importlib
import logging
import threading
import time
from collections import deque

import requests

from omnistat import optimizer_signals as sig
from omnistat import utils


def _resolve_sclk_range(config):
    """Resolve sclk min/max and arch from [omnistat.optimize] config.

    Returns (arch_name, sclk_min, sclk_max).
    """
    section = "omnistat.optimize"
    arch_setting = config[section].get("sclk_frequencies", "auto").strip().lower()

    if arch_setting == "auto":
        arch = utils.detect_gpu_arch()
        if arch is None:
            raise ValueError("[omnistat.optimize] sclk_frequencies=auto but could not detect GPU arch via rocminfo")
        logging.info("[optimize] Auto-detected GPU architecture: %s" % arch)
    else:
        arch = arch_setting

    freq_section = "omnistat.optimize.frequencies.%s" % arch
    if not config.has_section(freq_section):
        raise ValueError(
            "[omnistat.optimize] No frequency table for arch '%s' — add a [%s] section with sclk_min/sclk_max"
            % (arch, freq_section)
        )

    sclk_min = config[freq_section].getint("sclk_min")
    sclk_max = config[freq_section].getint("sclk_max")
    return arch, sclk_min, sclk_max


def _load_optimizer_class(class_path):
    """Dynamically load an optimizer class from a dotted module path.

    Args:
        class_path: e.g. ``"omnistat.optimizer_stepdown.StepDownOptimizer"``

    Returns:
        The class object.
    """
    module_path, class_name = class_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


class EnergyAnalyzer:
    def __init__(self, config, host_endpoints, collector_port, on_state_change=None, on_perf_drop=None):
        """Initialize from an already-parsed ConfigParser object.

        Args:
            config: ConfigParser object passed from standalone.py
            host_endpoints: list[str] of hostnames for all job nodes, or None
            collector_port: int — port on which each node's /command endpoint listens
            on_state_change: optional callback(state_str, state_int, freq_mhz)
            on_perf_drop: optional callback(drop_pct, freq_mhz)
        """
        section = "omnistat.optimize"

        # Resolve GPU architecture and frequency bounds
        self._arch, sclk_min, sclk_max = _resolve_sclk_range(config)
        self._sclk_max = sclk_max

        # FOM metadata from config
        fom_direction = config[section].get("fom_direction", "higher").strip().lower()
        if fom_direction not in ("higher", "lower"):
            raise ValueError("[omnistat.optimize] fom_direction must be 'higher' or 'lower', got: '%s'" % fom_direction)
        fom_units = config[section].get("fom_units", None)
        if fom_units is not None:
            fom_units = fom_units.strip() or None

        # Load and instantiate the optimizer plugin
        optimizer_class_path = (
            config[section].get("optimizer_class", "omnistat.optimizer_stepdown.StepDownOptimizer").strip()
        )
        OptimizerClass = _load_optimizer_class(optimizer_class_path)
        self._optimizer = OptimizerClass(
            config,
            sclk_min,
            sclk_max,
            fom_direction=fom_direction,
            fom_units=fom_units,
        )

        # FOM history: bounded by approximate byte budget
        fom_history_cache_mb = config[section].getfloat("fom_history_cache_mb", 10)
        max_entries = max(100, int((fom_history_cache_mb * 1024 * 1024) / 28))
        self._fom_history = deque(maxlen=max_entries)

        # Host endpoints for clock commands
        self._hosts = host_endpoints or []
        self._port = int(collector_port)

        # Callbacks
        self._on_state_change = on_state_change
        self._on_perf_drop = on_perf_drop

        # Threading
        self._lock = threading.Lock()

        # Whether the startup max-clock reset has completed successfully
        self._startup_reset_done = False

        logging.info(
            "[energy] Initialized. arch=%s  optimizer=%s  hosts=%d"
            % (self._arch, optimizer_class_path, len(self._hosts))
        )
        logging.info(
            "[energy] Resetting GPUs to max clock (%d MHz) at startup (will retry until all nodes respond)."
            % self._sclk_max
        )
        self._startup_reset_with_retry()

        # Push initial state
        if self._on_state_change is not None:
            try:
                self._on_state_change(
                    self._optimizer.state,
                    self._optimizer.state_int,
                    self._optimizer.current_freq,
                )
            except Exception as e:
                logging.warning("[energy] Initial state push failed: %s" % e)

    def ingest_fom(self, name, value, timestamp_msecs):
        """Receive a FOM sample from the /fom endpoint (thread-safe).

        Thin adapter over ingest_batch() so the original app-push path and the
        existing Cholla patch keep working unchanged.
        """
        try:
            float(value)
        except (TypeError, ValueError):
            logging.warning("[energy] Ignoring non-numeric FOM value: %s" % value)
            return
        self.ingest_batch(sig.batch_from_legacy_fom(name, value, timestamp_msecs))

    def ingest_batch(self, batch):
        """Receive a self-describing sample batch (thread-safe).

        See optimizer_signals.make_batch().  Samples may carry FOM, per-device
        utilization, counters, etc; the optimizer selects what it understands.
        Returned decisions may be per-GPU.
        """
        # FOM tracks performance impact even when decisions come from elsewhere
        fom_samples = sig.select(batch, sig.KIND_FOM)

        with self._lock:
            for s in fom_samples:
                try:
                    self._fom_history.append(float(s["value"]))
                except (TypeError, ValueError, KeyError):
                    continue

            if not self._startup_reset_done:
                return

            prev_state = self._optimizer.state

            try:
                decisions = sig.normalize_decisions(self._optimizer.ingest(batch))
            except Exception as e:
                logging.error("[energy] optimizer.ingest() raised: %s" % e)
                return

            # Fire state-change callback if state transitioned
            cur_state = self._optimizer.state
            if cur_state != prev_state and self._on_state_change is not None:
                try:
                    self._on_state_change(cur_state, self._optimizer.state_int, self._optimizer.current_freq)
                except Exception as e:
                    logging.warning("[energy] on_state_change callback failed: %s" % e)

            # Fire perf-drop callback from optimizer status
            try:
                status = self._optimizer.get_status()
            except Exception:
                status = {}
            drop_pct = status.get("last_drop_pct")
            if drop_pct is not None and self._on_perf_drop is not None:
                try:
                    self._on_perf_drop(drop_pct, self._optimizer.current_freq)
                except Exception as e:
                    logging.warning("[energy] on_perf_drop callback failed: %s" % e)

        # Dispatch outside the lock
        if decisions:
            self._send_clock_commands(decisions)

    def reset(self):
        """Reset the optimizer back to IDLE and restore max clock frequency (thread-safe)."""
        with self._lock:
            prev_state = self._optimizer.state
            self._fom_history.clear()
            self._startup_reset_done = False
            self._optimizer.reset()
            logging.info(
                "[energy] Reset from state=%s — restoring max clock (%d MHz) and returning to IDLE."
                % (prev_state, self._sclk_max)
            )
            if self._on_state_change is not None:
                try:
                    self._on_state_change(
                        self._optimizer.state,
                        self._optimizer.state_int,
                        self._optimizer.current_freq,
                    )
                except Exception as e:
                    logging.warning("[energy] on_state_change callback failed: %s" % e)
        self._send_clock_command(self._sclk_max)

    def get_status(self):
        """Return a dict describing the current optimizer state (thread-safe)."""
        with self._lock:
            status = self._optimizer.get_status()
            status["arch"] = self._arch
            return status

    # ------------------------------------------------------------------
    # Host communication
    # ------------------------------------------------------------------

    def _startup_reset_with_retry(self, retry_interval=1, max_attempts=300):
        """Spawn a background thread that retries sending max-clock to all hosts until success."""
        freq = self._sclk_max
        hosts = list(self._hosts)
        port = self._port
        payload = {"clock": "sclk", "type": "max", "value": freq}

        if not hosts:
            logging.warning("[energy] No job hosts — skipping startup reset.")
            self._startup_reset_done = True
            return

        def _retry_loop():
            pending = list(hosts)
            for attempt in range(1, max_attempts + 1):
                still_pending = []
                for host in pending:
                    url = "http://%s:%d/command" % (host, port)
                    try:
                        response = requests.post(url, json=payload, timeout=5)
                        if response.status_code == 200:
                            logging.info("[energy] Startup reset accepted by %s (attempt %d)" % (host, attempt))
                        else:
                            logging.warning(
                                "[energy] Startup reset: %s returned HTTP %d" % (host, response.status_code)
                            )
                            still_pending.append(host)
                    except requests.exceptions.RequestException:
                        still_pending.append(host)
                pending = still_pending
                if not pending:
                    logging.info("[energy] Startup reset complete — all hosts at max clock (%d MHz)." % freq)
                    self._startup_reset_done = True
                    return
                logging.info(
                    "[energy] Startup reset: waiting for %d host(s), retrying in %ds..."
                    % (len(pending), retry_interval)
                )
                time.sleep(retry_interval)
            logging.error("[energy] Startup reset failed after %d attempts for hosts: %s" % (max_attempts, pending))
            self._startup_reset_done = True

        t = threading.Thread(target=_retry_loop, daemon=True)
        t.start()

    def _send_clock_command(self, freq_mhz):
        """Dispatch one all-GPU clock command (legacy scalar path)."""
        self._send_clock_commands([sig.clock_decision(int(freq_mhz))])

    def _send_clock_commands(self, decisions):
        """Dispatch clock decisions to all job hosts in a background thread.

        Each decision is a dict accepted verbatim by CommandController.
        apply_command(), which already supports an optional ``gpu_index`` --
        omitting it applies to every GPU, preserving the original behaviour.
        """
        hosts = list(self._hosts)
        port = self._port
        decisions = [d for d in decisions if d]

        if not decisions:
            return
        if not hosts:
            logging.warning("[energy] no job hosts available — %d clock decision(s) NOT sent" % len(decisions))
            return

        summary = ", ".join(
            "%s=%dMHz" % ("gpu%d" % d["gpu_index"] if "gpu_index" in d else "all", d["value"]) for d in decisions
        )
        logging.info(
            "[energy] Dispatching %d clock decision(s) to %d host(s): %s" % (len(decisions), len(hosts), summary)
        )

        def _dispatch():
            for host in hosts:
                url = "http://%s:%d/command" % (host, port)
                for payload in decisions:
                    try:
                        response = requests.post(url, json=payload, timeout=10)
                        if response.status_code != 200:
                            logging.warning(
                                "[energy] Clock command to %s returned HTTP %d (payload=%s)"
                                % (host, response.status_code, payload)
                            )
                    except requests.exceptions.RequestException as e:
                        logging.error("[energy] Failed to send clock command to %s: %s" % (host, e))

        t = threading.Thread(target=_dispatch, daemon=True)
        t.start()
