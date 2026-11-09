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

        # Non-stationary workloads (Cholla FOM drifts 0.175 -> 0.33 s/step purely
        # from particle clustering) make a frozen baseline useless. In trailing
        # mode the reference is the window before last, so slow drift cancels and
        # only step changes from our own actions show.
        self._reference = str(cfg.get("util_reference", "detrended")).strip().lower()
        self._trend_windows = int(_f("util_trend_windows", 5))
        self._fom_window = []
        self._window_means = []
        self._windows_judged = 0

        self._fom_baseline = None
        self._fom_warm = []
        self._fom_recent = []
        self._last_drop_pct = None

        if self._fom_direction not in ("higher", "lower"):
            self._fom_direction = "lower"
        logging.info(
            "[util-guarded] FOM guard: max_drop=%.1f%% reference=%s direction=%s n=%d"
            % (self._max_drop, self._reference, self._fom_direction, self._baseline_n)
        )

    def reset(self):
        super().reset()
        self._fom_window = []
        self._window_means = []
        self._windows_judged = 0
        self._fom_baseline = None
        self._fom_warm = []
        self._fom_recent = []
        self._last_drop_pct = None

    def get_status(self):
        st = super().get_status()
        st["fom_baseline"] = self._fom_baseline
        st["last_drop_pct"] = self._last_drop_pct
        return st

    def _degradation(self, cur, ref):
        """Percent slower than ref, sign-corrected for fom_direction."""
        if ref in (None, 0) or cur is None:
            return None
        if self._fom_direction == "lower":
            return 100.0 * (cur - ref) / ref
        return 100.0 * (ref - cur) / ref

    def _predict_next(self):
        """Least-squares extrapolation of the window means."""
        h = self._window_means[-self._trend_windows :]
        n = len(h)
        if n < 3:
            return None
        xbar = (n - 1) / 2.0
        ybar = sum(h) / n
        sxx = sum((i - xbar) ** 2 for i in range(n))
        if sxx == 0:
            return ybar
        sxy = sum((i - xbar) * (h[i] - ybar) for i in range(n))
        return ybar + (sxy / sxx) * (n - xbar)

    def _drop_pct(self):
        # detrended: compare the latest window against the trend's prediction,
        # so smooth workload drift cancels and only our own effect remains
        if self._reference == "detrended":
            if len(self._window_means) < 3:
                return None
            return self._degradation(self._window_means[-1], self._predict_next_excluding_last())
        if self._reference == "trailing":
            if len(self._window_means) < 2:
                return None
            return self._degradation(self._window_means[-1], self._window_means[-2])
        if self._fom_baseline is None or not self._fom_recent:
            return None
        return self._degradation(sum(self._fom_recent) / len(self._fom_recent), self._fom_baseline)

    def _predict_next_excluding_last(self):
        saved = self._window_means
        self._window_means = saved[:-1]
        try:
            return self._predict_next()
        finally:
            self._window_means = saved

    def ingest(self, batch):
        # absorb FOM for the guard
        for s in sig.select(batch, sig.KIND_FOM):
            try:
                v = float(s["value"])
            except (TypeError, ValueError, KeyError):
                continue

            self._fom_window.append(v)
            if len(self._fom_window) >= self._baseline_n:
                self._window_means.append(sum(self._fom_window) / len(self._fom_window))
                self._fom_window = []

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

        # Evaluate at most once per completed window. Otherwise a single
        # over-budget reading fires on every subsequent FOM sample and ratchets
        # every device back to max within a few seconds.
        fresh = len(self._window_means) > self._windows_judged
        if fresh:
            self._windows_judged = len(self._window_means)
            self._last_drop_pct = self._drop_pct()

        # over budget -> step everything back toward max and skip this round
        if fresh and self._last_drop_pct is not None and self._last_drop_pct > self._max_drop:
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


