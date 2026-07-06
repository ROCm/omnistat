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
            "gpu_type": compute.collapse(ds.gpu_types),
            "driver_version": compute.collapse(ds.driver_versions),
            "vbios_version": compute.collapse(ds.vbios_versions),
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
