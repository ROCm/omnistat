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
import time

import pytest
from prometheus_api_client import PrometheusConnect

from test import config


class TestJobSystem:
    @pytest.mark.parametrize("node", config.nodes)
    def test_job(self, node):
        prometheus = PrometheusConnect(url=config.prometheus_url)
        last_jobid, _ = self.query_last_job(prometheus)

        job_seconds = 2
        self.run_job([node], job_seconds)
        time.sleep(job_seconds + 1)

        jobid, series = self.query_last_job(prometheus)
        assert jobid == last_jobid + 1, "One job should have been executed"
        assert len(series) == 1, "Expected a different number of series"

        num_samples = len(series[0]["values"])
        assert (
            num_samples == job_seconds or num_samples == job_seconds + 1
        ), "Expected approximately one sample per second"

    def test_job_multinode(self):
        prometheus = PrometheusConnect(url=config.prometheus_url)
        last_jobid, _ = self.query_last_job(prometheus)

        job_seconds = 2
        self.run_job(config.nodes, job_seconds)
        time.sleep(job_seconds + 1)

        jobid, series = self.query_last_job(prometheus)
        assert jobid == last_jobid + 1, "One job should have been executed"
        assert len(series) == len(config.nodes), "Expected a different number of series"

        for instance in series:
            num_samples = len(instance["values"])
            assert (
                num_samples == job_seconds or num_samples == job_seconds + 1
            ), "Expected approximately one sample per second"

    # Get ID and metrics for the most recently executed job
    def query_last_job(self, prometheus):
        jobid = 0
        series = None

        query = f"rmsjob_info{{jobid=~'.+'}}[{config.time_range}]"
        results = prometheus.custom_query(query)
        if len(results) != 0:
            last_job_metric = max(results, key=lambda x: int(x["metric"]["jobid"]))
            jobid = int(last_job_metric["metric"]["jobid"])
            query = f"rmsjob_info{{jobid='{jobid}'}}[{config.time_range}]"
            series = prometheus.custom_query(query)

        return (jobid, series)

    # Execute an empty job lasting a given amount of seconds
    def run_job(self, nodes, seconds):
        nodelist = ",".join(nodes)
        sbatch_cmd = f'sbatch --nodes={len(nodes)} --nodelist={nodelist} --wrap="srun sleep {seconds}"'
        exec_cmd = f"docker exec slurm-controller bash -c 'cd /jobs; {sbatch_cmd}'"
        os.system(exec_cmd)
