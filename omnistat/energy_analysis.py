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
from collections import deque
from datetime import datetime, timezone

import requests

_STATE_IDLE = "IDLE"
_STATE_OBSERVING_BASELINE = "OBSERVING_BASELINE"
_STATE_STEPPING_DOWN = "STEPPING_DOWN"
_STATE_CONVERGED = "CONVERGED"


class EnergyAnalyzer:
    def __init__(self, config, host_endpoints, collector_port):
        """Initialize from an already-parsed ConfigParser object.

        Args:
            config: ConfigParser object passed from standalone.py
            host_endpoints: list[str] of hostnames for all job nodes, or None
            collector_port: int — port on which each node's /command endpoint listens
        """
        section = "omnistat.optimize"

        self._min_observe_samples = config[section].getint("min_observe_samples", 10)
        self._max_performance_drop = config[section].getfloat("max_performance_drop", 5.0)
        fom_history_cache_mb = config[section].getfloat("fom_history_cache_mb", 10)

        fom_direction_raw = config[section].get("fom_direction", "higher").strip().lower()
        if fom_direction_raw not in ("higher", "lower"):
            raise ValueError(
                "[omnistat.optimize] fom_direction must be 'higher' or 'lower', got: '%s'" % fom_direction_raw
            )
        self._fom_higher_is_better = fom_direction_raw == "higher"

        raw_freqs = config[section].get("sclk_frequencies", "").strip()
        self._sclk_frequencies = sorted(int(f.strip()) for f in raw_freqs.split(","))

        # host endpoints for clock commands
        self._hosts = host_endpoints or []
        self._port = int(collector_port)

        # State machine
        self._state = _STATE_IDLE
        self._lock = threading.Lock()

        # FOM history: each entry is a float value.  Bounded by approximate byte budget.
        # We store plain floats (~28 bytes each in CPython); convert cache_mb to entry count.
        max_entries = max(100, int((fom_history_cache_mb * 1024 * 1024) / 28))
        self._fom_history = deque(maxlen=max_entries)

        # Samples collected at the *current* frequency (reset on each freq change)
        self._samples_at_current_freq = []

        # Frequency tracking: index into self._sclk_frequencies (ascending); start at max
        self._current_freq_index = len(self._sclk_frequencies) - 1
        self._previous_freq_index = self._current_freq_index

        # Baseline FOM mean established during OBSERVING_BASELINE
        self._baseline_fom_mean = None

        logging.info(
            "[EnergyAnalyzer] Initialized. freqs=%s  min_observe=%d  max_drop=%.1f%%  fom_direction=%s  hosts=%d"
            % (self._sclk_frequencies, self._min_observe_samples, self._max_performance_drop,
               fom_direction_raw, len(self._hosts))
        )

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
            logging.warning("[EnergyAnalyzer] Ignoring non-numeric FOM value: %s" % value)
            return

        with self._lock:
            self._fom_history.append(fom_value)
            self._samples_at_current_freq.append(fom_value)
            logging.debug(
                "[EnergyAnalyzer] FOM ingested: name=%s value=%.4f  state=%s  samples_at_freq=%d"
                % (name, fom_value, self._state, len(self._samples_at_current_freq))
            )
            self._evaluate()

    def _evaluate(self):
        """Run the Phase I state machine.  Must be called with self._lock held."""

        if self._state == _STATE_IDLE:
            # First FOM sample — start collecting baseline at max frequency
            logging.info("[EnergyAnalyzer] First FOM sample received — entering OBSERVING_BASELINE state.")
            self._state = _STATE_OBSERVING_BASELINE
            return

        if self._state == _STATE_OBSERVING_BASELINE:
            if len(self._samples_at_current_freq) < self._min_observe_samples:
                return  # Still collecting baseline samples

            self._baseline_fom_mean = self._mean(self._samples_at_current_freq)
            logging.info(
                "[EnergyAnalyzer] Baseline established: mean FOM = %.4f over %d samples at %d MHz"
                % (self._baseline_fom_mean, len(self._samples_at_current_freq), self._current_frequency())
            )

            if self._current_freq_index == 0:
                # Already at lowest frequency — nothing to step down to
                logging.info("[EnergyAnalyzer] Already at lowest frequency; converging immediately.")
                self._state = _STATE_CONVERGED
                return

            self._step_down()
            return

        if self._state == _STATE_STEPPING_DOWN:
            if len(self._samples_at_current_freq) < self._min_observe_samples:
                return  # Still collecting samples at this frequency

            current_mean = self._mean(self._samples_at_current_freq)
            if self._fom_higher_is_better:
                drop_pct = (self._baseline_fom_mean - current_mean) / self._baseline_fom_mean * 100.0
            else:
                drop_pct = (current_mean - self._baseline_fom_mean) / self._baseline_fom_mean * 100.0

            logging.info(
                "[EnergyAnalyzer] At %d MHz: mean FOM = %.4f  drop = %.2f%%  (threshold %.1f%%)"
                % (self._current_frequency(), current_mean, drop_pct, self._max_performance_drop)
            )

            if drop_pct >= self._max_performance_drop:
                # Performance degraded beyond threshold — revert to previous frequency
                logging.info(
                    "[EnergyAnalyzer] Performance drop %.2f%% exceeds threshold %.1f%% — reverting to %d MHz and converging."
                    % (drop_pct, self._max_performance_drop, self._sclk_frequencies[self._previous_freq_index])
                )
                self._send_clock_command(self._sclk_frequencies[self._previous_freq_index])
                self._current_freq_index = self._previous_freq_index
                self._state = _STATE_CONVERGED
                logging.info(
                    "[EnergyAnalyzer] CONVERGED at %d MHz." % self._current_frequency()
                )
            else:
                # Performance is acceptable — keep the original baseline and try stepping down further
                self._previous_freq_index = self._current_freq_index

                if self._current_freq_index == 0:
                    logging.info(
                        "[EnergyAnalyzer] Reached lowest frequency %d MHz with acceptable performance — CONVERGED."
                        % self._current_frequency()
                    )
                    self._state = _STATE_CONVERGED
                else:
                    self._step_down()
            return

        # _STATE_CONVERGED — no further action
        return

    def _step_down(self):
        """Decrement frequency index by one and dispatch clock command.  Lock must be held."""
        self._previous_freq_index = self._current_freq_index
        self._current_freq_index -= 1
        new_freq = self._current_frequency()
        logging.info("[EnergyAnalyzer] Stepping down to %d MHz." % new_freq)
        self._samples_at_current_freq = []
        self._state = _STATE_STEPPING_DOWN
        self._send_clock_command(new_freq)

    def _current_frequency(self):
        return self._sclk_frequencies[self._current_freq_index]

    @staticmethod
    def _mean(samples):
        return sum(samples) / len(samples)

    def _send_clock_command(self, freq_mhz):
        """Dispatch a clock command to all job hosts in a background daemon thread."""
        hosts = list(self._hosts)
        port = self._port
        payload = {"clock": "sclk", "type": "max", "value": int(freq_mhz)}

        def _dispatch():
            for host in hosts:
                url = "http://%s:%d/command" % (host, port)
                try:
                    response = requests.post(url, json=payload, timeout=10)
                    if response.status_code == 200:
                        logging.debug("[EnergyAnalyzer] Clock command accepted by %s" % host)
                    else:
                        logging.warning(
                            "[EnergyAnalyzer] Clock command to %s returned HTTP %d" % (host, response.status_code)
                        )
                except requests.exceptions.RequestException as e:
                    logging.error("[EnergyAnalyzer] Failed to send clock command to %s: %s" % (host, e))

        t = threading.Thread(target=_dispatch, daemon=True)
        t.start()

    def reset(self):
        """Reset the optimizer back to IDLE and restore max clock frequency (thread-safe).

        All collected FOM history and baseline data are cleared.  Clock frequencies
        on all job hosts are restored to the configured maximum.  Useful during
        interactive testing when the application or its inputs change.
        """
        max_freq = self._sclk_frequencies[-1]
        with self._lock:
            prev_state = self._state
            self._state = _STATE_IDLE
            self._fom_history.clear()
            self._samples_at_current_freq = []
            self._current_freq_index = len(self._sclk_frequencies) - 1
            self._previous_freq_index = self._current_freq_index
            self._baseline_fom_mean = None
            logging.info(
                "[EnergyAnalyzer] Reset from state=%s — restoring max clock (%d MHz) and returning to IDLE."
                % (prev_state, max_freq)
            )
        self._send_clock_command(max_freq)

    def get_status(self):
        """Return a dict describing the current optimizer state (thread-safe)."""
        with self._lock:
            return {
                "state": self._state,
                "current_freq_mhz": self._current_frequency(),
                "baseline_fom_mean": self._baseline_fom_mean,
                "samples_at_current_freq": len(self._samples_at_current_freq),
                "sclk_frequencies": self._sclk_frequencies,
            }
