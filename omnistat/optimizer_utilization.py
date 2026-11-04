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
# optimizer_utilization.py
#
# Per-GPU frequency control driven by GPU utilization: average each device's
# busy fraction over a window and reduce the clock on the less busy ones.
#
# Low utilization does not imply free slack -- on Cholla phase time is a max
# across ranks, so slowing any working device extends the step. Defaults are
# conservative, and the guarded variant measures the cost with FOM.
# See designs/sweetspot-phase3-notes.md for the measurements.

import logging
import time

from omnistat import optimizer_signals as sig
from omnistat.optimizer_base import BaseOptimizer

_STATE_IDLE = "IDLE"
_STATE_WARMING_UP = "WARMING_UP"
_STATE_CONTROLLING = "CONTROLLING"
_STATE_BACKED_OFF = "BACKED_OFF"

_STATE_INT = {
    _STATE_IDLE: 0,
    _STATE_CONTROLLING: 1,
    _STATE_WARMING_UP: 2,
    _STATE_BACKED_OFF: 5,
}


class UtilizationProportionalOptimizer(BaseOptimizer):
    """Scale each GPU's clock with its busy fraction relative to the busiest.

    Config keys, all under [omnistat.optimize]:

        util_window_secs        trailing window averaged per device (default 10)
        util_update_secs        minimum interval between decisions (default 5)
        util_warmup_secs        ignore everything before this (default 30).
                                Matters here because the workload is balanced
                                early -- capping then is pure loss.
        util_hold_ratio         devices at or above this fraction of the busiest
                                device stay at max clock (default 0.90)
        util_floor_mhz          never go below this (default: sclk_min)
        util_aggressiveness     0..1 scaling of the computed reduction
                                (default 0.5 -- deliberately timid, see above)
        util_step_mhz           quantize targets to this grid (default 50)
        util_min_delta_mhz      suppress changes smaller than this (default 50)
    """

    def __init__(self, config, sclk_min, sclk_max, fom_direction="lower", fom_units=None):
        section = "omnistat.optimize"
        cfg = config[section] if config.has_section(section) else {}

        def _f(key, default):
            try:
                return float(cfg.get(key, default))
            except (TypeError, ValueError):
                return default

        self._sclk_min = int(sclk_min)
        self._sclk_max = int(sclk_max)
        self._window = _f("util_window_secs", 10.0)
        self._update_secs = _f("util_update_secs", 5.0)
        self._warmup_secs = _f("util_warmup_secs", 30.0)
        self._hold_ratio = _f("util_hold_ratio", 0.90)
        self._floor = int(_f("util_floor_mhz", sclk_min))
        self._aggr = _f("util_aggressiveness", 0.5)
        self._step = int(_f("util_step_mhz", 50))
        self._min_delta = int(_f("util_min_delta_mhz", 50))

        self._fom_direction = fom_direction
        self._samples = {}  # device -> [(ts, busy), ...]
        self._applied = {}  # device -> last applied MHz
        self._state = _STATE_IDLE
        self._t_first = None
        self._t_last_update = 0.0

        logging.info(
            "[util] Initialized. sclk=[%d..%d] window=%.1fs update=%.1fs warmup=%.1fs "
            "hold_ratio=%.2f floor=%d aggressiveness=%.2f"
            % (
                self._sclk_min,
                self._sclk_max,
                self._window,
                self._update_secs,
                self._warmup_secs,
                self._hold_ratio,
                self._floor,
                self._aggr,
            )
        )

    # -- BaseOptimizer surface -------------------------------------------------

    @property
    def state(self):
        return self._state

    @property
    def state_int(self):
        return _STATE_INT[self._state]

    @property
    def current_freq(self):
        """Representative frequency -- the lowest currently applied."""
        return min(self._applied.values()) if self._applied else self._sclk_max

    def reset(self):
        self._samples = {}
        self._applied = {}
        self._state = _STATE_IDLE
        self._t_first = None
        self._t_last_update = 0.0

    def get_status(self):
        return {
            "state": self._state,
            "current_freq_mhz": self.current_freq,
            "per_device_mhz": dict(sorted(self._applied.items())),
            "per_device_busy": {d: round(self._mean(d), 1) for d in sorted(self._samples)},
            "tracked_devices": len(self._samples),
        }

    # -- control ---------------------------------------------------------------

    def ingest(self, batch):
        now = batch.get("timestamp_msecs", int(time.time() * 1000)) / 1000.0
        util = sig.select(batch, sig.KIND_UTILIZATION)
        if not util:
            return []

        if self._t_first is None:
            self._t_first = now
            self._state = _STATE_WARMING_UP

        for s in util:
            if "device" not in s:
                continue
            d = int(s["device"])
            self._samples.setdefault(d, []).append((now, float(s["value"])))

        # trim to the window
        cutoff = now - self._window
        for d in self._samples:
            self._samples[d] = [(t, v) for t, v in self._samples[d] if t >= cutoff]

        if now - self._t_first < self._warmup_secs:
            return []
        if now - self._t_last_update < self._update_secs:
            return []
        if len(self._samples) < 2:
            return []

        self._t_last_update = now
        self._state = _STATE_CONTROLLING
        return self._decide()

    def _mean(self, device):
        vals = [v for _, v in self._samples.get(device, [])]
        return sum(vals) / len(vals) if vals else 0.0

    def _target_for(self, busy, busiest):
        """Frequency for a device at `busy` when the busiest device is `busiest`."""
        if busiest <= 0:
            return self._sclk_max
        ratio = busy / busiest
        if ratio >= self._hold_ratio:
            return self._sclk_max
        # linear in the shortfall below hold_ratio, scaled by aggressiveness
        shortfall = (self._hold_ratio - ratio) / self._hold_ratio
        span = self._sclk_max - self._floor
        target = self._sclk_max - span * shortfall * self._aggr
        return max(self._floor, min(self._sclk_max, target))

    def _quantize(self, mhz):
        return int(round(mhz / self._step) * self._step)

    def _decide(self):
        means = {d: self._mean(d) for d in self._samples}
        busiest = max(means.values()) if means else 0.0
        decisions = []
        for d in sorted(means):
            target = self._quantize(self._target_for(means[d], busiest))
            prev = self._applied.get(d)
            if prev is not None and abs(target - prev) < self._min_delta:
                continue
            self._applied[d] = target
            decisions.append(sig.clock_decision(target, device=d))
        if decisions:
            logging.info(
                "[util] busy=%s -> %s"
                % ({d: round(means[d], 1) for d in sorted(means)}, {d["gpu_index"]: d["value"] for d in decisions})
            )
        return decisions


