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

import json
import os
import subprocess
import sys
import tempfile

import pytest

from test.test_collectors import OmnistatTestServer


def run_rms_env(output_file, env_overrides=None, extra_args=None):
    """Run omnistat-rms-env with a controlled environment.

    Args:
        output_file: Path where the job detection file will be written.
        env_overrides: Dict of environment variables to set (e.g. SLURM_JOB_ID).
        extra_args: Additional CLI args (e.g. ["--nostep", "--localjob=myjob"]).

    Returns:
        subprocess.CompletedProcess result.
    """
    env = os.environ.copy()
    # Strip any real RMS variables to avoid interference
    for key in list(env.keys()):
        if key.startswith(("SLURM_", "FLUX_", "PBS_")):
            del env[key]
    if env_overrides:
        env.update(env_overrides)

    cmd = [sys.executable, "-m", "omnistat.rms_env"]
    if extra_args:
        cmd.extend(extra_args)
    cmd.append(output_file)

    return subprocess.run(cmd, env=env, capture_output=True, text=True)


def read_job_file(path):
    """Read and return parsed JSON from a job detection file."""
    with open(path) as f:
        return json.load(f)


class RMSTestServer(OmnistatTestServer):
    """Omnistat server with only the RMS collector enabled."""

    def __init__(self, job_file, step_file=None, enable_annotations=False):
        config_sections = {
            "omnistat.collectors.rms": {
                "job_detection_mode": "file-based",
                "job_detection_file": job_file,
                "enable_annotations": str(enable_annotations),
            },
        }
        if step_file:
            config_sections["omnistat.collectors.rms"]["step_detection_file"] = step_file

        super().__init__(["rms"], config_sections=config_sections)

    def get(self):
        """Scrape /metrics and return {name: {frozenset(labels): value}}."""
        results = {}
        for family in super().get():
            for sample in family.samples:
                key = frozenset(sample.labels.items())
                results.setdefault(sample.name, {})[key] = sample.value
        return results


class TestRMSEnv:
    """Tests for omnistat-rms-env with simulated RMS environments."""

    def test_no_rms_detected(self, tmp_path):
        """Verify exit code 1 when no RMS environment is detected."""
        job_file = str(tmp_path / "rmsjobinfo")
        result = run_rms_env(job_file)
        assert result.returncode == 1

    def test_slurm_env(self, tmp_path):
        """Verify omnistat-rms-env produces correct output with SLURM env vars."""
        job_file = str(tmp_path / "rmsjobinfo")
        result = run_rms_env(
            job_file,
            env_overrides={
                "SLURM_JOB_ID": "12345",
                "SLURM_JOB_USER": "testuser",
                "SLURM_JOB_PARTITION": "batch",
                "SLURM_JOB_NUM_NODES": "4",
                "SLURM_PTY_PORT": "12345",
            },
            extra_args=["--nostep"],
        )
        assert result.returncode == 0, f"rms_env failed: {result.stderr}"
        data = read_job_file(job_file)
        assert data["RMS_TYPE"] == "slurm"
        assert data["RMS_JOB_ID"] == "12345"
        assert data["RMS_JOB_USER"] == "testuser"
        assert data["RMS_JOB_PARTITION"] == "batch"
        assert data["RMS_JOB_NUM_NODES"] == "4"
        assert data["RMS_JOB_BATCHMODE"] == 0  # PTY_PORT set = interactive
        assert data["RMS_STEP_ID"] == -1

    def test_slurm_batch_mode(self, tmp_path):
        """Verify batch mode detection when SLURM_PTY_PORT is absent."""
        job_file = str(tmp_path / "rmsjobinfo")
        result = run_rms_env(
            job_file,
            env_overrides={
                "SLURM_JOB_ID": "67890",
                "SLURM_JOB_USER": "batchuser",
                "SLURM_JOB_PARTITION": "gpu",
                "SLURM_JOB_NUM_NODES": "8",
            },
            extra_args=["--nostep"],
        )
        assert result.returncode == 0, f"rms_env failed: {result.stderr}"
        data = read_job_file(job_file)
        assert data["RMS_JOB_BATCHMODE"] == 1  # no PTY_PORT = batch

    def test_localjob_mode(self, tmp_path):
        """Verify --localjob flag produces expected output without RMS."""
        job_file = str(tmp_path / "rmsjobinfo")
        result = run_rms_env(job_file, extra_args=["--localjob=mytest"])
        assert result.returncode == 0, f"rms_env failed: {result.stderr}"
        data = read_job_file(job_file)
        assert data["RMS_TYPE"] == "local"
        assert data["RMS_JOB_ID"] == "mytest"
        assert data["RMS_STEP_ID"] == -1


