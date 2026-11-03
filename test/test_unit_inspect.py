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

"""Unit tests for the pure-logic and backend-helper surface of omnistat-inspect.

Scope: pure math/statistics, step selection, series wrappers, cache, query
ledger, backend step helpers, TSDB pure string builders, the CSV backend
end-to-end, and the ``job/`` module layer (Info/Stats/Health/Iterations/
Timeseries/Report/Query driven through a CSV fixture, plus the core lifecycle
and pure helpers). No live TSDB and no CLI layer.
"""

import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from omnistat.inspect import compute
from omnistat.inspect.backend.base import DataSource, QueryLedger
from omnistat.inspect.backend.csv import CsvDataSource
from omnistat.inspect.backend.tsdb import TsdbDataSource, _escape_label_value, _matchers
from omnistat.inspect.cache import JsonStore
from omnistat.inspect.constants import VM_MAX_POINTS
from omnistat.inspect.helpers import auto_step, build_jobs_summary, compute_step
from omnistat.inspect.job.context import JobContext
from omnistat.inspect.job.core import (
    Job,
    Module,
    is_complete,
    load_context,
    save_context,
)
from omnistat.inspect.job.health import Health
from omnistat.inspect.job.info import Info
from omnistat.inspect.job.iterations import Iterations
from omnistat.inspect.job.query import Query
from omnistat.inspect.job.report import Report
from omnistat.inspect.job.stats import Stats
from omnistat.inspect.job.timeseries import Timeseries
from omnistat.inspect.series import SeriesSet


def _series(values, **labels):
    """Build a TSDB-shape series dict from ``[[ts, val], ...]`` and labels."""
    return {"metric": dict(labels), "values": values}


# ---------------------------------------------------------------------------
# compute.py — series unpacking
# ---------------------------------------------------------------------------


class TestExtract:
    def test_extract_values_drops_nan(self):
        """extract_values casts to float and drops "NaN" string samples."""
        r = _series([[0, "1"], [1, "NaN"], [2, "3.5"]])
        assert compute.extract_values(r) == [1.0, 3.5]

    def test_extract_values_empty(self):
        """extract_values on a series with no values returns []."""
        assert compute.extract_values(_series([])) == []

    def test_pool_values_flattens(self):
        """pool_values flattens numeric values from many series into one array."""
        out = compute.pool_values([_series([[0, "1"], [1, "2"]]), _series([[0, "3"]])])
        assert list(out) == [1.0, 2.0, 3.0]

    def test_pool_values_empty(self):
        """pool_values on no data returns an empty ndarray."""
        out = compute.pool_values([])
        assert isinstance(out, np.ndarray)
        assert out.size == 0

    def test_counter_deltas(self):
        """counter_deltas is last-first per series; <2 values are skipped."""
        results = [_series([[0, "10"], [1, "40"]]), _series([[0, "5"]])]
        assert compute.counter_deltas(results) == [30.0]

    def test_dedup_consecutive(self):
        """dedup_consecutive collapses equal runs; handles empty and single."""
        assert compute.dedup_consecutive([1, 1, 2, 2, 2, 3, 1]) == [1, 2, 3, 1]
        assert compute.dedup_consecutive([]) == []
        assert compute.dedup_consecutive([7]) == [7]


# ---------------------------------------------------------------------------
# compute.py — statistics
# ---------------------------------------------------------------------------


class TestStats:
    def test_cv_of_known(self):
        """cv_of is population std over |mean|."""
        assert compute.cv_of([1, 2, 3]) == pytest.approx(np.std([1, 2, 3]) / 2.0)

    def test_cv_of_empty(self):
        """cv_of on empty input is 0.0."""
        assert compute.cv_of([]) == 0.0

    def test_cv_of_zero_mean(self):
        """cv_of returns 0.0 when the mean is exactly zero."""
        assert compute.cv_of([-1, 1]) == 0.0

    def test_percentiles_of_keys(self):
        """percentiles_of keys are p<q> with integer q rendered without a dot."""
        out = compute.percentiles_of([1, 2, 3, 4, 5], (5, 50, 95))
        assert set(out) == {"p5", "p50", "p95"}
        assert out["p50"] == 3.0

    def test_percentiles_of_float_q(self):
        """A non-integer q is rendered verbatim in the key."""
        out = compute.percentiles_of([1, 2, 3, 4], (2.5,))
        assert set(out) == {"p2.5"}

    def test_percentiles_of_empty(self):
        """percentiles_of on empty input returns {}."""
        assert compute.percentiles_of([], (50,)) == {}

    def test_gauge_stats_basic(self):
        """gauge_stats returns (mean,min,max,cv,percentiles,n) rounded."""
        arr = np.array([1.0, 2.0, 3.0, 4.0])
        mean, mn, mx, cv, pct, n = compute.gauge_stats(arr, qs=(50,))
        assert (mean, mn, mx, n) == (2.5, 1.0, 4.0, 4)
        assert cv == round(float(np.std(arr)) / 2.5, 4)
        assert pct == {"p50": 2.5}

    def test_gauge_stats_empty(self):
        """gauge_stats on an empty array returns the all-None tuple."""
        assert compute.gauge_stats(np.array([])) == (None, None, None, None, {}, 0)

    def test_gauge_stats_no_qs(self):
        """An empty qs skips percentile computation."""
        _, _, _, _, pct, _ = compute.gauge_stats(np.array([1.0, 2.0]))
        assert pct == {}

    def test_gauge_stats_zero_mean(self):
        """A zero mean yields cv 0.0."""
        _, _, _, cv, _, _ = compute.gauge_stats(np.array([-1.0, 1.0]))
        assert cv == 0.0

    def test_rate_summary_basic(self):
        """rate_summary totals deltas and averages per-node rates."""
        total, mean, mn, mx, cv, pct, n = compute.rate_summary({"a": (100.0, 10.0), "b": (200.0, 20.0)})
        assert total == 300.0
        assert (mean, mn, mx, cv, n) == (10.0, 10.0, 10.0, 0.0, 2)
        assert pct == {}

    def test_rate_summary_min_duration_filter(self):
        """Nodes below min_duration are dropped from rates but not from total."""
        total, mean, _, _, _, _, n = compute.rate_summary({"a": (100.0, 5.0), "b": (200.0, 20.0)}, min_duration=10.0)
        assert total == 300.0
        assert (mean, n) == (10.0, 1)

    def test_rate_summary_nonpositive_filtered(self):
        """Nodes with delta<=0 or dur<=0 are excluded from the rate population."""
        total, mean, _, _, _, _, n = compute.rate_summary({"a": (0.0, 10.0), "z": (100.0, 0.0), "b": (100.0, 10.0)})
        assert total == 200.0
        assert (mean, n) == (10.0, 1)

    def test_rate_summary_empty(self):
        """rate_summary on no nodes returns zeros with n=0."""
        assert compute.rate_summary({}) == (0.0, 0.0, 0.0, 0.0, 0.0, {}, 0)