class UtilizationGuardedOptimizer(UtilizationProportionalOptimizer):
    """Utilization-driven control with a FOM guard.

    Utilization decides; FOM measures the damage and pulls back if the cost
    exceeds the budget.  This is the variant to trust on a tightly-coupled
    application, where low utilization does not reliably imply free slack.

    Extra config keys:

        max_performance_drop     percent degradation tolerated vs the baseline
                                 FOM mean (default 2.0, matching the 16%/2%
                                 target on slide 4)
        util_baseline_samples    FOM samples averaged to establish the baseline
                                 before control begins (default 20)
        util_backoff_mhz         step back toward max when over budget
                                 (default 100)
    """

    def __init__(self, config, sclk_min, sclk_max, fom_direction="lower", fom_units=None):
        super().__init__(config, sclk_min, sclk_max, fom_direction, fom_units)
        section = "omnistat.optimize"
        cfg = config[section] if config.has_section(section) else {}

        def _f(key, default):
            try:
                return float(cfg.get(key, default))
            except (TypeError, ValueError):
                return default

        self._max_drop = _f("max_performance_drop", 2.0)
        self._baseline_n = int(_f("util_baseline_samples", 20))
        self._backoff = int(_f("util_backoff_mhz", 100))
        self._fom_baseline = None
        self._fom_warm = []
        self._fom_recent = []
        self._last_drop_pct = None

        logging.info(
            "[util-guarded] FOM guard active: max_performance_drop=%.1f%% baseline_samples=%d"
            % (self._max_drop, self._baseline_n)
        )

    def reset(self):
        super().reset()
        self._fom_baseline = None
        self._fom_warm = []
        self._fom_recent = []
        self._last_drop_pct = None

    def get_status(self):
        st = super().get_status()
        st["fom_baseline"] = self._fom_baseline
        st["last_drop_pct"] = self._last_drop_pct
        return st

    def _drop_pct(self):
        """Percent performance degradation, sign-corrected for fom_direction."""
        if self._fom_baseline is None or not self._fom_recent:
            return None
        cur = sum(self._fom_recent) / len(self._fom_recent)
        if self._fom_direction == "lower":
            # larger value == slower
            return 100.0 * (cur - self._fom_baseline) / self._fom_baseline
        return 100.0 * (self._fom_baseline - cur) / self._fom_baseline

    def ingest(self, batch):
        # absorb FOM for the guard
        for s in sig.select(batch, sig.KIND_FOM):
            try:
                v = float(s["value"])
            except (TypeError, ValueError, KeyError):
                continue
            if self._fom_baseline is None:
                self._fom_warm.append(v)
                if len(self._fom_warm) >= self._baseline_n:
                    self._fom_baseline = sum(self._fom_warm) / len(self._fom_warm)
                    logging.info(
                        "[util-guarded] FOM baseline = %.5f over %d samples" % (self._fom_baseline, len(self._fom_warm))
                    )
            else:
                self._fom_recent.append(v)
                if len(self._fom_recent) > self._baseline_n:
                    self._fom_recent.pop(0)

        self._last_drop_pct = self._drop_pct()

        # over budget -> step everything back toward max and skip this round
        if self._last_drop_pct is not None and self._last_drop_pct > self._max_drop:
            self._state = _STATE_BACKED_OFF
            decisions = []
            for d in sorted(self._applied):
                target = min(self._sclk_max, self._applied[d] + self._backoff)
                if target != self._applied[d]:
                    self._applied[d] = target
                    decisions.append(sig.clock_decision(target, device=d))
            if decisions:
                logging.info(
                    "[util-guarded] drop %.2f%% > budget %.2f%% — backing off to %s"
                    % (self._last_drop_pct, self._max_drop, {d["gpu_index"]: d["value"] for d in decisions})
                )
            return decisions

        # hold the baseline until there is enough FOM to judge
        if self._fom_baseline is None:
            return []

        return super().ingest(batch)