@pytest.fixture(scope="class")
def rms_server(request, tmp_path_factory):
    """Start an RMSTestServer with a job detection file populated by omnistat-rms-env."""
    tmp_dir = tmp_path_factory.mktemp("rms")
    job_file = str(tmp_dir / "rmsjobinfo")
    step_file = str(tmp_dir / "rmsjobinfo_step")

    # Populate the job file using omnistat-rms-env with simulated SLURM env
    result = run_rms_env(
        job_file,
        env_overrides={
            "SLURM_JOB_ID": "99999",
            "SLURM_JOB_USER": "testuser",
            "SLURM_JOB_PARTITION": "compute",
            "SLURM_JOB_NUM_NODES": "2",
        },
        extra_args=["--nostep"],
    )
    assert result.returncode == 0, f"rms_env setup failed: {result.stderr}"

    request.cls.job_file = job_file
    request.cls.step_file = step_file

    server = RMSTestServer(job_file, step_file=step_file)
    request.cls.server = server

    yield server

    server.stop()


@pytest.mark.usefixtures("rms_server")
class TestRMSCollector:
    """Tests for collector_rms.py via live Omnistat server."""

    def test_job_info_metric(self):
        """Verify rmsjob_info metric reflects the job detection file."""
        metrics = self.server.get()
        assert "rmsjob_info" in metrics, f"Missing rmsjob_info, got: {list(metrics.keys())}"
        for label_set, value in metrics["rmsjob_info"].items():
            labels = dict(label_set)
            assert labels["jobid"] == "99999"
            assert labels["user"] == "testuser"
            assert labels["partition"] == "compute"
            assert labels["nodes"] == "2"
            assert labels["type"] == "slurm"
            assert value == 1.0

    def test_no_job_clears_labels(self):
        """Verify empty labels when job file is removed."""
        os.unlink(self.job_file)
        metrics = self.server.get()
        assert "rmsjob_info" in metrics
        for label_set, value in metrics["rmsjob_info"].items():
            labels = dict(label_set)
            assert labels["jobid"] == ""
            assert labels["user"] == ""
            assert value == 1.0

    def test_step_file_priority(self):
        """Verify step file takes precedence over job file."""
        # Re-create the job file
        run_rms_env(
            self.job_file,
            env_overrides={
                "SLURM_JOB_ID": "99999",
                "SLURM_JOB_USER": "testuser",
                "SLURM_JOB_PARTITION": "compute",
                "SLURM_JOB_NUM_NODES": "2",
            },
            extra_args=["--nostep"],
        )
        # Create a step file with a different step ID
        step_data = {
            "RMS_TYPE": "slurm",
            "RMS_JOB_ID": "99999",
            "RMS_JOB_USER": "testuser",
            "RMS_JOB_PARTITION": "compute",
            "RMS_JOB_NUM_NODES": "2",
            "RMS_JOB_BATCHMODE": "1",
            "RMS_STEP_ID": 7,
        }
        with open(self.step_file, "w") as f:
            json.dump(step_data, f)

        metrics = self.server.get()
        assert "rmsjob_info" in metrics
        for label_set, value in metrics["rmsjob_info"].items():
            labels = dict(label_set)
            assert labels["jobstep"] == "7"

    def test_cached_reads(self):
        """Verify cached results are returned when files haven't changed."""
        # Scrape once to populate the cache (step file still exists from previous test)
        metrics1 = self.server.get()
        assert "rmsjob_info" in metrics1

        # Scrape again without modifying files — should hit the cache branches
        metrics2 = self.server.get()
        assert "rmsjob_info" in metrics2
        for label_set, value in metrics2["rmsjob_info"].items():
            labels = dict(label_set)
            assert labels["jobstep"] == "7"
            assert value == 1.0

        # Remove step file, scrape to populate job file cache
        os.unlink(self.step_file)
        metrics3 = self.server.get()
        assert "rmsjob_info" in metrics3

        # Scrape again without modifying job file — hits job file cache branch
        metrics4 = self.server.get()
        assert "rmsjob_info" in metrics4
        for label_set, value in metrics4["rmsjob_info"].items():
            labels = dict(label_set)
            assert labels["jobid"] == "99999"
            assert value == 1.0


