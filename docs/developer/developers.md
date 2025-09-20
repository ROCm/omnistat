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

   # Base Collector class - defines optional and required methods for all metric collectors
   # implemented as a child class.

   from abc import ABC, abstractmethod

   class Collector(ABC):
      # Optional init method that can be overridden
      def __init__(self, config: configparser.ConfigParser):
        """Optional initializer for Collector.

        Args:
            config (configparser.ConfigParser): Cached copy of runtime configuration.
        """
        self.runtimeConfig = config

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

As shown above, the base `Collector` class requires developers to implement a minimum of **two** methods when adding a new data collector:

1. `registerMetrics()`: this method is called once during Omnistat startup process and defines one or more Prometheus metrics to be monitored by the new collector.
1. `updateMetrics()`: this method is called during every sampling request and is tasked with updating all defined metrics with the latest measured values.

In addition, developers may also override the default `__init__` method shown above. When overriding the default implementation, note the `configparser.ConfigParser` argument which provides a cached copy of the Omnistat runtime configuration that can be used to further control collector behavior.

Note: developers are free to implement other supporting routines to assist in their data collection needs, but are required to implement the two named methods above.

## Example collector addition
To demonstrate the high-level steps for this process, this section walks thru the steps needed to create an additional collection mechanism within Omnistat to track a node-level metric.  For this example, we assume a developer has already cloned the Omnistat repository locally and has all necessary Python dependencies installed per the {ref}`Installation <system-install>`  discussion.

The specific goal of this example is to extend Omnistat with a new collector that provides a gauge metric called `node_uptime_secs`. This metric will derive information from the `/proc/uptime` file to track node uptime in seconds.  In addition, since it is common to include [labels](https://prometheus.io/docs/practices/naming/#labels) with Prometheus metrics, we will include a label on the `node_uptime_secs` metric that tracks the local running Linux kernel version.

```{note}
We prefer to always embed the metric units directly into the name of the metric to avoid ambiguity.
```

### Implement the uptime data collector

First, let's implement the uptime data collection in a new source code file. Recall that we need to implement two methods leveraging the `Collector` base class provided by Omnistat and the code listing below shows a complete working example.  Note that Omnistat data collectors leverage the Python [prometheus client](https://github.com/prometheus/client_python) to define Gauge metrics. In this example, we include a `kernel` label for the `node_uptime_secs` metric that is determined from `/proc/version` during initialization. The node uptime is determined from `/proc/uptime` and is updated on every call to `updateMetrics()`.

```eval_rst
.. literalinclude:: collector_uptime.py
   :caption: Code example implementing an uptime collector: omnistat/collector_uptime.py
   :language: python
   :lines: 25-
```

### Register the new collector

With our newly created collector housed in *omnistat/collector_uptime.py*, the next step to is  to register this new collector with Omnistat.  Collector definitions are defined in a JSON file for dynamic loading housed in the [collector_definitions.py](https://github.com/ROCm/omnistat/blob/main/omnistat/collector_definitions.json) file.  Four elements are required to define a new collector:
1. **runtime_option** - specifies the runtime configuration variable
1. **enabled_by_default** - specifies whether to enable by default or not
1. **file** - file path to the collector (omitting the .py extension)
1. **class_name** - name of defined collector class
        
The code snippet below highlights changes applied to the JSON file to add a registration block for the new uptime collector (not enabled by default):

```eval_rst
.. code-block:: python
   :caption: Code modification for JSON in omnistat/collector_definitions.py to register new uptime collector
   :emphasize-lines: 7-12

        {
            "runtime_option": "enable_kmsg",
            "enabled_by_default": false,
            "file": "omnistat.contrib.collector_kmsg",
            "class_name": "KmsgCollector"
        },
        {
            "runtime_option": "enable_uptime",
            "enabled_by_default": false,
            "file": "omnistat.collector_uptime",
            "class_name": "NODEUptime"
        },
        {
            "runtime_option": "enable_vendor_counters",
            "enabled_by_default": true,
            "file": "omnistat.collector_pm_counters",
            "class_name": "PM_COUNTERS"
        }
```

### Putting it all together

Following the two steps above to implement a new uptime data collector, we should now be able to run the `omnistat-monitor` data collector interactively to confirm availability of the additional metric.  Since we configured this to be an optional collector that is not enabled by default, we need to first modify the runtime configuration file to enable the new option. To do this, add the highlighted line below to the local `omnistat/config/omnistat.default` file.

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

If all went well, we should see new log messages for the `node_uptime_secs` metric.

```eval_rst
.. code-block:: shell-session
   :emphasize-lines: 21-22

   Reading configuration from /home1/omnidc/omnistat/omnistat/config/omnistat.default
   ...
   GPU topology indexing: Scanning devices from /sys/class/kfd/kfd/topology/nodes
   --> Mapping: {0: '1', 1: '0'}
   --> Using primary temperature location at edge
   --> Using HBM temperature location at vram
   --> [registered] rocm_temperature_celsius -> Temperature (C) (gauge)
   --> [registered] rocm_temperature_memory_celsius -> Memory Temperature (C) (gauge)
   --> [registered] rocm_average_socket_power_watts -> Average Graphics Package Power (W) (gauge)
   --> [registered] rocm_sclk_clock_mhz -> current sclk clock speed (Mhz) (gauge)
   --> [registered] rocm_mclk_clock_mhz -> current mclk clock speed (Mhz) (gauge)
   --> [registered] rocm_vram_total_bytes -> VRAM Total Memory (B) (gauge)
   --> [registered] rocm_vram_used_percentage -> VRAM Memory in Use (%) (gauge)
   --> [registered] rocm_vram_busy_percentage -> Memory controller activity (%) (gauge)
   --> [registered] rocm_utilization_percentage -> GPU use (%) (gauge)

   Registering metrics for collector: NETWORK
      --> [registered] omnistat_network_rx_bytes -> Network received (bytes) (gauge)
      --> [registered] omnistat_network_tx_bytes -> Network transmitted (bytes) (gauge)

   Registering metrics for collector: NODEUptime
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
   node_uptime_secs{kernel="5.14.0-503.38.1.el9_5.x86_64"} 948415.84
```

Here we see the new metric reporting the latest node uptime along with the locally running kernel version embedded as a label.  Wahoo, we did a thing.