class UniformGuardedOptimizer(UtilizationGuardedOptimizer):
    """Step a single clock for ALL GPUs, guarded by the detrended FOM reference.

    Measured on Cholla DMO (see designs/sweetspot-phase3-notes.md): per-device
    control loses to uniform capping at every operating point, because slowing a
    subset makes the rest wait at full clock. This searches the uniform axis
    instead -- the same idea as StepDownOptimizer, but with the detrended
    reference, since a frozen baseline is useless on a drifting FOM.

    Config:
        uniform_step_mhz     step size when lowering (default 100)
        uniform_min_mhz      floor (default sclk_min)
    """

    def __init__(self, config, sclk_min, sclk_max, fom_direction="lower", fom_units=None):
        super().__init__(config, sclk_min, sclk_max, fom_direction, fom_units)
        cfg = config["omnistat.optimize"] if config.has_section("omnistat.optimize") else {}
        try:
            self._step_down = int(float(cfg.get("uniform_step_mhz", 100)))
        except (TypeError, ValueError):
            self._step_down = 100
        try:
            self._uniform_min = int(float(cfg.get("uniform_min_mhz", sclk_min)))
        except (TypeError, ValueError):
            self._uniform_min = sclk_min
        try:
            self._confirm_n = int(float(cfg.get("uniform_confirm_windows", 2)))
        except (TypeError, ValueError):
            self._confirm_n = 2
        self._current = sclk_max
        self._converged = False
        self._breaches = 0
        logging.info(
            "[uniform] step=%d floor=%d budget=%.1f%% confirm=%d"
            % (self._step_down, self._uniform_min, self._max_drop, self._confirm_n)
        )

    def reset(self):
        super().reset()
        self._current = self._sclk_max
        self._converged = False
        self._breaches = 0

    @property
    def current_freq(self):
        return self._current

    def get_status(self):
        st = super().get_status()
        st["uniform_mhz"] = self._current
        st["converged"] = self._converged
        return st

    def ingest(self, batch):
        # absorb FOM, update the detrended judgement; act once per window only
        before = self._windows_judged
        for s in sig.select(batch, sig.KIND_FOM):
            try:
                v = float(s["value"])
            except (TypeError, ValueError, KeyError):
                continue
            self._fom_window.append(v)
            if len(self._fom_window) >= self._baseline_n:
                self._window_means.append(sum(self._fom_window) / len(self._fom_window))
                self._fom_window = []

        if len(self._window_means) <= before:
            return []
        self._windows_judged = len(self._window_means)
        self._last_drop_pct = self._drop_pct()
        if self._last_drop_pct is None:
            return []

        if self._last_drop_pct > self._max_drop:
            # one over-budget window is usually noise (measured stdev ~1.6%),
            # so require consecutive breaches before giving up the search
            self._breaches += 1
            if self._breaches < self._confirm_n:
                logging.info(
                    "[uniform] drop %.2f%% > %.2f%% (%d/%d, holding %d MHz)"
                    % (self._last_drop_pct, self._max_drop, self._breaches, self._confirm_n, self._current)
                )
                return []
            if self._current >= self._sclk_max:
                return []
            self._current = min(self._sclk_max, self._current + self._backoff)
            self._converged = True
            self._state = _STATE_BACKED_OFF
            logging.info(
                "[uniform] drop %.2f%% > %.2f%% confirmed -- up to %d MHz (converged)"
                % (self._last_drop_pct, self._max_drop, self._current)
            )
            return [sig.clock_decision(self._current)]

        self._breaches = 0

        if self._converged or self._current <= self._uniform_min:
            self._state = _STATE_CONTROLLING
            return []

        self._current = max(self._uniform_min, self._current - self._step_down)
        self._state = _STATE_CONTROLLING
        logging.info("[uniform] drop %.2f%% ok -- down to %d MHz" % (self._last_drop_pct, self._current))
        return [sig.clock_decision(self._current)]