# ---------------------------------------------------------------------------
# compute.py — reset-aware counter logic
# ---------------------------------------------------------------------------


class TestCounters:
    def test_despike_single_glitch(self):
        """100,0,100 drops the single spurious zero."""
        out = compute.despike([(0, 100.0), (1, 0.0), (2, 100.0)])
        assert [v for _, v in out] == [100.0, 100.0]

    def test_despike_double_glitch(self):
        """100,0,0,100 drops the double spurious zero."""
        out = compute.despike([(0, 100.0), (1, 0.0), (2, 0.0), (3, 100.0)])
        assert [v for _, v in out] == [100.0, 100.0]

    def test_despike_trailing_zero(self):
        """A lone trailing 0 after a positive run is dropped."""
        out = compute.despike([(0, 100.0), (1, 0.0)])
        assert [v for _, v in out] == [100.0]

    def test_despike_leading_zeros_preserved(self):
        """Leading zeros are legitimate pre-accumulation baseline."""
        out = compute.despike([(0, 0.0), (1, 0.0), (2, 100.0)])
        assert [v for _, v in out] == [0.0, 0.0, 100.0]

    def test_despike_short(self):
        """A series with fewer than two samples is returned unchanged."""
        assert compute.despike([(0, 5.0)]) == [(0, 5.0)]

    def test_reset_aware_delta_monotonic(self):
        """A pure monotonic series returns (last-first, True)."""
        assert compute.reset_aware_delta([1.0, 2.0, 4.0]) == (3.0, True)

    def test_reset_aware_delta_drop(self):
        """A genuine drop adds b and flags monotonic False."""
        assert compute.reset_aware_delta([10.0, 3.0]) == (3.0, False)

    def test_reset_aware_delta_short(self):
        """Empty / single series delta to (0.0, True)."""
        assert compute.reset_aware_delta([]) == (0.0, True)
        assert compute.reset_aware_delta([5.0]) == (0.0, True)

    def test_per_node_counter_deltas(self):
        """Per-label deltas sum across series with observed-span duration."""
        results = [
            _series([[0, "1"], [10, "5"]], instance="n1"),
            _series([[5, "2"], [20, "10"]], instance="n1"),
        ]
        out = compute.per_node_counter_deltas(results)
        assert out["n1"] == (12.0, 20.0)

    def test_per_node_counter_deltas_skips_short_and_nan(self):
        """Series with <2 numeric samples (after NaN drop) are skipped."""
        results = [
            _series([[0, "1"], [1, "NaN"]], instance="n1"),
            _series([[0, "2"], [10, "12"]], instance="n2"),
        ]
        out = compute.per_node_counter_deltas(results)
        assert "n1" not in out
        assert out["n2"] == (10.0, 10.0)

    def test_per_key_counter_deltas(self):
        """Multi-label key tuples sum per key; <2 samples skipped."""
        results = [
            _series([[0, "0"], [1, "4"]], instance="n1", card="0"),
            _series([[0, "0"], [1, "6"]], instance="n1", card="0"),
            _series([[0, "0"]], instance="n1", card="1"),
        ]
        out = compute.per_key_counter_deltas(results, ("instance", "card"))
        assert out == {("n1", "0"): 10.0}

    def test_per_key_increase(self):
        """despike->reset_aware pipeline; monotonic is AND of per-series flags."""
        results = [
            _series([[0, "0"], [10, "100"]], instance="n1"),
            _series([[0, "10"], [10, "3"]], instance="n1"),
        ]
        out = compute.per_key_increase(results, ("instance",))
        delta, span, monotonic = out[("n1",)]
        assert delta == 103.0  # 100 (monotonic) + 3 (reset-read)
        assert span == 10.0
        assert monotonic is False

    def test_per_key_increase_skips_short(self):
        """Series shorter than two numeric samples are skipped."""
        results = [_series([[0, "5"]], instance="n1"), _series([[0, "0"], [5, "50"]], instance="n2")]
        out = compute.per_key_increase(results, ("instance",))
        assert ("n1",) not in out
        assert out[("n2",)] == (50.0, 5.0, True)


# ---------------------------------------------------------------------------
# compute.py — formatting / aggregation
# ---------------------------------------------------------------------------


class TestFormatUnits:
    def test_collapse(self):
        """collapse: empty->None, non-empty->sorted list."""
        assert compute.collapse(set()) is None
        assert compute.collapse({"b", "a"}) == ["a", "b"]

    def test_human_duration_branches(self):
        """human_duration renders h/m/s branches and rounds to whole seconds."""
        assert compute.human_duration(3661) == "1h 1m 1s"
        assert compute.human_duration(61) == "1m 1s"
        assert compute.human_duration(5) == "5s"
        assert compute.human_duration(3661.4) == "1h 1m 1s"

    def test_filter_zero_series(self):
        """All-zero and NaN-only series are dropped; any nonzero keeps it."""
        results = [
            _series([[0, "0"], [1, "0"]], instance="z"),
            _series([[0, "0"], [1, "5"]], instance="k"),
            _series([[0, "NaN"]], instance="n"),
        ]
        kept = compute.filter_zero_series(results)
        assert [r["metric"]["instance"] for r in kept] == ["k"]

    def test_per_label_means(self):
        """per_label_means pools all series for a label; missing label -> unknown."""
        results = [
            _series([[0, "2"], [1, "4"]], instance="n1"),
            _series([[2, "6"]], instance="n1"),
            _series([[0, "8"]]),
        ]
        out = compute.per_label_means(results)
        assert out["n1"] == 4.0
        assert out["unknown"] == 8.0


