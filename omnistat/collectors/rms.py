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

"""Resource manager data collector

Implements a prometheus info metric to track job-related info.  The
default resulting metric is named "rmsjob_info{}" and is always published.  An
optional "rmsjob_annotations{}" metric can be published to provide
user-provided annotation timestamps.
"""

import json
import logging
import os
import platform
import sys

from prometheus_client import Gauge

import omnistat.utils as utils
from omnistat.collector_base import Collector


class RMSJob(Collector):
    def __init__(self, config):
        logging.debug("Initializing resource manager job data collector")

        annotations = False
        mode = "file-based"
        file = "/tmp/omni_rmsjobinfo"
        stepfile = "/tmp/omni_rmsjobinfo_step"

        if config.has_section("omnistat.collectors.rms"):
            section = config["omnistat.collectors.rms"]
            annotations = section.getboolean("enable_annotations", False)
            mode = section.get("job_detection_mode", "file-based")
            file = section.get("job_detection_file", "/tmp/omni_rmsjobinfo")
            stepfile = section.get("step_detection_file", f"{file}_step")

        self.__prefix = "rmsjob_"
        self.__annotationsEnabled = annotations
        self.__RMSMetrics = {}
        self.__rmsJobInfo = []
        self.__lastAnnotationLabel = None
        self.__rmsJobMode = mode
        self.__rmsJobFile = file
        self.__rmsJobStepFile = stepfile
        self.__rmsJobFileTimeStamp = 0
        self.__rmsJobStepFileTimeStamp = 0
        self.__rmsAnnotationsFileTimeStamp = 0

        self.__resultsCached = {}
        self.__resultsStepCached = {}
        self.__annotationsCached = {}

        # jobMode
        if self.__rmsJobMode == "file-based":
            logging.info(
                "collector_rms: reading job information from prolog/epilog derived file (%s)" % self.__rmsJobFile
            )
        elif self.__rmsJobMode == "squeue":
            logging.info("collector_rms: configured to poll slurm periodically with squeue")

            # setup squeue binary path to query slurm to determine node ownership
            command = utils.resolvePath("squeue", "SLURM_PATH")
            if command is None:
                logging.error("")
                logging.error("Please verify SLURM is installed and squeue binary is available")
                logging.error("")
                sys.exit(4)
            # command-line flags for use with squeue to obtained desired metrics
            hostname = platform.node().split(".", 1)[0]
            flags = "-w " + hostname + " -h  --Format=JobID::,UserName::,Partition::,NumNodes::,BatchFlag"
            # cache query command with options
            self.__squeue_query = [command] + flags.split()
            # job step query command
            flags = "-s -w " + hostname + " -h --Format=StepID"
            self.__squeue_steps = [command] + flags.split()
            logging.debug("squeue_exec = %s" % self.__squeue_query)
        else:
            logging.error("Unsupported slurm job data collection mode")

    def querySlurmJob(self, timeout=1, exit_on_error=False, mode="squeue"):
        """
        Query SLURM and return job info for local host.
        Supports two query modes: squeue call and read from file.

        Returns dictionary containing job id, user, partition, # of nodes, and batchmode flag
        """

        results = {}

        if mode == "squeue":
            data = utils.runShellCommand(self.__squeue_query, timeout=timeout, exit_on_error=exit_on_error)
            if data == None:
                logging.warning(
                    "Failed to capture job information: squeue timed out. "
                    "Please increase sampling interval or switch to file-based mode."
                )
            # squeue query output format: JOBID:USER:PARTITION:NUM_NODES:BATCHFLAG
            elif data.stdout.strip():
                data = data.stdout.strip().split(":")
                keys = [
                    "RMS_JOB_ID",
                    "RMS_JOB_USER",
                    "RMS_JOB_PARTITION",
                    "RMS_JOB_NUM_NODES",
                    "RMS_JOB_BATCHMODE",
                ]
                results = dict(zip(keys, data))
                results["RMS_TYPE"] = "slurm"

                # require a 2nd query to ascertain job steps (otherwise, miss out on batchflag)
                results["RMS_STEP_ID"] = -1
                data = utils.runShellCommand(self.__squeue_steps, timeout=timeout, exit_on_error=exit_on_error)
                if data == None:
                    logging.warning(
                        "Failed to capture job step information: squeue timed out. "
                        "Please increase sampling interval or switch to file-based mode."
                    )
                elif data.stdout.strip():
                    # If we are in an active job step, the STEPID will have an integer index appended, e.g.
                    # 57735.10
                    # 57735.interactive
                    stepField = (data.stdout.splitlines()[0]).strip()
                    jobstep = stepField.split(".")[1]
                    if jobstep.isdigit():
                        results["RMS_STEP_ID"] = jobstep

        elif mode == "file-based":
            # preference is given to job step file if it exists
            if os.path.isfile(self.__rmsJobStepFile):
                # only read contents if modify timestamp has been updated
                modTime = os.path.getmtime(self.__rmsJobStepFile)
                if modTime > self.__rmsJobStepFileTimeStamp:
                    with open(self.__rmsJobStepFile, "r") as file:
                        logging.info("[file-based (step)]: reading %s " % self.__rmsJobStepFile)
                        self.__rmsJobStepFileTimeStamp = modTime
                        results = json.load(file)
                    self.__resultsStepCached = results
                else:
                    results = self.__resultsStepCached
            elif os.path.isfile(self.__rmsJobFile):
                # only read contents if modify timestamp has been updated
                modTime = os.path.getmtime(self.__rmsJobFile)
                if modTime > self.__rmsJobFileTimeStamp:
                    with open(self.__rmsJobFile, "r") as file:
                        logging.info("[file-based]: reading %s " % self.__rmsJobFile)
                        self.__rmsJobFileTimeStamp = modTime
                        results = json.load(file)
                    self.__resultsCached = results
                else:
                    results = self.__resultsCached

        return results

    def registerMetrics(self):
        """Register metrics of interest"""

        # alternate approach - define an info metric
        # (https://ypereirareis.github.io/blog/2020/02/21/how-to-join-prometheus-metrics-by-label-with-promql/)
        labels = ["jobid", "user", "partition", "nodes", "batchflag", "jobstep", "type"]
        self.__RMSMetrics["info"] = Gauge(self.__prefix + "info", "RMS job details", labels)

        # metric to support user annotations
        self.__RMSMetrics["annotations"] = Gauge(
            self.__prefix + "annotations", "User job annotations", ["marker", "jobid"]
        )

        for metric in self.__RMSMetrics:
            logging.debug("--> Registered RMS metric = %s" % metric)

    def updateMetrics(self):
        self.__RMSMetrics["info"].clear()
        self.__RMSMetrics["annotations"].clear()
        jobEnabled = False

        results = self.querySlurmJob(mode=self.__rmsJobMode)
        if results:
            jobEnabled = True

        # Case when SLURM job is allocated
        if jobEnabled:
            self.__RMSMetrics["info"].labels(
                jobid=results["RMS_JOB_ID"],
                user=results["RMS_JOB_USER"],
                partition=results["RMS_JOB_PARTITION"],
                nodes=results["RMS_JOB_NUM_NODES"],
                batchflag=results["RMS_JOB_BATCHMODE"],
                jobstep=results["RMS_STEP_ID"],
                type=results["RMS_TYPE"],
            ).set(1)

            # Check for user supplied annotations
            if self.__annotationsEnabled:
                userFile = "/tmp/omnistat_%s_annotate.json" % results["RMS_JOB_USER"]

                userFileExists = os.path.isfile(userFile)
                if userFileExists:
                    # only read contents if modify timestamp has been updated
                    modTime = os.path.getmtime(userFile)
                    if modTime > self.__rmsAnnotationsFileTimeStamp:
                        with open(userFile, "r") as file:
                            data = json.load(file)
                        self.__rmsAnnotationsFileTimeStamp = modTime
                        self.__annotationsCached = data
                    else:
                        data = self.__annotationsCached

                # Reset existing annotation in two scenarios:
                #  1. Previous annotation stopped (file no longer present)
                #  2. There is a new annotation (label has changed)
                if self.__lastAnnotationLabel != None and (
                    not userFileExists or self.__lastAnnotationLabel != data["annotation"]
                ):
                    self.__RMSMetrics["annotations"].labels(
                        marker=self.__lastAnnotationLabel,
                        jobid=results["RMS_JOB_ID"],
                    ).set(0)
                    self.__lastAnnotationLabel = None

                if userFileExists:
                    self.__RMSMetrics["annotations"].labels(
                        marker=data["annotation"],
                        jobid=results["RMS_JOB_ID"],
                    ).set(data["timestamp_secs"])
                    self.__lastAnnotationLabel = data["annotation"]

        # Case when no job detected
        else:
            self.__RMSMetrics["info"].labels(
                jobid="", user="", partition="", nodes="", batchflag="", jobstep="", type=""
            ).set(1)

        return
