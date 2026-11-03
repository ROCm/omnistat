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
# optimizer_stepdown.py
#
# Phase I hierarchical step-down optimizer.  Implements BaseOptimizer.
#
# Algorithm:
#   1. Collect baseline FOM samples at max clock frequency.
#   2. Step clock frequency down by clock_step_major MHz.
#   3. Collect min_observe_samples at new frequency and compute performance drop.
#   4. If drop exceeds max_performance_drop, revert and refine at half the step
#      size (next level).  Repeat up to num_levels refinement levels.
#   5. Converge at the lowest acceptable frequency found.

import logging
import time

from omnistat.optimizer_base import BaseOptimizer

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


class StepDownOptimizer(BaseOptimizer):

    def __init__(self, config, sclk_min, sclk_max, fom_direction="higher", fom_units=None):
        section = "omnistat.optimize"

        self._min_warmup_time_sec = config[section].getfloat("min_warmup_time_sec", 0)
        self._min_observe_samples = config[section].getint("min_observe_samples", 10)
        self._max_performance_drop = config[section].getfloat("max_performance_drop", 5.0)
        self._clock_step_major = config[section].getint("clock_step_major", 50)
        self._num_levels = config[section].getint("num_levels", 3)

        self._fom_higher_is_better = fom_direction == "higher"
        self._fom_units = fom_units

        self._sclk_min = sclk_min
        self._sclk_max = sclk_max

        self._state = _STATE_IDLE
        self._current_level = 1
        self._current_step = self._clock_step_major
        self._current_freq = sclk_max
        self._previous_freq = sclk_max

        self._samples_at_current_freq = []
        self._baseline_fom_mean = None
        self._warmup_start_time = None
        self._last_drop_pct = None

        logging.info(
            "[stepdown] Initialized. sclk=[%d..%d]  step_major=%d  num_levels=%d  "
            "warmup=%.0fs  min_observe=%d  max_drop=%.1f%%  fom_direction=%s  fom_units=%s"
            % (sclk_min, sclk_max, self._clock_step_major, self._num_levels,
               self._min_warmup_time_sec, self._min_observe_samples,
               self._max_performance_drop, fom_direction, fom_units)
        )

    @property
    def state(self):
        return self._state

    @property
    def state_int(self):
        return _STATE_INT[self._state]

    @property
    def current_freq(self):
        return self._current_freq

    def ingest_fom(self, name, value, timestamp_msecs):
        try:
            fom_value = float(value)
        except (TypeError, ValueError):
            logging.warning("[stepdown] Ignoring non-numeric FOM value: %s" % value)
            return None

        self._samples_at_current_freq.append(fom_value)
        logging.debug(
            "[stepdown] FOM ingested: name=%s value=%.4f  state=%s  samples_at_freq=%d"
            % (name, fom_value, self._state, len(self._samples_at_current_freq))
        )
        return self._evaluate()

    def reset(self):
        self._state = _STATE_IDLE
        self._current_level = 1
        self._current_step = self._clock_step_major
        self._current_freq = self._sclk_max
        self._previous_freq = self._sclk_max
        self._samples_at_current_freq = []
        self._baseline_fom_mean = None
        self._warmup_start_time = None
        self._last_drop_pct = None
        logging.info("[stepdown] Reset to IDLE at max clock (%d MHz)." % self._sclk_max)

    def get_status(self):
        return {
            "state": self._state,
            "current_level": self._current_level,
            "num_levels": self._num_levels,
            "current_step_mhz": self._current_step,
            "current_freq_mhz": self._current_freq,
            "sclk_min": self._sclk_min,
            "sclk_max": self._sclk_max,
            "clock_step_major": self._clock_step_major,
            "baseline_fom_mean": self._baseline_fom_mean,
            "samples_at_current_freq": len(self._samples_at_current_freq),
            "last_drop_pct": self._last_drop_pct,
            "warmup_elapsed_sec": (
                round(time.monotonic() - self._warmup_start_time, 1)
                if self._state == _STATE_WARMING_UP else None
            ),
        }

    # ------------------------------------------------------------------
    # Internal state machine
    # ------------------------------------------------------------------

    def _evaluate(self):
        """Run the step-down state machine.

        Returns the target frequency (int) when a clock change is needed,
        or None when no action is required.
        """

        if self._state == _STATE_IDLE:
            if self._min_warmup_time_sec > 0:
                self._warmup_start_time = time.monotonic()
                logging.info(
                    "[stepdown] First FOM sample — warming up for %.0f sec before collecting baseline."
                    % self._min_warmup_time_sec
                )
                self._state = _STATE_WARMING_UP
            else:
                logging.info("[stepdown] First FOM sample — entering OBSERVING_BASELINE.")
                self._state = _STATE_OBSERVING_BASELINE
            return None

        if self._state == _STATE_WARMING_UP:
            elapsed = time.monotonic() - self._warmup_start_time
            if elapsed < self._min_warmup_time_sec:
                return None
            logging.info(
                "[stepdown] Warmup complete (%.1f sec) — entering OBSERVING_BASELINE."
                % elapsed
            )
            self._samples_at_current_freq = []
            self._state = _STATE_OBSERVING_BASELINE
            return None

        if self._state == _STATE_OBSERVING_BASELINE:
            if len(self._samples_at_current_freq) < self._min_observe_samples:
                return None

            self._baseline_fom_mean = self._mean(self._samples_at_current_freq)
            logging.info(
                "[stepdown] Baseline established: mean FOM = %.4f over %d samples at %d MHz"
                % (self._baseline_fom_mean, len(self._samples_at_current_freq), self._current_freq)
            )

            if self._current_freq - self._current_step < self._sclk_min:
                logging.info("[stepdown] Already at or near minimum frequency; converging immediately.")
                self._state = _STATE_CONVERGED
                return None

            return self._step_down()

        if self._state == _STATE_STEPPING_DOWN:
            current_mean = self._mean(self._samples_at_current_freq)
            drop_pct = self._compute_drop(current_mean)
            self._last_drop_pct = drop_pct

            if len(self._samples_at_current_freq) < self._min_observe_samples:
                return None

            logging.info(
                "[stepdown] At %d MHz: mean FOM = %.4f  drop = %.2f%%  (threshold %.1f%%)"
                % (self._current_freq, current_mean, drop_pct, self._max_performance_drop)
            )

            if drop_pct >= self._max_performance_drop:
                revert_freq = self._previous_freq
                if self._current_level < self._num_levels:
                    next_level = self._current_level + 1
                    next_step = self._clock_step_major >> (next_level - 1)
                    next_freq = revert_freq - next_step
                    if next_step < 1 or next_freq < self._sclk_min:
                        logging.info(
                            "[stepdown] Level %d/%d: no room to refine below %d MHz — CONVERGED."
                            % (self._current_level, self._num_levels, revert_freq)
                        )
                        self._current_freq = revert_freq
                        self._state = _STATE_CONVERGED
                        return revert_freq
                    else:
                        logging.info(
                            "[stepdown] Level %d/%d boundary at %d MHz — starting level %d with step=%d MHz from %d MHz"
                            % (self._current_level, self._num_levels, self._current_freq,
                               next_level, next_step, revert_freq)
                        )
                        self._current_level = next_level
                        self._current_step = next_step
                        self._current_freq = next_freq
                        self._previous_freq = revert_freq
                        self._samples_at_current_freq = []
                        self._state = _STATE_STEPPING_DOWN
                        return next_freq
                else:
                    logging.info(
                        "[stepdown] Level %d/%d: drop %.2f%% exceeds threshold — CONVERGED at %d MHz."
                        % (self._current_level, self._num_levels, drop_pct, revert_freq)
                    )
                    self._current_freq = revert_freq
                    self._state = _STATE_CONVERGED
                    return revert_freq
            else:
                if self._current_freq - self._current_step < self._sclk_min:
                    logging.info(
                        "[stepdown] Level %d/%d: at floor %d MHz with acceptable performance — CONVERGED."
                        % (self._current_level, self._num_levels, self._current_freq)
                    )
                    self._state = _STATE_CONVERGED
                    return None
                else:
                    return self._step_down()

        # CONVERGED — compute drop for metrics but take no action
        if self._state == _STATE_CONVERGED:
            if self._baseline_fom_mean is not None:
                current_mean = self._mean(self._samples_at_current_freq)
                self._last_drop_pct = self._compute_drop(current_mean)
            return None

    def _step_down(self):
        """Step down by current_step.  Returns the new target frequency."""
        self._previous_freq = self._current_freq
        self._current_freq -= self._current_step
        logging.info("[stepdown] Stepping down to %d MHz (level %d/%d, step=%d MHz)."
                     % (self._current_freq, self._current_level, self._num_levels, self._current_step))
        self._samples_at_current_freq = []
        self._state = _STATE_STEPPING_DOWN
        return self._current_freq

    def _compute_drop(self, current_mean):
        """Compute percentage performance drop from baseline."""
        if self._fom_higher_is_better:
            return (self._baseline_fom_mean - current_mean) / self._baseline_fom_mean * 100.0
        else:
            return (current_mean - self._baseline_fom_mean) / self._baseline_fom_mean * 100.0

    @staticmethod
    def _mean(samples):
        return sum(samples) / len(samples)
