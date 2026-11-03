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
# optimizer_base.py
#
# Abstract base class for energy/performance optimizer plugins.
#
# An optimizer receives FOM (figure-of-merit) samples and decides which GPU
# clock frequency to target.  It is a pure decision-maker — it never
# communicates with hosts or the network.  The EnergyAnalyzer orchestrator
# dispatches clock commands and manages host communication based on the
# optimizer's recommendations.
#
# To implement a custom optimizer, subclass BaseOptimizer and point the
# ``optimizer_class`` setting in [omnistat.optimize] to your class's
# fully-qualified Python path.

from abc import ABC, abstractmethod


class BaseOptimizer(ABC):
    """Interface that every optimizer plugin must implement.

    Constructor keyword arguments supplied by the orchestrator:

        config        ConfigParser — full runtime configuration so the plugin
                      can read its own keys from [omnistat.optimize].
        sclk_min      int — minimum allowed GPU clock frequency in MHz.
        sclk_max      int — maximum (default) GPU clock frequency in MHz.
        fom_direction  str — ``"higher"`` if a larger FOM value means better
                      performance, ``"lower"`` if smaller is better.
        fom_units     str or None — optional human-readable label for FOM
                      units (e.g. ``"GFLOPS"``, ``"seconds"``).
    """

    @abstractmethod
    def __init__(self, config, sclk_min, sclk_max, fom_direction="higher", fom_units=None):
        """Initialize the optimizer from configuration and frequency bounds."""

    @abstractmethod
    def ingest_fom(self, name, value, timestamp_msecs):
        """Process a single FOM sample.

        Args:
            name (str): FOM metric name.
            value (float): FOM value.
            timestamp_msecs (int): Unix timestamp in milliseconds.

        Returns:
            int or None: Target GPU clock frequency in MHz if a change is
            needed, or ``None`` if no adjustment is required.
        """

    @abstractmethod
    def reset(self):
        """Reset the optimizer to its initial state.

        The orchestrator is responsible for restoring GPU clocks to max
        before or after calling this method.
        """

    @abstractmethod
    def get_status(self):
        """Return a dict describing the current optimizer state.

        The returned dict is serialized as JSON by the ``/optimizer/status``
        endpoint.  Include at least ``"state"`` and ``"current_freq_mhz"``.
        """

    @property
    @abstractmethod
    def state(self):
        """Current state as a human-readable string (e.g. ``'IDLE'``)."""

    @property
    @abstractmethod
    def state_int(self):
        """Integer encoding of the current state for Prometheus metrics."""

    @property
    @abstractmethod
    def current_freq(self):
        """Current target GPU clock frequency in MHz."""
