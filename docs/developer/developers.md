# Developer Guide

```eval_rst
.. toctree::
   :glob:
   :maxdepth: 4
```

The core telemetry collection facilities within Omnistat are oriented around GPU metrics. However, Omnistat is designed with extensibility in mind and adopts an object oriented approach using [abstract base classes](https://docs.python.org/3/library/abc.html) in Python to facilitate implementation of multiple data collectors. This functionality allows developers to extend Omnistat to add custom data collectors relatively easily by instantiating additional instances of the `Collector` class highlighted below.

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

## Example collector addition
To demonstrate the high-level steps for this process, this section walks thru the steps needed to create an additional collection mechanism within Omnistat to track a node-level metric.  For this example, we assume a developer has already cloned the Omnistat repository locally and has all necessary Python dependencies installed per the {ref}`Installation <system-install>`  discussion.

The specific goal of this example is to extend Omnistat with a new collector that provides a gauge metric called `node_uptime_secs`. This metric will derive information from the `proc/uptime` file to track node uptime in seconds.  In addition, since it is common to include [labels](https://prometheus.io/docs/practices/naming/#labels) with Prometheus metrics, we will include a label on the `node_uptime_secs` metric that tracks the local running Linux kernel version.

```{note}
We prefer to always embed the metric units directly into the name of the metric to avoid ambiguity.
```

### Add runtime config option for new collector

To begin enabling optional support for this new collector, let's first add a runtime option that can be queried during initialization to decide whether to enable the collector or not.  This requires changes to the initialization method of the `Monitor` class of Omnistat housed within the [monitor.py](https://github.com/ROCm/omnistat/blob/main/omnistat/monitor.py) source file. The code snippet below highlights addition of this new runtime option called `enable_uptime` that defaults to `False` (meaning, not enabled by default).

```eval_rst
.. code-block:: python
   :caption: Code modification for **Monitor::__init__** method in omnistat/monitor.py (new runtime option)
   :emphasize-lines: 6

   self.runtimeConfig = {}

   self.runtimeConfig["collector_enable_rocm_smi"] = config["omnistat.collectors"].getboolean("enable_rocm_smi", True)
   self.runtimeConfig["collector_enable_rms"] = config["omnistat.collectors"].getboolean("enable_rms", False)
   self.runtimeConfig["collector_enable_amd_smi"] = config["omnistat.collectors"].getboolean("enable_amd_smi", False)
   self.runtimeConfig["collector_enable_uptime"] = config["omnistat.collectors"].getboolean("enable_uptime", False)
```

### Implement the uptime data collector

Next, let's implement the actual data collection mechanism. Recall that we simply need to implement two methods leveraging the `Collector` base class provided by Omnistat and the code listing below shows a complete working example.  Note that Omnistat data collectors leverage the Python [prometheus client](https://github.com/prometheus/client_python) to define Gauge metrics. In this example, we include a `kernel` label for the `node_uptime_secs` metric that is determined from `/proc/version` during initialization. The node uptime is determined from `/proc/uptime` and is updated on every call to `updateMetrics()`.

```eval_rst
.. literalinclude:: collector_uptime.py
   :caption: Code example implementing an uptime collector: omnistat/collector_uptime.py
   :language: python
   :lines: 25-
```

### Register the new collector

Assuming the raw data collector code from the previous step has been stored locally as `omnistat/collector_uptime.py` file, the final step is to register the new collector when the runtime option is enabled.  This modification also needs to amend the initialization method for the `Monitor` class residing in [monitor.py](https://github.com/ROCm/omnistat/blob/main/omnistat/monitor.py) with the changes necessary highlighted below.

```eval_rst
.. code-block:: python
   :caption: Code modification for **Monitor::__init__** method in omnistat/monitor.py (register collector)
   :emphasize-lines: 5-7

         if self.runtimeConfig["collector_enable_events"]:
            from omnistat.collector_events import ROCMEvents
            self.__collectors.append(ROCMEvents())

         if self.runtimeConfig["collector_enable_uptime"]:
            from omnistat.collector_uptime import NODEUptime
            self.__collectors.append(NODEUptime())
```

### Putting it all together

Following the three steps above to implement a new uptime data collector, we should now be able to run the `omnistat-monitor` data collector interactively to confirm availability of the additional metric.  Since we configured this to be an optional collector that is not enabled by default, we need to first modify the runtime configuration file to enable the new option. To do this, add the highlighted line below to the local `omnistat/config/omnistat.default` file.

```eval_rst
.. code-block:: ini
   :emphasize-lines: 7

   [omnistat.collectors]

   port = 8001
   enable_rocm_smi = True
   enable_amd_smi = False
   enable_rms = False
   enable_uptime = True
```

Now, launch data collector interactively:

```shell-session
[omnidc@login]$ ./omnistat-monitor
```

If all went well, we should see a new log message for the `node_uptime_secs` metric.

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

As a final test while the `omnistat-monitor` client is still running interactively, use a *separate* command shell to query the prometheus endpoint.


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

Here we see the new metric reporting the latest node uptime along with the locally running kernel version embedded as a label.  Wahoo, we did a thing.