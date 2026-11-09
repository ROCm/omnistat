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

import logging

from abc import ABC, abstractmethod

from omnistat import optimizer_signals as sig


# Canonical state -> integer encoding, published as the omnistat_optimize
# metric. Prometheus gauges hold numbers only, so the human-readable state has
# to be encoded to be plottable -- but that encoding belongs here, once, not in
# every plugin. It used to live in each optimizer module and they disagreed:
# optimizer_stepdown mapped CONVERGED to 1 while optimizer_utilization mapped
# CONTROLLING to 1, so the same metric value meant different things depending
# on which plugin was loaded. Values below preserve what the two maps agreed
# on; CONVERGED moved 1 -> 6 to resolve the collision.
STATE_INT = {
    "IDLE": 0,
    "CONTROLLING": 1,
    "WARMING_UP": 2,
    "OBSERVING_BASELINE": 3,
    "STEPPING_DOWN": 4,
    "BACKED_OFF": 5,
    "CONVERGED": 6,
}
STATE_INT_UNKNOWN = -1


def register_state(name, value):
    """Add a plugin-specific state. Raises on a value already in use."""
    clash = [k for k, v in STATE_INT.items() if v == value and k != name]
    if clash:
        raise ValueError("state value %d already used by %s" % (value, clash[0]))
    STATE_INT[name] = value


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

    Two ingest paths exist:

        ingest_fom(name, value, ts)   original scalar FOM path.  Returns an int
                                      frequency for *all* GPUs, or None.

        ingest(batch)                 self-describing sample batch (see
                                      optimizer_signals).  Returns a list of
                                      clock decisions, each optionally carrying
                                      a ``gpu_index`` for per-GPU control.

    A plugin implements whichever it needs.  The default ``ingest()`` below
    extracts FOM samples and forwards them to ``ingest_fom()``, so plugins
    written against the original interface keep working unchanged.
    """

    @abstractmethod
    def __init__(self, config, sclk_min, sclk_max, fom_direction="higher", fom_units=None):
        """Initialize the optimizer from configuration and frequency bounds."""

    def ingest_fom(self, name, value, timestamp_msecs):
        """Process a single FOM sample.

        Args:
            name (str): FOM metric name.
            value (float): FOM value.
            timestamp_msecs (int): Unix timestamp in milliseconds.

        Returns:
            int or None: Target GPU clock frequency in MHz if a change is
            needed, or ``None`` if no adjustment is required.

        Optimizers driven by something other than FOM (utilization, counters)
        may leave this unimplemented and override ``ingest()`` instead.
        """
        return None

    def ingest(self, batch):
        """Process a self-describing sample batch.

        Args:
            batch (dict): see ``optimizer_signals.make_batch``.

        Returns:
            list: clock decisions from ``optimizer_signals.clock_decision``.
            An empty list means no change.  A decision without ``gpu_index``
            applies to all GPUs.

        The default implementation forwards FOM samples to ``ingest_fom()`` so
        scalar-FOM plugins need no changes.  Override for per-device control or
        to consume utilization / counter samples.
        """
        decisions = []
        for s in sig.select(batch, sig.KIND_FOM):
            out = self.ingest_fom(s.get("name"), s.get("value"), batch.get("timestamp_msecs"))
            decisions.extend(sig.normalize_decisions(out))
        return decisions

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
    def state_int(self):
        """Integer encoding of ``state``, for Prometheus.

        Derived, not implemented by plugins: a plugin supplies ``state`` as a
        string and the framework encodes it. Override only to publish a state
        outside the shared registry, and prefer ``register_state()`` to adding
        a private map -- that is what caused the two encodings to disagree.
        """
        value = STATE_INT.get(self.state)
        if value is None:
            logging.warning(
                "[optimizer] state %r is not in STATE_INT; publishing %d. "
                "Use register_state() to give it a value." % (self.state, STATE_INT_UNKNOWN)
            )
            return STATE_INT_UNKNOWN
        return value

    @property
    @abstractmethod
    def current_freq(self):
        """Current target GPU clock frequency in MHz."""
