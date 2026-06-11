"""Info module: identity, topology, and descriptive job metadata."""

from __future__ import annotations

from omnistat.inspect import compute
from omnistat.inspect.job.core import Module


class Info(Module):
    name = "info"

    def build(self) -> dict:
        ds = self.ds
        # A rehydrated context carries no descriptive metadata; fetch it lazily.
        ds.ensure_metadata()
        num_nodes = len(ds.hosts)
        num_gpus = (ds.gpus_per_node or 0) * num_nodes if ds.gpus_per_node else None
        return {
            "jobid": ds.jobid,
            "user": ds.user,
            "partition": ds.partition,
            "start_time": ds.start_time.isoformat() if ds.start_time else None,
            "end_time": ds.end_time.isoformat() if ds.end_time else None,
            "duration_seconds": round(ds.job_duration, 2),
            "duration_human": compute.human_duration(ds.job_duration),
            "num_nodes": num_nodes,
            "num_gpus": num_gpus,
            "hosts": ds.hosts,
            "gpu_arch": ds.gpu_arch,
            "gpu_type": ds.gpu_type,
            "vbios_version": ds.vbios_version,
            "omnistat_version": ds.omnistat_version,
            "sampling_interval": ds.sampling_interval,
            "annotations": self._annotations(),
            "figure_of_merit": self._fom(),
        }

    def _annotations(self) -> list[str]:
        results = self._try_query("rmsjob_annotations", self.ds.coarse_step())
        seen: set[str] = set()
        out: list[str] = []
        for r in results:
            m = r.get("metric", {})
            text = m.get("marker") or m.get("annotation") or ""
            if text and text not in seen:
                seen.add(text)
                out.append(text)
        return out

    def _fom(self) -> list[dict] | None:
        results = self._try_query("omnistat_fom", self.ds.coarse_step())
        entries: list[dict] = []
        for m, values in self._iter_series(results):
            entries.append(
                {
                    "name": m.get("name", "unknown"),
                    "instance": m.get("instance", "unknown"),
                    "min": round(min(values), 4),
                    "max": round(max(values), 4),
                    "last": round(values[-1], 4),
                    "num_points": len(values),
                }
            )
        return entries or None