# ---------------------------------------------------------------------------
# compute.py — flops
# ---------------------------------------------------------------------------


class TestFlops:
    def test_vector_and_matrix(self):
        """Vector and matrix FLOPS use the documented per-precision formulas."""
        totals = {
            "SQ_INSTS_VALU_ADD_F32": 1.0,
            "SQ_INSTS_VALU_MUL_F32": 1.0,
            "SQ_INSTS_VALU_TRANS_F32": 1.0,
            "SQ_INSTS_VALU_FMA_F32": 1.0,
            "SQ_INSTS_VALU_MFMA_MOPS_F16": 2.0,
        }
        out = compute.flops(totals, active_duration=10.0, effective_duration=20.0)
        vec = next(e for e in out if e["kind"] == "vector")
        mat = next(e for e in out if e["kind"] == "matrix")
        assert vec["precision"] == "f32"
        assert vec["total_flops"] == 64 * (1 + 1 + 1 + 2)  # 320
        assert vec["active_rate_flops_per_s"] == 32.0
        assert vec["effective_rate_flops_per_s"] == 16.0
        assert mat["precision"] == "f16"
        assert mat["total_flops"] == 512 * 2  # 1024

    def test_effective_duration_zero(self):
        """effective_duration<=0 returns None."""
        assert compute.flops({"SQ_INSTS_VALU_ADD_F32": 1.0}, 10.0, 0.0) is None

    def test_no_contribution(self):
        """No contributing precision returns None."""
        assert compute.flops({}, 10.0, 20.0) is None

    def test_active_duration_zero(self):
        """active_duration==0 yields active rate 0.0 while effective rate computes."""
        out = compute.flops({"SQ_INSTS_VALU_ADD_F32": 1.0}, active_duration=0.0, effective_duration=10.0)
        entry = out[0]
        assert entry["active_rate_flops_per_s"] == 0.0
        assert entry["effective_rate_flops_per_s"] == round(64.0 / 10.0, 4)


# ---------------------------------------------------------------------------
# helpers.py
# ---------------------------------------------------------------------------


class TestSteps:
    def test_compute_step_uses_interval(self):
        """compute_step uses the sampling interval when the point cap is finer."""
        assert compute_step(3600, 10.0) == 10.0

    def test_compute_step_vm_cap(self):
        """When the point cap dominates, compute_step returns the cap value."""
        duration = VM_MAX_POINTS * 2.0  # vm_limit = 2.0
        assert compute_step(duration, 10.0) == 10.0
        assert compute_step(duration, None) == 2.0

    def test_compute_step_none_floor(self):
        """A None interval with a tiny cap falls back to the 1.0 floor."""
        assert compute_step(3600, None) == 1.0

    def test_auto_step_csv(self):
        """CSV backend returns the interval directly, or 1.0 when None."""
        assert auto_step(3600, 10.0, backend="csv") == 10.0
        assert auto_step(3600, None, backend="csv") == 1.0

    def test_auto_step_tsdb(self):
        """TSDB backend delegates to compute_step."""
        assert auto_step(3600, 10.0, backend="tsdb") == compute_step(3600, 10.0)

    def test_build_jobs_summary(self):
        """build_jobs_summary rolls up per-job info, counts multi-host nodes."""
        results = [
            _series([[0, "1"], [3600, "1"]], jobid="100", instance="n1", user="alice", partition="p"),
            _series([[0, "1"], [3600, "1"]], jobid="100", instance="n2", user="alice", partition="p"),
            _series([[0, "1"], [1800, "1"]], jobid="200", instance="n3", user="bob", partition="q"),
            _series([], jobid="300", instance="n4"),
        ]
        jobs = build_jobs_summary(results)
        assert [j["jobid"] for j in jobs] == ["100", "200"]
        j100 = jobs[0]
        assert j100["num_nodes"] == 2
        assert j100["approximate_duration_hours"] == 1.0
        assert j100["user"] == "alice"
        assert j100["partition"] == "p"
        assert j100["start_time"] == datetime.fromtimestamp(0, tz=timezone.utc).isoformat()
        assert j100["end_time"] == datetime.fromtimestamp(3600, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# series.py
# ---------------------------------------------------------------------------


class TestSeriesSet:
    def test_len_and_bool_empty(self):
        """An empty SeriesSet (list or None) is falsy with length 0."""
        assert len(SeriesSet([])) == 0
        assert not SeriesSet([])
        assert len(SeriesSet(None)) == 0
        assert not SeriesSet(None)

    def test_len_and_bool_populated(self):
        """A populated SeriesSet reports its length and is truthy."""
        ss = SeriesSet([_series([[0, "1"]])])
        assert len(ss) == 1
        assert ss

    def test_per_series_nan_filtered(self):
        """per_series yields (labels, NaN-filtered values) per series."""
        ss = SeriesSet([_series([[0, "1"], [1, "NaN"], [2, "3"]], instance="n1")])
        ((labels, values),) = list(ss.per_series())
        assert labels == {"instance": "n1"}
        assert values == [1.0, 3.0]

    def test_raw_signal_imputes_zero(self):
        """raw_signal returns aligned arrays with NaN imputed as 0.0."""
        ss = SeriesSet([_series([[0, "1"], [1, "NaN"], [2, "3"]])])
        ts, vals = ss.raw_signal()
        assert list(ts) == [0.0, 1.0, 2.0]
        assert list(vals) == [1.0, 0.0, 3.0]  # NaN -> 0.0, unlike per_series which drops it

    def test_raw_signal_empty(self):
        """raw_signal on an empty set returns two empty arrays."""
        ts, vals = SeriesSet([]).raw_signal()
        assert ts.size == 0 and vals.size == 0


# ---------------------------------------------------------------------------
# job/context.py
# ---------------------------------------------------------------------------


class TestJobContext:
    def test_round_trip(self):
        """to_dict/from_dict preserve jobid, tz-aware datetimes, and interval."""
        ctx = JobContext(
            jobid="12345",
            start_time=datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
            end_time=datetime(2024, 1, 1, 1, 0, 0, tzinfo=timezone.utc),
            sampling_interval=10.0,
        )
        assert JobContext.from_dict(ctx.to_dict()) == ctx

    def test_round_trip_none_interval(self):
        """A None sampling_interval survives the round trip."""
        ctx = JobContext(
            jobid="j",
            start_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2024, 1, 2, tzinfo=timezone.utc),
            sampling_interval=None,
        )
        assert JobContext.from_dict(ctx.to_dict()).sampling_interval is None


