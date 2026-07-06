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

"""CSV backend for omnistat-inspect.

Backs a :class:`DataSource` with ``omnistat-query --export`` CSV files.
:meth:`job_query` accepts ``aggregate`` / ``start`` / ``end`` parameters for
iteration detection; the ``avg`` aggregate computes the per-timestamp mean
across the matching series client-side (mirroring PromQL ``avg(...)``).
"""

from __future__ import annotations

import functools
import glob
import hashlib
import logging
import os
import pickle
import re
import time
from datetime import datetime

import numpy as np
import pandas as pd

from omnistat.inspect.backend.base import DataSource
from omnistat.inspect.helpers import build_jobs_summary

logger = logging.getLogger(__name__)

CACHE_FILENAME = ".omnistat-cache.pkl"
CACHE_VERSION = 1


class CsvDataSource(DataSource):
    """DataSource backed by ``omnistat-query --export`` CSV files."""

    backend_kind = "csv"

    def __init__(self, csv_dir: str, cache_dir: str | None = None, sampling_interval: float | None = None) -> None:
        super().__init__(sampling_interval=sampling_interval)
        self.csv_dir = csv_dir
        self._cache_dir = cache_dir
        self._series: list[dict] = []
        self._csv_start: datetime | None = None
        self._csv_end: datetime | None = None
        self._load_all()

    # -- Caching ---------------------------------------------------------

    @staticmethod
    def _cache_key(files: list[str]) -> str:
        h = hashlib.sha256()
        for fp in files:
            st = os.stat(fp)
            h.update(f"{os.path.basename(fp)}:{st.st_size}:{st.st_mtime_ns}\n".encode())
        return h.hexdigest()

    def _cache_path(self) -> str:
        if self._cache_dir:
            d = hashlib.sha256(os.path.abspath(self.csv_dir).encode()).hexdigest()[:16]
            return os.path.join(self._cache_dir, f".omnistat-cache-{d}.pkl")
        return os.path.join(self.csv_dir, CACHE_FILENAME)

    def _load_cache(self, path: str, key: str) -> bool:
        if not os.path.exists(path):
            return False
        try:
            with open(path, "rb") as f:
                cache = pickle.load(f)
        except (OSError, pickle.UnpicklingError, EOFError, AttributeError) as exc:
            logger.warning("Failed to read CSV cache %s: %s", path, exc)
            return False
        if cache.get("version") != CACHE_VERSION or cache.get("key") != key:
            return False
        self._series = cache["series"]
        self._csv_start = cache["start"]
        self._csv_end = cache["end"]
        return True

    def _save_cache(self, path: str, key: str) -> None:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                pickle.dump(
                    {
                        "version": CACHE_VERSION,
                        "key": key,
                        "series": self._series,
                        "start": self._csv_start,
                        "end": self._csv_end,
                    },
                    f,
                    pickle.HIGHEST_PROTOCOL,
                )
        except OSError as exc:
            logger.warning("Failed to write CSV cache %s: %s", path, exc)

    # -- Loading ---------------------------------------------------------

    def _load_all(self) -> None:
        files = sorted(glob.glob(os.path.join(self.csv_dir, "*.csv")))
        if not files:
            raise FileNotFoundError(f"No CSV files found in {self.csv_dir}")

        path = self._cache_path()
        key = self._cache_key(files)
        if self._load_cache(path, key):
            return

        for fp in files:
            self._series.extend(self._parse_csv(fp))
        self._save_cache(path, key)

    def _parse_csv(self, filepath: str) -> list[dict]:
        with open(filepath) as f:
            for n_header, line in enumerate(f):
                if line.split(",", 1)[0].strip() == "timestamp":
                    break
            else:
                return []
        if n_header == 0:
            return []

        header = pd.read_csv(filepath, nrows=n_header, header=None, dtype=str)
        level_names = header.iloc[:, 0].tolist()
        levels = header.iloc[:, 1:].ffill(axis=1)

        df = pd.read_csv(filepath, skiprows=n_header + 1, header=None, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        if df.empty:
            return []

        ts_min = df.index.min().to_pydatetime()
        ts_max = df.index.max().to_pydatetime()
        if self._csv_start is None or ts_min < self._csv_start:
            self._csv_start = ts_min
        if self._csv_end is None or ts_max > self._csv_end:
            self._csv_end = ts_max

        epochs = (df.index - pd.Timestamp("1970-01-01", tz="UTC")).total_seconds().values

        out = []
        for col_idx in range(len(df.columns)):
            labels = {}
            for i, lname in enumerate(level_names):
                v = str(levels.iloc[i, col_idx]).strip()
                if v and v != "nan":
                    labels[lname] = v
            metric_name = labels.pop("metric", None)
            if metric_name:
                labels["__name__"] = metric_name
            valid = df.iloc[:, col_idx].notna()
            if not valid.any():
                continue
            out.append(
                {
                    "metric": labels,
                    "timestamps": np.ascontiguousarray(epochs[valid]),
                    "values": np.ascontiguousarray(df.iloc[:, col_idx][valid].values),
                }
            )
        return out

    # -- Helpers ---------------------------------------------------------

    @staticmethod
    def _to_tsdb_values(ts: np.ndarray, values: np.ndarray) -> list[list]:
        return [[float(t), str(v)] for t, v in zip(ts, values)]

    @staticmethod
    @functools.lru_cache(maxsize=256)
    def _compiled_filter(pattern: str) -> "re.Pattern[str]":
        return re.compile(f"^(?:{pattern})$")

    def _match_filters(
        self,
        m: dict[str, str],
        literal_filters: dict[str, str] | None = None,
        regex_filters: dict[str, str] | None = None,
    ) -> bool:
        for k, v in (literal_filters or {}).items():
            if m.get(k) != v:
                return False
        for k, v in (regex_filters or {}).items():
            sv = m.get(k)
            if sv is None or not self._compiled_filter(v).match(sv):
                return False
        return True

    def _iter_metric(
        self,
        metric: str,
        literal_filters: dict[str, str] | None = None,
        regex_filters: dict[str, str] | None = None,
    ):
        for s in self._series:
            m = s["metric"]
            if m.get("__name__") != metric:
                continue
            if (literal_filters or regex_filters) and not self._match_filters(m, literal_filters, regex_filters):
                continue
            yield s

    def _time_filter(self, ts: np.ndarray, values: np.ndarray, start: datetime, end: datetime):
        s = start.timestamp()
        e = end.timestamp()
        mask = (ts >= s) & (ts <= e)
        return ts[mask], values[mask]

    # -- DataSource API --------------------------------------------------

    def job_query(
        self, metric, step, literal_filters=None, regex_filters=None, join=True, aggregate=None, start=None, end=None
    ):
        t0 = time.monotonic()
        q_start = start if start is not None else self.start_time
        q_end = end if end is not None else self.end_time

        matched: list[dict] = []
        for s in self._iter_metric(metric, literal_filters, regex_filters):
            ts, values = self._time_filter(s["timestamps"], s["values"], q_start, q_end)
            if len(ts) == 0:
                continue
            matched.append({"metric": s["metric"], "ts": ts, "values": values})

        if aggregate == "avg" and matched:
            frames = {i: pd.Series(m["values"], index=m["ts"]) for i, m in enumerate(matched)}
            combined = pd.DataFrame(frames).mean(axis=1).dropna()
            results = [
                {
                    "metric": {"__name__": metric},
                    "values": [[float(t), str(v)] for t, v in combined.items()],
                }
            ]
        else:
            results = [{"metric": m["metric"], "values": self._to_tsdb_values(m["ts"], m["values"])} for m in matched]

        elapsed = time.monotonic() - t0
        points = sum(len(r["values"]) for r in results)
        self.ledger.record(
            f"csv:{metric}(literal={literal_filters}, regex={regex_filters}, agg={aggregate})",
            str(step),
            elapsed,
            points,
        )
        return results

    def label_values(self, label, metric=None, match_filters=None, start=None, end=None):
        # ``start``/``end`` are accepted for signature parity with the TSDB
        # backend but ignored: a CSV export has no server-side time window, so
        # label values are always drawn from the whole loaded dataset.
        del start, end
        t0 = time.monotonic()
        values = set()
        for s in self._series:
            m = s["metric"]
            if metric:
                name = m.get("__name__", "")
                if ".*" in metric:
                    if not re.match(f"^{metric}$", name):
                        continue
                elif name != metric:
                    continue
            if match_filters and any(m.get(k) != v for k, v in match_filters.items()):
                continue
            v = m.get(label)
            if v is not None:
                values.add(v)
        out = sorted(values)
        elapsed = time.monotonic() - t0
        self.ledger.record(f"csv:label_values({label})", "n/a", elapsed, len(out))
        return out

    def agg_by_label(self, metric, label, step, literal_filters=None, regex_filters=None):
        t0 = time.monotonic()
        grouped: dict[str, list[float]] = {}
        for s in self._iter_metric(metric, literal_filters, regex_filters):
            ts, values = self._time_filter(s["timestamps"], s["values"], self.start_time, self.end_time)
            if len(values) == 0:
                continue
            key = str(s["metric"].get(label, "unknown"))
            grouped.setdefault(key, []).append(float(np.mean(values)))
        out = {k: float(np.mean(v)) for k, v in grouped.items() if v}
        elapsed = time.monotonic() - t0
        self.ledger.record(f"csv:agg_by({label}, {metric})", str(step), elapsed, len(out))
        return out

    def get_label_for_series(self, metric: str) -> str:
        return "instance"

    def discover_job(self, jobid: str) -> bool:
        # The jobid is accepted as-is and not validated against the file
        # contents: CSV exports don't reliably carry ``rmsjob_info``, so there is
        # no in-file job record to check against. We therefore treat the whole
        # export as belonging to the requested job and adopt its full time range.
        self.jobid = jobid
        self.start_time = self._csv_start
        self.end_time = self._csv_end
        if self.start_time is None:
            return False

        hosts: set[str] = set()
        intervals: list[float] = []
        for s in self._series:
            m = s["metric"]
            host = m.get("instance")
            if host:
                hosts.add(host)
            name = m.get("__name__")
            if name == "rmsjob_info":
                self.user = self.user or m.get("user")
                self.partition = self.partition or m.get("partition")
                self.nodes_label = self.nodes_label or m.get("nodes")
            elif name == "rocm_num_gpus":
                values = s.get("values")
                if values is not None and len(values):
                    n = int(np.max(values))
                    if n > (self.gpus_per_node or 0):
                        self.gpus_per_node = n
            elif name == "rocm_version_info":
                gtype = m.get("type", "")
                if gtype:
                    self.gpu_types.add(gtype)
                vbios = m.get("vbios")
                if vbios:
                    self.vbios_versions.add(vbios)
                driver = m.get("driver_ver")
                if driver:
                    self.driver_versions.add(driver)
            elif name == "omnistat_info":
                self.omnistat_version = self.omnistat_version or m.get("version")
                try:
                    if m.get("interval_secs") is not None:
                        intervals.append(float(m["interval_secs"]))
                except (ValueError, TypeError):
                    pass
        self.hosts = sorted(hosts)

        if self.sampling_interval is None and intervals:
            self.sampling_interval = min(intervals)
        if self.sampling_interval is None:
            deltas: list[float] = []
            for s in self._series:
                ts = s.get("timestamps")
                if ts is not None and len(ts) >= 2:
                    deltas.extend(np.diff(ts).tolist())
                if len(deltas) > 10000:
                    break
            if deltas:
                self.sampling_interval = float(np.median(deltas))

        return True

    def _refine_range(self, interval: float) -> None:
        return

    def get_db_info(self):
        return {"type": "csv", "dir": self.csv_dir}

    def db_summary(self):
        """List the loaded CSV export's jobs, hosts, and Omnistat metrics."""
        names = {s["metric"].get("__name__", "") for s in self._series}
        relevant = sorted(n for n in names if n.startswith(("rocm_", "omnistat_", "rmsjob_")))
        hosts = sorted({s["metric"]["instance"] for s in self._series if s["metric"].get("instance")})

        rmsjob = [
            {"metric": s["metric"], "values": self._to_tsdb_values(s["timestamps"], s["values"])}
            for s in self._iter_metric("rmsjob_info")
        ]
        jobs = build_jobs_summary(rmsjob)

        return {
            "num_jobs": len(jobs),
            "jobs": jobs,
            "num_hosts": len(hosts),
            "hosts": hosts,
            "num_metrics": len(relevant),
            "metrics": relevant,
        }
