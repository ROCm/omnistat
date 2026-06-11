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
