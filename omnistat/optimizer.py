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
#
# optimizer.py
#
# Standalone energy optimizer process.  Runs on the head node where RMS
# environment variables are available.  Receives FOM data forwarded from
# standalone.py instances on each job node and dispatches GPU clock commands
# to converge on the lowest frequency with acceptable performance.
#
# Endpoints:
#   POST /fom              -- receive a FOM sample from standalone.py
#   GET  /optimizer/status  -- return current optimizer state
#   POST /optimizer/reset   -- reset optimizer to IDLE and restore max clock
#   GET  /health           -- liveness check

import argparse
import logging
import os
import platform
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, request

from omnistat import utils
from omnistat.command import CommandController
from omnistat.energy_analysis import EnergyAnalyzer

app = Flask(__name__)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--configfile", type=str, default=None)
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--log", type=str, default=None)
    parser.add_argument("--vm-endpoint", type=str, default=None, help="hostname of VictoriaMetrics server")
    parser.add_argument("--vm-port", type=int, default=9090, help="port of VictoriaMetrics server")
    parser.add_argument("--pushinterval", type=float, default=5.0, help="state metric push interval in minutes (default=5)")
    return parser.parse_args()


@app.route("/fom", methods=["POST"])
def figureOfMerit():
    try:
        data = request.get_json()
        name = data.get("name")
        value = data.get("value")
        timestamp_msecs = int(datetime.now(timezone.utc).timestamp() * 1000.0)
        analyzer = app.config.get("ENERGY_ANALYZER")
        if analyzer is not None:
            analyzer.ingest_fom(name, value, timestamp_msecs)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/optimizer/status", methods=["GET"])
def optimizeStatus():
    analyzer = app.config.get("ENERGY_ANALYZER")
    if analyzer is None:
        return jsonify({"error": "Optimizer not initialized."}), 503
    return jsonify(analyzer.get_status()), 200


@app.route("/optimizer/reset", methods=["POST"])
def optimizeReset():
    analyzer = app.config.get("ENERGY_ANALYZER")
    if analyzer is None:
        return jsonify({"error": "Optimizer not initialized."}), 503
    analyzer.reset()
    return jsonify({"status": "ok", "message": "Optimizer reset to IDLE, max clock frequency restored."}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


def make_state_push_thread(vm_url, push_interval_secs, threshold):
    """Start a dedicated thread that pushes the current optimizer state to VictoriaMetrics.

    The thread polls the shared state every 1 second and caches data points, then flushes
    them to VM once per push_interval_secs.  This ensures short-lived states are always
    captured regardless of push cadence.

    Returns (state_callback, perf_drop_callback) for EnergyAnalyzer.
    """
    import requests as _requests
    push_url = vm_url + "/api/v1/import/prometheus"
    headers = {"Content-Type": "text/plain"}
    hostname = platform.node().split(".", 1)[0]
    threshold_str = "%.1f" % threshold

    _lock = threading.Lock()
    _current = {"state_str": "IDLE", "state_int": 0}
    _drop = {"drop_pct": None}  # None means no drop observation yet

    def _state_callback(state_str, state_int, freq_mhz):
        with _lock:
            _current["state_str"] = state_str
            _current["state_int"] = state_int

    def _perf_drop_callback(drop_pct, freq_mhz):
        with _lock:
            _drop["drop_pct"] = drop_pct

    def _push_loop():
        poll_interval = 1  # seconds between state samples
        elapsed = 0
        cached_lines = []
        while True:
            time.sleep(poll_interval)
            elapsed += poll_interval
            with _lock:
                state_str = _current["state_str"]
                state_int = _current["state_int"]
                drop_pct = _drop["drop_pct"]
            ts = int(time.time() * 1000)
            cached_lines.append('omnistat_optimize{instance="%s",mode="energy"} %d %d' % (hostname, state_int, ts))
            if drop_pct is not None:
                cached_lines.append(
                    'omnistat_optimize_perf_percentage{instance=\"%s\",threshold=\"%s\"} %.4f %d'
                    % (hostname, threshold_str, drop_pct, ts)
                )
            if elapsed >= push_interval_secs:
                elapsed = 0
                payload = "\n".join(cached_lines)
                cached_lines = []
                try:
                    resp = _requests.post(push_url, data=payload.encode(), headers=headers, timeout=5)
                    if resp.status_code != 204:
                        logging.warning("[optimizer] VM state push returned HTTP %d: %s" % (resp.status_code, resp.text))
                    else:
                        logging.debug("[optimizer] State pushed: %s(%d)" % (state_str, state_int))
                except Exception as e:
                    logging.warning("[optimizer] Failed to push state metric: %s" % e)

    t = threading.Thread(target=_push_loop, daemon=True, name="state-push")
    t.start()
    return _state_callback, _perf_drop_callback


def main():
    args = parse_args()

    log_format = "%(message)s"
    if args.log:
        logging.basicConfig(filename=args.log, format=log_format, level=logging.INFO)
    else:
        logging.basicConfig(format=log_format, level=logging.INFO, stream=sys.stdout)

    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    config = utils.readConfig(utils.findConfigFile(args.configfile))

    host_list = utils.getRMSHosts()
    collector_port = config["omnistat.collectors"].getint("port", 8001)

    try:
        controller = CommandController(config)
        state_callback = None
        perf_drop_callback = None
        if args.vm_endpoint:
            vm_url = "http://%s:%d" % (args.vm_endpoint, args.vm_port)
            push_interval_secs = int(args.pushinterval * 60)
            threshold = config["omnistat.optimize"].getfloat("max_performance_drop", 5.0)
            state_callback, perf_drop_callback = make_state_push_thread(vm_url, push_interval_secs, threshold)
            logging.info("[optimizer] State metric will be pushed to %s every %.1f min" % (vm_url, args.pushinterval))
        analyzer = EnergyAnalyzer(config, host_list, collector_port, on_state_change=state_callback, on_perf_drop=perf_drop_callback)
        app.config["COMMAND_CONTROLLER"] = controller
        app.config["ENERGY_ANALYZER"] = analyzer
        logging.info("[optimizer] Initialized. hosts=%s  port=%d" % (host_list, args.port))
        logging.info("[optimizer]   status:  curl http://localhost:%d/optimizer/status" % args.port)
        logging.info("[optimizer]   reset:   curl -X POST http://localhost:%d/optimizer/reset" % args.port)
    except Exception as e:
        logging.error("[optimizer] Failed to initialize: %s" % e)
        sys.exit(1)

    def _on_sigterm(signum, frame):
        analyzer = app.config.get("ENERGY_ANALYZER")
        if analyzer is not None:
            status = analyzer.get_status()
            logging.info("[optimizer] Shutting down. Final status: %s" % status)
        else:
            logging.info("[optimizer] Shutting down.")
        os._exit(0)

    signal.signal(signal.SIGTERM, _on_sigterm)

    app.run(host="0.0.0.0", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