# ---------------------------------------------------------------------------
# cache.py
# ---------------------------------------------------------------------------


class TestJsonStore:
    def test_put_get_round_trip(self, tmp_path):
        """put then get returns the stored data dict."""
        store = JsonStore(str(tmp_path))
        data = {"answer": 42}
        store.put("j1", "stats", "csv:/x", data, params={"k": 1})
        assert store.get("j1", "stats", "csv:/x", params={"k": 1}) == data

    def test_get_miss_absent(self, tmp_path):
        """A get with no file present is a miss."""
        assert JsonStore(str(tmp_path)).get("nope", "stats", "csv:/x") is None

    def test_get_miss_source_mismatch(self, tmp_path):
        """A differing source_id is a miss."""
        store = JsonStore(str(tmp_path))
        store.put("j1", "stats", "csv:/x", {"v": 1})
        assert store.get("j1", "stats", "csv:/y") is None

    def test_get_miss_params_mismatch(self, tmp_path):
        """A differing params is a miss."""
        store = JsonStore(str(tmp_path))
        store.put("j1", "stats", "csv:/x", {"v": 1}, params={"k": 1})
        assert store.get("j1", "stats", "csv:/x", params={"k": 2}) is None

    @pytest.mark.parametrize("jobid", ["../../etc/passwd", "/etc/passwd", "a/b/c"])
    def test_path_traversal_guard(self, tmp_path, jobid):
        """Malicious jobids resolve to a file directly inside the cache dir."""
        store = JsonStore(str(tmp_path))
        path = store._path(jobid, "context")
        assert os.path.dirname(path) == str(tmp_path)
        assert os.path.commonpath([str(tmp_path), path]) == str(tmp_path)

    @pytest.mark.parametrize("jobid", ["12345", "12345_0"])
    def test_normal_jobids_pass_through(self, tmp_path, jobid):
        """Ordinary jobids are used verbatim in the filename."""
        store = JsonStore(str(tmp_path))
        path = store._path(jobid, "context")
        assert os.path.basename(path) == f"{jobid}.context.json"

    def test_corrupt_json_returns_none(self, tmp_path):
        """A corrupt cache file yields a miss rather than raising."""
        store = JsonStore(str(tmp_path))
        path = store._path("j1", "stats")
        with open(path, "w") as f:
            f.write("{not valid json")
        assert store.get("j1", "stats", "csv:/x") is None


# ---------------------------------------------------------------------------
# backend/base.py — QueryLedger
# ---------------------------------------------------------------------------


class TestQueryLedger:
    def test_record_and_totals(self):
        """record appends and totals round query times to 4 decimals."""
        ledger = QueryLedger()
        ledger.record("q1", "10s", 1.23456, 5)
        ledger.record("q2", "10s", 2.0, 3)
        assert ledger.total_queries() == 2
        assert ledger.total_query_seconds() == round(1.2346 + 2.0, 4)

    def test_summary_keys(self):
        """summary carries the three expected keys."""
        ledger = QueryLedger()
        ledger.record("q", "1s", 0.5, 1)
        summary = ledger.summary()
        assert set(summary) == {"total_queries", "total_query_time_seconds", "elapsed_seconds"}


# ---------------------------------------------------------------------------
# backend/base.py — DataSource step helpers (via a tiny stub)
# ---------------------------------------------------------------------------


class _StubSource(DataSource):
    """Minimal concrete DataSource for exercising base-class helpers."""

    def discover_job(self, jobid):
        return True

    def _refine_range(self, interval):
        return None

    def get_db_info(self):
        return {"type": "stub", "url": "u"}

    def db_summary(self):
        return {}

    def job_query(
        self, metric, step, literal_filters=None, regex_filters=None, join=True, aggregate=None, start=None, end=None
    ):
        return []

    def label_values(self, label, metric=None, match_filters=None, start=None, end=None):
        return []

    def agg_by_label(self, metric, label, step, literal_filters=None, regex_filters=None):
        return {}

    def get_label_for_series(self, metric):
        return "instance"


def _stub(duration_seconds, sampling_interval=10.0):
    src = _StubSource(sampling_interval=sampling_interval)
    src.jobid = "j1"
    src.start_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
    src.end_time = src.start_time + timedelta(seconds=duration_seconds)
    return src


class TestBaseStepHelpers:
    def test_job_duration(self):
        """job_duration is end minus start in seconds."""
        assert _stub(120).job_duration == 120.0

    def test_padded_range(self):
        """padded_range widens the window symmetrically by the pad seconds."""
        src = _stub(120)
        start, end = src.padded_range(60)
        assert start == src.start_time - timedelta(seconds=60)
        assert end == src.end_time + timedelta(seconds=60)

    def test_auto_step(self):
        """auto_step floors the sampling interval at 1.0."""
        assert _stub(120, sampling_interval=10.0).auto_step() == 10.0
        assert _stub(120, sampling_interval=None).auto_step() == 1.0

    def test_coarse_step_breakpoints(self):
        """coarse_step returns 3600/300/60 across the duration breakpoints."""
        assert _stub(7200).coarse_step() == 3600.0
        assert _stub(1200).coarse_step() == 300.0
        assert _stub(300).coarse_step() == 60.0

    def test_iteration_auto_step_delegates(self):
        """iteration_auto_step delegates to helpers.auto_step (tsdb kind)."""
        src = _stub(3600, sampling_interval=10.0)
        assert src.iteration_auto_step() == auto_step(3600.0, 10.0, backend="tsdb")

    def test_context_round_trip(self):
        """to_context / bind_context preserve identity and time range."""
        src = _stub(3600)
        ctx = src.to_context()
        other = _StubSource()
        other.bind_context(ctx)
        assert other.jobid == src.jobid
        assert other.start_time == src.start_time
        assert other.end_time == src.end_time
        assert other.sampling_interval == src.sampling_interval

    def test_source_id(self):
        """source_id combines the db type and location."""
        assert _stub(120).source_id() == "stub:u"


