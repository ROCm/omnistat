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

"""Report module: the one-shot report card, composed from the report modules.

``Report`` is a thin wrapper that runs :class:`~omnistat.inspect.job.info.Info`,
:class:`~omnistat.inspect.job.stats.Stats`, and
:class:`~omnistat.inspect.job.health.Health` and nests each under its own key. It
carries the stats knobs (``cv_threshold``/``verbose``) and forwards them to the
stats module; the surrounding envelope (jobid, data_source, query stats) is the
CLI's responsibility. The nested report-card keys are ``overview``/``stats``/
``health``, matching the ``info``/``stats``/``health`` subcommand names.
"""

from __future__ import annotations

from omnistat.inspect.constants import DEFAULT_CV_THRESHOLD
from omnistat.inspect.job.core import Module
from omnistat.inspect.job.health import Health
from omnistat.inspect.job.info import Info
from omnistat.inspect.job.stats import Stats


class Report(Module):
    name = "report"
    param_defaults = {"cv_threshold": DEFAULT_CV_THRESHOLD, "verbose": False}

    def build(self) -> dict:
        return {
            "overview": Info(self.ds, self._store).get(),
            "stats": Stats(self.ds, self._store, cv_threshold=self.p.cv_threshold, verbose=self.p.verbose).get(),
            "health": Health(self.ds, self._store).get(),
        }
