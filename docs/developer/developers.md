# Developer Guide

```eval_rst
.. toctree::
   :glob:
   :maxdepth: 4
```


The core telemetry collection facilities within Omnistat are oriented around
GPU metrics. However, Omnistat is designed with extensibility in mind and
adopts an object-oriented approach using [abstract base
classes](https://docs.python.org/3/library/abc.html) in Python to facilitate
implementation of multiple data collectors. This functionality allows
developers to extend Omnistat to add custom data collectors relatively easily
by creating new modules under `omnistat/collectors` or
`omnistat/collectors/contrib` that implement the `Collector` class highlighted
below.

```eval_rst
.. code-block:: python
   :caption: Base class definition housed in omnistat/collector_base.py

   # Base Collector class - defines required methods for all metric collectors
   # implemented as a child class.

   from abc import ABC, abstractmethod

   class Collector(ABC):
      # Required methods to be implemented by child classes
      @abstractmethod
      def registerMetrics(self):
         """Defines desired metrics to monitor with Prometheus. Called only once."""
         pass

      @abstractmethod
      def updateMetrics(self):
         """Updates defined metrics with latest values. Called at every polling interval."""
         pass
```

As shown above, the base `Collector` class requires developers to implement **two** methods when adding a new data collection mechanism:

1. `registerMetrics()`: this method is called once during Omnistat startup process and defines one or more Prometheus metrics to be monitored by the new collector.
1. `updateMetrics()`: this method is called during every sampling request and is tasked with updating all defined metrics with the latest measured values.

Note: developers are free to implement other supporting routines to assist in their data collection needs, but are required to implement the two named methods above.


## Example: Adding a New Collector

This section demonstrates the high-level steps needed to create an additional collection mechanism within Omnistat to track a node-level metric. For this example, we assume a developer has already cloned the Omnistat repository locally and has all necessary Python dependencies installed per the {ref}`Installation <system-install>` discussion.

The specific goal of this example is to extend Omnistat with a new collector that provides a gauge metric called `node_uptime_secs`. This metric will derive information from the `proc/uptime` file to track node uptime in seconds. In addition, since it is common to include [labels](https://prometheus.io/docs/practices/naming/#labels) with Prometheus metrics, we will include a label on the `node_uptime_secs` metric that tracks the local running Linux kernel version.

```{note}
We prefer to always embed the metric units directly into the name of the metric to avoid ambiguity.
```

### Implement the Uptime Data Collector

Create a new Python file for your collector in `omnistat/collectors/` or `omnistat/collectors/contrib/`. For example, create `omnistat/collectors/uptime.py`.

Implement your collector by subclassing the `Collector` base class and implementing the required `registerMetrics()` and `updateMetrics()` methods. Omnistat data collectors leverage the Python [prometheus client](https://github.com/prometheus/client_python) to define Gauge metrics. In this example, we include a `kernel` label for the `node_uptime_secs` metric that is determined from `/proc/version` during initialization. The node uptime is determined from `/proc/uptime` and is updated on every call to `updateMetrics()`.

**Important:** The class name for your collector must exactly match the file name of the collector. For example, if your file is `uptime.py`, your class should be `uptime`. This naming convention is required for Omnistat to automatically discover and register your collector.

```eval_rst
.. literalinclude:: collector_uptime.py
   :caption: Code example implementing an uptime collector: omnistat/collectors/uptime.py
   :language: python
   :lines: 25-
```

### Enable the Collector in the Config File

Omnistat automatically discovers and registers collectors based on configuration options. Simply add a new `enable_<collector>` option to your config file (e.g., `omnistat/config/omnistat.default`) and set it to `True` to enable your collector. For example:

```ini
[omnistat.collectors]
port = 8001
enable_rocm_smi = True
enable_amd_smi = False
enable_rms = False
enable_uptime = True
```

Now, launch the data collector interactively:

```shell-session
$ ./omnistat-monitor
```

If all went well, you should see a new log message for the `node_uptime_secs` metric.

```eval_rst
.. code-block:: shell-session
   :emphasize-lines: 16

   Reading configuration from /home1/omnidc/omnistat/omnistat/config/omnistat.default
   ...
   GPU topology indexing: Scanning devices from /sys/class/kfd/kfd/topology/nodes
   --> Mapping: {0: '3', 1: '2', 2: '1', 3: '0'}
   --> Using primary temperature location at edge
   --> Using HBM temperature location at hbm_0
   --> [registered] rocm_temperature_celsius -> Temperature (C) (gauge)
   --> [registered] rocm_temperature_hbm_celsius -> HBM Temperature (C) (gauge)
   --> [registered] rocm_average_socket_power_watts -> Average Graphics Package Power (W) (gauge)
   --> [registered] rocm_sclk_clock_mhz -> current sclk clock speed (Mhz) (gauge)
   --> [registered] rocm_mclk_clock_mhz -> current mclk clock speed (Mhz) (gauge)
   --> [registered] rocm_vram_total_bytes -> VRAM Total Memory (B) (gauge)
   --> [registered] rocm_vram_used_percentage -> VRAM Memory in Use (%) (gauge)
   --> [registered] rocm_vram_busy_percentage -> Memory controller activity (%) (gauge)
   --> [registered] rocm_utilization_percentage -> GPU use (%) (gauge)
   --> [registered] node_uptime_secs -> System uptime (secs) (gauge)
```

As a final test while the `omnistat-monitor` client is still running interactively, use a *separate* command shell to query the Prometheus endpoint.

```eval_rst
.. code-block:: shell-session
   :emphasize-lines: 12

   [omnidc@login]$ curl localhost:8001/metrics | grep -v "^#"
   rocm_num_gpus 4.0
   rocm_temperature_celsius{card="3",location="edge"} 38.0
   rocm_temperature_celsius{card="2",location="edge"} 43.0
   rocm_temperature_celsius{card="1",location="edge"} 40.0
   rocm_temperature_celsius{card="0",location="edge"} 54.0
   rocm_average_socket_power_watts{card="3"} 35.0
   rocm_average_socket_power_watts{card="2"} 33.0
   rocm_average_socket_power_watts{card="1"} 35.0
   rocm_average_socket_power_watts{card="0"} 35.0
   ...
   node_uptime_secs{kernel="5.14.0-162.18.1.el9_1.x86_64"} 280345.19
```

Here we see the new metric reporting the latest node uptime along with the locally running kernel version embedded as a label. Wahoo, we did a thing.