# ---------------------------------------------------------------------------
# backend/tsdb.py — pure string builders (no network)
# ---------------------------------------------------------------------------


class TestTsdbMatchers:
    def test_escape_label_value(self):
        """_escape_label_value escapes backslashes and double quotes."""
        assert _escape_label_value('a\\b"c') == 'a\\\\b\\"c'

    def test_matchers_literal_and_regex(self):
        """Literal matchers use =, regex use =~, literals escaped, literal first."""
        out = _matchers({"instance": 'a"b'}, {"card": "0|1"})
        assert out == 'instance="a\\"b", card=~"0|1"'

    def test_matchers_empty(self):
        """No filters produce an empty matcher string."""
        assert _matchers() == ""

    def test_scoped_selector_join(self):
        """join=True emits the group_left rmsjob_info identity multiply."""
        ds = TsdbDataSource("http://x")
        ds.jobid = "42"
        out = ds._scoped_selector("m", literal_filters={"instance": "n1"}, join=True)
        assert "* on (instance) group_left()" in out
        assert 'max by (instance) (rmsjob_info{jobid="42", jobstep=~".*"})' in out
        assert out.startswith('m{instance="n1"}')

    def test_scoped_selector_inline(self):
        """join=False inlines the jobid/jobstep scope plus matchers."""
        ds = TsdbDataSource("http://x")
        ds.jobid = "42"
        out = ds._scoped_selector("m", literal_filters={"instance": "n1"}, join=False)
        assert out == 'm{jobid="42", jobstep=~".*", instance="n1"}'


# ---------------------------------------------------------------------------
# backend/csv.py — end-to-end (uses tmp_path)
# ---------------------------------------------------------------------------

_CSV_CONTENT = """\
metric,rocm_utilization_percentage,rocm_utilization_percentage,omnistat_vendor_energy_joules,omnistat_vendor_energy_joules
instance,node1,node2,node1,node2
card,0,1,,
timestamp,,,,
2024-01-01T00:00:00,10,20,100,1000
2024-01-01T00:00:10,20,30,200,1100
2024-01-01T00:00:20,30,40,300,1200
2024-01-01T00:00:30,40,50,400,1300
2024-01-01T00:00:40,50,60,500,1400
"""


@pytest.fixture
def csv_source(tmp_path):
    """A discovered CsvDataSource backed by a minimal export CSV."""
    (tmp_path / "export.csv").write_text(_CSV_CONTENT)
    src = CsvDataSource(str(tmp_path))
    assert src.discover_job("job1") is True
    return src


def _utc(hh, mm, ss):
    return datetime(2024, 1, 1, hh, mm, ss, tzinfo=timezone.utc)


class TestCsvBackend:
    def test_discover_job(self, csv_source):
        """discover_job adopts the full range, hosts, and inferred interval."""
        assert csv_source.start_time == _utc(0, 0, 0)
        assert csv_source.end_time == _utc(0, 0, 40)
        assert csv_source.hosts == ["node1", "node2"]
        assert csv_source.sampling_interval == 10.0

    def test_job_query_literal(self, csv_source):
        """A literal instance filter returns only that node, exact match only."""
        node1 = csv_source.job_query("rocm_utilization_percentage", 10, literal_filters={"instance": "node1"})
        assert len(node1) == 1
        assert node1[0]["metric"]["instance"] == "node1"

        none = csv_source.job_query("rocm_utilization_percentage", 10, literal_filters={"instance": "node.1"})
        assert none == []

    def test_job_query_regex(self, csv_source):
        """A regex card filter matches both card slots."""
        both = csv_source.job_query("rocm_utilization_percentage", 10, regex_filters={"card": "0|1"})
        assert len(both) == 2

    def test_time_filter_narrows(self, csv_source):
        """start/end narrow the returned window."""
        r = csv_source.job_query(
            "rocm_utilization_percentage",
            10,
            literal_filters={"instance": "node1"},
            start=_utc(0, 0, 10),
            end=_utc(0, 0, 30),
        )
        assert len(r[0]["values"]) == 3

    def test_aggregate_avg(self, csv_source):
        """aggregate=avg yields a single per-timestamp mean series."""
        r = csv_source.job_query("rocm_utilization_percentage", 10, aggregate="avg")
        assert len(r) == 1
        vals = [float(v[1]) for v in r[0]["values"]]
        assert vals == [15.0, 25.0, 35.0, 45.0, 55.0]

    def test_agg_by_label(self, csv_source):
        """agg_by_label returns per-label time-averaged means."""
        out = csv_source.agg_by_label("rocm_utilization_percentage", "instance", 10)
        assert out == {"node1": 30.0, "node2": 40.0}

    def test_label_values(self, csv_source):
        """label_values returns distinct values and ignores start/end."""
        assert csv_source.label_values("instance") == ["node1", "node2"]
        assert csv_source.label_values("instance", start=_utc(0, 0, 0), end=_utc(0, 0, 40)) == ["node1", "node2"]

    def test_counter_increase(self, csv_source):
        """A monotonic counter yields the correct per-key delta and monotonic=True."""
        out = csv_source.counter_increase("omnistat_vendor_energy_joules", ("instance",))
        assert out[("node1",)] == (400.0, 40.0, True)
        assert out[("node2",)] == (400.0, 40.0, True)

    def test_db_info_and_summary(self, csv_source):
        """get_db_info and db_summary carry the expected key sets."""
        assert csv_source.get_db_info() == {"type": "csv", "dir": csv_source.csv_dir}
        summary = csv_source.db_summary()
        assert set(summary) == {"num_jobs", "jobs", "num_hosts", "hosts", "num_metrics", "metrics"}
        assert summary["num_hosts"] == 2


# ===========================================================================
# job/ module layer — driven end-to-end through a rich CSV fixture
# ===========================================================================


_BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _iso(sec):
    return (_BASE + timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S")


def _write_csv(path, columns, timestamps, col_values):
    """Write an omnistat-query --export-shape CSV.

    ``columns`` is a list of label dicts (each must carry ``metric``);
    ``col_values[c][i]`` is column ``c``'s value at ``timestamps[i]`` (``None``
    emits a blank -> missing sample). All columns in one file share the same
    label keys, so header forward-fill is a no-op.
    """
    keys = ["metric"] + sorted({k for c in columns for k in c if k != "metric"})
    lines = []
    for key in keys:
        lines.append(",".join([key] + [str(c.get(key, "")) for c in columns]))
    lines.append("timestamp" + "," * len(columns))
    for i, sec in enumerate(timestamps):
        row = [_iso(sec)]
        for c in range(len(columns)):
            v = col_values[c][i]
            row.append("" if v is None else str(v))
        lines.append(",".join(row))
    path.write_text("\n".join(lines) + "\n")


# 24 samples, 10 s apart: busy / idle / busy -> two detectable iterations.
_UTIL_TS = [i * 10 for i in range(24)]
_UTIL = [90.0] * 8 + [5.0] * 4 + [90.0] * 12


@pytest.fixture
def job_ds(tmp_path):
    """A discovered CsvDataSource backed by a rich set of per-metric CSVs."""
    d = tmp_path

    # GPU utilization: identical across two cards so the avg signal is clean.
    _write_csv(
        d / "util.csv",
        [
            {"metric": "rocm_utilization_percentage", "instance": "node1", "card": "0"},
            {"metric": "rocm_utilization_percentage", "instance": "node1", "card": "1"},
        ],
        _UTIL_TS,
        [_UTIL, list(_UTIL)],
    )

    # Temperature: card1 hot (thermal indicator) and spatially divergent (variance).
    _write_csv(
        d / "temp.csv",
        [
            {"metric": "rocm_temperature_celsius", "instance": "node1", "card": "0"},
            {"metric": "rocm_temperature_celsius", "instance": "node1", "card": "1"},
        ],
        [0, 230],
        [[60.0, 60.0], [90.0, 90.0]],
    )

    # Monotonic energy counter on two nodes.
    _write_csv(
        d / "energy.csv",
        [
            {"metric": "omnistat_vendor_energy_joules", "instance": "node1"},
            {"metric": "omnistat_vendor_energy_joules", "instance": "node2"},
        ],
        [0, 230],
        [[100.0, 500.0], [1000.0, 1400.0]],
    )

    # Hardware counter (drives hardware_counters + flops + hw variance).
    _write_csv(
        d / "hw.csv",
        [
            {"metric": "omnistat_hardware_counter", "instance": "node1", "card": "0", "name": "SQ_INSTS_VALU_ADD_F32"},
            {"metric": "omnistat_hardware_counter", "instance": "node1", "card": "1", "name": "SQ_INSTS_VALU_ADD_F32"},
        ],
        [0, 230],
        [[0.0, 1000.0], [0.0, 2000.0]],
    )

    # rmsjob_info on both nodes, with a deliberate gap on node1 (missing sample).
    _write_csv(
        d / "rmsjob.csv",
        [
            {"metric": "rmsjob_info", "instance": "node1", "user": "alice", "partition": "gpu", "nodes": "2"},
            {"metric": "rmsjob_info", "instance": "node2", "user": "alice", "partition": "gpu", "nodes": "2"},
        ],
        [0, 10, 20, 230],
        [[1.0, 1.0, None, 1.0], [1.0, 1.0, 1.0, None]],
    )

    _write_csv(
        d / "info.csv",
        [
            {
                "metric": "omnistat_info",
                "instance": "node1",
                "version": "1.2.3",
                "interval_secs": "10",
                "push_interval_secs": "5",
            }
        ],
        [0, 10],
        [[1.0, 1.0]],
    )
    _write_csv(d / "gpus.csv", [{"metric": "rocm_num_gpus", "instance": "node1"}], [0, 10], [[2.0, 2.0]])
    _write_csv(
        d / "version.csv",
        [{"metric": "rocm_version_info", "instance": "node1", "type": "MI250X", "vbios": "vb1", "driver_ver": "d1"}],
        [0, 10],
        [[1.0, 1.0]],
    )
    # RAS counter that increments (uncorrectable errors) -> a health indicator.
    _write_csv(
        d / "ras.csv",
        [{"metric": "rocm_ras_uncorrectable_count", "instance": "node1", "card": "0"}],
        [0, 230],
        [[0.0, 5.0]],
    )

    ds = CsvDataSource(str(d))
    assert ds.discover_job("job1") is True
    return ds


# ---------------------------------------------------------------------------
# core.py — lifecycle, Module base, context adapters, Job factories
# ---------------------------------------------------------------------------


class _Clock:
    """Minimal object exposing the attributes is_complete() reads."""

    def __init__(self, end_time, sampling_interval=10.0):
        self.end_time = end_time
        self.sampling_interval = sampling_interval


class _CountingModule(Module):
    name = "counting_test"
    param_defaults = {"x": 1}

    def build(self):
        self.calls += 1
        return {"x": self.p.x, "calls": self.calls}


class TestCore:
    def test_is_complete_past(self):
        """A job ended well in the past is complete."""
        assert is_complete(_Clock(datetime(2020, 1, 1, tzinfo=timezone.utc))) is True

    def test_is_complete_recent(self):
        """A job that just ended is within the safety margin -> not complete."""
        assert is_complete(_Clock(datetime.now(timezone.utc))) is False

    def test_is_complete_no_end(self):
        """A job with no end time is not complete."""
        assert is_complete(_Clock(None)) is False

    def test_is_complete_naive_end(self):
        """A tz-naive end time is treated as UTC."""
        assert is_complete(_Clock(datetime(2020, 1, 1))) is True

    def test_module_rejects_unknown_params(self):
        """Constructing a Module with an undeclared knob raises TypeError."""
        with pytest.raises(TypeError):
            _CountingModule(object(), None, bogus=1)

    def test_module_params_and_overrides(self):
        """Declared knobs resolve onto self.p and _params echoes them."""
        m = _CountingModule(object(), None, x=7)
        assert m.p.x == 7
        assert m._params() == {"x": 7}

    def test_module_no_store_rebuilds(self):
        """With no store, get() runs build() every call."""
        m = _CountingModule(_Clock(datetime(2020, 1, 1, tzinfo=timezone.utc)))
        m.calls = 0
        m.get()
        m.get()
        assert m.calls == 2

    def test_module_caches_when_complete(self, job_ds, tmp_path):
        """With a store and a complete job, get() builds once then serves cache."""
        store = JsonStore(str(tmp_path / "cache"))
        m = _CountingModule(job_ds, store, x=3)
        m.calls = 0
        first = m.get()
        second = m.get()
        assert first == second == {"x": 3, "calls": 1}
        assert m.calls == 1

    def test_iter_series_skips_empty(self):
        """_iter_series yields numeric series and skips empty ones."""
        results = [
            {"metric": {"instance": "n1"}, "values": [[0, "1"], [1, "3"]]},
            {"metric": {"instance": "n2"}, "values": [[0, "NaN"]]},
        ]
        out = list(Module._iter_series(results))
        assert len(out) == 1
        assert out[0][0] == {"instance": "n1"}
        assert out[0][1] == [1.0, 3.0]

    def test_context_adapters_round_trip(self, tmp_path):
        """save_context then load_context returns an equal JobContext."""
        store = JsonStore(str(tmp_path))
        ctx = JobContext(
            jobid="j9",
            start_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_time=datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
            sampling_interval=10.0,
        )
        save_context(store, ctx, "csv:/x")
        assert load_context(store, "j9", "csv:/x") == ctx

    def test_load_context_miss(self, tmp_path):
        """load_context returns None when nothing is cached."""
        assert load_context(JsonStore(str(tmp_path)), "nope", "csv:/x") is None

    def test_job_discover_and_context(self, job_ds):
        """Job.discover wraps a discovered source and exposes its context."""
        job = Job(job_ds)
        assert isinstance(job.context, JobContext)
        assert job.context.jobid == "job1"
        assert job.is_complete() is True

    def test_job_discover_not_found(self):
        """Job.discover returns None when the source reports no such job."""

        class _NoJob:
            def discover_job(self, jobid):
                return False

        assert Job.discover(_NoJob(), "job1") is None

    def test_job_from_context(self, job_ds):
        """Job.from_context rehydrates a source without rescanning."""
        ctx = job_ds.to_context()
        fresh = CsvDataSource(job_ds.csv_dir)
        job = Job.from_context(fresh, ctx)
        assert job.ds.jobid == "job1"
        assert job.ds.start_time == job_ds.start_time


# ---------------------------------------------------------------------------
# info.py
# ---------------------------------------------------------------------------


class TestInfo:
    def test_build(self, job_ds):
        """Info rolls identity, topology, versions, and metadata into one dict."""
        out = Info(job_ds).build()
        assert out["jobid"] == "job1"
        assert out["user"] == "alice"
        assert out["partition"] == "gpu"
        assert out["num_nodes"] == 2
        assert out["num_gpus"] == 4  # gpus_per_node(2) * num_nodes(2)
        assert out["gpu_type"] == ["MI250X"]
        assert out["driver_version"] == ["d1"]
        assert out["vbios_version"] == ["vb1"]
        assert out["omnistat_version"] == "1.2.3"
        assert out["sampling_interval"] == 10.0
        assert out["duration_seconds"] == 230.0
        assert out["annotations"] == []
        assert out["figure_of_merit"] is None


# ---------------------------------------------------------------------------
# stats.py
# ---------------------------------------------------------------------------


class TestJobStats:
    def test_build_shape(self, job_ds):
        """Stats.build emits the five top-level report sections."""
        out = Stats(job_ds).build()
        assert set(out) == {"gauges", "counters", "hardware_counters", "kernels", "variance"}

    def test_gauges_and_counters(self, job_ds):
        """Utilization/temperature appear as gauges; energy as a summed counter."""
        out = Stats(job_ds).build()
        gauge_names = {g["name"] for g in out["gauges"]}
        assert "rocm_utilization_percentage" in gauge_names
        assert "rocm_temperature_celsius" in gauge_names

        energy = next(c for c in out["counters"] if c["name"] == "omnistat_vendor_energy_joules")
        assert energy["total"] == pytest.approx(800.0)  # 400 + 400

    def test_hardware_counters_and_flops(self, job_ds):
        """Hardware counters roll up per-name totals and derive vector FLOPS."""
        hw = Stats(job_ds).build()["hardware_counters"]
        row = next(r for r in hw["rows"] if r["counter"] == "SQ_INSTS_VALU_ADD_F32")
        assert row["total"] == pytest.approx(3000.0)  # 1000 + 2000
        assert row["monotonic"] is True
        assert hw["flops"] and hw["flops"][0]["kind"] == "vector"

    def test_temperature_variance(self, job_ds):
        """A spatially divergent temperature yields a by_gpu variance entry."""
        variance = Stats(job_ds).build()["variance"]
        gpu_names = {e["name"] for e in variance["by_gpu"]}
        assert "rocm_temperature_celsius" in gpu_names

    def test_kernels_absent(self, job_ds):
        """With no kernel-trace metric, the kernels section is None."""
        assert Stats(job_ds).build()["kernels"] is None

    def test_extreme_single_and_tuple(self, job_ds):
        """_extreme labels the value with one field, or zips a key tuple."""
        s = Stats(job_ds)
        assert s._extreme(("instance",), "n1", 5.0) == {"value": 5.0, "instance": "n1"}
        assert s._extreme(("instance", "card"), ("n1", "0"), 5.0) == {
            "value": 5.0,
            "instance": "n1",
            "card": "0",
        }

    def test_variance_entry_gated(self, job_ds):
        """_variance_entry emits for divergent keys and gates out uniform ones."""
        s = Stats(job_ds)
        assert s._variance_entry("rocm_utilization_percentage", ("instance",), {"n1": 5.0, "n2": 5.0}) is None
        entry = s._variance_entry("rocm_utilization_percentage", ("instance",), {"n1": 1.0, "n2": 3.0})
        assert entry is not None
        assert entry["n"] == 2
        assert entry["min"]["instance"] == "n1"
        assert entry["max"]["instance"] == "n2"


# ---------------------------------------------------------------------------
# health.py
# ---------------------------------------------------------------------------


class TestHealth:
    def test_data_collection(self, job_ds):
        """Data-collection reports node counts, staggers, and gap detection."""
        dc = Health(job_ds).build()["data_collection"]
        assert dc["reporting_nodes"] == 2
        assert dc["expected_nodes"] == 2
        assert dc["nodes_with_gaps"] == 1  # node1 has a 220 s gap
        assert dc["total_gaps"] == 1
        assert dc["deactivation_stagger_seconds"] == 210.0  # node1 ends 230, node2 ends 20

    def test_health_indicators(self, job_ds):
        """Thermal (>=90 C) and RAS (increasing error counter) indicators fire."""
        indicators = Health(job_ds).build()["health"]["indicators"]
        categories = {i["category"] for i in indicators}
        assert "thermal" in categories
        assert "ras" in categories
        ras = next(i for i in indicators if i["category"] == "ras")
        assert ras["delta"] == 5

    def test_gaps(self):
        """_gaps flags diffs above 3x the expected step; <2 samples -> none."""
        assert Health._gaps([0, 10, 20, 100], 10) == [80.0]
        assert Health._gaps([0], 10) == []

    def test_stats(self):
        """_stats reduces a list to rounded mean/min/max."""
        assert Health._stats([10, 20, 30]) == {"mean": 20.0, "min": 10.0, "max": 30.0}

    def test_expected_nodes_from_label(self, job_ds):
        """_expected_nodes prefers the numeric nodes label."""
        assert Health(job_ds)._expected_nodes() == 2

    def test_push_exceeded_indicator(self):
        """_push_exceeded_indicator summarizes nodes whose push exceeds interval."""
        out = Health._push_exceeded_indicator([("n1", [1.0, 2.0, 10.0]), ("n2", [1.0])], 5.0)
        assert out["category"] == "push_exceeded"
        assert out["nodes_exceeded"] == 1
        assert out["worst_instance"] == "n1"
        assert Health._push_exceeded_indicator([("n1", [1.0, 2.0])], 5.0) is None

    def test_push_trend_indicator(self):
        """_push_trend_indicator fires when the second half rises >25%."""
        rising = Health._push_trend_indicator([("n1", [1.0, 1.0, 10.0, 10.0])])
        assert rising["category"] == "push_trend"
        assert Health._push_trend_indicator([("n1", [5.0, 5.0, 5.0])]) is None
        assert Health._push_trend_indicator([("n1", [1.0, 2.0])]) is None  # too short


# ---------------------------------------------------------------------------
# iterations.py
# ---------------------------------------------------------------------------


class TestIterations:
    def test_build_detects_two(self, job_ds):
        """The busy/idle/busy signal is split into two iterations with a summary."""
        out = Iterations(job_ds).build()
        assert out["num_iterations"] == 2
        assert len(out["iterations"]) == 2
        assert out["summary"] is not None
        assert out["iterations"][0]["iteration"] == 1

    def test_build_no_data(self, job_ds):
        """A metric with no series returns the empty-iteration error shape."""
        out = Iterations(job_ds, metric="does_not_exist").build()
        assert out["num_iterations"] == 0
        assert out["iterations"] == []
        assert "error" in out

    def test_idle_regions_duration_gate(self):
        """Idle runs shorter than min_idle_seconds are discarded."""
        is_idle = [False, False, True, True, True, False]
        ts = [0, 10, 20, 30, 40, 50]
        assert Iterations._idle_regions(is_idle, ts, 30) == []
        assert Iterations._idle_regions(is_idle, ts, 20) == [(2, 4)]

    def test_idle_regions_trailing(self):
        """A run of idle samples at the end is captured."""
        assert Iterations._idle_regions([False, True, True], [0, 10, 40], 30) == [(1, 2)]

    def test_iteration_bounds(self):
        """Bounds are the spans between (and around) idle regions."""
        assert Iterations._iteration_bounds([], 5) == [(0, 4)]
        assert Iterations._iteration_bounds([(2, 3)], 6) == [(0, 1), (4, 5)]

    def test_summary(self):
        """_summary aggregates durations/integrals; empty -> None."""
        assert Iterations._summary([]) is None
        iters = [
            {"duration_seconds": 60.0, "utilization_integral": 100.0},
            {"duration_seconds": 80.0, "utilization_integral": 200.0},
        ]
        summary = Iterations._summary(iters)
        assert summary["mean_duration"] == 70.0
        assert summary["duration_range"] == [60.0, 80.0]
        assert summary["stddev_duration"] == round(float(np.std([60.0, 80.0])), 1)


# ---------------------------------------------------------------------------
# timeseries.py
# ---------------------------------------------------------------------------


class TestTimeseries:
    def test_node_filter(self, job_ds):
        """A --node/--card literal filter narrows the exported series."""
        out = Timeseries(job_ds, metric="rocm_utilization_percentage", node="node1", card="0").build()
        assert out["metric"] == "rocm_utilization_percentage"
        assert out["num_series"] == 1
        assert out["series"][0]["labels"]["card"] == "0"

    def test_label_regex(self, job_ds):
        """A --label KEY=VALUE regex filter matches multiple cards."""
        out = Timeseries(job_ds, metric="rocm_utilization_percentage", label=["card=0|1"]).build()
        assert out["num_series"] == 2

    def test_malformed_label_raises(self, job_ds):
        """A --label without '=' is rejected."""
        with pytest.raises(ValueError):
            Timeseries(job_ds, metric="rocm_utilization_percentage", label=["bogus"]).build()


# ---------------------------------------------------------------------------
# report.py + query.py
# ---------------------------------------------------------------------------


class TestReportAndQuery:
    def test_report_composes_sections(self, job_ds):
        """Report nests overview/stats/health under their report-card keys."""
        out = Report(job_ds).build()
        assert set(out) == {"overview", "stats", "health"}
        assert out["overview"]["jobid"] == "job1"
        assert "gauges" in out["stats"]
        assert "indicators" in out["health"]["health"]

    def test_query_rejects_csv(self, job_ds):
        """Arbitrary PromQL is unsupported on the CSV backend."""
        out = Query(job_ds, promql="up", step=None).build()
        assert "error" in out
