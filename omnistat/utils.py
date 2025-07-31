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
import configparser
import importlib.resources
import logging
import os
import re
import resource
import shutil
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path


def convert_bdf_to_gpuid(bdf_string):
    """
    Converts BDF text string in hex format to a GPU location id in the form written by kfd driver
    into /sys/class/kfd/kfd/topology/nodes/<node>/properties

    Args:
        bdf_string (string): bdf string for GPU (domain:bus:device.func)

    Returns:
        int: location_id
    """

    domain = int(bdf_string.split(":")[0], 16)
    # strip leading domain
    bdf = bdf_string.split(":")[1:]
    # cull out bus, device, and function as ints
    bus = int(bdf[0], 16)
    dev_func = bdf[1].split(".")
    device = int(dev_func[0], 16)
    function = int(dev_func[1], 16)
    # assemble id per kfd driver
    location_id = (bus << 8) | function
    return location_id


def pass_through_indexing(numGpus):
    """returns a pass through GPU indexingwith 0:0, 1:1, etc.  Intended for use in cases where
    exact mapping cannot be ascertained by reading sysfs topology files.
    """
    gpu_index_mapping = {}
    for i in range(numGpus):
        gpu_index_mapping[i] = str(i)
    return gpu_index_mapping


def gpu_index_mapping_based_on_guids(guidMapping, expectedNumGPUs):
    """Generate a mapping between kfd gpu_id  (SMI lib) to those of HIP_VISIBLE_DEVICES. Intended for
    use with metric labeling to identify devices based on HIP_VISIBLE_DEVICES indexing.

    Args:
        guidMapping (dict): maps kfd indices to gpu_ids
        expectedNumGPUs (int): number of GPUs detected locally

    Returns:
        dict: maps kfd indices to HIP_VISIBLE_DEVICES indices
    """
    kfd_nodes = "/sys/class/kfd/kfd/topology/nodes"
    logging.info("GPU topology indexing: Scanning devices from %s" % kfd_nodes)
    if not os.path.isdir(kfd_nodes):
        logging.warning("--> directory not found")
        return pass_through_indexing(expectedNumGPUs)

    devices = os.listdir(kfd_nodes)
    numNonGPUs = 0
    numGPUs = 0
    tmpMapping = {}
    for id in range(len(devices)):
        file = os.path.join(kfd_nodes, str(id), "gpu_id")
        logging.debug("--> reading contents of %s" % file)
        if os.path.isfile(file):
            with open(file) as f:
                guid = int(f.readline().strip())
            if guid == 0:
                numNonGPUs += 1
                logging.debug("--> ...ignoring CPU device")
            else:
                tmpMapping[guid] = numGPUs
                numGPUs += 1
        else:
            logging.warning("Unable to access expected file (%s)" % file)
            return pass_through_indexing(expectedNumGPUs)

    if numGPUs != expectedNumGPUs:
        logging.warning("--> did not detect expected number of GPUs in sysfs (%i vs %i)" % (numGPUs, expectedNumGPUs))
        return pass_through_indexing(expectedNumGPUs)

    gpuMappingOrder = {}

    for gpuIndex, id in guidMapping.items():
        if id in tmpMapping:
            gpuMappingOrder[gpuIndex] = str(tmpMapping[id])
        else:
            logging.warning("--> unable to resolve gpu location_id=%s" % id)
            return pass_through_indexing(expectedNumGPUs)

    logging.info("--> Mapping: %s" % gpuMappingOrder)
    return gpuMappingOrder


