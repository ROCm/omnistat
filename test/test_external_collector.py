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

import os
import subprocess
import tempfile

import pytest

from test.test_collectors import OmnistatTestServer

EXTERNAL_COLLECTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "external_collector.sh")


class ExternalTestServer(OmnistatTestServer):
    """Omnistat server with only the external collector enabled.

    The server runs the stateful helper script that emits different metrics
    on each invocation, driven by a counter in a state file.
    """

    def __init__(self, statefile):
        self.statefile = statefile

        config_sections = {
            "omnistat.collectors.external": {
                "script": f"{EXTERNAL_COLLECTOR} {statefile}",
                "timeout_secs": "5",
            },
        }
        super().__init__(["external"], config_sections=config_sections)

        # Reset counter after server startup so that scrapes from
        # wait_for_server() don't consume test runs.
        subprocess.run([EXTERNAL_COLLECTOR, "-init", statefile], check=True)

    def get(self):
        """Scrape /metrics and return {name: {frozenset(labels): value}}."""
        results = {}
        for family in super().get():
            for sample in family.samples:
                key = frozenset(sample.labels.items())
                results.setdefault(sample.name, {})[key] = sample.value
        return results


@pytest.fixture(scope="class")
def external_server(request):
    """Start a single ExternalTestServer and perform 3 sequential scrapes."""
    fd, statefile = tempfile.mkstemp(suffix=".state")
    os.close(fd)
    server = ExternalTestServer(statefile)

    # Perform all scrapes up front so tests can reference them by index
    request.cls.scrapes = [server.get(), server.get(), server.get()]

    yield server

    server.stop()
    if os.path.exists(statefile):
        os.unlink(statefile)


@pytest.mark.usefixtures("external_server")
class TestExternalCollector:
    def test_single_metric(self):
        """Scrape 1: verify single metric with known value and label."""
        metrics = self.scrapes[0]
        assert "my_snazzy_metric" in metrics, f"Missing my_snazzy_metric, got: {list(metrics.keys())}"
        for label_set, value in metrics["my_snazzy_metric"].items():
            labels = dict(label_set)
            assert labels.get("omnistat_external") == "1", f"Missing omnistat_external label: {labels}"
            assert labels.get("my_snazzy_label") == "omnistat_for_the_win"
            assert value == 42.0

    def test_metric_change(self):
        """Scrape 2: verify new metric replaces previous one."""
        metrics = self.scrapes[1]
        assert "my_snazzy_metric2" in metrics, f"Missing my_snazzy_metric2, got: {list(metrics.keys())}"
        assert "my_snazzy_metric" not in metrics, "my_snazzy_metric should have been removed"
        for label_set, value in metrics["my_snazzy_metric2"].items():
            labels = dict(label_set)
            assert labels.get("omnistat_external") == "1"
            assert labels.get("my_snazzy_label2") == "rocks"
            assert value == 43.0

    def test_multiple_metrics(self):
        """Scrape 3: verify two new metrics replace previous one."""
        metrics = self.scrapes[2]
        assert "my_snazzy_metric2" not in metrics, "my_snazzy_metric2 should have been removed"

        assert "my_snazzy_metric3" in metrics, f"Missing my_snazzy_metric3, got: {list(metrics.keys())}"
        for label_set, value in metrics["my_snazzy_metric3"].items():
            labels = dict(label_set)
            assert labels.get("omnistat_external") == "1"
            assert labels.get("my_snazzy_label3") == "ftw"
            assert value == 44.0

        assert "my_snazzy_metric3b" in metrics, f"Missing my_snazzy_metric3b, got: {list(metrics.keys())}"
        for label_set, value in metrics["my_snazzy_metric3b"].items():
            labels = dict(label_set)
            assert labels.get("omnistat_external") == "1"
            assert labels.get("my_snazzy_label3b") == "bonus"
            assert value == 45.0
