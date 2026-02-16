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

# User-level Omnistat job execution tests
#
# Job submissions with user-level Omnistat are created by generating
# temporary job files in the host, and then executing them in the containerized
# environment. Communication between the local test environment in the host and
# the containerized SLURM environment happens primarily in three ways:
#
# 1. Temporary job files are located under the local working copy of the
#    Omnistat repository, which is exposed to the containers as `/host-source`.
#
# 2. Commands to manage the execution of jobs, like `sbatch` and `squeue`, are
#    executed in the controller container with `docker exec`.
#
# 3. Once a job has been executed, Prometheus data is temporarily exposed to
#    the host testing environment using the controller container as a Prometheus
#    server, allowing further validation of the trace.

import os
import re
import subprocess
import time
from string import Template

import pytest
from prometheus_api_client import PrometheusConnect

from test import config

slurm_job_template = """\
#!/bin/bash
#SBATCH --job-name=test-usermode
#SBATCH --partition=default-partition
#SBATCH --nodes=$num_nodes
#SBATCH --nodelist=$node_list

export OMNISTAT_CONFIG=/etc/omnistat-user.config
export OMNISTAT_DIR=/source

# If source directory is not present, switch to package execution.
if [[ ! -d $OMNISTAT_DIR ]]; then
    export OMNISTAT_DIR=/opt/omnistat/bin
fi

. /opt/omnistat/bin/activate
cd /jobs

$OMNISTAT_DIR/omnistat-usermode --start --interval 1 --no-push
$cmd
$OMNISTAT_DIR/omnistat-usermode --stop --no-push
"""


class TestJobUser:
    job_file = "slurm-job-user.sh"

    def test_job_multinode(self):
        job_seconds = 2
        jobid = self.run_job(config.nodes, job_seconds)

        self.start_prometheus_proxy()

        prometheus = PrometheusConnect(url=config.prometheus_url)
        results = prometheus.custom_query("rmsjob_info")
        assert len(results) >= 1, "Metric rmsjob_info not available"

        query = f"rmsjob_info{{jobid='{jobid}'}}[{config.time_range}]"
        results = prometheus.custom_query(query)
        assert len(results) == len(config.nodes), "Expected a different number of series"

        for result in results:
            num_samples = len(result["values"])
            assert num_samples >= job_seconds, "Expected at least one sample per second"

        self.stop_prometheus_proxy()

    def generate_job_file(self, nodes, cmd):
        num_nodes = len(nodes)
        node_list = ",".join(nodes)

        substitutions = {
            "num_nodes": num_nodes,
            "node_list": node_list,
            "cmd": cmd,
        }

        template = Template(slurm_job_template)
        job = template.safe_substitute(substitutions)

        with open(self.job_file, "w") as f:
            f.write(job)

    def remove_job_file(self):
        os.remove(self.job_file)

    # Launch a Prometheus server to read user-level data using the controller.
    def start_prometheus_proxy(self, data_path=config.prometheus_data_user):
        self.stop_prometheus_proxy(ignore_errors=True)

        start_cmd = ["docker", "exec", "-d", "slurm-controller", "prometheus", f"--storage.tsdb.path={data_path}"]
        p = subprocess.run(start_cmd)
        assert p.returncode == 0

    def stop_prometheus_proxy(self, ignore_errors=False):
        stop_cmd = ["docker", "exec", "slurm-controller", "pkill", "prometheus"]
        p = subprocess.run(stop_cmd)
        if not ignore_errors:
            assert p.returncode == 0

    def run_job(self, nodes, seconds):
        self.generate_job_file(nodes, f"srun sleep {seconds}")

        base_exec = ["docker", "exec", "slurm-controller"]
        bash = base_exec + ["bash", "-c"]
        timeout = base_exec + ["timeout", "1m", "bash", "-c"]

        run_cmd = f"cd /jobs && sbatch --parsable /host-source/test/{self.job_file}"
        p = subprocess.run(bash + [run_cmd], capture_output=True, text=True)
        assert p.returncode == 0
        jobid = p.stdout.strip()

        wait_cmd = "until [[ $(squeue -h | wc -l) == 0 ]]; do echo 'Wait...'; sleep 1; done"
        p = subprocess.run(timeout + [wait_cmd], capture_output=True, text=True)
        assert p.returncode == 0

        cat_cmd = f"cat /jobs/slurm-{jobid}.out"
        p = subprocess.run(bash + [cat_cmd], capture_output=True, text=True)
        assert p.returncode == 0

        # Make sure executions seem successful by looking for certain
        # Omnistat-generated lines in the log file.
        num_nodes = len(nodes)
        patterns = [
            f"{num_nodes} of {num_nodes} exporters available",
            "User mode data collectors: SUCCESS",
            "Stopping prometheus server on localhost",
            f"Stopping {num_nodes} exporters",
        ]

        for pattern in patterns:
            assert re.search(pattern, p.stdout) != None, f"Missing expected pattern\n{p.stdout}"

        self.remove_job_file()
        return jobid