def gpu_index_mapping_based_on_bdfs(bdfMapping, expectedNumGPUs):
    """Generate a mapping between kfd gpu indexing (SMI lib) to those of HIP_VISIBLE_DEVICES. Intended for
    use with metric labeling to identify devices based on HIP_VISIBLE_DEVICES indexing.

    Args:
        bdfMapping (dict): maps kfd indices to location ids derived from bdf strings
        expectedNumGPUs (int): number of GPUs detected locally

    Returns:
        dict: maps kfd indices to HIP_VISIBLE_DEVICES indices
    """
    kfd_nodes = "/sys/class/kfd/kfd/topology/nodes"
    logging.info("GPU topology indexing: Scanning devices from %s" % kfd_nodes)
    if not os.path.isdir(kfd_nodes):
        logging.warning("--> directory not found")
        return pass_through_indexing(expectedNumGPUs)

    devices = os.listdir(kfd_nodes)
    numNonGPUs = 0
    numGPUs = 0
    tmpMapping = {}
    for id in range(len(devices)):
        file = os.path.join(kfd_nodes, str(id), "properties")
        logging.debug("--> reading contents of %s" % file)
        if os.path.isfile(file):
            properties = {}
            with open(file) as f:
                for line in f:
                    key, value = line.strip().split(" ")
                    if key == "location_id":
                        location_id = int(value)
            if location_id == 0:
                numNonGPUs += 1
                logging.debug("--> ...ignoring CPU device")
            else:
                tmpMapping[location_id] = numGPUs
                numGPUs += 1
        else:
            logging.warning("Unable to access expected file (%s)" % file)
            return pass_through_indexing(expectedNumGPUs)

    if numGPUs != expectedNumGPUs:
        logging.warning("--> did not detect expected number of GPUs in sysfs (%i vs %i)" % (numGPUs, expectedNumGPUs))
        return pass_through_indexing(expectedNumGPUs)

    gpuMappingOrder = {}
    for gpuIndex, id in bdfMapping.items():
        if id in tmpMapping:
            gpuMappingOrder[gpuIndex] = str(tmpMapping[id])
        else:
            logging.warning("--> unable to resolve gpu location_id=%s" % id)
            return pass_through_indexing(expectedNumGPUs)

    logging.info("--> Mapping: %s" % gpuMappingOrder)
    return gpuMappingOrder


def count_compute_units(nodes):
    """
    Count the number of compute units for each one of the given GPU node IDs
    (KFD internal GPU indices).

    Args:
        nodes (list): list of GPU node IDs to calculate the number of CUs for.

    Returns:
        dict: dictionary of CU counts indexed by GPU node ID.
    """
    base_path = Path("/sys/class/kfd/kfd/topology/nodes")
    pattern = re.compile(r"^(simd_count|simd_per_cu)\s+(\d+)", re.MULTILINE)

    compute_units = {}
    for node in nodes:
        # The properties file should contain simd_count and simd_per_cu
        # values, which can be used to calculate the number of CUs. Abort the
        # execution if there are issues opening the file or if simd values
        # aren't available.
        properties = base_path / f"{node}/properties"
        try:
            with open(properties, "r") as f:
                data = f.read()

            simd_values = {}
            matches = pattern.finditer(data)
            for match in matches:
                key = match.group(1)
                value = int(match.group(2))
                simd_values[key] = value

            compute_units[node] = simd_values["simd_count"] / simd_values["simd_per_cu"]
        except:
            logging.error(f"ERROR: Failed to read node properties file {properties}.")
            sys.exit(4)

    return compute_units


def get_occupancy(guid):
    """
    Get aggregated CU occupancy for all the processes running in a given GPU
    device ID (guid).

    Args:
        guid (int): GPU device ID.

    Returns:
        int: CU occupancy in number of CUs.
    """
    base_path = Path("/sys/class/kfd/kfd/proc")
    file_pattern = f"*/stats_{guid}/cu_occupancy"

    cu_occupancy = 0
    for cu_file in list(base_path.glob(file_pattern)):
        try:
            with open(cu_file, "r") as f:
                value = f.read().strip()
            cu_occupancy += int(value)
        except Exception:
            # Ignore issues while reading cu_occupancy files. A common reason
            # that triggers an exception is when the file is no longer there
            # because the process ended.
            pass

    return cu_occupancy


def error(message):
    """Log an error message and exit

    Args:
        message (string): message
    """
    logging.error("Error: " + message)
    sys.exit(1)


