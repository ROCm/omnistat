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
import concurrent.futures
import importlib.resources
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

from omnistat import utils


class UserBasedMonitoring:
    def __init__(self):
        logging.basicConfig(format="%(message)s", level=logging.INFO, stream=sys.stdout)
        self.scrape_interval = 10  # default scrape interval in seconds
        self.timeout = 5  # default scrape timeout in seconds
        self.__hosts = None
        self.__RMS_Detected = False
        self.__external_proxy = None
        self.__victoriaModeSetup_initialized = False

    def setup(self, configFileArgument):
        self.configFile = utils.findConfigFile(configFileArgument)
        self.runtimeConfig = utils.readConfig(self.configFile)

        # Path to Omnistat's executable scripts. For source deployments, this
        # is the top directory of a working copy of Omnistat. For package
        # deployments, it's the `bin` directory of the Python environment. By
        # resolving the path to `sys.argv[0]`, we ensure commands executed in
        # other nodes/environments use the same deployment and path as the
        # current local execution.
        self.binDir = Path(sys.argv[0]).resolve().parent

    def setMonitoringInterval(self, interval):
        self.scrape_interval = float(interval)
        return

    def setPushFrequency(self, pushfreq):
        self.push_frequency = float(pushfreq)
        return

    def rmsDetection(self):
        """Query environment to infer resource manager"""

        if self.__RMS_Detected:
            return

        if "SLURM_JOB_NODELIST" in os.environ:
            self.__rms = "slurm"
        elif "FLUX_URI" in os.environ:
            self.__rms = "flux"
        elif "PBS_JOBID" in os.environ:
            self.__rms = "pbs"
        else:
            utils.error("Unknown/unsupported resource manager")
        logging.info("RMS detected = %s" % self.__rms)

        self.getRMSHosts()
        self.__RMS_Detected = True

        return

    def victoriaModeSetup(self):
        """
        Initialize VictoraMetrics settings and cache for use by startserver and
        startexporter methods.
        """
        if self.__victoriaModeSetup_initialized:
            logging.debug("Skipping VictoriaMetrics setup - already initialized")
            return

        section = "omnistat.usermode"

        self.__external_victoria = self.runtimeConfig[section].getboolean("external_victoria", False)
        if self.__external_victoria:
            logging.info("External VictoriaMetrics server requested")
            self.__external_victoria_endpoint = self.runtimeConfig[section].get("external_victoria_endpoint")
            self.__external_victoria_port = self.runtimeConfig[section].get("external_victoria_port")
            logging.info("--> external host = %s" % self.__external_victoria_endpoint)
            logging.info("--> external port = %s" % self.__external_victoria_port)

            if self.runtimeConfig.has_option(section, "external_proxy"):
                self.__external_proxy = self.runtimeConfig[section].get("external_proxy")
                logging.info("--> external proxy = %s" % self.__external_proxy)
        else:
            logging.info("Local VictoriaMetrics server requested")

        self.__victoriaModeSetup_initialized = True

        return

    def disableProxies(self):
        """
        Used to disable any inherited proxies in cases where expected communication
        is only local between compute nodes. Necessary to avoid problems imposed by users
        who have proxy settings in their runtime environment to access the outside world.
        """

        os.environ.pop("http_proxy", None)
        os.environ.pop("https_proxy", None)
        os.environ.pop("all_proxy", None)

        return

    def getRMSHosts(self):
        if self.__rms == "slurm":
            hostlist = os.getenv("SLURM_JOB_NODELIST", None)
            if hostlist:
                results = utils.runShellCommand(["scontrol", "show", "hostname", hostlist], timeout=10)
                if results.stdout.strip():
                    self.__hosts = results.stdout.splitlines()
                    # return results.stdout.splitlines()
                    return
                else:
                    utils.error("Unable to detect assigned SLURM hosts from %s" % hostlist)
            else:
                logging.warning(
                    "\nNo SLURM_JOB_NODELIST var detected - please verify running under active SLURM job.\n"
                )
        elif self.__rms == "flux":
            results = utils.runShellCommand(["flux", "hostlist", "local", "-e", "-d", ","])
            if results.stdout.strip():
                self.__hosts = results.stdout.strip().split(",")
                return
            else:
                utils.error("Unable to detect assigned Flux hosts.")
        elif self.__rms == "pbs":
            nodefile = os.environ.get("PBS_NODEFILE")
            with open(nodefile) as f:
                nodes_uniq = {line.strip() for line in f}
            self.__hosts = [name.split(".")[0] for name in nodes_uniq]
            return
        else:
            utils.error("Unsupported RMS.")

    def startVictoriaServer(self):

        section = "omnistat.usermode"

        self.victoriaModeSetup()

        # noop if using an external server
        if self.__external_victoria:
            logging.info("Pushing data to external VictoriaMetrics server")
            return
        else:
            logging.info("Starting VictoriaMetrics server on localhost")

        vm_binary = self.runtimeConfig[section].get("victoria_binary")

        if not os.path.exists(vm_binary):
            logging.error("")
            logging.error("[ERROR]: Unable to resolve path to VictoriaMetrics binary -> %s" % vm_binary)
            logging.error('[ERROR]: Please verify setting for "victoria_binary" in runtime configfile.')
            sys.exit(1)

        # datadir can be overridden by separate env variable
        if "OMNISTAT_VICTORIA_DATADIR" in os.environ:
            vm_datadir = os.getenv("OMNISTAT_VICTORIA_DATADIR")
        elif "OMNISTAT_VICSERVER_DATADIR" in os.environ:
            vm_datadir = os.getenv("OMNISTAT_VICSERVER_DATADIR")
            logging.warning(
                "OMNISTAT_VICSERVER_DATADIR variable is being deprecated - please use OMNISTAT_VICTORIA_DATADIR instead"
            )
        else:
            vm_datadir = self.runtimeConfig[section].get("victoria_datadir", "data_prom", vars=os.environ)

        vm_logfile = self.runtimeConfig[section].get("victoria_logfile", "victoria_server.log")
        vm_corebinding = self.runtimeConfig[section].getint("victoria_corebinding", None)
        # corebinding can also be overridden by separate env variable
        if "OMNISTAT_VICTORIA_COREBINDING" in os.environ:
            vm_corebinding = int(os.getenv("OMNISTAT_VICTORIA_COREBINDING"))
            logging.info("--> Overriding corebinding setting using OMNISTAT_VICTORIA_COREBINDING=%i" % vm_corebinding)

        command = [
            vm_binary,
            "--storageDataPath=%s" % vm_datadir,
            "-memory.allowedPercent=10",
            "-retentionPeriod=10y",
            "-search.disableCache",
            "-search.latencyOffset=0",
            "-search.maxPointsPerTimeseries=90000",
            "-httpListenAddr=:9090",
        ]
        envAddition = {}
        # restrict thread usage
        envAddition["GOMAXPROCS"] = "4"

        # optional NUMA setup
        numa_command = self.verifyNumaCommand(vm_corebinding)
        if numa_command:
            command = numa_command + command
        else:
            logging.info("Skipping VictoriaMetrics corebinding")

        logging.info("Server start command: %s" % command)
        utils.runBGProcess(command, outputFile=vm_logfile, envAdds=envAddition)

    def startPromServer(self, victoriaMode=True):

        if victoriaMode:
            self.startVictoriaServer()
            return

        self.rmsDetection()

        logging.info("Starting prometheus server on localhost")
        if self.scrape_interval >= 1:
            scrape_interval = "%ss" % int(self.scrape_interval)
        else:
            scrape_interval = "0s%sms" % int(self.scrape_interval * 1000)
        logging.info("--> sampling interval = %s" % scrape_interval)

        if self.timeout < self.scrape_interval:
            scrape_timeout = "5s"
        else:
            scrape_timeout = scrape_interval

        section = "omnistat.usermode"
        ps_binary = self.runtimeConfig[section].get("prometheus_binary")
        ps_datadir = self.runtimeConfig[section].get("prometheus_datadir", "data_prom", vars=os.environ)

        # datadir can be overridden by separate env variable
        if "OMNISTAT_PROMSERVER_DATADIR" in os.environ:
            ps_datadir = os.getenv("OMNISTAT_PROMSERVER_DATADIR")

        ps_logfile = self.runtimeConfig[section].get("prometheus_logfile", "prom_server.log")
        ps_corebinding = self.runtimeConfig[section].getint("prometheus_corebinding", None)
        # corebinding can also be overridden by separate env variable
        if "OMNISTAT_PROMSERVER_COREBINDING" in os.environ:
            ps_corebinding = int(os.getenv("OMNISTAT_PROMSERVER_COREBINDING"))

        # generate prometheus config file to scrape local exporters
        computes = {}
        computes["targets"] = []
        port = self.runtimeConfig["omnistat.collectors"].get("port", "8001")
        if self.__hosts:
            for host in self.__hosts:
                computes["targets"].append("%s:%s" % (host, port))

            prom_config = {}
            prom_config["scrape_configs"] = []
            prom_config["scrape_configs"].append(
                {
                    "job_name": "omnistat",
                    "scrape_interval": scrape_interval,
                    "scrape_timeout": scrape_timeout,
                    "static_configs": [computes],
                }
            )

            with open("prometheus.yml", "w") as yaml_file:
                yaml.dump(prom_config, yaml_file, sort_keys=False)

            command = [
                ps_binary,
                "--config.file=%s" % "prometheus.yml",
                "--storage.tsdb.path=%s" % ps_datadir,
            ]

            # optional NUMA setup
            numa_command = self.verifyNumaCommand(ps_corebinding)
            if numa_command:
                command = numa_command + command
            else:
                logging.info("Skipping Prometheus corebinding")

            logging.debug("Server start command: %s" % command)
            utils.runBGProcess(command, outputFile=ps_logfile)
        else:
            utils.error("No compute hosts avail for startPromServer")

    def stopPromServer(self, victoriaMode=False):
        if victoriaMode:
            logging.info("Stopping VictoriaMetrics server on localhost")

            command = ["pkill", "-f", "-SIGTERM", "-u", "%s" % os.getuid(), "victoria-metrics.*storageDataPath"]
            utils.runShellCommand(command, timeout=5)
            time.sleep(1)
            return
        else:
            logging.info("Stopping prometheus server on localhost")

            command = ["pkill", "-SIGTERM", "-u", "%s" % os.getuid(), "prometheus"]

            utils.runShellCommand(command, timeout=5)
            time.sleep(1)
            return

    def startExporters(self, victoriaMode=False):
        port = self.runtimeConfig["omnistat.collectors"].get("port", "8001")
        corebinding = self.runtimeConfig["omnistat.usermode"].getint("exporter_corebinding", None)

        self.rmsDetection()
        self.disableProxies()
        self.victoriaModeSetup()

        if victoriaMode:
            if os.path.exists("./exporter.log"):
                os.remove("./exporter.log")
            logging.info("[exporter]: Standalone sampling interval = %s" % self.scrape_interval)
            hostname = platform.node().split(".", 1)[0]

            if self.__external_victoria:
                cmd = f"nice -n 20 {sys.executable} -m omnistat.standalone --configfile={self.configFile} --interval {self.scrape_interval} --pushinterval {self.push_frequency} --endpoint {self.__external_victoria_endpoint} --port {self.__external_victoria_port} --log exporter.log"
            else:
                cmd = f"nice -n 20 {sys.executable} -m omnistat.standalone --configfile={self.configFile} --interval {self.scrape_interval} --pushinterval {self.push_frequency} --endpoint {hostname} --log exporter.log"
        else:
            cmd = f"nice -n 20 {sys.executable} -m omnistat.node_monitoring --configfile={self.configFile}"

        logging.debug("[exporter]: %s" % cmd)

        if "OMNISTAT_EXPORTER_COREBINDING" in os.environ:
            corebinding = int(os.getenv("OMNISTAT_EXPORTER_COREBINDING"))
            logging.info(
                "[exporter]: Overriding corebinding setting using OMNISTAT_EXPORTER_COREBINDING=%i" % corebinding
            )

        # Assume environment is the same across nodes; if numactl is present
        # here, we expect it to be present on all nodes.
        numa_command = self.verifyNumaCommand(corebinding)
        if numa_command:
            numa_command_string = " ".join(numa_command)
            cmd = f"{numa_command_string} {cmd}"
        else:
            logging.info("[exporter]: Skipping exporter corebinding")

        if self.__hosts:
            logging.info("[exporter]: Saving RMS job state locally to compute hosts...")
            detection_file = self.runtimeConfig["omnistat.collectors.rms"].get(
                "job_detection_file", "/tmp/omni_rmsjobinfo"
            )
            if self.__rms == "slurm":
                numNodes = os.getenv("SLURM_JOB_NUM_NODES")
                srun_cmd = [
                    "srun",
                    "-N %s" % numNodes,
                    "--ntasks-per-node=1",
                    "%s" % sys.executable,
                    "%s/omnistat-rms-env" % self.binDir,
                    "--nostep",
                    "%s" % detection_file,
                ]
                utils.runShellCommand(srun_cmd, timeout=35, exit_on_error=True)
            elif self.__rms == "flux":
                flux_cmd = [
                    "flux",
                    "exec",
                    "%s" % sys.executable,
                    "%s/omnistat-rms-env" % self.binDir,
                    "--nostep",
                    "%s" % detection_file,
                ]
                utils.runShellCommand(flux_cmd, timeout=35, exit_on_error=True)
            elif self.__rms == "pbs":
                # cache pbs vars needed for rms-env query
                pbs_vars = (
                    f"PBS_JOBID={os.getenv('PBS_JOBID')} "
                    f"PBS_O_LOGNAME={os.getenv('PBS_O_LOGNAME')} "
                    f"PBS_QUEUE={os.getenv('PBS_QUEUE')} "
                    f"PBS_NODEFILE={os.getenv('PBS_NODEFILE')} "
                    f"PBS_ENVIRONMENT={os.getenv('PBS_ENVIRONMENT')}"
                )

                results = utils.execute_ssh_parallel(
                    command=f"sh -c 'cd {os.getcwd()} && PYTHONPATH={':'.join(sys.path)} {pbs_vars} {sys.executable} {self.binDir}/omnistat-rms-env --nostep {detection_file}'",
                    hostnames=self.__hosts,
                    max_concurrent=128,
                    ssh_timeout=15,
                    max_retries=2,
                    retry_delay=5,
                )
                time.sleep(1)

            logging.info("Launching exporters in parallel via ssh")

            additional_env = ""
            if self.__external_proxy:
                additional_env = f"http_proxy={self.__external_proxy}"

            # trying local ssh client implementation
            launch_results = utils.execute_ssh_parallel(
                command=f"sh -c 'cd {os.getcwd()} && LD_LIBRARY_PATH={os.getenv('LD_LIBRARY_PATH')} PYTHONPATH={':'.join(sys.path)} {additional_env} {cmd}'",
                hostnames=self.__hosts,
                max_concurrent=128,
                ssh_timeout=100,
                max_retries=3,
                retry_delay=5,
            )

            # verify exporter available on all nodes...
            if len(self.__hosts) <= 8:
                psecs = 5
            elif len(self.__hosts) <= 128:
                psecs = 30
            else:
                psecs = 90

            logging.info("Exporters launched, pausing for %i secs" % psecs)
            time.sleep(psecs)  # <-- needed for slow SLURM query times on ORNL
            numHosts = len(self.__hosts)
            numAvail = 0

            if True:
                logging.info("Testing exporter availability")
                delay_start = 0.05
                hosts_ok = []
                hosts_bad = []
                for host in self.__hosts:
                    host_ok = False
                    for iter in range(1, 25):
                        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                            try:
                                result = s.connect_ex((host, int(port)))

                                if result == 0:
                                    numAvail = numAvail + 1
                                    hosts_ok.append(host)
                                    logging.debug("Exporter on %s ok" % host)
                                    host_ok = True
                                    break
                                else:
                                    delay = delay_start * iter
                                    logging.debug("Retrying %s (sleeping for %.2f sec)" % (host, delay))
                                    time.sleep(delay)
                                s.close()
                            except Exception as e:
                                break

                    if not host_ok:
                        logging.error("Missing exporter on %s (%s)" % (host, result))
                        hosts_bad.append(host)

                logging.info("%i of %i exporters available" % (numAvail, numHosts))
                if numAvail == numHosts:
                    logging.info("User mode data collectors: SUCCESS")

                # cache any failed hosts to file
                jobid = os.getenv("SLURM_JOB_ID", None)
                if jobid:
                    fileout = "omnistat_failed_hosts.%s.out" % jobid
                    if hosts_bad:
                        with open(fileout, "w") as f:
                            for host in hosts_bad:
                                f.write(host + "\n")
                        f.close()
                        logging.info("Cached failed startup hosts in %s" % fileout)

        return

    def stopSingleExporters(self, host, port, timeout=120):
        logging.debug("Stopping exporter for host -> %s" % host)
        cmd = ["curl", f"{host}:{port}/shutdown"]
        logging.debug("-> running command: %s" % cmd)
        t1 = time.perf_counter()
        utils.runShellCommand(cmd, timeout=timeout)
        t2 = time.perf_counter()

        return t2 - t1

    def stopExporters(self, victoriaMode=False):
        self.rmsDetection()
        self.disableProxies()

        logging.info("Stopping %i exporters" % len(self.__hosts))

        port = self.runtimeConfig["omnistat.collectors"].get("port", "8001")

        with concurrent.futures.ThreadPoolExecutor(max_workers=256) as executor:
            future_to_host = {
                executor.submit(
                    self.stopSingleExporters,
                    host,
                    port,
                ): host
                for host in self.__hosts
            }

        # Collect results as they complete
        min_time = float("inf")
        max_time = float("-inf")
        avg_time = 0.0
        count = 0

        for future in concurrent.futures.as_completed(future_to_host):
            host = future_to_host[future]
            timing = future.result()
            avg_time += timing
            count += 1
            logging.debug("--> %s required %.2f secs to shutdown" % (host, timing))
            if timing < min_time:
                min_time = timing
            if timing > max_time:
                max_time = timing

        logging.info(
            "--> average time to shutdown = %.2f secs (min=%.2f, max=%.2f)" % (avg_time / count, min_time, max_time)
        )

        return

    def verifyNumaCommand(self, coreid):
        """Verify numactl is available and works with supplied core id when provided

        Args:
            coreid (int): desired CPU core to pin with numacl

        Returns:
            list: numactl command
        """

        numactl = shutil.which("numactl")
        if not numactl:
            return None

        if not isinstance(coreid, int):
            return None

        # we have numactl and a core provided - verify we can use it locally
        numa_command = ["numactl", f"--physcpubind={coreid}"]

        results = utils.runShellCommand(numa_command + ["hostname"], timeout=2)
        if results.returncode != 0:
            logging.warning("Unable to use numactl with supplied cpu core = %i" % coreid)
            logging.debug("--> " + results.stdout.splitlines()[0])
            logging.debug("--> " + results.stderr.splitlines()[0])
            return None
        else:
            return numa_command