ANNOTATION_FILE = "/tmp/omnistat_testuser_annotate.json"


@pytest.fixture(scope="class")
def rms_annotation_server(request, tmp_path_factory):
    """Start an RMSTestServer with annotations enabled."""
    tmp_dir = tmp_path_factory.mktemp("rms_annot")
    job_file = str(tmp_dir / "rmsjobinfo")
    step_file = str(tmp_dir / "rmsjobinfo_step")

    result = run_rms_env(
        job_file,
        env_overrides={
            "SLURM_JOB_ID": "99999",
            "SLURM_JOB_USER": "testuser",
            "SLURM_JOB_PARTITION": "compute",
            "SLURM_JOB_NUM_NODES": "2",
        },
        extra_args=["--nostep"],
    )
    assert result.returncode == 0, f"rms_env setup failed: {result.stderr}"

    request.cls.job_file = job_file

    server = RMSTestServer(job_file, step_file=step_file, enable_annotations=True)
    request.cls.server = server

    yield server

    server.stop()
    # Clean up annotation file
    if os.path.exists(ANNOTATION_FILE):
        os.unlink(ANNOTATION_FILE)


@pytest.mark.usefixtures("rms_annotation_server")
class TestRMSAnnotations:
    """Tests for RMS collector annotation support."""

    def test_annotation_published(self):
        """Verify annotation metric is published when annotation file exists."""
        annotation_data = {"annotation": "phase1_start", "timestamp_secs": 1700000000}
        with open(ANNOTATION_FILE, "w") as f:
            json.dump(annotation_data, f)

        metrics = self.server.get()
        assert "rmsjob_annotations" in metrics, f"Missing rmsjob_annotations, got: {list(metrics.keys())}"
        for label_set, value in metrics["rmsjob_annotations"].items():
            labels = dict(label_set)
            assert labels["marker"] == "phase1_start"
            assert labels["jobid"] == "99999"
            assert value == 1700000000

    def test_annotation_cached_read(self):
        """Verify cached annotation is used when file hasn't changed."""
        # Scrape again without modifying the annotation file — hits cache branch
        metrics = self.server.get()
        assert "rmsjob_annotations" in metrics
        for label_set, value in metrics["rmsjob_annotations"].items():
            labels = dict(label_set)
            assert labels["marker"] == "phase1_start"
            assert value == 1700000000

    def test_annotation_change_resets_previous(self):
        """Verify previous annotation is reset when label changes."""
        annotation_data = {"annotation": "phase2_start", "timestamp_secs": 1700001000}
        with open(ANNOTATION_FILE, "w") as f:
            json.dump(annotation_data, f)

        metrics = self.server.get()
        assert "rmsjob_annotations" in metrics
        # Should have the new annotation
        labels_found = {}
        for label_set, value in metrics["rmsjob_annotations"].items():
            labels = dict(label_set)
            labels_found[labels["marker"]] = value
        assert "phase2_start" in labels_found
        assert labels_found["phase2_start"] == 1700001000

    def test_annotation_removed(self):
        """Verify annotation resets when annotation file is removed."""
        os.unlink(ANNOTATION_FILE)
        metrics = self.server.get()
        assert "rmsjob_annotations" in metrics
        # Previous annotation should be reset to 0
        for label_set, value in metrics["rmsjob_annotations"].items():
            labels = dict(label_set)
            if labels["marker"] == "phase2_start":
                assert value == 0
