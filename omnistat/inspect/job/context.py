"""Serializable job context for omnistat-inspect.

The inspect modules build and return plain ``dict`` / ``list[dict]`` structures
that stdlib :mod:`json` encodes directly, so no boundary structs are needed. The
one value that does not survive a JSON round-trip unaided is :class:`JobContext`,
whose ``start_time`` / ``end_time`` are :class:`~datetime.datetime` objects;
:meth:`JobContext.to_dict` / :meth:`JobContext.from_dict` localize that
ISO-8601 conversion here so the cache store stays generic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# ---------------------------------------------------------------------------
# Job context (serializable discovery snapshot)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobContext:
    """Minimal serializable discovery snapshot for a single job.

    Carries only what's needed to re-query the job: identity and time range.
    Descriptive metadata (user, node/GPU counts, GPU arch, ...) is *not* cached
    here — it lives in the overview module, which re-fetches it lazily via
    ``DataSource.ensure_metadata`` when a job is rehydrated from a context.

    :meth:`to_dict` / :meth:`from_dict` convert the two datetimes to/from
    ISO-8601 strings so the snapshot can cross the stdlib-JSON cache boundary.
    """

    jobid: str
    start_time: datetime
    end_time: datetime
    sampling_interval: float | None

    def to_dict(self) -> dict:
        return {
            "jobid": self.jobid,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "sampling_interval": self.sampling_interval,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "JobContext":
        return cls(
            jobid=data["jobid"],
            start_time=datetime.fromisoformat(data["start_time"]),
            end_time=datetime.fromisoformat(data["end_time"]),
            sampling_interval=data["sampling_interval"],
        )
