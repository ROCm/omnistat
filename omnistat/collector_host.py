# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2025 Advanced Micro Devices, Inc. All Rights Reserved.
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

"""Host-level monitoring

Implements a number of prometheus gauge metrics based to track host-level utilization
including CPU usage, memory usage, and I/O stats. The following highlights example
metrics:

omnistat_host_mem_total_bytes 5.40314181632e+011
omnistat_host_mem_free_bytes 5.23302158336e+011
omnistat_host_mem_available_bytes 5.22178281472e+011
omnistat_host_io_read_bytes_total 7.794688e+06
omnistat_host_io_write_bytes_total 45056.0
omnistat_host_cpu_load1 3.08
omnistat_host_cpu_load5 3.05
omnistat_host_cpu_load15 3.06
omnistat_host_cpu_num_physical_cores 128.0
omnistat_host_cpu_num_logical_cores 128.0
"""

import configparser
import logging
import os
import platform
import sys
from pathlib import Path

from prometheus_client import Counter, Gauge

import omnistat.utils as utils
from omnistat.collector_base import Collector


class HOST(Collector):
    def __init__(self, config: configparser.ConfigParser):
        """Initialize the HOST data collector.

        Args:
            config (configparser.ConfigParser): Cached copy of runtime configuration.
        """
        logging.debug(f"Initializing {self.__class__.__name__} data collector")

        self.__prefix = "omnistat_host_"
        self.__metrics = {}

    def registerMetrics(self):
        """Register metrics of interest"""

        # --
        # Memory oriented metrics
        # --

        # fmt: off
        self.__mem_metrics = [
            {"entry":"MemTotal",    "index":0,"metricName":"mem_total_bytes","description":"Total memory available on the host in bytes"},
            {"entry":"MemFree",     "index":1,"metricName":"mem_free_bytes","description":"Free memory available on the host in bytes"},
            {"entry":"MemAvailable","index":2,"metricName":"mem_available_bytes","description":"Available memory on the host in bytes"},
        ]
        # fmt: on
        self.__max_mem_metric_index = max(item["index"] for item in self.__mem_metrics)

        # verify /proc/meminfo exists
        if not os.path.exists("/proc/meminfo"):
            logging.error("--> [ERROR] /proc/meminfo not found")
            sys.exit(4)
            return

        # verify metric ordering expectations
        with open("/proc/meminfo", "r") as f:
            for item in self.__mem_metrics:
                line = f.readline()
                # Check if the memory entry matches expected
                if not line.startswith(item["entry"] + ":"):
                    logging.error(f"--> [ERROR] Expected {item['entry']} in /proc/meminfo but found: {line.strip()}")
                    sys.exit(4)
                else:
                    logging.debug(f"--> verified {item['entry']} in /proc/meminfo")

        for item in self.__mem_metrics:
            metric = item["metricName"]
            description = item["description"]
            self.__metrics[metric] = Gauge(self.__prefix + metric, description)
            logging.info("--> [registered] %s (gauge)" % (self.__prefix + metric))

        # --
        # I/O oriented metrics
        # --

        # fmt: off
        self.__user_io_metrics = [
            {"metricName": "io_read_bytes_total",  "description": "Total bytes read by all processes owned by this user"},
            {"metricName": "io_write_bytes_total", "description": "Total bytes written by all processes owned by this user"},
        ]
        # fmt: on

        for item in self.__user_io_metrics:
            metric = item["metricName"]
            description = item["description"]
            self.__metrics[metric] = Gauge(self.__prefix + metric, description)
            logging.info("--> [registered] %s (gauge)" % (self.__prefix + metric))

        # --
        # CPU oriented metrics
        # --

        # fmt: off
        self.__loadavg_metrics = [
            {"metricName": "cpu_load1",  "description": "1-minute load average"},
            {"metricName": "cpu_load5",  "description": "5-minute load average"},
            {"metricName": "cpu_load15", "description": "15-minute load average"},
            {"metricName": "cpu_num_physical_cores", "description": "Number of physical CPU cores"},
            {"metricName": "cpu_num_logical_cores", "description": "Number of logical CPU cores"},
        ]
        # fmt: on

        # verify /proc/loadavg exists
        if not os.path.exists("/proc/loadavg"):
            logging.error("--> [ERROR] /proc/loadavg not found")
            sys.exit(4)
            return

        # verify expected entries in /proc/loadavg
        with open("/proc/loadavg", "r") as f:
            content = f.read().strip()
        parts = content.split()
        if len(parts) < 3:
            logging.error(f"--> [ERROR] Unexpected entry in /proc/loadavg: {parts}")
            sys.exit(4)

        for item in self.__loadavg_metrics:
            metric = item["metricName"]
            description = item["description"]
            self.__metrics[metric] = Gauge(self.__prefix + metric, description)
            logging.info("--> [registered] %s (gauge)" % (self.__prefix + metric))

        phys_cnt, logical_cnt = self.get_cpu_counts()
        self.__metrics["cpu_num_physical_cores"].set(phys_cnt)
        self.__metrics["cpu_num_logical_cores"].set(logical_cnt)

        # Check for elevated /proc access: ef we can read /proc/1/io, assume we have elevated read access
        # and will explicitly filter out root-owned processes when aggregating per-process I/O (typically in system-mode).
        # Otherwise we just report on user-owned processes (user-mode)
        self.__filter_root_processes = False
        try:
            with open("/proc/1/io", "rb") as _f:
                _ = _f.read(1)
            self.__filter_root_processes = True
            logging.info("--> elevated /proc access detected; will filter out root-owned processes")
        except PermissionError:
            self.__filter_root_processes = False
            logging.debug("--> standard /proc access; will rely on permissions to limit visibility")

    def updateMetrics(self):
        """Update registered metrics of interest"""

        # --
        # Memory oriented metrics
        # --

        mem_info = self.read_meminfo(self.__max_mem_metric_index + 1)

        for item in self.__mem_metrics:
            metric = item["metricName"]
            index = item["index"]
            if metric in self.__metrics and index < len(mem_info):
                self.__metrics[metric].set(mem_info[index])

        # --
        # I/O oriented metrics
        # --

        # sum per-user process I/O metrics
        read_total, write_total = self.read_user_proc_io()
        self.__metrics["io_read_bytes_total"].set(read_total)
        self.__metrics["io_write_bytes_total"].set(write_total)

        # --
        # CPU/load metrics
        # --

        load_averages = self.read_loadavg()
        self.__metrics["cpu_load1"].set(load_averages[0])
        self.__metrics["cpu_load5"].set(load_averages[1])
        self.__metrics["cpu_load15"].set(load_averages[2])

        return

    def read_meminfo(self, num_lines):
        """Read and parse the top num_lines of /proc/meminfo"""
        mem_info = []

        try:
            with open("/proc/meminfo", "r") as f:
                for _ in range(num_lines):
                    line = f.readline()
                    value = line.split()[1]
                    mem_info.append(int(value) * 1024)  # Convert kB to bytes

        except Exception as e:
            logging.warning(f"Failed reading /proc/meminfo: {e}")

        return mem_info

    def read_user_proc_io(self):
        """Sum read_bytes and write_bytes from /proc/<pid>/io.

        In user-mode, rely on kernel permissions and only include processes we can read. In elevated 
        system mode: explicitly filter out root-owned (UID 0) processes before summing.
        """
        import os

        read_total = 0
        write_total = 0

        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit() and int(p) >= 1000]
        except OSError:
            return 0, 0

        for pid in pids:
            try:
                if self.__filter_root_processes:
                    # Read minimal status to get UID and skip root-owned processes
                    with open(f"/proc/{pid}/status", "rb") as f:
                        status_data = f.read(256)
                        status_str = status_data.decode("ascii", errors="ignore")
                        uid_idx = status_str.find("Uid:")
                        if uid_idx != -1:
                            uid_line_end = status_str.find("\n", uid_idx)
                            uid_fields = status_str[uid_idx:uid_line_end].split()
                            proc_uid = int(uid_fields[1])
                            if proc_uid == 0:
                                continue

                # Cull io stats
                with open(f"/proc/{pid}/io", "rb") as f:
                    io_data = f.read(512)
                io_str = io_data.decode("ascii", errors="ignore")

                rb_idx = io_str.find("read_bytes:")
                if rb_idx != -1:
                    rb_end = io_str.find("\n", rb_idx)
                    read_total += int(io_str[rb_idx + 11 : rb_end].strip())

                wb_idx = io_str.find("write_bytes:")
                if wb_idx != -1:
                    wb_end = io_str.find("\n", wb_idx)
                    write_total += int(io_str[wb_idx + 12 : wb_end].strip())

            except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
                continue 
            except Exception:
                continue

        return read_total, write_total

    def read_loadavg(self):
        """Read /proc/loadavg and return the (1, 5, 15) minute load averages.
        Returns None on failure.
        """
        try:
            with open("/proc/loadavg", "r") as f:
                content = f.read().strip()
            parts = content.split()
            return parts[:3]
        except Exception as e:
            logging.debug(f"Failed reading /proc/loadavg: {e}")
        return None

    def get_cpu_counts(self):
        """Determine physical core count and logical CPU count.

        Returns:
            tuple: (physical_cores, logical_cores)
        """
        # Logical CPUs
        logical = os.cpu_count() or 0

        # Physical cores determined via sysfs topology
        physical = 0
        try:
            core_pairs = set()
            base = "/sys/devices/system/cpu"
            for entry in os.listdir(base):
                if entry.startswith("cpu") and entry[3:].isdigit():
                    topo_dir = os.path.join(base, entry, "topology")
                    try:
                        with open(os.path.join(topo_dir, "core_id"), "r") as f:
                            core_id = f.read().strip()
                        with open(os.path.join(topo_dir, "physical_package_id"), "r") as f:
                            pkg_id = f.read().strip()
                        core_pairs.add((pkg_id, core_id))
                    except (FileNotFoundError, PermissionError):
                        continue
            physical = len(core_pairs) if core_pairs else logical
        except Exception:
            physical = logical

        return (physical, logical)
