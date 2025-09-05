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
#
# Standalone data collector for HPC systems.
# --> intended for user-mode data collection to run within a job
# --> provides a flask endpoint to terminate data collection (http://host:port/shutdown)

import argparse
import ctypes
import logging
import os
import platform
import pwd
import signal
import sys
import threading
import time
import warnings
from datetime import datetime, timezone

import requests
from flask import Flask, abort, jsonify, request
from prometheus_client import REGISTRY, Gauge

from omnistat import utils
from omnistat.monitor import Monitor

app = Flask(__name__)
terminateFlagEvent = threading.Event()
dataDeliveredEvent = threading.Event()

fomData = []
fomLock = threading.Lock()


def push_to_victoria_metrics(metrics_data_list, victoria_url):
    logging.info("Pushing local node telemetry to VictoriaMetrics endpoint -> %s" % victoria_url)
    headers = {
        "Content-Type": "text/plain",
    }

    metrics_data = "\n".join(metrics_data_list)
    try:
        response = requests.post(victoria_url + "/api/v1/import/prometheus", data=metrics_data, headers=headers)
    except requests.ConnectionError:
        logging.error("")
        logging.error(
            "[FAILED]: Unable to make connection - please verify VictoriaMetrics is running and accessible from this host."
        )
        return
    except Exception as e:
        logging.error("")
        logging.error("[FAILED]: Unable to post data to Victoria endpoint")
        logging.error(e)
        return

    if response.status_code != 204:
        logging.error("")
        logging.error(f"[FAILED] Unable to push metrics: {response.status_code}, {response.text}")
        return
    else:
        logging.info("Metrics pushed successfully!")

    # notify on backfill event
    endpoints = ["/internal/resetRollupResultCache", "/internal/force_flush"]
    for endpoint in endpoints:
        try:
            response = requests.get(victoria_url + endpoint)
            logging.debug("--> Response from victoria endpoint %s = %s" % (endpoint, response.status_code))
        except Exception as e:
            logging.error("")
            logging.error("[FAILED]: Unable to GET Victoria endpoint -> %s" % endpoint)
            logging.error(e)
            return

        if response.status_code != 200:
            logging.warning(f"[WARN] Unexpected return code from VM endpoint: {endpoint} = {response.status_code}")

    return


