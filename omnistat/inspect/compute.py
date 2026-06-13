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
    duration: float,
    vector_precisions: tuple[str, ...] = VECTOR_PRECISIONS,
    matrix_precisions: tuple[str, ...] = MATRIX_PRECISIONS,
    valu_wavefront: int = VALU_WAVEFRONT,
    mfma_ops: int = MFMA_OPS,
) -> list[dict] | None:
    """Compute per-precision FLOPS from rocprofiler counter totals.

    Vector FLOPS = ``valu_wavefront * (ADD + MUL + TRANS + 2*FMA)`` per precision.
    Matrix FLOPS = ``mfma_ops * MFMA_MOPS`` per precision.
    Returns a list of ``{precision, kind, total_flops, rate_flops_per_s}`` or
    ``None`` when no precision contributed any work.
    """
    if duration <= 0:
        return None
    out: list[dict] = []
    for p in vector_precisions:
        add = totals.get(f"SQ_INSTS_VALU_ADD_{p}", 0.0)
        mul = totals.get(f"SQ_INSTS_VALU_MUL_{p}", 0.0)
        trans = totals.get(f"SQ_INSTS_VALU_TRANS_{p}", 0.0)
        fma = totals.get(f"SQ_INSTS_VALU_FMA_{p}", 0.0)
        v = valu_wavefront * (add + mul + trans + fma * 2.0)
        if v > 0:
            out.append(
                {
                    "precision": p.lower(),
                    "kind": "vector",
                    "total_flops": round(v, 4),
                    "rate_flops_per_s": round(v / duration, 4),
                }
            )
    for p in matrix_precisions:
        mfma = totals.get(f"SQ_INSTS_VALU_MFMA_MOPS_{p}", 0.0)
        m = mfma_ops * mfma
        if m > 0:
            out.append(
                {
                    "precision": p.lower(),
                    "kind": "matrix",
                    "total_flops": round(m, 4),
                    "rate_flops_per_s": round(m / duration, 4),
                }
            )
    return out or None
