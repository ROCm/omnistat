#!/usr/bin/env python3
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

import argparse
import logging
import os
import shutil
import subprocess
import sys
import timeit
from datetime import datetime, timedelta
from pathlib import Path
from string import Template

import matplotlib.dates as mdates
import matplotlib.pylab as plt
import numpy as np
import pandas
from prometheus_api_client import MetricRangeDataFrame, PrometheusConnect
from prometheus_api_client.utils import parse_datetime
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    HRFlowable,
    Image,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from omnistat import utils


# Query job metrics from Omnistat databases to generate reports and export
# collected metrics.
#
# Requirements:
#  - Job ID must be known.
#  - Execution must have happened in the last 365 days.
#  - There are certain limitations to how short or how long job executions need
#    to be to work with the query tool. For minimum job durations, require at
#    least 5 samples to generate results. The maximum supported job durations
#    depend on Victoria Metrics settings. The following table provides an
#    estimate for different intervals assuming Omnistat defaults:
#     ---------------------------------------------------------------------
#     | Interval | Min duration | Max duration (30k) | Max duration (90k) |
#     ---------------------------------------------------------------------
#     |   0.01 s |          1 s |              5.0 m |             15.0 m |
#     |   0.10 s |          1 s |              0.8 h |              2.5 h |
#     |   1.00 s |          5 s |              8.3 h |             25.0 h |
#     |   5.00 s |         25 s |              1.7 d |              5.2 d |
#     |  15.00 s |         75 s |              5.2 d |             15.0 d |
#     ---------------------------------------------------------------------
#    Note that the maximum job duration is not a hard constraint: 1) the query
#    tool can still be used with intervals longer than the sampling interval,
#    and 2) Victoria Metrics can be tweaked to support longer job durations by
#    increasing the `-search.maxPointsPerTimeseries` setting, which is 30k by
#    default and automatically bumped up to 90k in usermode Omnistat.
class QueryMetrics:

    # Minimum number of samples required to process data and generate reports.
    MIN_SAMPLES = 5

    # The database is initially scanned to identify the time range in which
    # the job was running. The step for this initial scan can't be too low
    # because queries have a maximum number of points per request, and we need
    # to potentially generate up to 365 queries to find a job in the last
    # year. Default to a scan step of 60s (1,440 points/day).
    SCAN_STEP = 60.0
    SCAN_DAYS = 365

    # Define metrics to report on. Set 'title_short' to indicate inclusion in
    # statistics summary.
    METRICS = [
        {
            "metric": "rocm_utilization_percentage",
            "title": "GPU Core Utilization",
            "title_short": "Utilization (%)",
        },
        {"metric": "rocm_vram_used_percentage", "title": "GPU Memory Used (%)", "title_short": "Memory Use (%)"},
        {
            "metric": "rocm_temperature_celsius",
            "title": "GPU Temperature (C)",
            "title_short": "Temperature (C)",
        },
        {"metric": "rocm_sclk_clock_mhz", "title": "GPU Clock Frequency (MHz)"},
        {"metric": "rocm_average_socket_power_watts", "title": "GPU Average Power (W)", "title_short": "Power (W)"},
    ]

    def __init__(self, interval, jobid, jobstep=None, marker=None, configfile=None, output_file=None):
        self.timer_start = timeit.default_timer()

        self.interval = interval
        self.jobID = jobid
        self.jobStep = jobstep
        self.marker = marker

        self.jobstepQuery = 'jobstep=~".*"'
        if self.jobStep:
            self.jobstepQuery = f'jobstep="{jobstep}"'
        logging.debug("Job step query set to -> %s" % self.jobstepQuery)

        self.markerQuery = 'marker=~".*"'
        if self.marker:
            self.markerQuery = f'marker="{marker}"'
        logging.debug("Annotation marker query set to -> %s" % self.markerQuery)

        config = utils.readConfig(utils.findConfigFile(configfile))
        self.config = {}
        self.config["system_name"] = config["omnistat.query"].get("system_name", "My Snazzy Cluster")
        self.config["prometheus_url"] = config["omnistat.query"].get("prometheus_url", "http://localhost:9090")

        self.prometheus = PrometheusConnect(url=self.config["prometheus_url"])

        self.enable_redirect = False
        self.vendorData = False
        self.output = None
        self.output_file = output_file

        # Optionally redirect stdout to an existing file
        if self.output_file:
            if os.path.isfile(self.output_file):
                self.output = open(self.output_file, "a")
                self.enable_redirect = True
                sys.stdout = self.output
            else:
                sys.exit()

        # Different ways to measure number of nodes:
        #  - num_nodes: Assigned to the job by the resource manager
        #  - num_nodes_job: Found when querying the database
        #  - num_nodes_step: Found when querying the database for a step
        self.num_nodes = None
        self.num_nodes_job = None
        self.num_nodes_step = None

        self.num_gpus = None
        self.start_time = None
        self.end_time = None

        self.hosts = None

        self.version = utils.getVersion()

    def __del__(self):
        if self.enable_redirect:
            self.output.close()

    def find_job_info(self):
        found = self._estimate_range()
        if not found:
            print("[ERROR]: no monitoring data found for job=%s" % self.jobID)
            sys.exit(1)

        if self.interval < QueryMetrics.SCAN_STEP:
            self._refine_range()

        # NOOP if job is very short running
        runtime = (self.end_time - self.start_time).total_seconds()
        estimated_samples = runtime / self.interval
        if estimated_samples < QueryMetrics.MIN_SAMPLES:
            logging.info(f"--> Unsupported query: short job duration ({runtime:.1f}s with {self.interval}s interval).")
            logging.info(f"--> Need at least {QueryMetrics.MIN_SAMPLES} samples to query.")
            sys.exit()

        self._retrieve_info()
        self._retrieve_hosts()

    def _estimate_range(self):
        """
        Scan the last SCAN_DAYS days, one day at a time, attempting to find
        the time range in which the job was running. Sets start_time and
        end_time accordingly if the job is found. Estimated start/end times
        are as accurate as the scan step.
        """
        # Ensure the scan to find the job ends 1 minute into the future to
        # guarantee we are not missing any recently pushed data.
        now = datetime.now() + timedelta(minutes=1)

        # Loop over days starting from now to find time window covering desired job
        for day in range(QueryMetrics.SCAN_DAYS):
            scan_end = now - timedelta(days=day)
            scan_start = scan_end - timedelta(days=1)

            results = self.query_range("max(rmsjob_info{$job,$step})", scan_start, scan_end, QueryMetrics.SCAN_STEP)
            if not self.end_time and len(results) > 0:
                self.end_time = datetime.fromtimestamp(results[0]["values"][-1][0])
                self.start_time = datetime.fromtimestamp(results[0]["values"][0][0])
                continue
            elif self.end_time and len(results) > 0:
                self.start_time = datetime.fromtimestamp(results[0]["values"][0][0])
                continue
            elif self.end_time and len(results) == 0:
                break

        if self.marker:
            results = self.query_range(
                "rmsjob_annotations{$job,$marker}>0", self.start_time, self.end_time, QueryMetrics.SCAN_STEP
            )
            if len(results) > 0:
                self.start_time = datetime.fromtimestamp(results[0]["values"][0][0])
                self.end_time = datetime.fromtimestamp(results[0]["values"][-1][0])
            else:
                return False

        return self.start_time != None

    def _refine_range(self):
        """
        Provide more accurate time range for the job by generating two
        additional queries around the estimated start and end times. Refined
        start/end times can be as accurate as the interval.
        """
        delta = timedelta(seconds=QueryMetrics.SCAN_STEP * 2)
        start_window = (self.start_time - delta, self.start_time + delta)
        end_window = (self.end_time - delta, self.end_time + delta)

        query = "max(rmsjob_info{$job,$step})"
        if self.marker:
            query = "rmsjob_annotations{$job,$marker}>0"

        # Force max lookback for more accurate results
        lookback = self.interval * 2

        results = self.query_range(query, start_window[0], start_window[1], self.interval, lookback)
        if len(results) > 0:
            self.start_time = datetime.fromtimestamp(results[0]["values"][0][0])

        results = self.query_range(query, end_window[0], end_window[1], self.interval, lookback)
        if len(results) > 0:
            self.end_time = datetime.fromtimestamp(results[0]["values"][-1][0])

    def _coarse_step(self, duration_seconds):
        # Make sure the default step is not longer than the execution.
        min_step = min(QueryMetrics.SCAN_STEP, duration_seconds)
        step = f"{min_step}s"

        # For longer jobs, select a coarser step for more efficient querying
        # of info data since we don't need accurate timing.
        duration_minutes = duration_seconds / 60
        if duration_minutes > 60:
            step = "1h"
        elif duration_minutes > 15:
            step = "15m"
        elif duration_minutes > 5:
            step = "5m"

        return step

    def _retrieve_info(self):
        """
        Query job info that is expected to remain constant during the
        execution.
        """
        duration_seconds = (self.end_time - self.start_time).total_seconds()
        assert duration_seconds > 0

        step = self._coarse_step(duration_seconds)

        # Cull job info with coarse resolution
        results = self.query_range("rmsjob_info{$job,$step}", self.start_time, self.end_time, step)
        assert len(results) > 0

        self.num_nodes = int(results[0]["metric"]["nodes"])

        # Cull number of GPUs with coarse resolution
        results = self.query_range(
            "rocm_num_gpus * on (instance) (max by (instance) (rmsjob_info{$job,$step}))",
            self.start_time,
            self.end_time,
            step,
        )
        assert len(results) > 0

        self.num_gpus = int(results[0]["values"][0][1])
        assert self.num_gpus > 0

        # Warn if nodes do not have same GPU counts
        for node in range(len(results)):
            value = int(results[node]["values"][0][1])
            if value != self.num_gpus:
                print(
                    "[WARNING]: compute nodes detected with differing number of GPUs (%i,%i) " % (self.num_gpus, value)
                )
                break

    # Detect hosts associated with this job
    def _retrieve_hosts(self):
        duration_seconds = (self.end_time - self.start_time).total_seconds()
        step = self._coarse_step(duration_seconds)

        self.hosts = []
        results = self.query_range(
            'rocm_utilization_percentage{card="0"} * on (instance) (max by (instance) (rmsjob_info{$job}))',
            self.start_time,
            self.end_time,
            step,
        )
        self.num_nodes_job = len(results)

        if self.jobStep:
            results = self.query_range(
                'rocm_utilization_percentage{card="0"} * on (instance) (max by (instance) (rmsjob_info{$job,$step}))',
                self.start_time,
                self.end_time,
                step,
            )
            for result in results:
                self.hosts.append(result["metric"]["instance"])
            self.num_nodes_step = len(self.hosts)
        else:
            for result in results:
                self.hosts.append(result["metric"]["instance"])
            self.num_nodes_step = self.num_nodes_job

        assert self.num_nodes_job > 0
        assert self.num_nodes_step > 0
        if self.num_nodes_job != self.num_nodes:
            logging.warning("")
            logging.warning("[WARNING]: telemetry data not collected for all nodes assigned to this job")
            logging.warning("--> # assigned hosts     = %i" % self.num_nodes)
            logging.warning("--> # of hosts with data = %i" % self.num_nodes_job)

    def metric_host_max_sum(self, values):
        """Determine host with <maximum> sum of all provided samples"""
        maxvalue = 0.0
        for i in range(len(values)):
            sum = values[i].sum()
            if sum > maxvalue:
                maxvalue = sum
                maxindex = i
        return maxindex, sum

    def metric_host_min_sum(self, values):
        """Determine host with <minimum> sum of all provided samples"""
        minvalue = sys.float_info.max
        for i in range(len(values)):
            sum = values[i].sum()
            if sum < minvalue:
                minvalue = sum
                minindex = i
        return minindex, sum

    def gather_vendor_data(self):
        # node-level data: total energy usage
        times_raw, values_raw, hosts = self.query_time_series_data("omnistat_vendor_energy_joules")
        if not values_raw:
            return
        self.vendorData = True

        node_level_energy_total = 0.0
        for i in range(len(values_raw)):
            node_energy = values_raw[i][-1] - values_raw[i][0]
            node_level_energy_total += node_energy
        # convert from J to kwH
        self.node_level_energy_total_kwh = node_level_energy_total / (1000 * 3600)

        # node-level data: memory energy usage
        times_raw, values_raw, hosts = self.query_time_series_data("omnistat_vendor_memory_energy_joules")
        if values_raw:
            node_level_memory_energy_total = 0.0
            for i in range(len(values_raw)):
                memory_energy = values_raw[i][-1] - values_raw[i][0]
                node_level_memory_energy_total += memory_energy
        # convert from J to kwH
        self.node_level_memory_energy_total_kwh = node_level_memory_energy_total / (1000 * 3600)

        # node-level data: cpu energy usage
        times_raw, values_raw, hosts = self.query_time_series_data("omnistat_vendor_cpu_energy_joules")
        node_level_cpu_energy_total = 0.0
        # sum energy over all nodes assigned to job
        for i in range(len(values_raw)):
            cpu_energy = values_raw[i][-1] - values_raw[i][0]
            node_level_cpu_energy_total += cpu_energy
        # convert from J to kwH
        self.node_level_cpu_energy_total_kwh = node_level_cpu_energy_total / (1000 * 3600)

        # node-level data: accelerator energy usage
        node_level_accel_energy_total = 0.0
        vendor_ngpus = 0
        energy_gpus = []

        for gpu in range(self.num_gpus):
            times_raw, values_raw, hosts = self.query_time_series_data(
                f'omnistat_vendor_accel_energy_joules{{card="{gpu}"}}'
            )
            if values_raw:
                vendor_ngpus += 1
                accel_energy = 0
                # loop over all assigned hosts for current gpu index to accumulate energy
                for i in range(len(values_raw)):
                    accel_energy += values_raw[i][-1] - values_raw[i][0]
                    node_level_accel_energy_total += values_raw[i][-1] - values_raw[i][0]
                energy_gpus.append(accel_energy)
                # convert from J to kwH
                self.node_level_accel_energy_total_kwh = node_level_accel_energy_total / (1000 * 3600)

        # override smi-based estimates
        if self.num_gpus == len(energy_gpus):
            for gpu in range(self.num_gpus):
                self.energyStats_kwh[gpu] = energy_gpus[gpu] / (1000 * 3600)
        elif self.num_gpus > len(energy_gpus):
            # deal with socket/gcd indexing (e.g. MI250)
            if self.num_gpus % len(energy_gpus) == 0:
                gpu_index_multiplier = int(self.num_gpus / len(energy_gpus))
                for i in range(len(energy_gpus)):
                    index = i * gpu_index_multiplier
                    self.energyStats_kwh[index] = energy_gpus[i] / (1000 * 3600)

        return

    def gather_data(self, saveTimeSeries=False):
        self.stats = {}
        self.time_series = {}
        self.max_GPU_memory_avail = []
        self.gpu_energy_total_kwh = 0
        self.energyStats_kwh = [None] * self.num_gpus
        self.mean_util_per_gpu = [None] * self.num_gpus

        for entry in QueryMetrics.METRICS:
            metric = entry["metric"]

            self.stats[metric + "_min"] = []
            self.stats[metric + "_max"] = []
            self.stats[metric + "_mean"] = []

            if saveTimeSeries:
                self.time_series[metric] = []
                self.time_series[metric + "_hostmax_raw"] = []
                self.time_series[metric + "_hostmin_raw"] = []

            # # init tracking of min/max
            # minValue =  sys.float_info.max
            # maxValue = -sys.float_info.max

            for gpu in range(self.num_gpus):

                query_metric = f'{metric}{{card="{gpu}"}}'
                try:
                    # (1) capture time series that assembles [mean] value at each timestamp across all assigned nodes
                    times, values_mean = self.query_time_series_data(query_metric, "avg")

                    # (2) capture time series that assembles [max] value at each timestamp across all assigned nodes
                    times, values_max = self.query_time_series_data(query_metric, "max")

                    # (3) capture raw time series
                    times_raw, values_raw, hosts = self.query_time_series_data(query_metric)
                except:
                    utils.error("Unable to query prometheus data for metric -> %s" % query_metric)

                # Sum total energy across all hosts and gpus
                if metric == "rocm_average_socket_power_watts":
                    energy_per_host = []

                    energyTotal = 0.0
                    for i in range(len(times_raw)):
                        startDate = times_raw[i][0]
                        x = (times_raw[i] - startDate).astype("timedelta64[s]").astype(float)
                        # Integrate time series to get energy used on this host/gpu index
                        # Units: W x [sec] = Joules -> Convert to kWH
                        energy = np.trapezoid(values_raw[i], x=x) / (1000 * 3600)
                        # accumulate to get total energy used by all hosts with this gpu index
                        energyTotal += energy
                        energy_per_host.append(energy)

                    # total energy used by this gpu index across all hosts
                    self.gpu_energy_total_kwh += energyTotal
                    self.energyStats_kwh[gpu] = energy_per_host

                # # Track hosts with min/max area under the curve (across all GPUs)
                # if len(self.hosts) > 1:
                #     minId, sum_min = self.metric_host_min_sum(values_raw)
                #     maxId, sum_max = self.metric_host_max_sum(values_raw)

                # if gpu == 0:
                #     for i in range(len(times)):
                #         print("%s %s %s" % (times[i],values_mean[i], values_max[i]))
                # sys.exit(1)

                self.stats[metric + "_max"].append(np.max(values_max))
                self.stats[metric + "_mean"].append(np.mean(values_mean))

                if metric == "rocm_vram_used":
                    # compute % memory used
                    times2, values2_min = self.query_time_series_data("card" + str(gpu) + "_rocm_vram_total", "min")
                    times2, values2_max = self.query_time_series_data("card" + str(gpu) + "_rocm_vram_total", "max")

                    memoryMin = np.min(values2_min)
                    memoryMax = np.max(values2_max)
                    if memoryMin != memoryMax:
                        print("[ERROR]: non-homogeneous memory sizes detected on assigned compute nodes")
                        sys.exit(1)

                    memoryAvail = memoryMax
                    self.stats[metric + "_max"][-1] = 100.0 * self.stats[metric + "_max"][-1] / memoryAvail
                    self.stats[metric + "_mean"][-1] = 100.0 * self.stats[metric + "_mean"][-1] / memoryAvail
                    self.max_GPU_memory_avail.append(memoryAvail)
                    values_mean = 100.0 * values_mean / memoryAvail
                    values_max = 100.0 * values_max / memoryAvail

                # save mean utilization per individual gpu
                values = []
                if metric == "rocm_utilization":
                    for i in range(len(times_raw)):
                        mean = np.mean(values_raw[i])
                        values.append(mean)
                    self.mean_util_per_gpu[gpu] = values

                if saveTimeSeries:
                    self.time_series[metric].append({"time": times, "values": values_mean})
                    # self.time_series[metric + '_hostmax_raw'].append({'time':times_raw[maxId],'values':values_raw[maxId],'host':hosts[maxId]})
                    # self.time_series[metric + '_hostmin_raw'].append({'time':times_raw[minId],'values':values_raw[minId],'host':hosts[minId]})
        return

    def generate_report_card(self):
        system = self.config["system_name"]

        print("")
        print("-" * 70)
        print("Omnistat Report Card for Job Id: %s" % self.jobID)
        if self.jobStep:
            print(
                "** Report confined to job step=%s (%i of %i nodes used)"
                % (self.jobStep, self.num_nodes_step, self.num_nodes_job)
            )
        if self.marker:
            print("** Report confined to annotation marker=%s" % (self.marker))
        print("-" * 70)
        print("")
        print("Job Overview (Num Nodes = %i, Machine = %s)" % (len(self.hosts), system))
        print(" --> Start time = %s" % self.start_time.strftime("%Y-%m-%d %H:%M:%S"))
        print(" --> End   time = %s" % self.end_time.strftime("%Y-%m-%d %H:%M:%S"))
        print(" --> Duration   = %i secs" % (self.end_time - self.start_time).seconds)

        print("")
        print("GPU Statistics:")
        print("")
        print("    %6s |" % "", end="")
        for entry in QueryMetrics.METRICS:
            if "title_short" in entry:
                # print("%16s |" % entry['title_short'],end='')
                print(" %s |" % entry["title_short"].center(16), end="")
        print(" Energy (kWh) |", end="")
        print("")
        print("    %6s |" % "GPU #", end="")
        for entry in QueryMetrics.METRICS:
            if "title_short" in entry:
                print(" %8s%8s |" % ("Max".center(6), "Mean".center(6)), end="")
        print("    Total     |", end="")
        print("")
        print("    " + "-" * 99)

        for card in range(self.num_gpus):
            print("    %6s |" % card, end="")
            for entry in QueryMetrics.METRICS:
                if "title_short" not in entry:
                    continue
                metric = entry["metric"]
                print(
                    "  %6.2f  %6.2f  |" % (self.stats[metric + "_max"][card], self.stats[metric + "_mean"][card]),
                    end="",
                )
            # add gpu-energy
            print("   %6.2e   |" % np.sum(self.energyStats_kwh[card]), end="")
            print("")

        if self.vendorData:
            print("")
            print("Vendor Energy Data:")
            print("  " + "-" * 65)
            print(
                "  Approximate Total Memory Energy Consumed = %.2e kWh (%5.2f %%)"
                % (
                    self.node_level_memory_energy_total_kwh,
                    100.0 * self.node_level_memory_energy_total_kwh / self.node_level_energy_total_kwh,
                )
            )
            print(
                "  Approximate Total CPU    Energy Consumed = %.2e kWh (%5.2f %%)"
                % (
                    self.node_level_cpu_energy_total_kwh,
                    100.0 * self.node_level_cpu_energy_total_kwh / self.node_level_energy_total_kwh,
                )
            )
            print(
                "  Approximate Total Accel  Energy Consumed = %.2e kWh (%5.2f %%)"
                % (
                    self.node_level_accel_energy_total_kwh,
                    100.0 * self.node_level_accel_energy_total_kwh / self.node_level_energy_total_kwh,
                )
            )
            print("  " + "-" * 65)
            print("  Approximate Total Node Energy Consumed   = %.2e kWh" % self.node_level_energy_total_kwh)
            print("")
        else:
            print("")
            print("Approximate Total GPU Energy Consumed = %.2e kWh" % self.gpu_energy_total_kwh)
            print("")

        print("--")
        print("Query interval = %.3f secs" % self.interval)
        print("Query execution time = %.1f secs" % (timeit.default_timer() - self.timer_start))
        print("Version = %s" % self.version)
        return

    def query_range(self, query_template, start, end, step, lookback=None):
        """
        Request a given query in the provided range. The query may optionally
        include variables to select job ID ($job) and step ID ($step) labels,
        and which are automatically substituted for the appropriate value.

        Args:
            query_template (str): PromQL query with substitutions.
            start (datetime): Query start time.
            end (datetime): Query end time.
            step (str|float): Query resolution string with unit, or float in seconds.

        Result:
            dict: Metric data in response of the submitted query.
        """
        template = Template(query_template)
        params = {}
        if lookback:
            params = {"max_lookback": lookback}
        query = template.substitute(job=f'jobid="{self.jobID}"', step=self.jobstepQuery, marker=self.markerQuery)
        results = self.prometheus.custom_query_range(query, start, end, step=step, params=params)
        return results

    def query_job_range(self, query_template):
        """
        Request a given query in the job's range. The query may optionally
        include variables to select job ID ($job) and step ID ($step) labels,
        and which are automatically substituted for the appropriate value.

        Args:
            query_template (str): PromQL query with substitutions.

        Result:
            dict: Metric data in response of the submitted query.
        """
        # The slowest sample time is used to improve query results in the
        # presence of unxpectectedly slow samples by adapting the lookback
        # period. While the expected sample time is in the 10-30ms range, we
        # tolerate some samples taking up to 10x longer to provide more
        # consistent results and avoid NaNs.
        slowest_sample_seconds = 0.3
        lookback = self.interval + slowest_sample_seconds
        results = self.query_range(query_template, self.start_time, self.end_time, self.interval, lookback)
        return results

    def query_time_series_data(self, metric_name, reducer=None, dataType=float):

        if reducer is None:
            query = "%s * on (instance) (max by (instance) (rmsjob_info{$job,$step}))" % (metric_name)
            results = self.query_job_range(query)
        else:
            query = "%s(%s * on (instance) (max by (instance) (rmsjob_info{$job,$step})))" % (reducer, metric_name)
            results = self.query_job_range(query)

        if reducer is None:
            # return lists with raw time series data from all hosts for given metric
            times = []
            values = []
            hosts = []

            for i in range(len(results)):
                tmpresult = np.asarray(results[i]["values"])
                # convert to time format
                tmptime = tmpresult[:, 0].astype(float).astype("datetime64[s]")
                if dataType == int:
                    tmpvalues = tmpresult[:, 1].astype(int)
                elif dataType == float:
                    tmpvalues = tmpresult[:, 1].astype(float)
                times.append(tmptime)
                values.append(tmpvalues)
                hosts.append(results[i]["metric"]["instance"])
            return times, values, hosts
        else:
            # only have a single time series when reduction operator applied
            results = np.asarray(results[0]["values"])

            # convert to time format
            time = results[:, 0].astype(float).astype("datetime64[s]")
            # let user decide on conversion type for gauge metric
            if dataType == int:
                values = results[:, 1].astype(int)
            elif dataType == float:
                values = results[:, 1].astype(float)
            return time, values

    # def query_gpu_metric(self, metricName):
    #     stats = {}
    #     stats["mean"] = []
    #     stats["max"] = []

    #     for gpu in range(self.num_gpus):
    #         metric = "card" + str(gpu) + "_" + metricName

    #         # --
    #         # Mean results
    #         results = self.prometheus.custom_query_range(
    #             'avg(%s * on (instance) rmsjob_info{jobid="%s"})' % (metric, self.jobID),
    #             self.start_time,
    #             self.end_time,
    #             step=60,
    #         )

    #         assert len(results) == 1
    #         data = results[0]["values"]
    #         data2 = np.asarray(data, dtype=float)
    #         stats["mean"].append(np.mean(data2[:, 1]))

    #         # --
    #         # Max results
    #         results = self.prometheus.custom_query_range(
    #             'max(%s * on (instance) rmsjob_info{jobid="%s"})' % (metric, self.jobID),
    #             self.start_time,
    #             self.end_time,
    #             step=60,
    #         )

    #         assert len(results) == 1
    #         data = results[0]["values"]
    #         data2 = np.asarray(data, dtype=float)
    #         stats["max"].append(np.max(data2[:, 1]))

    #     return stats

    def generate_pdf(self, outputFile):
        doc = SimpleDocTemplate(
            outputFile,
            pagesize=letter,
            rightMargin=1 * inch,
            leftMargin=1 * inch,
            topMargin=62,
            bottomMargin=18,
            showBoundary=0,
        )

        styles = getSampleStyleSheet()
        normal = ParagraphStyle("normal")
        Story = []
        Story.append(Spacer(1, 0.1 * inch))
        Story.append(HRFlowable(width="100%", thickness=2))
        ptext = """
        <strong>Omnistat Report Card</strong>: JobID = %s<br/>
        <strong>Start Time</strong>: %s<br/>
        <strong>End Time</strong>: %s<br/>
        <strong>Duration</strong>: %s secs<br/>
        """ % (
            self.jobID,
            self.start_time.strftime("%Y-%m-%d %H:%M:%S"),
            self.end_time.strftime("%Y-%m-%d %H:%M:%S"),
            (self.end_time - self.start_time).seconds,
        )
        Story.append(Paragraph(ptext, styles["Bullet"]))
        Story.append(HRFlowable(width="100%", thickness=2))
        if self.jobStep:
            ptext = """<strong>Job Step Mode</strong>: Report confined to job step = %s""" % (self.jobStep)
            Story.append(Paragraph(ptext))
            Story.append(HRFlowable(width="100%", thickness=2))
        if self.marker:
            ptext = """<strong>Annotation Mode</strong>: Report confined to marker = %s""" % (self.marker)
            Story.append(Paragraph(ptext))
            Story.append(HRFlowable(width="100%", thickness=2))

        # generate Utilization Table
        Story.append(Spacer(1, 0.2 * inch))
        ptext = """<strong>GPU Statistics</strong>"""
        Story.append(Paragraph(ptext, normal))
        Story.append(Spacer(1, 0.2 * inch))
        # Story.append(HRFlowable(width="100%",thickness=1))

        # --
        # Display general GPU Statistics
        # --

        data = []
        data.append(
            ["", "Utilization (%)", "", "Memory Use (%)", "", "Temperature (C)", "", "Power (W)", "", "Energy (kWh)"]
        )
        data.append(["GPU", "Max", "Mean", "Max", "Mean", "Max", "Mean", "Max", "Mean", "Total"])

        for gpu in range(self.num_gpus):
            data.append(
                [
                    gpu,
                    "%.2f" % self.stats["rocm_utilization_percentage_max"][gpu],
                    "%.2f" % self.stats["rocm_utilization_percentage_mean"][gpu],
                    "%.2f" % self.stats["rocm_vram_used_percentage_max"][gpu],
                    "%.2f" % self.stats["rocm_vram_used_percentage_mean"][gpu],
                    "%.2f" % self.stats["rocm_temperature_celsius_max"][gpu],
                    "%.2f" % self.stats["rocm_temperature_celsius_mean"][gpu],
                    "%.2f" % self.stats["rocm_average_socket_power_watts_max"][gpu],
                    "%.2f" % self.stats["rocm_average_socket_power_watts_mean"][gpu],
                    "%.2e" % np.sum(self.energyStats_kwh[gpu]),
                ]
            )

        t = Table(data, rowHeights=[0.21 * inch] * len(data), colWidths=[0.4 * inch] + [0.62 * inch] * 8 + [1 * inch])
        t.hAlign = "LEFT"
        t.setStyle(
            TableStyle([("LINEBELOW", (0, 1), (-1, 1), 1.5, colors.black), ("ALIGN", (0, 0), (-1, -1), "CENTER")])
        )
        t.setStyle(
            TableStyle(
                [
                    ("LINEBEFORE", (1, 0), (1, -1), 1.25, colors.darkgrey),
                    ("LINEAFTER", (2, 0), (2, -1), 1.25, colors.darkgrey),
                    ("LINEAFTER", (4, 0), (4, -1), 1.25, colors.darkgrey),
                    ("LINEAFTER", (6, 0), (6, -1), 1.25, colors.darkgrey),
                    ("LINEAFTER", (8, 0), (8, -1), 1.25, colors.darkgrey),
                ]
            )
        )
        t.setStyle(
            TableStyle(
                [("SPAN", (1, 0), (2, 0)), ("SPAN", (3, 0), (4, 0)), ("SPAN", (5, 0), (6, 0)), ("SPAN", (7, 0), (8, 0))]
            )
        )
        t.setStyle(TableStyle([("FONTSIZE", (1, 0), (-1, -1), 10)]))

        for each in range(2, len(data)):
            if each % 2 == 0:
                bg_color = colors.lightgrey
            else:
                bg_color = colors.whitesmoke

            t.setStyle(TableStyle([("BACKGROUND", (0, each), (-1, each), bg_color)]))
        Story.append(t)

        Story.append(Spacer(1, 0.2 * inch))

        if self.vendorData:

            ptext = """<strong>Vendor Energy Data Totals</strong>"""
            Story.append(Paragraph(ptext, normal))
            Story.append(Spacer(1, 0.2 * inch))

            data = []
            data.append(["Type", "Energy Consumed (kWh)", "% of Total"])
            data.append(
                [
                    "Memory",
                    "%.2e" % self.node_level_memory_energy_total_kwh,
                    "%5.2f %%" % (100.0 * self.node_level_memory_energy_total_kwh / self.node_level_energy_total_kwh),
                ]
            )
            data.append(
                [
                    "CPU",
                    "%.2e" % self.node_level_cpu_energy_total_kwh,
                    "%5.2f %%" % (100.0 * self.node_level_cpu_energy_total_kwh / self.node_level_energy_total_kwh),
                ]
            )
            data.append(
                [
                    "Accelerator (GPU)",
                    "%.2e" % self.node_level_accel_energy_total_kwh,
                    "%5.2f %%" % (100.0 * self.node_level_accel_energy_total_kwh / self.node_level_energy_total_kwh),
                ]
            )
            data.append(["Total", "%.2e" % self.node_level_energy_total_kwh, "%5.2f %%" % 100])

            twidth = 6.348
            t = Table(
                data,
                rowHeights=[0.21 * inch] * (len(data) - 1) + [0.23 * inch],
                colWidths=[0.3 * twidth * inch] + [0.5 * twidth * inch] + [0.2 * twidth * inch],
            )
            t.hAlign = "LEFT"
            t.setStyle(
                TableStyle(
                    [
                        ("LINEBELOW", (0, 0), (-1, 0), 1.5, colors.black),
                        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        ("LINEBELOW", (0, 3), (-1, 3), 1.5, colors.black),
                        ("ALIGN", (0, 3), (-1, -1), "CENTER"),
                    ]
                )
            )
            t.setStyle(TableStyle([("FONTSIZE", (1, 0), (-1, -1), 10)]))

            t.setStyle(
                TableStyle(
                    [
                        ("LINEBEFORE", (1, 0), (1, -1), 1.25, colors.darkgrey),
                        ("LINEAFTER", (1, 0), (1, -1), 1.25, colors.darkgrey),
                    ]
                )
            )

            for each in range(1, len(data) - 1):
                bg_color = colors.whitesmoke
                t.setStyle(TableStyle([("BACKGROUND", (0, each), (-1, each), bg_color)]))
            t.setStyle(TableStyle([("BACKGROUND", (0, 4), (-1, 4), colors.lightgrey)]))

            Story.append(t)
            Story.append(Spacer(1, 0.2 * inch))

        ##             Story.append(HRFlowable(width="75%", thickness=1, hAlign="LEFT"))
        ##             Story.append(Paragraph("<strong>Vendor energy data</strong>:", normal))
        ##
        ##             ptext = "Approximate Total Memory Energy Consumed = %.2e kWh (%5.2f %%)"% (
        ##                     self.node_level_memory_energy_total_kwh,
        ##                     100.0 * self.node_level_memory_energy_total_kwh / self.node_level_energy_total_kwh,
        ##                 )
        ##             Story.append(Paragraph(ptext, normal))
        ##             ptext = "  Approximate Total CPU    Energy Consumed = %.2e kWh (%5.2f %%)" % (
        ##                     self.node_level_cpu_energy_total_kwh,
        ##                     100.0 * self.node_level_cpu_energy_total_kwh / self.node_level_energy_total_kwh,
        ##                 )
        ##             Story.append(Paragraph(ptext, normal))
        ##
        ##             ptext = "  Approximate Total Accel  Energy Consumed = %.2e kWh (%5.2f %%)" % (
        ##                     self.node_level_accel_energy_total_kwh,
        ##                     100.0 * self.node_level_accel_energy_total_kwh / self.node_level_energy_total_kwh,
        ##                 )
        ##             Story.append(Paragraph(ptext, normal))
        ##
        ##             ptext = "Approximate Total Node Energy Consumed   = %.2e kWh" % self.node_level_energy_total_kwh
        ##             Story.append(Paragraph(ptext, normal))
        ##
        ##             Story.append(HRFlowable(width="75%", thickness=1, hAlign="LEFT"))
        else:
            ptext = """Approximate Total GPU Energy Consumed = %.2e kWh""" % (self.gpu_energy_total_kwh)
            Story.append(Paragraph(ptext, normal))

        # --
        # Display time-series plots
        # --

        Story.append(Spacer(1, 0.2 * inch))
        Story.append(HRFlowable(width="100%", thickness=1))
        Story.append(Spacer(1, 0.2 * inch))
        ptext = """<strong>Time Series</strong>"""
        Story.append(Paragraph(ptext, normal))
        Story.append(Spacer(1, 0.2 * inch))

        for entry in QueryMetrics.METRICS:
            metric = entry["metric"]
            plt.figure(figsize=(9, 2.5))

            for gpu in range(self.num_gpus):
                plt.plot(
                    self.time_series[metric][gpu]["time"],
                    self.time_series[metric][gpu]["values"],
                    linewidth=0.4,
                    label="Card %i" % gpu,
                )
            #                         self.time_series[metric][gpu]['values'],marker='o',markersize=1,linewidth=0.4,label='Card %i' % gpu)
            # plt.plot(self.time_series[metric+'_hostmax_raw'][gpu]['time'],
            #          self.time_series[metric+'_hostmax_raw'][gpu]['values'],'--',linewidth=0.4,label=None)
            # wip - show high/low usage
            # plt.plot(self.time_series[metric+'_hostmin_raw'][gpu]['time'],
            #          self.time_series[metric+'_hostmin_raw'][gpu]['values'],'--',linewidth=0.4,label=None)

            plt.title(entry["title"])
            plt.legend(bbox_to_anchor=(1.0, 0.5), loc="center left", ncol=1, frameon=True)
            plt.grid()
            ax = plt.gca()

            locator = mdates.AutoDateLocator(minticks=4, maxticks=12)
            formatter = mdates.ConciseDateFormatter(locator)
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(formatter)
            plt.savefig(".utilization.png", dpi=150, bbox_inches="tight")
            plt.close()
            aplot = Image(".utilization.png")
            aplot.hAlign = "LEFT"
            aplot._restrictSize(6.5 * inch, 4 * inch)
            Story.append(aplot)
            os.remove(".utilization.png")

        # Energy chart
        if True:
            labels = []
            min_energy = []
            mean_energy = []
            max_energy = []
            for gpu in range(self.num_gpus):
                labels.append("Card %i" % gpu)
                min_energy.append(np.min(self.energyStats_kwh[gpu]))
                max_energy.append(np.max(self.energyStats_kwh[gpu]))
                mean_energy.append(np.mean(self.energyStats_kwh[gpu]))

            # build max/min bars
            emax = []
            emin = []
            for gpu in range(self.num_gpus):
                emin.append(mean_energy[gpu] - min_energy[gpu])
                emax.append(max_energy[gpu] - mean_energy[gpu])

            #            plt.figure(figsize=(6.,2))
            plt.figure(figsize=(9, 2.5))
            plt.barh(
                labels,
                mean_energy,
                align="center",
                height=0.75,
                edgecolor="black",
                color="grey",
                alpha=0.6,
                xerr=[emin, emax],
                capsize=5,
                error_kw=dict(lw=1, capthick=2),
            )
            plt.title("GPU Energy Consumed - Average (min/max) across All %i Hosts" % len(self.hosts))
            # ,fontsize=8)
            plt.xlabel("Energy (kWH)")
            plt.grid()
            # plt.gca().tick_params(axis='both',labelsize=8)
            plt.savefig(".energy.png", dpi=150, bbox_inches="tight")
            plt.close()

            aplot = Image(".energy.png")
            aplot.hAlign = "LEFT"
            aplot._restrictSize(6.5 * inch, 3 * inch)
            Story.append(aplot)
            os.remove(".energy.png")

        Story.append(Spacer(1, 0.2 * inch))
        Story.append(HRFlowable(width="100%", thickness=1))

        # Multi-node distributions
        if False:
            Story.append(Spacer(1, 0.2 * inch))
            ptext = """<strong>Multi-node Distribution</strong>"""
            Story.append(Paragraph(ptext, normal))
            Story.append(Spacer(1, 0.2 * inch))

            # mean gpu utilization
            allgpus = []
            for gpu in range(self.num_gpus):
                allgpus += self.mean_util_per_gpu[gpu]
            allgpus.sort(reverse=True)
            plt.figure(figsize=(9, 2.5))
            plt.plot(allgpus, "o", markersize=2, fillstyle="none")
            # linewidth=0.8)
            plt.grid()
            plt.xlabel("GPU #")
            plt.ylabel("GPU Utilization (%)")
            plt.ylim(0, 100)
            plt.title("Individual GPU Utilizations Averaged over Job Duration")
            plt.savefig(".distribution.png", dpi=150, bbox_inches="tight")
            plt.close()
            aplot = Image(".distribution.png")
            aplot.hAlign = "LEFT"
            aplot._restrictSize(6.5 * inch, 4 * inch)
            Story.append(aplot)
            os.remove(".distribution.png")

            Story.append(Spacer(1, 0.2 * inch))
            Story.append(HRFlowable(width="100%", thickness=1))

        footerStyle = ParagraphStyle(
            "footer",
            fontSize=8,
            parent=styles["Normal"],
        )

        ptext = """Query interval = %i secs""" % self.interval
        Story.append(Paragraph(ptext, footerStyle))
        ptext = """Query execution time = %.1f secs""" % (timeit.default_timer() - self.timer_start)
        Story.append(Paragraph(ptext, footerStyle))
        ptext = """Version = %s""" % self.version
        Story.append(Paragraph(ptext, footerStyle))
        Story.append(HRFlowable(width="100%", thickness=1))

        # Build the .pdf
        doc.build(Story)

        return

    def export_metrics(self, output_file, metrics, pivot_labels):
        """Export time series for given metrics as a CSV file.

        Organize columns hierarchically, aligning timestamps and allowing
        access to values by the labels provided as "pivot_labels". For
        example, for ROCm metrics we use the metric -> instance -> card
        hierarchy as follows:
         | metric              | rocm_utilization_percentage   |
         | instance            | node01        | node02        |
         | card                | 0     | 1     | 0     | 1     |
         | timestamp           |       |       |       |       |
         | ------------------- | ----- | ----- | ----- | ----- |
         | 2025-01-01 10:00:00 | 100.0 | 100.0 | 100.0 | 100.0 |
         | 2025-01-01 10:00:00 | 100.0 | 100.0 | 100.0 | 100.0 |
         | ...                 | ...   | ...   | ...   | ...   |

        Args:
            output_file (string): path to output CSV file
            metrics (list): list of metrics to export
            pivot_labels (list): list of labels to use for hierarchical indexing
        """

        index = ["timestamp"] + pivot_labels

        metric_dfs = []
        for metric in metrics:
            query = "%s * on (instance) group_left() (max by (instance) (rmsjob_info{$job,$step}))" % (metric)
            metric_data = self.query_job_range(query)

            if len(metric_data) == 0:
                continue

            metric_df = MetricRangeDataFrame(metric_data)

            # Discard additional labels some metrics may have, like "location"
            # in "rocm_temperature_celsius".
            metric_df = metric_df.reset_index()
            extra_labels = set(metric_df.columns) - set(index) - {"value"}
            for label in extra_labels:
                metric_df = metric_df.drop(label, axis=1)

            metric_df = metric_df.rename(columns={"value": metric})
            metric_df = metric_df.set_index(index, drop=True)
            metric_df = metric_df.unstack(pivot_labels)
            metric_df.columns = metric_df.columns.set_names("metric", level=0)
            metric_df = metric_df.sort_index(axis=1)

            metric_dfs.append(metric_df)

        if len(metric_dfs) > 0:
            df = pandas.concat(metric_dfs, axis=1)
            df.to_csv(output_file)

    def export(self, export_path):
        export_prefix = "omnistat-"

        # List files to be generated for different subsets of metrics. Values
        # are tuples containing 1) file name, 2) a list of metrics, and 3) a
        # list of labels to be used for hierarchical indexing.
        exports = [
            (
                "rocm",
                [x["metric"] for x in QueryMetrics.METRICS],
                ["instance", "card"],
            ),
            (
                "network",
                ["omnistat_network_rx_bytes", "omnistat_network_tx_bytes"],
                ["instance", "device_class", "interface"],
            ),
            (
                "rocprofiler",
                ["omnistat_rocprofiler"],
                ["instance", "card", "counter"],
            ),
            (
                "fom",
                ["omnistat_fom"],
                ["instance", "name"],
            ),
            (
                "vendor",
                [
                    "omnistat_vendor_energy_joules",
                    "omnistat_vendor_memory_energy_joules",
                    "omnistat_vendor_cpu_energy_joules",
                    "omnistat_vendor_power_watts",
                    "omnistat_vendor_memory_power_watts",
                    "omnistat_vendor_cpu_power_watts",
                ],
                ["instance", "vendor"],
            ),
            (
                "vendor",
                ["omnistat_vendor_accel_energy_joules", " omnistat_vendor_accel_power_watts"],
                ["instance", "accel", "vendor"],
            ),
        ]

        for name, metrics, labels in exports:
            extension = ".csv"
            if "card" in labels or "accel" in labels:
                extension = ".gpu.csv"
            export_file = f"{export_path}/{export_prefix}{name}{extension}"
            self.export_metrics(export_file, metrics, labels)


