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
# Feedback control module for the GPU energy/performance sweet-spot optimizer.
#
# EnergyAnalyzer receives FOM (figure-of-merit) samples from standalone.py,
# maintains a bounded history, and runs a Phase I step-down algorithm:
#
#   IDLE
#     -> OBSERVING_BASELINE  (first FOM sample arrives)
#     -> STEPPING_DOWN       (baseline established; steps down one freq at a time)
#     -> CONVERGED           (performance degraded beyond threshold, or no lower freq)
#
# On each state transition that requires a clock change, clock commands are
# dispatched to all job-host /command endpoints in background threads.

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests

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
            raise ValueError(
                "[omnistat.optimize] sclk_frequencies=auto but could not detect GPU arch via rocminfo"
            )
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


_STATE_IDLE = "IDLE"
_STATE_WARMING_UP = "WARMING_UP"
_STATE_OBSERVING_BASELINE = "OBSERVING_BASELINE"
_STATE_STEPPING_DOWN = "STEPPING_DOWN"
_STATE_CONVERGED = "CONVERGED"

# Integer values published to VictoriaMetrics for the omnistat_optimize metric
_STATE_INT = {
    _STATE_IDLE: 0,
    _STATE_CONVERGED: 1,
    _STATE_WARMING_UP: 2,
    _STATE_OBSERVING_BASELINE: 3,
    _STATE_STEPPING_DOWN: 4,
}


