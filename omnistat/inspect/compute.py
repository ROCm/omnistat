# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2026 Advanced Micro Devices, Inc. All Rights Reserved.
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

"""Pure math/statistics helpers for omnistat-inspect.

These are stateless functions that transform raw TSDB-shape results into
summary numbers.
"""

from __future__ import annotations

import numpy as np

# ---------------------------------------------------------------------------
# Series unpacking
# ---------------------------------------------------------------------------


def extract_values(r: dict) -> list[float]:
    """Pull numeric values from a TSDB-shape series result, dropping NaN strings."""
    return [float(v[1]) for v in r.get("values", []) if v[1] != "NaN"]


def pool_values(results: list[dict]) -> np.ndarray:
    """Flatten values from many series into a single array."""
    out: list[float] = []
    for r in results:
        out.extend(extract_values(r))
    return np.asarray(out, dtype=float) if out else np.array([])


def counter_deltas(results: list[dict]) -> list[float]:
    """Last-minus-first delta per series (assumes monotonic counters)."""
    deltas: list[float] = []
    for r in results:
        values = extract_values(r)
        if len(values) >= 2:
            deltas.append(values[-1] - values[0])
    return deltas


def dedup_consecutive(values: list[float]) -> list[float]:
    """Collapse runs of equal consecutive values to a single sample."""
    if not values:
        return []
    out = [values[0]]
    for v in values[1:]:
        if v != out[-1]:
            out.append(v)
    return out


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def cv_of(values) -> float:
    """Population CV (std / |mean|) of a finite numeric iterable.

    Returns 0.0 when the input is empty or its mean is exactly zero.
    Matches the CV definition used by :func:`gauge_stats` and
    :func:`rate_summary` so a single rounding/zero convention applies
    everywhere variance is reported.
    """
    arr = np.asarray(list(values), dtype=float)
    if arr.size == 0:
        return 0.0
    mean = float(arr.mean())
    if mean == 0.0:
        return 0.0
    return float(arr.std() / abs(mean))


def percentiles_of(values, qs) -> dict[str, float]:
    """``{f"p{q}": np.percentile(values, q)}`` for every q in ``qs``.

    Returns ``{}`` when the input is empty. Output keys (``p5``, ``p25``, …)
    match the ``percentiles: {...}`` shape used across the gauge and variance
    summaries so a single vocabulary covers both.
    """
    arr = np.asarray(list(values), dtype=float)
    if arr.size == 0:
        return {}
    return {f"p{int(q) if float(q).is_integer() else q}": round(float(np.percentile(arr, q)), 4) for q in qs}


def gauge_stats(
    arr: np.ndarray, qs=()
) -> tuple[float | None, float | None, float | None, float | None, dict[str, float], int]:
    """Return ``(mean, min, max, cv, percentiles, n)`` rounded to 4 decimals.

    ``(None, None, None, None, {}, 0)`` when ``arr`` is empty. ``percentiles``
    is a ``{"p<q>": value}`` dict over the same pooled population as ``cv``;
    pass an empty ``qs`` to skip percentile computation. ``n`` is the size of
    the pooled population that fed ``cv`` / ``percentiles``.
    """
    if arr.size == 0:
        return None, None, None, None, {}, 0
    mean = float(np.mean(arr))
    min_value = float(np.min(arr))
    max_value = float(np.max(arr))
    sd = float(np.std(arr))
    cv = sd / abs(mean) if mean != 0 else 0.0
    percentiles = percentiles_of(arr, qs) if qs else {}
    return round(mean, 4), round(min_value, 4), round(max_value, 4), round(cv, 4), percentiles, int(arr.size)