def main():
    userUtils = UserBasedMonitoring()

    parser = argparse.ArgumentParser(formatter_class=utils.HelpFormatterWide)
    parser.add_argument("-v", "--verbose", help="increase output verbosity", action="store_true")
    parser.add_argument("--configfile", type=str, help="runtime config file", default=None)
    parser.add_argument("--startserver", help="start local victoria metrics server", action="store_true")
    parser.add_argument("--stopserver", help="stop local victoria metrics server", action="store_true")
    parser.add_argument("--startexporters", help="start data exporters", action="store_true")
    parser.add_argument("--stopexporters", help="stop data exporters", action="store_true")
    parser.add_argument("--start", help="start all necessary user-based monitoring services", action="store_true")
    parser.add_argument("--stop", help="stop all user-based monitoring services", action="store_true")
    parser.add_argument("--interval", type=float, help="data sampling frequency in secs (default=10)")
    parser.add_argument("--pushinterval", type=float, help="data push frequency in minutes (default=5)", default=5.0)

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if len(sys.argv) <= 1:
        parser.print_help()
        sys.exit(1)

    userUtils.setup(args.configfile)
    if args.interval:
        userUtils.setMonitoringInterval(args.interval)

    if args.pushinterval:
        userUtils.setPushFrequency(args.pushinterval)

    victoriaMode = True

    if args.startserver:
        userUtils.startPromServer(victoriaMode=victoriaMode)
    elif args.stopserver:
        userUtils.stopPromServer(victoriaMode=victoriaMode)
    elif args.startexporters:
        userUtils.startExporters(victoriaMode=victoriaMode)
    elif args.stopexporters:
        userUtils.stopExporters(victoriaMode=victoriaMode)
    elif args.start:
        if victoriaMode:
            logging.info("Initiating data collection in [push] mode -> VictoriaMetrics")
        else:
            logging.info("Initiating data collection in [pull] mode -> Prometheus")
        userUtils.startPromServer(victoriaMode=victoriaMode)
        userUtils.startExporters(victoriaMode=victoriaMode)
    elif args.stop:
        userUtils.stopExporters(victoriaMode=victoriaMode)
        userUtils.stopPromServer(victoriaMode=victoriaMode)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