class DitherOptimizer(BaseOptimizer):
    """Alternate uncapped/capped windows and compare the two directly.

    A fixed FOM baseline is useless when the workload drifts, and a trend model
    only predicts what the reference would have been. Alternating measures it a
    few seconds either side instead, so drift cancels by construction.

    The guard signal is **critical-path busy** (the busiest device), not FOM:
    measured at the collector interval it has ~0.96% window-to-window noise vs
    FOM's 1.64%, and it is the physically right quantity -- if a capped device
    becomes the bottleneck, the busiest device waits more and its busy drops.

    Cycle:  PROBE (all at max, measure reference)
         -> TEST  (cap set at per-device levels, measure)
         -> accept and step deeper, or revert and refine the step.

    Three things make the saving land on a short run:

    * **Per-device depth.** Each capped device descends at a rate scaled by its
      headroom, so an idle device ends far below a half-busy one instead of the
      busiest capped device setting the floor for everyone.
    * **Coarse-to-fine steps.** Descent starts at `dither_step_mhz` and halves
      on each rejection, so the knee is bracketed in a few cycles rather than
      crawled up to in nine.
    * **Settling.** Once the step refines past `dither_step_min_mhz` the search
      is done; the test window jumps to its maximum so the uncapped probe stops
      costing duty cycle.

    Config:
        dither_probe_secs      uncapped window (default 10)
        dither_test_secs       initial capped window (default 10)
        dither_test_max_secs   cap on test window growth (default 90)
        dither_tolerance_pct   allowed drop in critical-path busy (default 3.0,
                               ~2.3 sigma against a measured 1.28% noise)
        dither_step_mhz        initial step (default 200)
        dither_step_min_mhz    refine floor; below this, converged (default 50)
        dither_floor_mhz       lower bound (default sclk_min)
        dither_cap_ratio       cap devices below this fraction of the
                               critical-path device (default 0.8)
        dither_min_busy_pct    discard windows quieter than this (default 20)
    """

    def __init__(self, config, sclk_min, sclk_max, fom_direction="lower", fom_units=None):
        cfg = config["omnistat.optimize"] if config.has_section("omnistat.optimize") else {}

        def _f(k, d):
            try:
                return float(cfg.get(k, d))
            except (TypeError, ValueError):
                return d

        self._sclk_min, self._sclk_max = int(sclk_min), int(sclk_max)
        self._probe_secs = _f("dither_probe_secs", 10.0)
        self._test_secs0 = _f("dither_test_secs", 10.0)
        self._test_max = _f("dither_test_max_secs", 90.0)
        self._tol = _f("dither_tolerance_pct", 3.0)  # ~2.3 sigma; noise stdev measured 1.28%
        self._step0 = int(_f("dither_step_mhz", 200))
        self._step_min = int(_f("dither_step_min_mhz", 50))
        self._floor = int(_f("dither_floor_mhz", sclk_min))
        self._cap_ratio = _f("dither_cap_ratio", 0.8)
        # ignore windows where nothing is really running (startup/teardown)
        self._min_busy = _f("dither_min_busy_pct", 20.0)
        # Growth of the test window per accepted cycle. Every second spent in a
        # longer window is a second not spent taking the next step down, so the
        # default keeps windows short while searching and relies on convergence
        # to jump to test_max for duty cycle.
        self._growth = _f("dither_test_growth", 1.0)
        # consecutive accepts needed before a halved step is restored
        self._recover_after = int(_f("dither_step_recover_after", 2))
        # consecutive rejections before the step is narrowed
        self._confirm = int(_f("dither_confirm_windows", 2))
        # a drop larger than this cannot be ours -- treat as a contaminated window
        self._max_plausible_drop = _f("dither_max_plausible_drop_pct", 15.0)

        self.reset()
        logging.info(
            "[dither] probe=%.0fs test=%.0f..%.0fs tol=%.2f%% step=%d..%d floor=%d cap_ratio=%.2f"
            % (
                self._probe_secs,
                self._test_secs0,
                self._test_max,
                self._tol,
                self._step0,
                self._step_min,
                self._floor,
                self._cap_ratio,
            )
        )

    @property
    def state(self):
        return self._state

    @property
    def current_freq(self):
        """Deepest level currently applied; sclk_max when nothing is capped."""
        return min(self._level.values()) if self._level else self._sclk_max

    def reset(self):
        self._phase, self._phase_t0, self._acc = "PROBE", None, []
        self._probe_ref = None
        self._test_secs = self._test_secs0
        self._step = self._step0
        self._accepts = 0
        self._rejects = 0
        self._level = {}  # device -> MHz currently under test
        self._best_ok = {}  # device -> deepest MHz that passed
        self._cap_set = []
        self._capped_now = set()
        self._converged = False
        self._state, self._last_drop_pct = _STATE_WARMING_UP, None

    def get_status(self):
        return {
            "state": self._state,
            "phase": self._phase,
            "levels_mhz": dict(sorted(self._level.items())),
            "best_ok_mhz": dict(sorted(self._best_ok.items())),
            "cap_set": list(self._cap_set),
            "step_mhz": self._step,
            "test_secs": self._test_secs,
            "converged": self._converged,
            "probe_ref_busy": self._probe_ref,
            "last_drop_pct": self._last_drop_pct,
        }

    def _release(self, devices):
        """Decisions returning the given devices to max."""
        out = [sig.clock_decision(self._sclk_max, device=d) for d in sorted(devices)]
        self._capped_now -= set(devices)
        return out

    def _apply(self, levels):
        out = [sig.clock_decision(int(mhz), device=d) for d, mhz in sorted(levels.items())]
        self._capped_now |= set(levels)
        return out

    def _alpha_for(self, d):
        return self._alpha_d.get(d, self._alpha) if self._alpha_auto else self._alpha

    def _learn_alpha(self, mean):
        """Measure alpha from what the last cut actually did.

        Each applied cut is already an experiment: we know the clock before and
        after and the busy before, and this window supplies the busy after. The
        model says busy_after = busy_before / (1 - alpha*delta), so

            alpha = (1 - busy_before/busy_after) / delta

        No pre-tuning, and it is per device -- which matters, because alpha is
        not a hardware constant. Measured on jobs 499680/499682 it ranged
        0.655-0.839 across six GPUs of identical silicon running identical code,
        tracking how much of each GPU's busy time is actually core-clock bound.
        """
        for d, (f_before, b_before, f_after, ref_before) in list(self._pending.items()):
            self._pending.pop(d, None)
            b_after = mean.get(d)
            if b_after is None or b_before <= 0 or b_after <= 0 or f_before <= 0:
                continue
            # Detrending against the reference GPU was tried and removed: the
            # drift is not common-mode. On this workload the reference rises
            # (92 -> 95%) while the capped GPUs fall, so dividing by the
            # reference's change destroyed the signal -- the learner returned
            # ~0.73 regardless of the true alpha instead of tracking it.
            delta = (f_before - f_after) / float(f_before)
            if delta < 0.02:
                continue  # too small a move to measure against noise
            if b_after <= b_before:
                continue  # slowing a GPU should raise its busy; otherwise noise
            obs = (1.0 - b_before / b_after) / delta
            if not (self._alpha_min <= obs <= self._alpha_max):
                continue
            prev = self._alpha_d.get(d, self._alpha)
            self._alpha_d[d] = (1.0 - self._alpha_ema) * prev + self._alpha_ema * obs
            logging.info(
                "[setpoint] alpha gpu%d: obs %.3f -> %.3f (cut %.1f%%, busy %.1f -> %.1f)"
                % (d, obs, self._alpha_d[d], 100 * delta, b_before, b_after)
            )

    def ingest(self, batch):
        util = sig.select(batch, sig.KIND_UTILIZATION)
        if not util:
            return []
        now = batch.get("timestamp_msecs", 0) / 1000.0
        if self._phase_t0 is None:
            self._phase_t0 = now

        busy = {int(s["device"]): float(s["value"]) for s in util if "device" in s}
        if busy:
            self._acc.append(busy)

        want = self._probe_secs if self._phase == "PROBE" else self._test_secs
        if now - self._phase_t0 < want or not self._acc:
            return []

        devs = sorted(self._acc[0])
        mean = {d: sum(a.get(d, 0.0) for a in self._acc) / len(self._acc) for d in devs}
        crit = max(mean.values()) if mean else 0.0
        self._acc, self._phase_t0 = [], now

        if crit < self._min_busy:
            # not running yet, or winding down -- discard the window
            self._phase = "PROBE"
            return self._release(self._capped_now) if self._capped_now else []

        if self._phase == "PROBE":
            return self._end_probe(mean, crit, devs)
        return self._end_test(mean, devs)

    def _reference(self, mean, devs):
        """Busy of the busiest device we are NOT capping.

        Must exclude the cap set. Using max() over every device looks right --
        it is "the critical path" -- but once a capped device is slowed past the
        real critical path it becomes the busiest device itself and pins the
        maximum high, hiding the very regression the guard exists to catch.
        Job 499947: max-over-all read 94.7% (a capped device) while the true
        reference had collapsed 95.2 -> 74.4 and the run was 9% slower.
        """
        ref_devs = [d for d in devs if d not in self._cap_set]
        if not ref_devs:
            ref_devs = devs
        return max(mean[d] for d in ref_devs)

    def _end_probe(self, mean, crit, devs):
        if self._converged:
            # Caps stay on through a converged probe, and a capped device reads
            # busier than it is. Recomputing membership from that would eject it,
            # release it, and let it back in next window -- so freeze the set.
            cap_set = list(self._cap_set)
        else:
            cap_set = [d for d in devs if mean[d] < self._cap_ratio * crit]

        if not cap_set:
            # Nothing has fallen behind the critical path yet. Stay in PROBE --
            # entering TEST here would "measure" a cap that was never applied
            # and step the ladder down on no evidence.
            self._cap_set = []
            if self._capped_now:
                return self._release(self._capped_now)
            return []

        self._cap_set = cap_set
        self._phase = "TEST"
        self._state = _STATE_CONTROLLING
        # Reference excludes the set about to be capped, so the comparison at
        # the end of TEST is against a device whose speed we did not change.
        self._probe_ref = self._reference(mean, devs)

        # Devices joining the cap set start from max.
        for d in cap_set:
            self._level.setdefault(d, self._sclk_max)
            self._best_ok.setdefault(d, self._sclk_max)
        stale = self._capped_now - set(cap_set)

        if self._converged:
            # Search finished: re-assert the known-good levels, do not descend.
            levels = {d: self._best_ok[d] for d in cap_set}
        else:
            # Step each device in proportion to its headroom, so an idle device
            # descends faster than a half-busy one and they settle at different
            # depths. Normalised so the idlest device gets the full step.
            #
            # Bounded by *absolute* slack as well as rank: normalisation alone
            # hands the full step to whichever device is idlest, even when that
            # device has barely any slack -- which is the usual case for the
            # first member of the cap set. Never cut the clock by a larger
            # fraction than the headroom actually measured (job 499930 rejected
            # a window at t~75 s for exactly this reason).
            head = {d: max(0.0, 1.0 - mean[d] / crit) for d in cap_set}
            hmax = max(head.values()) or 1.0
            levels = {}
            for d in cap_set:
                stepd = min(self._step * head[d] / hmax, head[d] * self._level[d])
                levels[d] = max(self._floor, int(round(self._level[d] - stepd)))
            self._level = levels

        logging.info(
            "[dither] probe crit=%.1f%% cap_set=%s -> test %s for %.0fs (step=%d%s)"
            % (
                crit,
                cap_set,
                {d: int(v) for d, v in sorted(levels.items())},
                self._test_secs,
                self._step,
                ", converged" if self._converged else "",
            )
        )
        return self._release(stale) + self._apply(levels)

    def _end_test(self, mean, devs):
        crit = self._reference(mean, devs)
        drop = 0.0 if not self._probe_ref else 100.0 * (self._probe_ref - crit) / self._probe_ref
        self._last_drop_pct = drop
        self._phase = "PROBE"

        # A capped device busier than the device we are *not* capping has been
        # slowed past the critical path and is now setting it. Back those off
        # individually -- it is a per-device fault and needs no attribution
        # guesswork, unlike the aggregate accept/reject below.
        over = [d for d in self._cap_set if mean.get(d, 0.0) > crit]
        if over:
            for d in over:
                self._level[d] = min(self._sclk_max, int(self._level[d] + self._step))
                self._best_ok[d] = self._level[d]
            logging.warning(
                "[dither] over-capped %s (busy %s vs uncapped ref %.1f%%) -- raising to %s"
                % (over, {d: round(mean[d], 1) for d in over}, crit, {d: self._level[d] for d in over})
            )

        if drop <= self._tol:
            self._best_ok = dict(self._level)
            self._test_secs = min(self._test_max, self._test_secs * self._growth)
            self._state = _STATE_CONTROLLING
            # Let the step recover after sustained success. A rejection early on
            # reflects slack the workload has not developed *yet*, not a
            # permanent limit; without this, one such window halves the step for
            # the rest of the run (job 499930: d1 reached only 1097 MHz vs 800
            # in the floor-800 run, which cost more than the deeper floor gained).
            self._accepts += 1
            if self._accepts >= self._recover_after and self._step < self._step0:
                self._step = min(self._step0, self._step * 2)
                self._accepts = 0
                logging.info("[dither] %d consecutive accepts -- step recovered to %d" % (self._recover_after, self._step))
            if self._level and all(v <= self._floor for v in self._level.values()):
                # Nowhere left to descend -- the search is finished even though
                # no window was ever rejected.
                self._converged = True
                self._test_secs = self._test_max
            logging.info(
                "[dither] test ok (crit %.1f%% vs %.1f%%, drop %.2f%%) -- keep %s, test window %.0fs"
                % (crit, self._probe_ref, drop, {d: int(v) for d, v in sorted(self._level.items())}, self._test_secs)
            )
        else:
            # Revert immediately -- that is cheap and safe -- but only narrow the
            # search after the rejection repeats. A step we applied cannot move
            # the *uncapped* reference by tens of percent, so a large drop is an
            # external transient (job 499983 rejected on a 33.98% collapse, which
            # no 400 MHz step could cause). Halving the step on such a window
            # permanently slows descent for a cause we did not create.
            self._level = dict(self._best_ok)
            self._accepts = 0
            self._state = _STATE_BACKED_OFF
            implausible = drop > self._max_plausible_drop
            if not implausible:
                # a discarded window is evidence of nothing, so it must not
                # count toward confirming the next genuine rejection either
                self._rejects += 1
            confirmed = self._rejects >= self._confirm and not implausible
            if confirmed:
                self._step = self._step // 2
                self._rejects = 0
                if self._step < self._step_min:
                    self._converged = True
                    self._test_secs = self._test_max
                    self._step = self._step_min
            logging.info(
                "[dither] test HURT (crit %.1f%% vs %.1f%%, drop %.2f%% > %.2f%%) -- revert to %s, step=%d%s"
                % (
                    crit,
                    self._probe_ref,
                    drop,
                    self._tol,
                    {d: int(v) for d, v in sorted(self._best_ok.items())},
                    self._step,
                    (
                        ", discarded as transient"
                        if implausible
                        else (", confirmed" if confirmed else ", awaiting confirmation")
                    )
                    + (", converged" if self._converged else ""),
                )
            )

        if self._converged:
            # Search is over; hold the caps through what would have been the
            # probe rather than paying full power to re-measure a settled answer.
            return []
        return self._release(self._capped_now) if self._capped_now else []