def per_node_counter_deltas(results: list[dict], label: str = "instance") -> dict[str, tuple[float, float]]:
    """Per-label ``(delta, observed_duration_seconds)`` from monotonic counters.

    ``delta`` sums ``last - first`` across all series sharing the label key
    (e.g. multiple NICs on one host). ``observed_duration_seconds`` is the
    span actually covered by that label's samples: ``max(last_ts) -
    min(first_ts)`` across the contributing series. This is the correct
    denominator for per-node rate — using the full job duration would
    underestimate rates on nodes whose reporting window was shorter than the
    job (activation/deactivation stagger).
    """
    deltas: dict[str, float] = {}
    first_ts: dict[str, float] = {}
    last_ts: dict[str, float] = {}
    for r in results:
        key = r.get("metric", {}).get(label, "unknown")
        ts_vals = [(float(v[0]), float(v[1])) for v in r.get("values", []) if v[1] != "NaN"]
        if len(ts_vals) < 2:
            continue
        deltas[key] = deltas.get(key, 0.0) + (ts_vals[-1][1] - ts_vals[0][1])
        t0, t1 = ts_vals[0][0], ts_vals[-1][0]
        first_ts[key] = min(first_ts[key], t0) if key in first_ts else t0
        last_ts[key] = max(last_ts[key], t1) if key in last_ts else t1
    return {k: (deltas[k], max(0.0, last_ts[k] - first_ts[k])) for k in deltas}


def per_key_counter_deltas(results: list[dict], labels: tuple[str, ...]) -> dict[tuple, float]:
    """Per-key ``last - first`` delta from monotonic counters, summed per key.

    ``labels`` names the metric labels that form each key tuple (e.g.
    ``("instance", "card", "kernel")``). Series sharing the same key tuple have
    their individual ``last - first`` deltas summed. Series with fewer than two
    numeric samples are skipped. Generalizes :func:`per_node_counter_deltas` to
    multi-label keys, returning deltas only (no observed-duration component).
    """
    deltas: dict[tuple, float] = {}
    for r in results:
        metric = r.get("metric", {})
        key = tuple(str(metric.get(label, "unknown")) for label in labels)
        values = extract_values(r)
        if len(values) >= 2:
            deltas[key] = deltas.get(key, 0.0) + (values[-1] - values[0])
    return deltas


