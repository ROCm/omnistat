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

import _thread
import configparser
import logging
import sys
import time

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from omnistat.collector_base import Collector
from omnistat.utils import get_amdsmi_module

# Shared amdsmi module
smi = get_amdsmi_module()


class ROCMEvents(Collector):
    def __init__(self, config: configparser.ConfigParser):
        """
        Initialize the ROCmEvents data collector.

        Args:
            config (configparser.ConfigParser): Cached copy of runtime configuration.
        """

        logging.debug("Initializing ROCm SMI event collector")

        global smi
        smi = get_amdsmi_module()
        try:
            smi.amdsmi_init()
            logging.debug("AMD SMI library API initialized")
        except:
            logging.error("ERROR: Unable to initialize AMD SMI python library")
            sys.exit(4)

        self.__prefix = "rocm_"
        self.__events = []
        self.__desiredEventTypes = [smi.AmdSmiEvtNotificationType.THERMAL_THROTTLE]

        try:
            devices = smi.amdsmi_get_processor_handles()
            if len(devices) == 0:
                logging.error("No GPUs detected on host")
                sys.exit(1)
            else:
                self.__numGpus = len(devices)
                for device in devices:
                    self.__events.append(smi.AmdSmiEventReader(device, self.__desiredEventTypes))
        except smi.AmdSmiException as e:
            logging.error("unable to get processor handles")
            logging.error(e)
            sys.exit(1)

        # Launch polling threads for event reads (1 per GPU) - polling at 0.1 sec interval
        try:
            for i in range(self.__numGpus):
                _thread.start_new_thread(self.poll_gpu_events, (self.__events[i], i, 100))
            time.sleep(0.25)

        except Exception as e:
            logging.error("Error: Unable to start new thread. %s" % (e))
            sys.exit(1)

        logging.debug("SMI event collector initialized")

        self.__GPUmetrics = {}
        self.__throttle_count = []

    # --------------------------------------------------------------------------------------
    # Required child methods

    def registerMetrics(self):
        """Register metrics of interest"""

        metricName = self.__prefix + "throttle_events"
        self.__GPUmetrics["throttle_events"] = Gauge(metricName, "# of throttling events detected", labelnames=["card"])
        logging.info("--> [registered] %s (gauge)" % metricName)
        for gpu in range(self.__numGpus):
            self.__GPUmetrics["throttle_events"].labels(card=gpu).set(0)
            self.__throttle_count.append(0)
        return

    def updateMetrics(self):
        """Update registered metrics of interest"""
        for gpu in range(self.__numGpus):
            self.__GPUmetrics["throttle_events"].labels(card=gpu).set(self.__throttle_count[gpu])
        return

    # --------------------------------------------------------------------------------------
    # Additional custom methods unique to this collector

    def poll_gpu_events(self, event, gpu_index, timeout_msec):
        while 1:
            try:
                newevents = event.read(timeout_msec)
                self.__throttle_count[gpu_index] += len(newevents)
            except:
                pass
        return
