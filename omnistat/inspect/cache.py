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

"""Cross-invocation cache backed by plain-dict JSON files.

:class:`JsonStore` writes one ``{jobid}.{kind}.json`` file per ``(jobid, kind)``
under a directory. Each file holds ``{source_id, params, data}`` encoded with
stdlib :mod:`json`. A read honours a cached entry only when ``source_id`` and
``params`` both match, so the same jobid in different sources / params never
collides and a bumped knob transparently triggers recomputation.

A single store serves every cached kind under one ``--cache-dir``:

- ``"context"`` — the discovery snapshot (:class:`~omnistat.inspect.job.context.JobContext`).
- ``"info"`` / ``"stats"`` / ``"health"`` / ``"iterations"`` — module outputs.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class JsonStore:
    """One JSON file per ``(jobid, kind)`` under ``directory``."""

    directory: str

    def _path(self, jobid: str, kind: str) -> str:
        return os.path.join(self.directory, f"{jobid}.{kind}.json")

    def _read(self, path: str) -> dict | None:
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                return json.load(f)
        except (OSError, ValueError) as exc:
            logger.warning("Failed to read cache %s: %s", path, exc)
            return None

    def _write(self, path: str, payload: Any) -> None:
        try:
            os.makedirs(self.directory, exist_ok=True)
            with open(path, "w") as f:
                json.dump(payload, f, indent=2)
        except OSError as exc:
            logger.warning("Failed to write cache %s: %s", path, exc)

    def get(self, jobid: str, kind: str, source_id: str, params=None) -> dict | None:
        """Return the cached ``data`` dict, or ``None`` on miss."""
        payload = self._read(self._path(jobid, kind))
        if payload is None:
            return None
        if payload.get("source_id") != source_id:
            return None
        if payload.get("params") != params:
            return None
        return payload.get("data")

    def put(self, jobid: str, kind: str, source_id: str, data: Any, params=None) -> None:
        self._write(
            self._path(jobid, kind),
            {
                "source_id": source_id,
                "params": params,
                "data": data,
            },
        )
