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

Implements a number of prometheus gauge metrics to track host-level utilization
including CPU usage, memory usage, and I/O stats. The following highlights example
metrics:

omnistat_host_mem_total_bytes 5.40314181632e+011
omnistat_host_mem_free_bytes 5.23302158336e+011
omnistat_host_mem_available_bytes 5.22178281472e+011
omnistat_host_io_read_total_bytes 7.794688e+06
omnistat_host_io_write_total_bytes 45056.0
omnistat_host_io_read_local_total_bytes 0.0
omnistat_host_io_write_local_total_bytes 0.0
omnistat_host_cpu_aggregate_core_utilization 1.9104
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
import threading
import time
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

        # CPU sampling thread state
        self.__cpu_lock = threading.Lock()
        self.__cpu_delta_idle = 0
        self.__cpu_delta_total = 0
        self.__sampler_running = False
        self.__sampler_thread = None
        self.__cpu_load_sampling_interval = 0.02
        self.__enable_proc_io_stats = False

        # runtime config parsing
        if config.has_section("omnistat.collectors.host"):
            self.__cpu_load_sampling_interval = float(
                config["omnistat.collectors.host"].get("cpu_load_sampling_interval", 0.05)
            )
            self.__enable_proc_io_stats = config["omnistat.collectors.host"].getboolean("enable_proc_io_stats", False)

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
            {"metricName": "io_read_local_total_bytes",  "description": "Total bytes read from local physical disks",  "enable": True},
            {"metricName": "io_write_local_total_bytes", "description": "Total bytes written to local physical disks", "enable": True},
            {"metricName": "io_read_total_bytes",        "description": "Total bytes read by visible processes (includes network I/O)",    "enable": self.__enable_proc_io_stats},
            {"metricName": "io_write_total_bytes",       "description": "Total bytes written by visible processes (includes network I/O)", "enable": self.__enable_proc_io_stats},
        ]
        # fmt: on

        # verify /proc/<pid>/io file format
        if self.__enable_proc_io_stats:
            desired_stats = ["rchar", "wchar"]
            with open("/proc/self/io", "r") as f:
                for entry in desired_stats:
                    line = f.readline()
                    if not line.startswith(f"{entry}:"):
                        logging.error(f"--> [ERROR] Unexpected {entry} format in /proc/<pid>/io ({line.strip()})")
                        sys.exit(4)
                    if len(line.split()) < 2:
                        logging.error(f"--> [ERROR] Unexpected {entry} line in /proc/<pid>/io ({line.strip()})")
                        sys.exit(4)

        for item in self.__user_io_metrics:
            if item["enable"]:
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
            {"metricName": "cpu_aggregate_core_utilization", "description": "Instantaneous number of busy CPU cores (0..num_logical_cores)"},
        ]
        # fmt: on

        # verify /proc/loadavg exists with expected entries
        try:
            with open("/proc/loadavg", "r") as f:
                content = f.read().strip()
        except:
            logging.error("--> [ERROR] /proc/loadavg not found")
            sys.exit(4)
        if len(content.split()) < 3:
            logging.error(f"--> [ERROR] Unexpected entry in /proc/loadavg: {content.strip()}")
            sys.exit(4)

        # verify /proc/stat exists with expected entries
        try:
            with open("/proc/stat", "r") as f:
                first = f.readline()
        except:
            logging.error("--> [ERROR] /proc/stat not found")
            sys.exit(4)
        if not first.startswith("cpu"):
            logging.error(f"--> [ERROR] Unexpected entry in /proc/stat: {first.strip()}")
            sys.exit(4)
        if len(first.split()) < 9:
            logging.error(f"--> [ERROR] Unexpected entry in /proc/stat: {first.strip()}")
            sys.exit(4)

        for item in self.__loadavg_metrics:
            metric = item["metricName"]
            description = item["description"]
            self.__metrics[metric] = Gauge(self.__prefix + metric, description)
            logging.info("--> [registered] %s (gauge)" % (self.__prefix + metric))

        phys_cnt, logical_cnt = self.get_cpu_counts()
        self.__metrics["cpu_num_physical_cores"].set(phys_cnt)
        self.__metrics["cpu_num_logical_cores"].set(logical_cnt)
        self.__logical_cpu_count = logical_cnt

        # Check for elevated /proc access: if we can read /proc/1/io, assume we have elevated read access
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

        # Cache current UID for fast process filtering
        try:
            self.__current_uid = os.geteuid()
        except AttributeError:
            self.__current_uid = None

        # Initiate background CPU sampler thread
        self.__sampler_running = True
        self.__sampler_thread = threading.Thread(
            target=self.cpu_load_sampler,
            args=(self.__cpu_load_sampling_interval,),
            daemon=True,
            name="CPU load sampler",
        )
        self.__sampler_thread.start()
        logging.info(
            f"--> initiated background CPU load sampling thread (interval: {self.__cpu_load_sampling_interval} sec)"
        )

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

        read_total_local, write_total_local = self.read_local_disk_io()
        self.__metrics["io_read_local_total_bytes"].set(read_total_local)
        self.__metrics["io_write_local_total_bytes"].set(write_total_local)

        if self.__enable_proc_io_stats:
            # sum per-user process I/O metrics
            read_total, write_total = self.read_user_proc_io()
            self.__metrics["io_read_total_bytes"].set(read_total)
            self.__metrics["io_write_total_bytes"].set(write_total)

        # --
        # CPU/load metrics
        # --

        load_averages = self.read_loadavg()
        self.__metrics["cpu_load1"].set(load_averages[0])
        self.__metrics["cpu_load5"].set(load_averages[1])
        self.__metrics["cpu_load15"].set(load_averages[2])

        # Instantaneous CPU usage via background sampler
        delta_idle, delta_total = self.read_cpu_times()
        if delta_total > 0 and delta_idle >= 0:
            busy = delta_total - delta_idle
            usage_ratio = busy / delta_total
            # Scale by number of logical cores to get a number of busy cores metric
            busy_cores = usage_ratio * self.__logical_cpu_count
            self.__metrics["cpu_aggregate_core_utilization"].set(round(busy_cores, 4))

        return

    def read_meminfo(self, num_lines):
        """Read and parse the top num_lines of /proc/meminfo
        Args:
            num_lines (int): Number of lines to read from /proc/meminfo

        Returns:
            list: List of memory values in bytes corresponding to the requested lines."""
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
        """Sum rchar and wchar from /proc/<pid>/io.

        Parses rchar/wchar to track total bytes read/written via syscalls to capture all I/O
        including network filesystems (WekaFS, NFS, Lustre) which bypass the block layer.
        This includes buffered I/O, direct I/O, and network filesystem operations.

        Returns:
            int: (read_bytes, write_bytes)
        """
        read_total = 0
        write_total = 0

        try:
            # Get numeric PIDs >= 1000
            proc_entries = os.listdir("/proc")
            pids = sorted([int(p) for p in proc_entries if p.isdigit() and int(p) >= 1000])
        except:
            return 0, 0

        for pid in pids:
            proc_dir = f"/proc/{pid}"
            try:

                # Perm checks
                stat_info = os.stat(proc_dir)
                proc_uid = stat_info.st_uid

                if self.__filter_root_processes:
                    if proc_uid == 0:
                        continue
                else:
                    if self.__current_uid is not None and proc_uid != self.__current_uid:
                        continue

                # Read first two lines to parse rchar and wchar values
                with open(f"{proc_dir}/io", "r") as f:
                    rchar_line = f.readline()  # Line 1: rchar: <value>
                    wchar_line = f.readline()  # Line 2: wchar: <value>
                    read_total += int(rchar_line.split(":", 1)[1])
                    write_total += int(wchar_line.split(":", 1)[1])
            except:
                # Process disappeared or permission denied
                continue

        return read_total, write_total

    def read_local_disk_io(self):
        """Read /proc/diskstats and return total read and write bytes across all local physical disks.

        Sums I/O for whole physical disks only (e.g., sda, nvme0n1), excluding:
        - Partitions (e.g., sda1, nvme0n1p1) to avoid double-counting
        - Virtual devices (loop, ram, dm-, md, sr, zram)

        Returns:
            int: (read_bytes, write_bytes)
        """
        read_bytes = 0
        write_bytes = 0

        try:
            with open("/proc/diskstats", "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 14:
                        continue

                    dev_name = parts[2]

                    # Skip virtual/pseudo devices and partitions
                    if (
                        dev_name.startswith(("loop", "ram", "dm-", "md", "sr", "zram")) or dev_name[-1].isdigit()
                    ):  # Skip partitions (sda1, nvme0n1p1)
                        continue

                    # parts[5] is sectors read, parts[9] is sectors written
                    # /proc/diskstats always reports in 512-byte sectors regardless of physical sector size
                    sectors_read = int(parts[5])
                    sectors_written = int(parts[9])
                    read_bytes += sectors_read * 512
                    write_bytes += sectors_written * 512
        except Exception as e:
            return (0, 0)

        return (read_bytes, write_bytes)

    def read_loadavg(self):
        """Read /proc/loadavg and return the (1, 5, 15) minute load averages.

        Returns:
            int: [load1, load5, load15]
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
            int: (physical_cores, logical_cores)
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

    def read_cpu_times(self):
        """Return cached CPU stats from background sampling thread.

        Returns:
            int: (change_in_idle_cpu_jiffies, change_in_total_cpu_jiffies)
        """
        with self.__cpu_lock:
            return (self.__cpu_delta_idle, self.__cpu_delta_total)

    def cpu_load_sampler(self, sample_interval: float):
        """Background thread to sample /proc/stat CPU times at regular intervals.

        Args:
            sample_interval (float, optional): Time in seconds between samples.
        """

        # Prime with first sample
        prev_idle, prev_total = self.sample_cpu_stats()

        # Sampling loop
        while self.__sampler_running:
            time.sleep(sample_interval)

            current_idle, current_total = self.sample_cpu_stats()
            with self.__cpu_lock:
                self.__cpu_delta_idle = current_idle - prev_idle
                self.__cpu_delta_total = current_total - prev_total
            prev_idle, prev_total = current_idle, current_total

    def sample_cpu_stats(self):
        """Read instantaneous CPU stats from first line of /proc/stat

        Returns:
            int: (idle_cpu_jiffies, total_cpu_jiffies)
        """

        # Note: see https://www.kernel.org/doc/html/latest/filesystems/proc.html for /proc/stat format
        # First 8 entries we care about are: user, nice, system, idle, iowait, irq, softirq, steal
        try:
            with open("/proc/stat", "r") as f:
                first_line = f.readline()
            values = [int(v) for v in first_line.split()[1:]]
            idle = values[3] + values[4]  # idle + iowait
            total = sum(values[:8])  # user..steal
            return (idle, total)
        except Exception:
            return (0, 0)
