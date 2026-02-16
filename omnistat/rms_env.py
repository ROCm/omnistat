#!/usr/bin/env python3
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
# Query resource manager environment and save job data to local file. Output
# intended for use by collector_rms.py collector to demarcate user jobs.
#
# Usage:rms_env.py [output_file]
#
# Default path for output_file: /tmp/omni_rmsjobinfo
# -------------------------------------------------------------------------------

import argparse
import fcntl
import json
import os
import subprocess
import sys


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument("--nostep", help="do not cache job step information", action="store_true")
    parser.add_argument(
        "output_file",
        type=str,
        nargs="?",
        help="path for output file (default=/tmp/omni_rmsjobinfo)",
        default="/tmp/omni_rmsjobinfo",
    )
    args = parser.parse_args()

    jobFile = args.output_file
    jobData = {}

    if "SLURM_JOB_ID" in os.environ:
        jobData["RMS_TYPE"] = "slurm"
        jobData["RMS_JOB_ID"] = os.getenv("SLURM_JOB_ID")
        jobData["RMS_JOB_USER"] = os.getenv("SLURM_JOB_USER")
        jobData["RMS_JOB_PARTITION"] = os.getenv("SLURM_JOB_PARTITION")
        jobData["RMS_JOB_NUM_NODES"] = os.getenv("SLURM_JOB_NUM_NODES")
        if "SLURM_PTY_PORT" in os.environ:
            jobData["RMS_JOB_BATCHMODE"] = 0
        else:
            jobData["RMS_JOB_BATCHMODE"] = 1
        if "SLURM_JOB_NAME" in os.environ:
            jobData["RMS_JOB_NAME"] = os.getenv("SLURM_JOB_NAME")

        # slurm stores large unsigned int if not in a job step, convert that to -1
        step = -1
        if not args.nostep:
            if "SLURM_STEP_ID" in os.environ:
                envstep = int(os.getenv("SLURM_STEP_ID"))
                if envstep < 4000000000:
                    step = envstep
        jobData["RMS_STEP_ID"] = step

    elif "FLUX_URI" in os.environ:
        # step 1: get parent jobid
        command = ["flux", "getattr", "jobid"]
        try:
            results = subprocess.run(command, capture_output=True, text=True, timeout=5.0)
        except:
            print("ERROR: Unable to query flux jobid")
            sys.exit(0)
        jobid = results.stdout.strip()

        # step 2: get details for given job
        command = ["flux", "-p", "jobs", "-n", "--format={id.f58},{username},{queue},{nnodes}", "%s" % jobid]
        try:
            results = subprocess.run(command, capture_output=True, text=True, timeout=5.0)
        except:
            print("ERROR: Unable to query flux for job information")
            sys.exit(0)

        fluxdata = results.stdout.strip().split(",")
        jobData["RMS_TYPE"] = "flux"
        jobData["RMS_JOB_ID"] = fluxdata[0]
        jobData["RMS_JOB_USER"] = fluxdata[1]
        jobData["RMS_JOB_PARTITION"] = fluxdata[2]
        jobData["RMS_JOB_NUM_NODES"] = fluxdata[3]
        jobData["RMS_JOB_BATCHMODE"] = 1  # marking all jobs as batch jobs to start
        step = -1
        if "FLUX_JOB_ID" in os.environ:
            step = os.getenv("FLUX_JOB_ID")
        jobData["RMS_STEP_ID"] = step

    elif "PBS_JOBID" in os.environ:
        jobData["RMS_TYPE"] = "pbs"
        id_raw = os.getenv("PBS_JOBID")
        jobid = id_raw.split(".")[0] if "." in id_raw else id_raw
        jobData["RMS_JOB_ID"] = jobid
        jobData["RMS_JOB_USER"] = os.getenv("PBS_O_LOGNAME")
        jobData["RMS_JOB_PARTITION"] = os.getenv("PBS_QUEUE")
        nodefile = os.environ.get("PBS_NODEFILE")
        with open(nodefile) as f:
            nodes = {line.strip() for line in f}
        jobData["RMS_JOB_NUM_NODES"] = len(nodes)
        if "INTERACTIVE" in os.getenv("PBS_ENVIRONMENT"):
            jobData["RMS_JOB_BATCHMODE"] = 0
        else:
            jobData["RMS_JOB_BATCHMODE"] = 1
        jobData["RMS_STEP_ID"] = -1

    else:
        print("ERROR: Unknown or undetected resource manager. Verify running in active job")
        sys.exit(1)

    # save to file - file lock is used to avoid contention from multiple processes on same node
    with open(jobFile, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(jobData, f, indent=4)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