def findConfigFile(configFileArgument=None):
    """Identify configuration file location

    Try to find one of the following locations in the filesystem:
     1. File pointed by configFileArgument (if defined)
     2. File pointed by OMNISTAT_CONFIG (if defined)
     3. Default configuration file in the package

    Args:
        configFileArgument (string, optional): optional path to config file
          provided as argument in the CLI

    Returns:
        string: path to an existing configuration file
    """
    # Resolve path to default config file in the current installation.
    # This configuration is only meant to provide sane defaults to run
    # locally, but most installations will need a custom file.
    packageDir = importlib.resources.files("omnistat")
    configFile = packageDir.joinpath("config/omnistat.default")

    if "OMNISTAT_CONFIG" in os.environ:
        configFile = os.environ["OMNISTAT_CONFIG"]

    if configFileArgument != None:
        configFile = configFileArgument

    if not os.path.isfile(configFile):
        error(f"Unable to find configuration file {configFile}")

    return configFile


def readConfig(configFile):
    """Read and parse configuration file

    Args:
        configFile (string): path to config file

    Returns:
        ConfigParser: object containing configuration options
    """
    print(f"Reading configuration from {configFile}")
    config = configparser.ConfigParser()
    config.read(configFile)
    return config


def runShellCommand(command, capture_output=True, text=True, exit_on_error=False, timeout=1.0):
    """Runs a provided shell command

    Args:
        command (list): shell command with options to execute
        capture_output (bool, optional): _description_. Defaults to True.
        text (bool, optional): _description_. Defaults to True.
        exit_on_error (bool, optional): Whether to exit on error or not. Defaults to False.
    """

    logging.debug("Command to run = %s" % command)
    try:
        results = subprocess.run(command, capture_output=capture_output, text=text, timeout=timeout)
    except subprocess.TimeoutExpired:
        logging.error("ERROR: Process timed out, ran for more than %i sec(s)" % timeout)
        logging.error("       %s" % command)
        if not exit_on_error:
            return None

    if exit_on_error and results.returncode != 0:
        logging.error("ERROR: Command failed")
        logging.error("       %s" % command)
        logging.error("stdout: %s" % results.stdout)
        logging.error("stderr: %s" % results.stderr)
        sys.exit(1)
    return results


def runBGProcess(command, outputFile=".bgcommand.output", mode="w", envAdds=None):
    logging.debug("Command to run in background = %s" % command)
    env = os.environ.copy()

    if envAdds:
        for entry in envAdds:
            env[entry] = envAdds[entry]

    outfile = open(outputFile, mode)
    results = subprocess.Popen(command, stdout=outfile, stderr=outfile, env=env)
    return results


def resolvePath(desiredCommand, envVar):
    """Resolve underlying path to a desired shell command.

    Args:
        desiredCommand (string): desired shell command to resolve
        envVar (string): environment variable string that can optionally provide path to desired command

    Returns:
        string: resolved path to desired comman
    """
    path = None
    command = desiredCommand
    if envVar in os.environ:
        customPath = os.getenv(envVar)
        logging.debug("Overriding command search path with %s=%s" % (envVar, customPath))
        if os.path.isdir(customPath):
            command = customPath + "/" + desiredCommand
        else:
            error("provided %s does not exist -> %s" % (envVar, customPath))
            sys.exit(1)

    # verify we can resolve the desired binary
    path = shutil.which(command)
    if not path:
        logging.error("ERROR: Unable to resolve path for %s" % command)
        return None
    else:
        logging.debug("--> %s path = %s" % (desiredCommand, path))

    return path


def removeQuotes(input):
    """Remove leading/trailing quotes from a string

    Args:
        input (str): string to update
    """
    if input.startswith('"'):
        input = input.strip('"')
    elif input.startswith("'"):
        input = input.strip("'")
    return input