class Standalone:
    def __init__(self, args, config):
        logging.basicConfig(format="%(message)s", level=logging.ERROR, stream=sys.stdout, flush=True)
        self.__dataVM = []
        self.__hostname = platform.node().split(".", 1)[0]
        self.__instanceLabel = 'instance="%s"' % self.__hostname

        if args.interval < 0.005:
            logging.error("")
            logging.error("[ERROR]: Please set sampling interval to be >= 5 millisecs (%s)" % args.interval)
            exit(1)

        self.__pushFrequencyMins = args.pushinterval
        if self.__pushFrequencyMins < 1:
            logging.error("")
            logging.error("[ERROR]: Please set data_frequency_mins >= 1 minute (%s)" % self.__pushFrequencyMins)
            sys.exit(1)

        # cache user name for metric labels
        uid = os.getuid()
        self.__userLabel = 'user="%s"' % pwd.getpwuid(uid).pw_name

        self.__victoriaURL = f"http://{args.endpoint}:{args.port}"

        self.__fomCheckFrequencySecs = config["omnistat.usermode"].getint("fom_check_frequency_secs", 10)
        if self.__fomCheckFrequencySecs < 5:
            logging.error("")
            logging.error(
                "[ERROR]: Please set fom_check_freqeuncy_secs  >= 5 seconds (%s)" % self.__fomCheckFrequencySecs
            )
            sys.exit(1)

        logflask = logging.getLogger("werkzeug")
        logflask.setLevel(logging.ERROR)

        # default labels applied to all metrics from this host
        self.__labelDefaults = self.__instanceLabel + "," + self.__userLabel
        logging.debug("Default metric labels = %s" % self.__labelDefaults)

        # verify victoriaURL is operational and ready to receive queries (poll for a bit if necessary)
        failed = True
        delay_start = 0.05
        testURL = f"http://{args.endpoint}:{args.port}/ready"
        for iter in range(1, 25):
            try:
                response = requests.get(testURL)
                logging.debug("VM ready response = %s" % response)
                if response.status_code != 200:
                    failed = True
                else:
                    failed = False
                    break
            except requests.exceptions.RequestException as e:
                logging.debug(e)
                failed = True

            delay = delay_start * iter
            logging.info("Retrying VM endpoint (sleeping for %.2f sec)" % (delay))
            time.sleep(delay)

        if failed:
            logging.warning("[WARN]: Unable to access VictoriaMetrics server endpoint (%s)" % self.__victoriaURL)
            logging.warning("[WARN]: Please verify server is running and accessible from this host.")

        logging.info("Telemetry data will be sampled every %.3f seconds" % args.interval)
        logging.info("Cached data will be pushed every %.1f minute(s)" % self.__pushFrequencyMins)
        logging.info("Figure-of-merit (FOM) data will be checked for every %i seconds" % self.__fomCheckFrequencySecs)

        # Init glibc for usleep access
        self.__libc = ctypes.CDLL("libc.so.6")

    def sleep_microsecs(self, microsecs):
        self.__libc.usleep(microsecs)

    def tokenizeMetricName(self, name, labels):
        token = name
        if "card" in labels:
            token = "card%s_" % labels["card"] + name
        return token

    def getMetrics(self, timestamp_millisecs, prefix=None):
        """Cache current metrics from latest query"""
        for metric in REGISTRY.collect():
            if metric.type == "gauge":
                if prefix and not metric.name.startswith(prefix):
                    continue
                for sample in metric.samples:
                    if sample.name == "rmsjob_info":
                        labels = self.__instanceLabel
                    else:
                        labels = self.__labelDefaults
                    if sample.labels:
                        for key, value in sample.labels.items():
                            labels += ',%s="%s"' % (key, value)
                    entry = "%s{%s} %s %i" % (sample.name, labels, sample.value, timestamp_millisecs)
                    self.__dataVM.append(entry)

    def polling(self, monitor, interval_secs):
        """main polling function"""

        num_samples = 0
        num_fom_samples = 0
        sample_duration = 0
        num_pushes = 0
        push_check_duration = 0.0
        push_frequency_secs = self.__pushFrequencyMins * 60
        push_time_accumulation = 0.0
        interval_microsecs = int(interval_secs * 1000000)
        mem_mb_base = utils.getMemoryUsageMB()
        base_start_time = time.perf_counter()
        push_thread = None
        fom_check_duration = 0.0

        # ---
        # main sampling loop
        try:
            while not terminateFlagEvent.is_set():
                start_time = time.perf_counter()
                timestamp_msecs = int(datetime.now(timezone.utc).timestamp() * 1000.0)
                monitor.updateAllMetrics()
                self.getMetrics(timestamp_msecs)
                num_samples += 1
                sample_duration += time.perf_counter() - start_time

                # periodically push cached data to VictoriaMetrics
                if push_check_duration > push_frequency_secs:
                    push_check_duration = 0.0
                    # ensure previous push thread completed...
                    if push_thread is not None and push_thread.is_alive():
                        logging.info("Previous metric push is still running - blocking till complete.")
                        push_thread.join()
                        logging.info("Resuming after previous metric push complete.")
                    try:
                        push_start_time = time.perf_counter()
                        dataToPush = self.__dataVM
                        push_thread = threading.Thread(
                            target=push_to_victoria_metrics, args=(dataToPush, self.__victoriaURL)
                        )
                        push_thread.start()
                        self.__dataVM = []
                        num_pushes += 1
                        push_time_accumulation += time.perf_counter() - push_start_time
                    except:
                        pass

                # periodically check for figure-of-merit (FOM) data
                if fom_check_duration > self.__fomCheckFrequencySecs:
                    logging.debug("Checking on FOM data...")
                    fom_check_duration = 0.0
                    if fomData:
                        with fomLock:
                            for entry in fomData:
                                entry = '%s{instance="%s",name="%s"} %s %i' % (
                                    "omnistat_fom",
                                    self.__hostname,
                                    entry["name"],
                                    entry["value"],
                                    entry["timestamp_msecs"],
                                )
                                self.__dataVM.append(entry)
                            logging.info("Registered %i sample(s) of FOM data" % len(fomData))
                            num_fom_samples += len(fomData)
                            fomData.clear()

                self.sleep_microsecs(interval_microsecs)
                # time.sleep(interval_secs)
                push_check_duration += time.perf_counter() - start_time
                fom_check_duration += time.perf_counter() - start_time

        except KeyboardInterrupt:
            logging.info("")
            logging.info("Terminating data collection from keyboard interrupt.")

        # end sampling loop
        # ---

        duration_secs = time.perf_counter() - base_start_time

        if push_thread is not None and push_thread.is_alive():
            logging.info("Last metric push from polling loop is still running - blocking till complete.")
            push_thread.join()
            logging.info("Ready for final data dump after previous metric push complete.")

        # check for any remaining FOM data
        if fomData:
            with fomLock:
                for entry in fomData:
                    entry = '%s{instance="%s",name="%s"} %s %i' % (
                        "omnistat_fom",
                        self.__hostname,
                        entry["name"],
                        entry["value"],
                        entry["timestamp_msecs"],
                    )
                    self.__dataVM.append(entry)
                logging.info("Registered %i sample(s) of FOM data" % len(fomData))
                num_fom_samples += len(fomData)
                fomData.clear()

        if len(self.__dataVM) > 0:
            logging.info("Initiating final data push...")
            push_to_victoria_metrics(self.__dataVM, self.__victoriaURL)

        logging.info("")
        logging.info("--> Sampling interval          = %.4f (secs)" % interval_secs)
        logging.info("--> Total # of samples         = %i" % num_samples)
        if num_samples > 0:
            logging.info("--> Average time/sample        = %.4f (secs)" % (sample_duration / num_samples))
        logging.info("--> Total data pushes          = %i" % num_pushes)
        if num_pushes > 0:
            logging.info("--> Average push duration      = %.4f (secs)" % (push_time_accumulation / num_pushes))
        if duration_secs >= 3600:
            logging.info("--> Data collection duration   = %.4f (hours)" % (1.0 * duration_secs / 3600.0))
        elif duration_secs >= 60:
            logging.info("--> Data collection duration   = %.4f (mins)" % (1.0 * duration_secs / 60.0))
        else:
            logging.info("--> Data collection duration   = %.4f (secs)" % duration_secs)
        logging.info("--> Base memory use at start   = %.3f MB" % mem_mb_base)
        logging.info("--> Memory growth at stop      = %.3f MB" % (utils.getMemoryUsageMB() - mem_mb_base))
        if num_fom_samples > 0:
            logging.info("--> Total # of FOM samples     = %i" % num_fom_samples)

        # deliver event to shutdown procedure
        logging.debug("setting shutdown delivery event")
        dataDeliveredEvent.set()
        logging.debug("shutdown delivery event is set")
        time.sleep(0.5)

        logging.info("Terminating execution...")
        logging.shutdown()
        os.kill(os.getpid(), signal.SIGTERM)
        return