class SetpointOptimizer(BaseOptimizer):
    """Drive each device's busy toward a setpoint, with no uncapped probe.

    Step-and-test (see DitherOptimizer) learns the limit by crossing it: it
    descends open loop and only discovers it went too far once a device is
    already past the critical path, paying a degraded window each time
    (job 500057 did this three times). It also spends every probe window at
    full power, so roughly a third of the run saves nothing.

    Both problems come from not knowing *where to land*. We measured that
    (499680 vs 499682): capping a device raises its busy by

        busy_new = busy_old / (1 - alpha * cut),  alpha ~= 0.74

    which predicted the cut d3 actually needed to within 0.6 points. So compute
    the frequency instead of searching for it.

    The reference is the busiest device we are *not* capping -- never the max
    over all devices, which an over-capped device pins high (gotcha #3,
    section 16 of the design notes). Because the reference is never capped, the
    setpoint is observable while everything else stays capped: no probe window,
    full duty cycle.

    Config:
        setpoint_ratio        target busy as a fraction of the reference (0.90)
        setpoint_alpha        response model coefficient. "auto" (default) learns it
                              per device from this run; a number pins it instead.
        setpoint_gain         fraction of the computed correction applied (0.5)
        setpoint_window_secs  measurement window (10)
        setpoint_warmup_secs  ignore everything before this (20)
        setpoint_deadband_pct no action within this of the setpoint (5)
        setpoint_max_step_mhz clamp per update (300)
        setpoint_floor_mhz    lower bound (600; 500 is refused by the hardware,
                              see section 14)
        setpoint_over_margin  busy margin above the reference that counts as
                              over-capped (3.0) -- bare '>' trips on noise
    """

    def __init__(self, config, sclk_min, sclk_max, fom_direction="lower", fom_units=None):
        cfg = config["omnistat.optimize"] if config.has_section("omnistat.optimize") else {}

        def _f(k, d):
            try:
                return float(cfg.get(k, d))
            except (TypeError, ValueError):
                return d

        self._sclk_min, self._sclk_max = int(sclk_min), int(sclk_max)
        self._ratio = _f("setpoint_ratio", 0.90)
        # alpha may be a number (fixed prior fit) or "auto" (learned per device
        # from this run's own observations -- no pre-tuning required).
        _raw_alpha = str(cfg.get("setpoint_alpha", "auto")).strip().lower()
        self._alpha_auto = _raw_alpha in ("auto", "learn", "online")
        self._alpha = 1.0 if self._alpha_auto else _f("setpoint_alpha", 0.74)
        # Starting at 1.0 is the conservative end: cut = frac/alpha, so a larger
        # alpha produces a SMALLER cut. An untrained controller therefore
        # under-cuts rather than overshooting, and observations pull it down.
        self._alpha_ema = _f("setpoint_alpha_ema", 0.4)
        self._alpha_min = _f("setpoint_alpha_min", 0.30)
        self._alpha_max = _f("setpoint_alpha_max", 1.50)
        self._gain = _f("setpoint_gain", 0.5)
        self._window = _f("setpoint_window_secs", 10.0)
        self._warmup = _f("setpoint_warmup_secs", 20.0)
        self._deadband = _f("setpoint_deadband_pct", 5.0)
        self._max_step = int(_f("setpoint_max_step_mhz", 300))
        self._floor = int(_f("setpoint_floor_mhz", 600))
        self._over_margin = _f("setpoint_over_margin", 3.0)
        self._min_busy = _f("setpoint_min_busy_pct", 20.0)

        self.reset()
        logging.info(
            "[setpoint] ratio=%.2f alpha=%.2f gain=%.2f window=%.0fs floor=%d deadband=%.1f%%"
            % (self._ratio, self._alpha, self._gain, self._window, self._floor, self._deadband)
        )

    @property
    def state(self):
        return self._state

    @property
    def current_freq(self):
        return min(self._level.values()) if self._level else self._sclk_max

    def reset(self):
        self._t0 = None
        self._win_t0 = None
        self._acc = []
        self._level = {}
        self._state = _STATE_WARMING_UP
        self._last_ref = None
        self._last_setpoint = None
        self._alpha_d = {}  # device -> learned alpha
        self._pending = {}  # device -> (freq_before, busy_before, freq_after)

    def get_status(self):
        return {
            "state": self._state,
            "levels_mhz": dict(sorted(self._level.items())),
            "reference_busy": self._last_ref,
            "setpoint_busy": self._last_setpoint,
            "alpha_mode": "auto" if self._alpha_auto else "fixed",
            "alpha": ({d: round(v, 3) for d, v in sorted(self._alpha_d.items())} if self._alpha_auto else self._alpha),
        }

    def _alpha_for(self, d):
        return self._alpha_d.get(d, self._alpha) if self._alpha_auto else self._alpha

    def _learn_alpha(self, mean):
        """Measure alpha from what the last cut actually did.

        Each applied cut is already an experiment: we know the clock before and
        after and the busy before, and this window supplies the busy after. The
        model says busy_after = busy_before / (1 - alpha*delta), so

            alpha = (1 - busy_before/busy_after) / delta

        No pre-tuning, and it is per device -- which matters, because alpha is
        not a hardware constant. Measured on jobs 499680/499682 it ranged
        0.655-0.839 across six GPUs of identical silicon running identical code,
        tracking how much of each GPU's busy time is actually core-clock bound.
        """
        for d, (f_before, b_before, f_after, ref_before) in list(self._pending.items()):
            self._pending.pop(d, None)
            b_after = mean.get(d)
            if b_after is None or b_before <= 0 or b_after <= 0 or f_before <= 0:
                continue
            # Detrending against the reference GPU was tried and removed: the
            # drift is not common-mode. On this workload the reference rises
            # (92 -> 95%) while the capped GPUs fall, so dividing by the
            # reference's change destroyed the signal -- the learner returned
            # ~0.73 regardless of the true alpha instead of tracking it.
            delta = (f_before - f_after) / float(f_before)
            if delta < 0.02:
                continue  # too small a move to measure against noise
            if b_after <= b_before:
                continue  # slowing a GPU should raise its busy; otherwise noise
            obs = (1.0 - b_before / b_after) / delta
            if not (self._alpha_min <= obs <= self._alpha_max):
                continue
            prev = self._alpha_d.get(d, self._alpha)
            self._alpha_d[d] = (1.0 - self._alpha_ema) * prev + self._alpha_ema * obs
            logging.info(
                "[setpoint] alpha gpu%d: obs %.3f -> %.3f (cut %.1f%%, busy %.1f -> %.1f)"
                % (d, obs, self._alpha_d[d], 100 * delta, b_before, b_after)
            )

    def ingest(self, batch):
        util = sig.select(batch, sig.KIND_UTILIZATION)
        if not util:
            return []
        now = batch.get("timestamp_msecs", 0) / 1000.0
        if self._t0 is None:
            self._t0, self._win_t0 = now, now

        busy = {int(s["device"]): float(s["value"]) for s in util if "device" in s}
        if busy:
            self._acc.append(busy)
        if now - self._t0 < self._warmup:
            self._acc = []
            self._win_t0 = now
            return []
        if now - self._win_t0 < self._window or not self._acc:
            return []

        devs = sorted(self._acc[0])
        mean = {d: sum(a.get(d, 0.0) for a in self._acc) / len(self._acc) for d in devs}
        self._acc, self._win_t0 = [], now

        capped = [d for d in devs if self._level.get(d, self._sclk_max) < self._sclk_max]
        ref_devs = [d for d in devs if d not in capped] or devs
        ref = max(mean[d] for d in ref_devs)
        if ref < self._min_busy:
            return []  # not running yet, or winding down

        setpoint = self._ratio * ref
        self._last_ref, self._last_setpoint = ref, setpoint
        self._state = _STATE_CONTROLLING
        ref_dev = max(ref_devs, key=lambda d: mean[d])

        if self._alpha_auto:
            self._learn_alpha(mean)

        out, moved = [], {}
        for d in devs:
            if d == ref_dev:
                continue
            cur = self._level.get(d, self._sclk_max)
            b = mean[d]

            if b > ref + self._over_margin:
                # Past the critical path -- it is now setting the pace. Back off
                # immediately; this is the one case that must not wait for the
                # next window.
                new = min(self._sclk_max, cur + self._max_step)
            elif b < setpoint - self._deadband:
                # Slack available. Invert the response model for the additional cut
                # that lands this device on the setpoint, damped by gain.
                frac = 1.0 - (b / setpoint) if setpoint > 0 else 0.0
                alpha = self._alpha_for(d)
                cut = max(0.0, min(0.9, frac / alpha)) * self._gain
                new = int(round(cur * (1.0 - cut)))
            elif b > setpoint + self._deadband:
                new = int(round(cur + self._max_step * 0.5))
            else:
                continue

            new = max(self._floor, min(self._sclk_max, new))
            new = max(cur - self._max_step, min(cur + self._max_step, new))
            if abs(new - cur) < 25:
                continue
            if self._alpha_auto and new < cur:
                # remember the before-state; next window's busy closes the loop
                self._pending[d] = (cur, b, new, self._last_ref)
            self._level[d] = new
            moved[d] = new
            out.append(sig.clock_decision(new, device=d))

        if moved:
            logging.info(
                "[setpoint] ref d%d=%.1f%% setpoint=%.1f%% busy=%s -> %s"
                % (ref_dev, ref, setpoint, {d: round(mean[d], 1) for d in sorted(moved)}, dict(sorted(moved.items())))
            )
        return out
