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

# Prometheus data collector for HPC systems.
#
# Base Collector class - defines required methods for all metric collectors
# implemented as a child class.
# --

import configparser
from abc import ABC, abstractmethod


class Collector(ABC):
    # Optional method(s)
    def __init__(self, config: configparser.ConfigParser):
        """Optional initializer for Collector.

        Args:
            config (configparser.ConfigParser): Cached copy of runtime configuration that can be used to further control collector behavior.
        """
        self.runtimeConfig = config

    # Required methods to be implemented by child classes
    @abstractmethod
    def registerMetrics(self):
        """Defines desired metrics to monitor with Omnistat data collector. Called once during initialization."""
        pass

    @abstractmethod
    def updateMetrics(self):
        """Updates defined metrics with latest values. Called at every polling interval."""
        pass