class EnergyAnalyzer:
    def __init__(self, config, host_endpoints, collector_port, on_state_change=None, on_perf_drop=None):
        """Initialize from an already-parsed ConfigParser object.

        Args:
            config: ConfigParser object passed from standalone.py
            host_endpoints: list[str] of hostnames for all job nodes, or None
            collector_port: int — port on which each node's /command endpoint listens
        """
        section = "omnistat.optimize"

        self._min_warmup_time_sec = config[section].getfloat("min_warmup_time_sec", 0)
        self._min_observe_samples = config[section].getint("min_observe_samples", 10)
        self._max_performance_drop = config[section].getfloat("max_performance_drop", 5.0)
        fom_history_cache_mb = config[section].getfloat("fom_history_cache_mb", 10)

        fom_direction_raw = config[section].get("fom_direction", "higher").strip().lower()
        if fom_direction_raw not in ("higher", "lower"):
            raise ValueError(
                "[omnistat.optimize] fom_direction must be 'higher' or 'lower', got: '%s'" % fom_direction_raw
            )
        self._fom_higher_is_better = fom_direction_raw == "higher"

        self._clock_step_major = config[section].getint("clock_step_major", 50)
        self._num_levels = config[section].getint("num_levels", 3)

        self._arch, sclk_min, sclk_max = _resolve_sclk_range(config)
        self._sclk_min = sclk_min
        self._sclk_max = sclk_max
        self._current_level = 1
        self._current_step = self._clock_step_major

        # host endpoints for clock commands
        self._hosts = host_endpoints or []
        self._port = int(collector_port)

        # State machine
        self._state = _STATE_IDLE
        self._on_state_change = on_state_change  # optional callback(state_str, state_int, freq_mhz)
        self._on_perf_drop = on_perf_drop          # optional callback(drop_pct, freq_mhz)
        self._lock = threading.Lock()

        # FOM history: each entry is a float value.  Bounded by approximate byte budget.
        # We store plain floats (~28 bytes each in CPython); convert cache_mb to entry count.
        max_entries = max(100, int((fom_history_cache_mb * 1024 * 1024) / 28))
        self._fom_history = deque(maxlen=max_entries)

        # Samples collected at the *current* frequency (reset on each freq change)
        self._samples_at_current_freq = []

        # Frequency tracking: current and previous freq in MHz; start at max
        self._current_freq = sclk_max
        self._previous_freq = sclk_max

        # Warmup tracking
        self._warmup_start_time = None

        # Baseline FOM mean established during OBSERVING_BASELINE
        self._baseline_fom_mean = None

        # Whether the startup max-clock reset has completed successfully
        self._startup_reset_done = False

        logging.info(
            "[energy] Initialized. arch=%s  sclk=[%d..%d]  step_major=%d  num_levels=%d  warmup=%.0fs  min_observe=%d  max_drop=%.1f%%  fom_direction=%s  hosts=%d"
            % (self._arch, self._sclk_min, self._sclk_max, self._clock_step_major, self._num_levels,
               self._min_warmup_time_sec, self._min_observe_samples, self._max_performance_drop,
               fom_direction_raw, len(self._hosts))
        )
        logging.info("[energy] Resetting GPUs to max clock (%d MHz) at startup (will retry until all nodes respond)." % self._sclk_max)
        self._startup_reset_with_retry()
        # Push initial IDLE state
        if self._on_state_change is not None:
            try:
                self._on_state_change(_STATE_IDLE, _STATE_INT[_STATE_IDLE], self._current_freq)
            except Exception as e:
                logging.warning("[energy] Initial state push failed: %s" % e)

    def ingest_fom(self, name, value, timestamp_msecs):
        """Receive a new FOM sample from the /fom endpoint (thread-safe).

        Args:
            name (str): FOM metric name
            value (float|int): FOM value (higher is assumed to be better performance)
            timestamp_msecs (int): Unix timestamp in milliseconds
        """
        try:
            fom_value = float(value)
        except (TypeError, ValueError):
            logging.warning("[energy] Ignoring non-numeric FOM value: %s" % value)
            return

        with self._lock:
            self._fom_history.append(fom_value)
            self._samples_at_current_freq.append(fom_value)
            logging.debug(
                "[energy] FOM ingested: name=%s value=%.4f  state=%s  samples_at_freq=%d"
                % (name, fom_value, self._state, len(self._samples_at_current_freq))
            )
            self._evaluate()

    def _set_state(self, new_state):
        """Set state and notify the on_state_change callback synchronously.  Lock must be held."""
        self._state = new_state
        if self._on_state_change is not None:
            try:
                self._on_state_change(new_state, _STATE_INT[new_state], self._current_freq)
            except Exception as e:
                logging.warning("[energy] on_state_change callback failed: %s" % e)

    def _evaluate(self):
        """Run the Phase I state machine.  Must be called with self._lock held."""

        if self._state == _STATE_IDLE:
            if not self._startup_reset_done:
                # Still waiting for startup reset to complete; discard sample
                return
            if self._min_warmup_time_sec > 0:
                self._warmup_start_time = time.monotonic()
                logging.info(
                    "[energy] First FOM sample — warming up for %.0f sec before collecting baseline."
                    % self._min_warmup_time_sec
                )
                self._set_state(_STATE_WARMING_UP)
            else:
                logging.info("[energy] Startup reset complete — entering OBSERVING_BASELINE.")
                self._set_state(_STATE_OBSERVING_BASELINE)
            return

        if self._state == _STATE_WARMING_UP:
            elapsed = time.monotonic() - self._warmup_start_time
            if elapsed < self._min_warmup_time_sec:
                return  # still warming up; discard this sample
            logging.info(
                "[energy] Warmup complete (%.1f sec) — entering OBSERVING_BASELINE."
                % elapsed
            )
            self._samples_at_current_freq = []
            self._set_state(_STATE_OBSERVING_BASELINE)
            return

        if self._state == _STATE_OBSERVING_BASELINE:
            if len(self._samples_at_current_freq) < self._min_observe_samples:
                return  # Still collecting baseline samples

            self._baseline_fom_mean = self._mean(self._samples_at_current_freq)
            logging.info(
                "[energy] Baseline established: mean FOM = %.4f over %d samples at %d MHz"
                % (self._baseline_fom_mean, len(self._samples_at_current_freq), self._current_freq)
            )

            if self._current_freq - self._current_step < self._sclk_min:
                logging.info("[energy] Already at or near minimum frequency; converging immediately.")
                self._set_state(_STATE_CONVERGED)
                return

            self._step_down()
            return

        if self._state == _STATE_STEPPING_DOWN:
            # Emit running delta on every sample — no minimum wait
            current_mean = self._mean(self._samples_at_current_freq)
            if self._fom_higher_is_better:
                drop_pct = (self._baseline_fom_mean - current_mean) / self._baseline_fom_mean * 100.0
            else:
                drop_pct = (current_mean - self._baseline_fom_mean) / self._baseline_fom_mean * 100.0
            if self._on_perf_drop is not None:
                try:
                    self._on_perf_drop(drop_pct, self._current_freq)
                except Exception as e:
                    logging.warning("[energy] on_perf_drop callback failed: %s" % e)

            if len(self._samples_at_current_freq) < self._min_observe_samples:
                return  # Still collecting samples; wait before making step decision

            logging.info(
                "[energy] At %d MHz: mean FOM = %.4f  drop = %.2f%%  (threshold %.1f%%)"
                % (self._current_freq, current_mean, drop_pct, self._max_performance_drop)
            )

            if drop_pct >= self._max_performance_drop:
                revert_freq = self._previous_freq
                if self._current_level < self._num_levels:
                    # Boundary found — refine with half the step size on the next level
                    next_level = self._current_level + 1
                    next_step = self._clock_step_major >> (next_level - 1)  # major / 2^(level-1)
                    next_freq = revert_freq - next_step
                    if next_step < 1 or next_freq < self._sclk_min:
                        logging.info(
                            "[energy] Level %d/%d: no room to refine below %d MHz — CONVERGED."
                            % (self._current_level, self._num_levels, revert_freq)
                        )
                        self._current_freq = revert_freq
                        self._send_clock_command(revert_freq)
                        self._set_state(_STATE_CONVERGED)
                    else:
                        logging.info(
                            "[energy] Level %d/%d boundary at %d MHz — starting level %d with step=%d MHz from %d MHz"
                            % (self._current_level, self._num_levels, self._current_freq,
                               next_level, next_step, revert_freq)
                        )
                        self._current_level = next_level
                        self._current_step = next_step
                        self._current_freq = next_freq
                        self._previous_freq = revert_freq
                        self._samples_at_current_freq = []
                        self._set_state(_STATE_STEPPING_DOWN)
                        self._send_clock_command(next_freq)
                else:
                    # Last level boundary — converge at last acceptable freq
                    logging.info(
                        "[energy] Level %d/%d: drop %.2f%% exceeds threshold — CONVERGED at %d MHz."
                        % (self._current_level, self._num_levels, drop_pct, revert_freq)
                    )
                    self._current_freq = revert_freq
                    self._send_clock_command(revert_freq)
                    self._set_state(_STATE_CONVERGED)
            else:
                # Performance acceptable — step down further
                if self._current_freq - self._current_step < self._sclk_min:
                    logging.info(
                        "[energy] Level %d/%d: at floor %d MHz with acceptable performance — CONVERGED."
                        % (self._current_level, self._num_levels, self._current_freq)
                    )
                    self._set_state(_STATE_CONVERGED)
                else:
                    self._step_down()
            return

        if self._state == _STATE_CONVERGED:
            if self._baseline_fom_mean is not None and self._on_perf_drop is not None:
                current_mean = self._mean(self._samples_at_current_freq)
                if self._fom_higher_is_better:
                    drop_pct = (self._baseline_fom_mean - current_mean) / self._baseline_fom_mean * 100.0
                else:
                    drop_pct = (current_mean - self._baseline_fom_mean) / self._baseline_fom_mean * 100.0
                try:
                    self._on_perf_drop(drop_pct, self._current_freq)
                except Exception as e:
                    logging.warning("[energy] on_perf_drop callback failed: %s" % e)
            return

    def _step_down(self):
        """Step down by current_step and dispatch clock command.  Lock must be held."""
        self._previous_freq = self._current_freq
        self._current_freq -= self._current_step
        logging.info("[energy] Stepping down to %d MHz (level %d/%d, step=%d MHz)."
                     % (self._current_freq, self._current_level, self._num_levels, self._current_step))
        self._samples_at_current_freq = []
        self._set_state(_STATE_STEPPING_DOWN)
        self._send_clock_command(self._current_freq)

    @staticmethod
    def _mean(samples):
        return sum(samples) / len(samples)

    def _startup_reset_with_retry(self, retry_interval=1, max_attempts=300):
        """Spawn a background thread that retries sending max-clock to all hosts until success.

        Retries up to max_attempts times with retry_interval seconds between attempts.
        Sets _startup_reset_done = True once all hosts acknowledge, unblocking ingest_fom.
        """
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
                            logging.warning("[energy] Startup reset: %s returned HTTP %d" % (host, response.status_code))
                            still_pending.append(host)
                    except requests.exceptions.RequestException:
                        still_pending.append(host)
                pending = still_pending
                if not pending:
                    logging.info("[energy] Startup reset complete — all hosts at max clock (%d MHz)." % freq)
                    self._startup_reset_done = True
                    return
                logging.info("[energy] Startup reset: waiting for %d host(s), retrying in %ds..." % (len(pending), retry_interval))
                time.sleep(retry_interval)
            logging.error("[energy] Startup reset failed after %d attempts for hosts: %s" % (max_attempts, pending))
            # Unblock anyway so the optimizer can proceed
            self._startup_reset_done = True

        t = threading.Thread(target=_retry_loop, daemon=True)
        t.start()

    def _send_clock_command(self, freq_mhz):
        """Dispatch a clock command to all job hosts in a background daemon thread."""
        hosts = list(self._hosts)
        port = self._port
        payload = {"clock": "sclk", "type": "max", "value": int(freq_mhz)}

        if not hosts:
            logging.warning("[energy] _send_clock_command: no job hosts available — clock command NOT sent (freq=%d MHz)" % freq_mhz)
            return

        logging.info(
            "[energy] Dispatching clock command to %d host(s): %s — payload=%s"
            % (len(hosts), hosts, payload)
        )

        def _dispatch():
            for host in hosts:
                url = "http://%s:%d/command" % (host, port)
                try:
                    response = requests.post(url, json=payload, timeout=10)
                    if response.status_code == 200:
                        logging.info("[energy] Clock command accepted by %s" % host)
                    else:
                        logging.warning(
                            "[energy] Clock command to %s returned HTTP %d" % (host, response.status_code)
                        )
                except requests.exceptions.RequestException as e:
                    logging.error("[energy] Failed to send clock command to %s: %s" % (host, e))

        t = threading.Thread(target=_dispatch, daemon=True)
        t.start()

    def reset(self):
        """Reset the optimizer back to IDLE and restore max clock frequency (thread-safe).

        All collected FOM history and baseline data are cleared.  Clock frequencies
        on all job hosts are restored to the configured maximum.  Useful during
        interactive testing when the application or its inputs change.
        """
        with self._lock:
            prev_state = self._state
            self._fom_history.clear()
            self._samples_at_current_freq = []
            self._current_freq = self._sclk_max
            self._previous_freq = self._sclk_max
            self._baseline_fom_mean = None
            self._warmup_start_time = None
            self._startup_reset_done = False
            self._current_level = 1
            self._current_step = self._clock_step_major
            logging.info(
                "[energy] Reset from state=%s — restoring max clock (%d MHz) and returning to IDLE."
                % (prev_state, self._sclk_max)
            )
            self._set_state(_STATE_IDLE)
        self._send_clock_command(self._sclk_max)

    def get_status(self):
        """Return a dict describing the current optimizer state (thread-safe)."""
        with self._lock:
            return {
                "state": self._state,
                "arch": self._arch,
                "current_level": self._current_level,
                "num_levels": self._num_levels,
                "current_step_mhz": self._current_step,
                "current_freq_mhz": self._current_freq,
                "sclk_min": self._sclk_min,
                "sclk_max": self._sclk_max,
                "clock_step_major": self._clock_step_major,
                "baseline_fom_mean": self._baseline_fom_mean,
                "samples_at_current_freq": len(self._samples_at_current_freq),
                "warmup_elapsed_sec": (
                    round(time.monotonic() - self._warmup_start_time, 1)
                    if self._state == _STATE_WARMING_UP else None
                ),
            }