def despike(samples: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Remove single-/double-sample downward spikes that recover to baseline.

    ``samples`` is a list of ``(ts, value)`` pairs with NaN already dropped.
    Targets the ROCm spurious-zero glitch where a counter momentarily drops and
    then recovers to (at least) its prior value — e.g. ``100, 0, 100`` or the
    double-zero ``100, 0, 0, 100``. Such dips are sensor artifacts, not genuine
    counter resets, and must not be counted by :func:`reset_aware_delta`.

    Rules, scanning left to right against the last *kept* value ``prev``:

    - drop sample ``i`` when ``v[i] < prev`` and ``v[i+1] >= prev`` (the
      ``100, 0, 100`` glitch);
    - drop samples ``i`` and ``i+1`` when both are ``< prev`` and ``v[i+2] >=
      prev`` (the ``100, 0, 0, 100`` double-zero glitch);
    - drop a lone trailing ``0`` after a positive run (no recovery follows);
    - leave leading zeros alone (legitimate pre-accumulation baseline).
    """
    n = len(samples)
    if n < 2:
        return list(samples)
    out: list[tuple[float, float]] = []
    i = 0
    while i < n:
        ts, v = samples[i]
        if out:
            prev = out[-1][1]
            if v < prev:
                # Single-sample glitch: dip recovers on the very next sample.
                if i + 1 < n and samples[i + 1][1] >= prev:
                    i += 1
                    continue
                # Double-sample glitch: two dips then recovery.
                if i + 2 < n and samples[i + 1][1] < prev and samples[i + 2][1] >= prev:
                    i += 2
                    continue
                # Lone trailing zero after a positive run.
                if i == n - 1 and v == 0.0 and prev > 0.0:
                    i += 1
                    continue
        out.append((ts, v))
        i += 1
    return out


def reset_aware_delta(values: list[float]) -> tuple[float, bool]:
    """``(delta, monotonic)`` over consecutive pairs with Prometheus reset semantics.

    ``delta = Σ(b - a if b >= a else b)`` over consecutive ``(a, b)`` pairs —
    the same accumulation VictoriaMetrics/Prometheus ``increase()`` uses to span
    counter resets (a drop is read as "reset to 0, then climbed to ``b``").
    ``monotonic`` is ``False`` when any ``b < a`` step survived (a sustained drop
    = genuine restart / counter multiplexing), ``True`` otherwise. Despike the
    series first so spurious recovering zeros do not flip the flag.
    """
    delta = 0.0
    monotonic = True
    for a, b in zip(values, values[1:]):
        if b >= a:
            delta += b - a
        else:
            delta += b
            monotonic = False
    return delta, monotonic


def per_key_increase(results: list[dict], labels: tuple[str, ...]) -> dict[tuple, tuple[float, float, bool]]:
    """Per-key ``(delta, observed_span_seconds, monotonic)`` from counter series.

    For each series: drop NaN, :func:`despike` the spurious-zero glitch, then sum
    via :func:`reset_aware_delta` (so genuine restarts/multiplexing are summed
    across the break). Series sharing the same key tuple (from ``labels``) have
    their deltas summed. ``observed_span_seconds`` is ``max(last_ts) -
    min(first_ts)`` across the key's series — the span actually covered by its
    samples, the correct denominator for the per-key *active* rate (the full job
    duration would under-rate keys whose reporting window was shorter than the
    job). ``monotonic`` is the AND of the per-series flags for the key. Series
    with fewer than two numeric samples are skipped.
    """
    deltas: dict[tuple, float] = {}
    first_ts: dict[tuple, float] = {}
    last_ts: dict[tuple, float] = {}
    mono: dict[tuple, bool] = {}
    for r in results:
        metric = r.get("metric", {})
        key = tuple(str(metric.get(label, "unknown")) for label in labels)
        ts_vals = [(float(v[0]), float(v[1])) for v in r.get("values", []) if v[1] != "NaN"]
        if len(ts_vals) < 2:
            continue
        clean = despike(ts_vals)
        if len(clean) < 2:
            continue
        delta, monotonic = reset_aware_delta([v for _, v in clean])
        deltas[key] = deltas.get(key, 0.0) + delta
        t0, t1 = clean[0][0], clean[-1][0]
        first_ts[key] = min(first_ts[key], t0) if key in first_ts else t0
        last_ts[key] = max(last_ts[key], t1) if key in last_ts else t1
        mono[key] = mono.get(key, True) and monotonic
    return {k: (deltas[k], max(0.0, last_ts[k] - first_ts[k]), mono[k]) for k in deltas}


def rate_summary(
    per_node_totals: dict[str, tuple[float, float]],
    min_duration: float = 0.0,
    qs=(),
) -> tuple[float, float, float, float, float, dict[str, float], int]:
    """From per-node ``(delta, duration)`` → ``(total, mean_rate, min_rate, max_rate, cv, percentiles, n)``.

    Each per-node rate uses that node's own observed duration. Nodes whose
    ``duration < min_duration`` (or whose ``delta <= 0``) are dropped before
    averaging. ``total`` still sums all deltas regardless of duration.
    ``percentiles`` is a ``{"p<q>": value}`` dict over the per-node-rate
    distribution (same population ``cv`` is computed from); pass empty
    ``qs`` to skip. ``n`` is the number of per-node rates that survived
    filtering — the size of the population behind ``cv`` / ``percentiles``.
    """
    total = sum(d for d, _ in per_node_totals.values())
    rates = np.array([d / dur for d, dur in per_node_totals.values() if d > 0 and dur >= min_duration and dur > 0])
    mean_node = float(rates.mean()) if rates.size else 0.0
    min_value = float(rates.min()) if rates.size else 0.0
    max_value = float(rates.max()) if rates.size else 0.0
    cv = float(rates.std() / abs(mean_node)) if rates.size and mean_node != 0 else 0.0
    percentiles = percentiles_of(rates, qs) if qs and rates.size else {}
    return total, mean_node, min_value, max_value, cv, percentiles, int(rates.size)


# ---------------------------------------------------------------------------
# Formatting / units
# ---------------------------------------------------------------------------


def collapse(values: set[str]) -> list[str] | None:
    """None if empty, otherwise a sorted list (even for a single value)."""
    return sorted(values) if values else None


def human_duration(seconds: float) -> str:
    """Format seconds as ``"Xh Ym Zs"`` / ``"Ym Zs"`` / ``"Zs"``."""
    secs = int(round(seconds))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def filter_zero_series(results: list[dict]) -> list[dict]:
    """Drop series whose every sample is exactly zero.

    Sharp predicate: a single nonzero sample keeps the series. Matches
    MI250X odd-card socket-power semantics (those sensors emit a literal 0
    for the entire job).
    """
    kept: list[dict] = []
    for r in results:
        values = extract_values(r)
        if values and any(v != 0.0 for v in values):
            kept.append(r)
    return kept


def per_label_means(results: list[dict], label: str = "instance") -> dict[str, float]:
    """Per-label arithmetic mean over time, pooling all series for that label.

    Client-side mirror of PromQL ``avg by (<label>)`` so callers can
    pre-filter the result list (e.g. via :func:`filter_zero_series`) before
    aggregating.
    """
    per_key: dict[str, list[float]] = {}
    for r in results:
        key = str(r.get("metric", {}).get(label, "unknown"))
        values = extract_values(r)
        if values:
            per_key.setdefault(key, []).extend(values)
    return {k: float(np.mean(v)) for k, v in per_key.items()}


# Generic FLOPS computation constants (ROCm ``omnistat_hardware_counter``).
# Vector lanes per wavefront and matrix-engine multiplier are constant across
# the AMD GPUs Omnistat currently targets; if future architectures diverge,
# branch in ``flops`` rather than re-introducing arch tables here. These are the
# defaults for ``flops`` and its only consumer; callers may override per call.
VALU_WAVEFRONT = 64
MFMA_OPS = 512
VECTOR_PRECISIONS: tuple[str, ...] = ("F16", "F32", "F64")
MATRIX_PRECISIONS: tuple[str, ...] = ("BF16", "F16", "F32", "F64")


def flops(
    totals: dict[str, float],
    active_duration: float,
    effective_duration: float,
    vector_precisions: tuple[str, ...] = VECTOR_PRECISIONS,
    matrix_precisions: tuple[str, ...] = MATRIX_PRECISIONS,
    valu_wavefront: int = VALU_WAVEFRONT,
    mfma_ops: int = MFMA_OPS,
) -> list[dict] | None:
    """Compute per-precision FLOPS from rocprofiler counter totals.

    Vector FLOPS = ``valu_wavefront * (ADD + MUL + TRANS + 2*FMA)`` per precision.
    Matrix FLOPS = ``mfma_ops * MFMA_MOPS`` per precision.

    Two rates are emitted per precision because they answer different questions:
    ``active_rate_flops_per_s`` divides by ``active_duration`` (the span GCDs were
    actually accumulating — compute speed while busy), while
    ``effective_rate_flops_per_s`` divides by ``effective_duration`` (full wall
    time, charging startup/activation idle). Returns a list of
    ``{precision, kind, total_flops, active_rate_flops_per_s,
    effective_rate_flops_per_s}`` or ``None`` when no precision contributed work.
    """
    if effective_duration <= 0:
        return None

    def _rates(x: float) -> dict:
        return {
            "total_flops": round(x, 4),
            "active_rate_flops_per_s": round(x / active_duration, 4) if active_duration > 0 else 0.0,
            "effective_rate_flops_per_s": round(x / effective_duration, 4),
        }

    out: list[dict] = []
    for p in vector_precisions:
        add = totals.get(f"SQ_INSTS_VALU_ADD_{p}", 0.0)
        mul = totals.get(f"SQ_INSTS_VALU_MUL_{p}", 0.0)
        trans = totals.get(f"SQ_INSTS_VALU_TRANS_{p}", 0.0)
        fma = totals.get(f"SQ_INSTS_VALU_FMA_{p}", 0.0)
        v = valu_wavefront * (add + mul + trans + fma * 2.0)
        if v > 0:
            out.append({"precision": p.lower(), "kind": "vector", **_rates(v)})
    for p in matrix_precisions:
        mfma = totals.get(f"SQ_INSTS_VALU_MFMA_MOPS_{p}", 0.0)
        m = mfma_ops * mfma
        if m > 0:
            out.append({"precision": p.lower(), "kind": "matrix", **_rates(m)})
    return out or None
