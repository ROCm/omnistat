# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2023 - 2026 Advanced Micro Devices, Inc. All Rights Reserved.
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

"""External script metric collector

Forks a user-defined script at each polling interval and parses its stdout to
populate Prometheus gauge metrics.  This allows site-specific metrics to be
injected into Omnistat without modifying core collector code.

Script output format (one metric per line):

    <metric_name> <float_value>
    <metric_name>{<label_key>="<label_value>"[,...]} <float_value>

Lines that are empty or begin with '#' are ignored.

Example script output:

    # site custom metrics
    site_job_queue_depth 42
    site_scratch_free_bytes{fs="/scratch"} 1.23e+12

Runtime configuration (omnistat.collectors.external section):

    script = /path/to/my/script.sh
    timeout_secs = 10

The script path is required; timeout_secs defaults to 10 seconds.  Metric names are
used verbatim; an "omnistat_external" label is added to each metric for identification.
"""

import configparser
import logging
import re
import shlex
import subprocess
import sys

from prometheus_client import Gauge

from omnistat.collector_base import Collector

# Matches:  name{k="v",...}  value   OR   name  value
_LINE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)(?P<labels>\{[^}]*\})?\s+(?P<value>[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*$"
)
# Matches individual label pairs inside {…}
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"')


class ExternalScript(Collector):
    def __init__(self, config: configparser.ConfigParser):
        logging.debug(f"Initializing {self.__class__.__name__} data collector")

        self.__prefix = ""
        self.__metrics = {}
        self.__metric_instances = {}  # key -> list of label-value tuples (in label_names order)
        self.__script = None
        self.__timeout = 10

        if not config.has_section("omnistat.collectors.external"):
            logging.error("[ERROR] ExternalScript collector requires an [omnistat.collectors.external] config section")
            sys.exit(1)

        section = config["omnistat.collectors.external"]

        if not section.get("script"):
            logging.error("[ERROR] ExternalScript collector requires 'script' option in [omnistat.collectors.external]")
            sys.exit(1)

        self.__script = section.get("script").strip()
        self.__timeout = section.getint("timeout_secs", 10)

    def registerMetrics(self):
        logging.info(f"script: {self.__script}")
        logging.info(f"timeout: {self.__timeout} sec")
        # Metrics are registered dynamically on first updateMetrics() call
        # because we don't know the metric names until the script runs.
        self._update(register=True)

    def updateMetrics(self):
        self._update(register=False)

    # ------------------------------------------------------------------
    # Internal helpers

    def _run_script(self):
        """Fork the configured script and return its stdout lines.

        Returns:
            list[str]: Lines of output, or empty list on error.
        """
        try:
            result = subprocess.run(
                shlex.split(self.__script),
                capture_output=True,
                text=True,
                timeout=self.__timeout,
            )
        except subprocess.TimeoutExpired:
            logging.warning(f"ExternalScript: script timed out after {self.__timeout}s: {self.__script}")
            return []
        except Exception as e:
            logging.warning(f"ExternalScript: failed to run script: {e}")
            return []

        if result.returncode != 0:
            logging.warning(f"ExternalScript: script exited with code {result.returncode}: {result.stderr.strip()}")

        return result.stdout.splitlines()

    def _parse_line(self, line):
        """Parse one line of script output.

        Returns:
            tuple: (metric_name, label_dict, float_value) or None if unparseable.
        """
        line = line.strip()
        if not line or line.startswith("#"):
            return None

        m = _LINE_RE.match(line)
        if not m:
            logging.warning(f"ExternalScript: skipping unparseable line: {line!r}")
            return None

        name = m.group("name")
        value = float(m.group("value"))

        labels = {}
        if m.group("labels"):
            for lm in _LABEL_RE.finditer(m.group("labels")):
                labels[lm.group(1)] = lm.group(2)

        return name, labels, value

    def _update(self, register: bool):
        """Run the script and update metrics, registering any new ones on the fly.

        At the start of each non-registration update, all previously set label
        combinations are removed from their gauges before the script output is
        applied.  If the script produces no output (e.g. the underlying command
        segfaults or the device is unavailable), no values are re-added and the
        metrics disappear from the /metrics endpoint.  VictoriaMetrics / Prometheus
        then marks them stale rather than holding the last known value.

        Args:
            register (bool): True on first call (logs at info level); False on subsequent calls (logs at debug level).
        """
        lines = self._run_script()

        # Clear all previously emitted label combinations so that metrics from a
        # failed or empty script run don't linger as stale values.
        if not register:
            for key, instances in self.__metric_instances.items():
                gauge = self.__metrics.get(key)
                if gauge is None:
                    continue
                for label_values in instances:
                    try:
                        if label_values:
                            gauge.remove(*label_values)
                        else:
                            gauge.set(float("nan"))
                    except KeyError:
                        pass
            self.__metric_instances.clear()

        for line in lines:
            parsed = self._parse_line(line)
            if parsed is None:
                continue

            name, labels, value = parsed
            labels["omnistat_external"] = "1"
            label_names = sorted(labels.keys())
            key = (name, tuple(label_names))

            if key not in self.__metrics:
                full_name = self.__prefix + name
                if label_names:
                    self.__metrics[key] = Gauge(full_name, f"External metric: {name}", labelnames=label_names)
                else:
                    self.__metrics[key] = Gauge(full_name, f"External metric: {name}")
                label_str = "{" + ",".join(f'{k}="{labels[k]}"' for k in label_names) + "}"
                if register:
                    logging.info(f"--> [registered] {full_name}{label_str} (gauge)")
                else:
                    logging.debug(f"--> [registered late] {full_name}{label_str} (gauge)")

            gauge = self.__metrics[key]
            label_values = tuple(labels[k] for k in label_names)
            if label_names:
                gauge.labels(**labels).set(value)
            else:
                gauge.set(value)

            self.__metric_instances.setdefault(key, []).append(label_values)
