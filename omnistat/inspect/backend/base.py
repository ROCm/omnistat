"""DataSource abstraction + query ledger for omnistat-inspect backends.

A ``DataSource`` hides the difference between a Prometheus-compatible TSDB and a
directory of CSV exports. The orchestrator (``Job``) only ever sees this
interface — it never builds PromQL itself.

The ``iterations`` analysis command relies on two extras here: :meth:`job_query`
accepts optional ``aggregate`` / ``start`` / ``end`` parameters, and
:meth:`iteration_auto_step` exposes the backend-aware step used for sub-window
iteration queries.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from omnistat.inspect import compute
from omnistat.inspect.helpers import auto_step as _auto_step_helper

# ---------------------------------------------------------------------------
# Query ledger
# ---------------------------------------------------------------------------


@dataclass
class QueryRecord:
    promql: str
    step: str
    time_seconds: float
    datapoints: int


@dataclass
class QueryLedger:
    queries: list[QueryRecord] = field(default_factory=list)
    _t_start: float = field(default_factory=time.monotonic)

    def record(self, promql: str, step: str, elapsed: float, datapoints: int) -> None:
        self.queries.append(QueryRecord(promql, step, round(elapsed, 4), datapoints))

    def total_queries(self) -> int:
        return len(self.queries)

    def total_query_seconds(self) -> float:
        return round(sum(q.time_seconds for q in self.queries), 4)

    def elapsed_seconds(self) -> float:
        return round(time.monotonic() - self._t_start, 4)

    def summary(self) -> dict:
        return {
            "total_queries": self.total_queries(),
            "total_query_time_seconds": self.total_query_seconds(),
            "elapsed_seconds": self.elapsed_seconds(),
        }


# ---------------------------------------------------------------------------
# DataSource ABC
# ---------------------------------------------------------------------------


class DataSource(ABC):
    """Common state and helpers shared by concrete backends."""

    # Whether a cached ``JobContext`` lets this backend skip expensive job
    # discovery. Only ``True`` for backends with a costly discovery scan
    # (TSDB's day-by-day ``rmsjob_info`` walk).
    supports_context_cache: bool = False

    # Backend kind, used for the iteration step heuristic.
    backend_kind: str = "tsdb"

    jobid: str | None
    start_time: datetime | None
    end_time: datetime | None
    sampling_interval: float | None
    hosts: list[str]
    gpu_types: set[str]
    vbios_versions: set[str]
    driver_versions: set[str]
    omnistat_version: str | None
    user: str | None
    partition: str | None
    nodes_label: str | None
    gpus_per_node: int | None

    def __init__(self, sampling_interval: float | None = None) -> None:
        self.ledger = QueryLedger()
        self.jobid = None
        self.start_time = None
        self.end_time = None
        self.sampling_interval = sampling_interval
        self.hosts = []
        self.gpu_types: set[str] = set()
        self.vbios_versions: set[str] = set()
        self.driver_versions: set[str] = set()
        self.omnistat_version = None
        self.user = None
        self.partition = None
        self.nodes_label = None
        self.gpus_per_node = None

    # -- Properties ---------------------------------------------------------

    @property
    def job_duration(self) -> float:
        if self.start_time is None or self.end_time is None:
            raise RuntimeError("job_duration requires discover_job() to have succeeded first")
        return (self.end_time - self.start_time).total_seconds()

    def padded_range(self, seconds: int = 60) -> tuple[datetime, datetime]:
        if self.start_time is None or self.end_time is None:
            raise RuntimeError("padded_range requires discover_job() to have succeeded first")
        pad = timedelta(seconds=seconds)
        return self.start_time - pad, self.end_time + pad

    # -- Step selection -----------------------------------------------------

    def auto_step(self) -> float:
        """Finest safe query step (report-section convention)."""
        si = self.sampling_interval or 1.0
        return max(si, 1.0)

    def coarse_step(self) -> float:
        duration = self.job_duration
        if duration > 3600:
            return 3600.0
        if duration > 600:
            return 300.0
        return 60.0

    def iteration_auto_step(self) -> float:
        """Backend-aware finest step for iteration detection (inspect convention)."""
        return _auto_step_helper(self.job_duration, self.sampling_interval, backend=self.backend_kind)

    # -- Query stats --------------------------------------------------------

    def get_query_stats(self) -> dict:
        return self.ledger.summary()

    # -- Context (serializable discovery snapshot) --------------------------

    def to_context(self):
        """Capture the minimal discovery snapshot needed to re-query the job."""
        from omnistat.inspect.job.context import JobContext

        assert (
            self.jobid is not None and self.start_time is not None and self.end_time is not None
        ), "to_context() requires a discovered job"
        return JobContext(
            jobid=self.jobid,
            start_time=self.start_time,
            end_time=self.end_time,
            sampling_interval=self.sampling_interval,
        )

    def bind_context(self, ctx) -> None:
        """Rehydrate identity/query state from a ``JobContext`` (no rediscovery).

        Only identity and time range are restored; descriptive metadata is
        re-fetched on demand via :meth:`ensure_metadata`.
        """
        self.jobid = ctx.jobid
        self.start_time = ctx.start_time
        self.end_time = ctx.end_time
        self.sampling_interval = ctx.sampling_interval

    def ensure_metadata(self) -> None:
        """Populate descriptive metadata if missing (idempotent, may query)."""
        return

    def source_id(self) -> str:
        """Stable identifier for the underlying data source."""
        info = self.get_db_info()
        kind = info.get("type", "unknown")
        loc = info.get("url") or info.get("dir") or ""
        return f"{kind}:{loc}"

    # -- Counter increase (reset-aware totals) ------------------------------

    def counter_increase(
        self,
        metric: str,
        key_labels: tuple[str, ...],
        literal_filters: dict[str, str] | None = None,
        regex_filters: dict[str, str] | None = None,
    ) -> dict[tuple, tuple[float, float, bool]]:
        """Per-key ``(delta, observed_span_seconds, monotonic)`` for a counter.

        Default (client-side) implementation: fetch the full per-key series at
        :meth:`auto_step` and run despike + reset-aware
        :func:`compute.per_key_increase`. This is what CSV uses unchanged and is
        also the TSDB per-key fallback. Backends with a server-side reset-aware
        ``increase()`` (TSDB) override this with a cheaper fast path that is
        bit-for-bit equivalent on monotonic series.
        """
        results = self.job_query(
            metric, self.auto_step(), literal_filters=literal_filters, regex_filters=regex_filters
        )
        return compute.per_key_increase(results, key_labels)

    # -- Abstract API -------------------------------------------------------

    @abstractmethod
    def discover_job(self, jobid: str) -> bool: ...

    @abstractmethod
    def _refine_range(self, interval: float) -> None: ...

    @abstractmethod
    def get_db_info(self) -> dict[str, str]: ...

    @abstractmethod
    def db_summary(self) -> dict[str, object]: ...

    @abstractmethod
    def job_query(
        self,
        metric: str,
        step: float,
        literal_filters: dict[str, str] | None = None,
        regex_filters: dict[str, str] | None = None,
        join: bool = True,
        aggregate: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[dict]: ...

    @abstractmethod
    def label_values(
        self,
        label: str,
        metric: str | None = None,
        match_filters: dict[str, str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[str]: ...

    @abstractmethod
    def agg_by_label(
        self,
        metric: str,
        label: str,
        step: float,
        literal_filters: dict[str, str] | None = None,
        regex_filters: dict[str, str] | None = None,
    ) -> dict[str, float]: ...

    @abstractmethod
    def get_label_for_series(self, metric: str) -> str: ...
