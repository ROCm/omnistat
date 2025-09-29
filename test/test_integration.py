# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2023 - 2025 Advanced Micro Devices, Inc. All Rights Reserved.
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

import pytest
import requests
from prometheus_api_client import PrometheusConnect

from test import config


class TestIntegration:
    def test_request(self):
        response = requests.get(config.prometheus_url)
        assert response.status_code == 200, "Unable to connect to Prometheus"

    @pytest.mark.parametrize("node", config.nodes)
    def test_query_up(self, node):
        prometheus = PrometheusConnect(url=config.prometheus_url)
        results = prometheus.custom_query("up")
        assert len(results) >= 1, "Metric up not available"

        instance = f"{node}:{config.port}"
        results = prometheus.custom_query(f"up{{instance='{instance}'}}")
        _, value = results[0]["value"]
        assert int(value) == 1, "Node exporter not running"

    def test_query_slurm(self):
        prometheus = PrometheusConnect(url=config.prometheus_url)
        results = prometheus.custom_query("rmsjob_info")
        assert len(results) >= 1, "Metric rmsjob_info not available"

    @pytest.mark.skipif(not config.rocm_host, reason="requires ROCm")
    @pytest.mark.parametrize("node", config.nodes)
    def test_query_rocm(self, node):
        prometheus = PrometheusConnect(url=config.prometheus_url)
        instance = f"{node}:{config.port}"
        query = f"rocm_average_socket_power_watts{{instance='{instance}'}}"
        results = prometheus.custom_query(query)
        assert len(results) >= 1, "Metric rocm_average_socket_power_watts not available"

        results = prometheus.custom_query(query)
        _, value = results[0]["value"]
        assert int(value) >= 0, "Reported power is too low"
