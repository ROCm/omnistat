"""Shared core for the inspect job package.

Holds the :class:`Job` lifecycle wrapper, the generalized :class:`Module` base
(safe-query helpers + transparent :class:`~omnistat.inspect.cache.JsonStore`
caching), the completeness gate (:func:`is_complete`), and the
:func:`load_context` / :func:`save_context` adapters that round-trip the
:class:`~omnistat.inspect.job.context.JobContext` through the store.

``Job`` pairs a :class:`DataSource` with an optional store and handles only
discovery/rehydration and the completeness gate; the report and analysis units
are :class:`Module` subclasses built directly from ``job.ds``/``job._store``.

``Module`` is the renamed/generalized ``Section`` from the report backend: it
spans both reporting units (overview/metrics/health) and analysis units
(iterations). Each subclass sets ``name``, declares its cacheable knobs via
``param_defaults`` (name → default), and overrides :meth:`build` (returns a
plain dict). The declared knobs are exposed on ``self.p`` and form the cache
key, so there is no per-module config class or ``_params`` override.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from omnistat.inspect import compute
from omnistat.inspect.backend.base import DataSource
from omnistat.inspect.cache import JsonStore
from omnistat.inspect.constants import DROP_ZERO_SERIES_METRICS
from omnistat.inspect.job.context import JobContext

logger = logging.getLogger(__name__)


def load_context(store: JsonStore, jobid: str, source_id: str) -> JobContext | None:
    """Read the cached discovery snapshot for ``jobid``, or ``None``."""
    payload = store.get(jobid, "context", source_id)
    if payload is None:
        return None
    try:
        return JobContext.from_dict(payload)
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("Failed to decode cached context for %s: %s", jobid, exc)
        return None


def save_context(store: JsonStore, ctx: JobContext, source_id: str) -> None:
    """Persist a discovery snapshot under kind ``"context"``."""
    store.put(ctx.jobid, "context", source_id, ctx.to_dict())


def is_complete(ds) -> bool:
    """Whether the job has finished (and is therefore safe to cache)."""
    end = ds.end_time
    if end is None:
        return False
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    interval = float(ds.sampling_interval or 1.0)
    margin = max(interval * 3.0, 30.0)
    return (datetime.now(timezone.utc) - end).total_seconds() > margin


class Job:
    """A discovered job: a data source paired with an optional cache store.

    Use the classmethods to obtain one: :meth:`discover` (fresh scan),
    :meth:`from_context` (rehydrate from a cached snapshot, no rescan), or
    :meth:`open` (cache-aware load-or-discover). The constructor itself does
    not touch the data source and assumes ``ds`` already identifies a job.
    """

    def __init__(self, ds: DataSource, *, store: JsonStore | None = None) -> None:
        self.ds = ds
        self._store = store

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def discover(cls, ds: DataSource, jobid: str, **kw) -> "Job | None":
        """Run a fresh discovery scan and wrap the result; ``None`` if not found."""
        if not ds.discover_job(jobid):
            return None
        return cls(ds, **kw)

    @classmethod
    def from_context(cls, ds: DataSource, ctx: JobContext, **kw) -> "Job":
        """Rehydrate from a cached snapshot, skipping the discovery scan."""
        ds.bind_context(ctx)
        return cls(ds, **kw)

    @classmethod
    def open(cls, ds: DataSource, jobid: str, *, store=None, refresh: bool = False, **kw) -> "Job | None":
        """Cache-aware load-or-discover.

        When the source benefits from context caching (TSDB) and a store is
        given, a validated cache hit rehydrates without rescanning. Completed
        jobs are persisted after a fresh discover so the next call is cheap.
        """
        use_store = store is not None and ds.supports_context_cache
        source_id = ds.source_id()
        if use_store and not refresh:
            ctx = load_context(store, jobid, source_id)
            if ctx is not None:
                return cls.from_context(ds, ctx, store=store, **kw)
        job = cls.discover(ds, jobid, store=store, **kw)
        if job is not None and use_store and job.is_complete():
            save_context(store, job.context, source_id)
        return job

    # ------------------------------------------------------------------
    # Context / lifecycle
    # ------------------------------------------------------------------

    @property
    def context(self) -> JobContext:
        return self.ds.to_context()

    def is_complete(self) -> bool:
        """Whether the job has finished (and is therefore safe to cache)."""
        return is_complete(self.ds)


class Module:
    """Base for a self-serializing job module (report section or analysis unit).

    A module takes ``(ds, store, **knobs)`` and exposes one :meth:`get` that
    returns its plain dict, caching itself transparently (kind = ``name``) when
    a store is given and the job is complete. Subclasses set ``name`` and
    declare their cacheable knobs in ``param_defaults`` (name → default); the
    resolved values are exposed on ``self.p`` and become the cache key.
    """

    name: str = ""
    # Cacheable knobs (name → default). Resolved onto ``self.p`` and used as the
    # cache key; an empty mapping means the module takes no parameters.
    param_defaults: dict[str, Any] = {}

    def __init__(self, ds, store: JsonStore | None = None, **overrides) -> None:
        unknown = set(overrides) - set(self.param_defaults)
        if unknown:
            raise TypeError(f"{type(self).__name__} got unexpected params: {sorted(unknown)}")
        self.ds = ds
        self._store = store
        self.p = SimpleNamespace(**{**self.param_defaults, **overrides})

    # ------------------------------------------------------------------
    # Transparent caching
    # ------------------------------------------------------------------

    def get(self) -> Any:
        if self._store is None or not is_complete(self.ds):
            return self.build()
        key = (self.ds.jobid, self.name, self.ds.source_id())
        params = self._params()
        hit = self._store.get(*key, params=params)
        if hit is not None:
            return hit
        data = self.build()
        self._store.put(*key, data, params)
        return data

    def _params(self) -> dict[str, Any]:
        return {k: getattr(self.p, k) for k in self.param_defaults}

    def build(self) -> Any:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Safe-query helpers
    # ------------------------------------------------------------------

    def _try_query(self, metric, step, *, filters=None, join=True):
        """job_query that returns [] (and logs) instead of raising."""
        try:
            return self.ds.job_query(metric, step, filters=filters, join=join)
        except Exception as exc:
            logger.warning("%s query failed: %s", metric, exc)
            return []

    def _try_label_values(self, label, *, metric=None):
        """label_values that returns [] (and logs) instead of raising."""
        try:
            return self.ds.label_values(label, metric=metric)
        except Exception as exc:
            logger.warning("%s discovery failed: %s", label, exc)
            return []

    @staticmethod
    def _iter_series(results):
        """Yield (labels, values) per series, skipping series with no numeric values."""
        for r in results:
            values = compute.extract_values(r)
            if not values:
                continue
            yield r.get("metric", {}), values

    def _fetch_series(self, name, step):
        """job_query + drop-zero filter for DROP_ZERO_SERIES_METRICS."""
        results = self.ds.job_query(name, step)
        if name in DROP_ZERO_SERIES_METRICS:
            results = compute.filter_zero_series(results)
        return results