def main():

    # command line args (jobID is required)
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--version", help="print version info and exit", action="store_true")
    parser.add_argument("--job", help="job ID to query", required=True)
    parser.add_argument("--step", help="job step ID to restrict query range")
    parser.add_argument("--marker", help="annotation marker string to restrict query range")
    parser.add_argument("--interval", help="sampling interval in seconds (default=30)", type=float, default=30)
    parser.add_argument("--configfile", help="Omnistat configuration file")
    parser.add_argument("--output", help="redirect plain text report to existing file")
    parser.add_argument("--pdf", help="generate PDF report")
    parser.add_argument("--export", help="export metric time-series in CSV format", nargs="?", default=None, const="./")
    args = parser.parse_args()

    # logger config
    logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stdout)

    if args.version:
        version = utils.getVersion()
        utils.displayVersion(version)
        sys.exit(0)

    query = QueryMetrics(args.interval, args.job, args.step, args.marker, args.configfile, args.output)
    query.find_job_info()
    query.gather_data(saveTimeSeries=True)
    query.gather_vendor_data()
    query.generate_report_card()

    if args.pdf:
        query.generate_pdf(args.pdf)

    if args.export:
        export_path = Path(args.export)
        if export_path.exists() and not export_path.is_dir():
            utils.error(f"--export argument should be be an existing or new directory directory")

        export_path.mkdir(exist_ok=True)
        query.export(export_path)


if __name__ == "__main__":
    main()