def parse_args():
    """Parse command-line arguments

    Returns:
        argparse.Namespace: parsed arguments
    """
    parser = argparse.ArgumentParser(formatter_class=utils.HelpFormatterWide)
    parser.add_argument("--configfile", type=str, help="runtime config file", default=None)
    parser.add_argument("--interval", type=float, help="data sampling frequency (in secs)", default=0.5)
    parser.add_argument("--pushinterval", type=float, help="data push frequency (in minutes)", default=5.0)
    parser.add_argument("--logfile", type=str, help="redirect stdout to logfile", default=None)
    parser.add_argument("--endpoint", type=str, help="hostname of VictoriaMetrics server", default="localhost")
    parser.add_argument("--port", type=int, help="port to access VictoriaMetrics server", default=9090)

    return parser.parse_args()


@app.route("/shutdown")
def terminate():
    """Endpoint that can be used to terminate execution"""
    logging.info("Received shutdown request")
    terminateFlagEvent.set()

    # spin loop till notice recieved that last data was pushed
    maxChecks = 0
    if "SAMPLING_INTERVAL" in app.config:
        interval = app.config["SAMPLING_INTERVAL"]
    else:
        interval = 5.0
    wait_interval = max(1, interval / 2.0)
    while not dataDeliveredEvent.isSet():
        logging.debug("waiting for data delivery event...(%.2f secs)" % wait_interval)
        maxChecks += 1
        if maxChecks > 10:
            break
        time.sleep(wait_interval)

    return jsonify({"message": "Shutting down..."}), 200


@app.errorhandler(403)
def forbidden(e):
    return jsonify(error="Access denied"), 403


@app.route("/fom", methods=["POST"])
def figureOfMerit():
    """Endpoint that can be used by user to provide application figure of merit"""
    try:
        data = request.get_json()
        name = data.get("name")
        timestamp_msecs = int(datetime.now(timezone.utc).timestamp() * 1000.0)
        value = data.get("value")
        with fomLock:
            fomData.append({"name": name, "value": value, "timestamp_msecs": timestamp_msecs})
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/metrics")
def heartbeat():
    """Endpoint that can be used to confirm exporter is running"""
    return jsonify({"status": "ok"}), 200


def runFlask(config):
    listenPort = config["omnistat.collectors"].get("port", 8001)
    app.run(host="0.0.0.0", port=listenPort)


def main():

    args = parse_args()
    config = utils.readConfig(utils.findConfigFile(args.configfile))

    # Initialize GPU monitoring
    monitor = Monitor(config, logFile=args.logfile)
    monitor.initMetrics()

    app.config["SAMPLING_INTERVAL"] = args.interval

    caching = Standalone(args, config)

    # Enforce network restrictions
    @app.before_request
    def restrict_ips():
        allowed_ips = config["omnistat.collectors"].get("allowed_ips", "127.0.0.1")
        if "0.0.0.0" in allowed_ips:
            return
        elif request.remote_addr not in allowed_ips:
            abort(403)

    # Launch flask app as separate thread so we can respond to remote shutdown requests
    flask_thread = threading.Thread(target=runFlask, args=[config])
    flask_thread.start()

    # Initiate main polling loop/data collection
    caching.polling(monitor, args.interval)


if __name__ == "__main__":
    main()