def getMemoryUsageMB():
    """Get current process memory usage in MB"""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def getVersion():
    """Return omnistat version info"""
    try:
        return version("omnistat")
    except importlib.metadata.PackageNotFoundError:
        # When package is not installed, rely on top-level VERSION file and local git tools to assemble version info

        omnistat_home = Path(__file__).resolve().parent.parent
        versionFile = os.path.join(omnistat_home, "VERSION")
        try:
            with open(versionFile, "r") as file:
                VER = file.read().replace("\n", "")
        except EnvironmentError:
            error("Cannot find VERSION file at {}".format(versionFile))

        # git version query
        SHA = None
        gitDir = os.path.join(omnistat_home, ".git")
        if (shutil.which("git") is not None) and os.path.exists(gitDir):
            gitQuery = subprocess.run(
                ["git", "log", "--pretty=format:%h", "-n", "1"],
                cwd=gitDir,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if gitQuery.returncode == 0:
                SHA = gitQuery.stdout.decode("utf-8")

        versionInfo = VER
        if SHA:
            versionInfo += " (%s)" % SHA

        return versionInfo


def displayVersion(version):
    """Pretty print versioning info"""
    print("-" * 40)
    print("Omnistat version: %s" % version)
    print("-" * 40)


def execute_ssh_command_nohup(
    hostname: str,
    command: str,
    max_retries: int,
    retry_delay: float,
    ssh_timeout: float,
    outputDir: str,
) -> list[bool, str]:
    """
    Executes a single command on a remote host via ssh with nohup for launching background processes.

    Returns:
        list containing [success_status, output_filename]
    """

    attempt = 1
    while attempt <= max_retries:
        try:
            outfile = outputDir + f"/omnistat_launch_{hostname}_try{attempt}.log"
            nohup_command = f"nohup {command} > {outfile} 2>&1 &"
            ssh_command = ["ssh", hostname, nohup_command]

            logging.debug(f"[pssh] {ssh_command}")

            # Run SSH command
            process = subprocess.run(ssh_command, capture_output=True, text=True, timeout=ssh_timeout)

            if process.returncode == 0:
                return True, outfile
            else:
                error = process.stderr or process.stdout
                logging.warning(f"[pssh] try {attempt} of {max_retries} failed for {hostname}: {error.rstrip()}")
                attempt += 1

        except subprocess.TimeoutExpired as e:
            logging.warning(
                f"[pssh] try {attempt} of {max_retries} timed out for {hostname} (timeout= {ssh_timeout} secs)"
            )
            attempt += 1

        except Exception as e:
            logging.warning(f"[pssh] try {attempt} of {max_retries} failed for {hostname}: {str(e)}")
            attempt += 1

        if attempt > max_retries:
            logging.warning(f"[pssh] Max retries reached for {hostname}")
            return False, outfile

        time.sleep(retry_delay)

    return False, None


def execute_ssh_parallel(
    command: str,
    hostnames: list[str],
    max_concurrent: int = 10,
    max_retries: int = 3,
    retry_delay: float = 2.0,
    ssh_timeout: float = 10.0,
    outputDir: str = "/tmp",
) -> dict:
    """
    Spawn commands on remote servers with nohup on multiple hosts in parallel using native ssh client.

     Returns:
         Dictionary of "status" and "output_filename" for each hostname
    """

    results = {}

    if not os.path.exists(outputDir):
        os.makedirs(outputDir)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_concurrent) as executor:
        future_to_host = {
            executor.submit(
                execute_ssh_command_nohup,
                host,
                command,
                max_retries,
                retry_delay,
                ssh_timeout,
                outputDir,
            ): host
            for host in hostnames
        }

        # Collect results as they complete
        for future in concurrent.futures.as_completed(future_to_host):
            host = future_to_host[future]
            try:
                success, outFile = future.result()
                results[host] = {"status": success, "output_filename": outFile}

                # file_path = Path(outFile)
                # if file_path.is_file():
                #     results[host] = {"status": success, "output_filename": outFile}
                # else:
                #     results[host] = {"status": success, "output_filename": None}

                if success:
                    logging.debug(f"[pssh] successfully launched ssh command on {host}")
                else:
                    logging.error(f"[pssh] Failed to launch ssh command on {host}")
                    if file_path.is_file():
                        output = file_path.read_text()
                        logging.warning(output)

            except Exception as e:
                logging.error("[pssh] Unknown error executing command on %s: %s", host, str(e))
                results[host] = {"status": False, "output_filename": None}

        logging.info("[pssh] All launch commands executed")

    return results


# custom argparse help formatter to allow for wider output
class HelpFormatterWide(argparse.HelpFormatter):
    def __init__(self, prog):
        super().__init__(prog, max_help_position=40)


#        super().__init__(prog, max_help_position=40, width=120)
