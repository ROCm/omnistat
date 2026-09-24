# Advanced Profiling

Omnistat supports two optional data collectors that instrument the GPU directly
to provide more detailed performance data than the standard telemetry
collectors:

* **[Hardware counters](#hardware-counters)** sample low-level GPU performance counters (e.g. cache
  traffic, cycles, memory requests, floating-point instructions) at the device level.
* **[Kernel tracing](#kernel-tracing)** records every GPU kernel dispatch, with
  its name and execution duration.

Both collectors are disabled by default and require additional setup beyond a
runtime configuration setting: a local build step and, in most cases, an
environment variable defined for the application being monitored rather than
for Omnistat. The sections that follow outline this setup along with the
available runtime configuration options. A complete list of the resulting
metric names is provided in the [Hardware
Counters](metrics.md#hardware-counters) and [Kernel
Tracing](metrics.md#kernel-tracing) sections of the metrics reference.

|                                    | Hardware counters                                 | Kernel tracing                        |
| :--------------------------------- | :------------------------------------------------ | :------------------------------------ |
| Collector option                   | `enable_rocprofiler`                               | `enable_kernel_trace`                 |
| Availability and build step        | {ref}`System-mode <optional-components>` and {ref}`user-mode <user-optional-components>` | {ref}`User-mode <user-optional-components>` only |
| Library loaded into the application | `libomnistat_count.so` (user-mode only)           | `libomnistat_trace.so` (always)       |

<hr style="border: 1px solid black;">

## Hardware counters

Omnistat samples hardware counters using ROCProfiler-SDK's *device counting*
service. Unlike a traditional profiler, it does not attach to a specific
process or serialize kernel dispatches: it reads counter values from the GPU
at each Omnistat sampling interval, so the overhead is that of a periodic
register read rather than per-kernel instrumentation.

The trade-off is that counters are a device-wide, time-sampled view. Values are
not attributed to individual kernels, and on a shared node they reflect
whatever work the GPU was doing at sampling time.

### Prerequisites

1. Build and install the ROCprofiler collector extension, as described under
   Optional component(s) for {ref}`system-mode <optional-components>` or
   {ref}`user-mode <user-optional-components>`. Omnistat reports an error and
   exits if the extension is missing.
2. Enable the collector in the Omnistat configuration file:
   ```ini
   [omnistat.collectors]
   enable_rocprofiler = True
   ```
3. Arrange for performance monitoring privileges, as described below. The
   requirements differ between the two modes of operation.

(counter-privileges)=
### Performance monitoring privileges

Reading GPU hardware counters requires performance monitoring privileges. The
kernel grants these through either the `CAP_PERFMON` capability (or the broader
`CAP_SYS_ADMIN`), or a permissive value of `/proc/sys/kernel/perf_event_paranoid`.
Omnistat checks for both at collector startup and warns when neither is
satisfied, in which case some or all counter values may be unavailable.

#### System-mode

Grant `CAP_PERFMON` to the Omnistat service. The
[service file](system-mode/system-install.md) shipped with Omnistat does *not*
set any capabilities, so this must be added locally, for example with a systemd
drop-in:

```{eval-rst}
.. code-block:: ini
   :caption: /etc/systemd/system/omnistat.service.d/perfmon.conf

   [Service]
   AmbientCapabilities=CAP_PERFMON
   CapabilityBoundingSet=CAP_PERFMON
```

(counter-enablement-usage)=
#### User-mode

Without administrative rights, `CAP_PERFMON` is not an option, and
`/proc/sys/kernel/perf_event_paranoid` must instead be `2` or less. Some
distributions ship a default of `4`, in which case a site administrator has to
lower it (typically via a `sysctl.d` drop-in); it cannot be changed from within
a job.

User-mode collection additionally requires the counter enablement library
(`libomnistat_count.so`) to be loaded into the application being monitored.
ROCProfiler-SDK only makes a process's GPU queues visible to counter collection
when that process has registered a counting service, and this library does
exactly that and nothing else: it registers the service and never starts it, so
it collects no data of its own. It also leaves the application's queues in
place rather than replacing them, so any CU masks or queue priorities the
application set are preserved.

Point `ROCP_TOOL_LIBRARIES` at the library in the environment of the
application:

```shell
export ROCP_TOOL_LIBRARIES=/path/to/build-count/libomnistat_count.so
```

```{important}
This variable belongs in the environment of the **application being
monitored**, not in Omnistat's environment. Counters are only collected for
queues belonging to processes that loaded the library; work from any other
process is invisible to the sampled values.
```

On startup the library writes a single line to the application's standard error,
reporting either `Omnistat: counters enabled` or `Omnistat: counters disabled`
followed by a reason, such as no visible GPU agent, a registration rejected by
ROCProfiler-SDK, or a ROCProfiler-SDK version mismatch.

An application that never loads the library collects no counters and reports no
error at all, so this line is the only confirmation that counter enablement is
active. The library refuses to load when the ROCProfiler-SDK major version it
was built against differs from the one the application is running against, so
sites offering several ROCm installations need one build per installation.

(counter-profiles)=
### Counter profiles

Which counters are collected, and how they are distributed across the GPUs in a
node, is described by a *profile*. The `profile` option in the
`[omnistat.collectors.rocprofiler]` section names a profile, and the matching
`[omnistat.collectors.rocprofiler.<profile>]` section defines it. When no
profile is named, `default` is used.

A profile section accepts two options:

- `sampling_mode`: how counter sets are distributed across the GPUs of a node.
  One of `constant`, `gpu-id`, or `periodic`, described below. Defaults to
  `constant`.
- `counters`: one or more sets of counters, written as a JSON list. A flat list
  such as `["GRBM_COUNT", "GRBM_GUI_ACTIVE"]` is a single set; a nested list
  such as `[["FETCH_SIZE"], ["WRITE_SIZE"]]` is multiple sets. For the counters
  available on a given architecture, see the [ROCm
  documentation](https://rocm.docs.amd.com/en/latest/reference/gpu-arch/mi300-mi200-performance-counters.html#mi300-and-mi200-series-performance-counters).

Profile problems are fatal rather than degraded: a profile section that is
missing or omits `counters`, a `counters` value that is not valid JSON, an
unrecognized `sampling_mode`, a set count that does not match the mode, or a
counter name the GPU does not support all cause Omnistat to log an error and
exit with status 4, leaving the node with no telemetry at all.

Multiple profiles can be defined in the same configuration file; only the one
named by `profile` is active. The configuration shipped with Omnistat
(`omnistat/config/omnistat.default`) defines several ready-to-use profiles as
worked examples.

#### Sampling modes

The number of counters that can be collected simultaneously is limited by the
hardware: each GPU block has a fixed number of counter registers. Sampling
modes exist to work within that limit by spreading counters across GPUs or
across time.

| `sampling_mode` | Counter sets required | Behavior                                                                                   |
| :-------------- | :--------------------- | :----------------------------------------------------------------------------------------- |
| `constant`      | exactly one            | Every GPU collects the same set on every sample.                                             |
| `gpu-id`        | two or more            | Sets are assigned cyclically to GPU IDs, so different GPUs collect different counters.      |
| `periodic`      | one or more            | All GPUs rotate through the sets, advancing to the next set after every sample.              |

`constant` gives a complete picture on every GPU, but only for as many counters
as fit in hardware at once.

`gpu-id` trades spatial uniformity for coverage: with a symmetric workload
across GPUs, sampling different counters on different GPUs approximates
collecting all of them everywhere. It requires at least as many GPUs per node
as counter sets.

`periodic` trades temporal resolution for coverage in the same way, and is the
option when a workload is not symmetric across GPUs. Note that in this mode
counter values are reset at each sampling interval rather than accumulated.

#### Examples

```{eval-rst}
.. code-block:: ini
   :caption: Free-running and active cycles, collected from every GPU on every sample

   [omnistat.collectors.rocprofiler]
   profile = cycles

   [omnistat.collectors.rocprofiler.cycles]
   sampling_mode = constant
   counters = ["GRBM_COUNT", "GRBM_GUI_ACTIVE"]
```

```{eval-rst}
.. code-block:: ini
   :caption: HBM reads and writes, split across alternating GPU IDs

   [omnistat.collectors.rocprofiler]
   profile = hbm

   [omnistat.collectors.rocprofiler.hbm]
   sampling_mode = gpu-id
   counters = [["FETCH_SIZE"], ["WRITE_SIZE"]]
```

With the `hbm` profile above on a node with four GPUs, GPU IDs 0 and 2 report
`FETCH_SIZE` while GPU IDs 1 and 3 report `WRITE_SIZE`.

```{eval-rst}
.. code-block:: ini
   :caption: HBM reads and writes, alternating on every sample across all GPUs

   [omnistat.collectors.rocprofiler]
   profile = hbm_periodic

   [omnistat.collectors.rocprofiler.hbm_periodic]
   sampling_mode = periodic
   counters = [["FETCH_SIZE"], ["WRITE_SIZE"]]
```

#### Reported values

All counters are reported through a single metric,
`omnistat_hardware_counter`, distinguished by labels: `card` identifies the
GPU, `name` carries the counter name as written in the profile, and `source` is
always `gpu`.

```text
omnistat_hardware_counter{source="gpu",card="0",name="GRBM_COUNT"} 1.42e+09
omnistat_hardware_counter{source="gpu",card="0",name="GRBM_GUI_ACTIVE"} 8.31e+08
```

Counters that the hardware reports per shader engine or per compute unit are
summed into a single value per GPU, so a reported value represents total
activity across the device rather than one hardware instance.

<hr style="border: 1px solid black;">

## Kernel tracing

`libomnistat_trace.so` is loaded into the application and intercepts every GPU
kernel dispatch, recording the kernel name, the GPU it ran on, and its start
and end timestamps. Kernel names are demangled, so they appear as readable C++
signatures.

Dispatch records are buffered in the application and sent over HTTP to the
Omnistat collector running on the same node, which aggregates them into
per-kernel time series. The result is a breakdown of how GPU time was actually
spent over the course of a run, rather than an aggregate utilization figure.

```{note}
Kernel tracing is **user-mode only**. Setting `enable_kernel_trace = True` in a
system-mode configuration has no effect and produces no warning.
```

### Prerequisites

1. Build the kernel tracing library (`libomnistat_trace.so`), as described under
   {ref}`Optional component(s) <user-optional-components>`. No Omnistat build
   step is needed; the library is standalone.
2. Enable the collector in the Omnistat configuration file used for the job:
   ```ini
   [omnistat.collectors]
   enable_kernel_trace = True
   ```
3. Load the library into the application by setting `ROCP_TOOL_LIBRARIES` in
   its environment:
   ```shell
   export ROCP_TOOL_LIBRARIES=/path/to/build-trace/libomnistat_trace.so
   ```

As with counter enablement, this variable must be set for the GPU application,
not for Omnistat. As with the counter enablement library, a build is needed for
each ROCm installation in use: a library built against a different
ROCProfiler-SDK major version reports a version mismatch and traces nothing.

Records are sent to `http://localhost:<port>/kernel_trace`, where `<port>`
defaults to `8001` and must match the `port` option in the
`[omnistat.collectors]` section of the Omnistat configuration. If a batch
cannot be delivered, the library writes `Omnistat: failed to post kernel trace
data` to standard error and those records are lost, which is what a missing or
not-yet-started Omnistat collector looks like from the application side.

(kernel-trace-tuning)=
### Tuning

The tracing library is configured entirely through environment variables set
alongside `ROCP_TOOL_LIBRARIES`. The defaults are appropriate for most runs.

| Variable                       | Default            | Description                                                      |
| :----------------------------- | :----------------- | :--------------------------------------------------------------- |
| `OMNISTAT_TRACE_MAX_INTERVAL`  | `13` (seconds)     | Maximum time between periodic buffer flushes.                     |
| `OMNISTAT_TRACE_BUFFER_SIZE`   | `262144` (bytes)   | Size of the ROCProfiler-SDK buffer holding dispatch records.      |
| `OMNISTAT_TRACE_ENDPOINT_PORT` | `8001`             | Port of the Omnistat endpoint receiving trace data.               |
| `OMNISTAT_TRACE_LOG`           | `0`                | Set to `1` to print a trace summary on application exit.          |

Each of these expects a positive integer. A value of `0`, or one that does not
begin with a digit, is reported as invalid on standard error and the default is
used instead, so setting `OMNISTAT_TRACE_MAX_INTERVAL` or
`OMNISTAT_TRACE_BUFFER_SIZE` to `0` does not disable flushing or buffering.
Parsing is otherwise lenient: trailing characters are ignored, so a value like
`13s` is accepted as `13` without comment.

With `OMNISTAT_TRACE_LOG=1`, the library prints a summary line per process when
the application exits, which is the quickest way to confirm that tracing worked
end to end:

```text
[node01][12345][omnistat] Trace summary: 1234/1234 processed records (12/12 successful flushes)
```

Omnistat retains roughly the last 15 seconds of time bins, and a record whose
kernel end timestamp falls outside that window, either older than the oldest
retained bin or later than the newest, is counted in
`omnistat_kernel_dropped_dispatches` rather than recorded. The default flush
interval of 13 seconds therefore leaves only a couple of seconds of margin, and
values of `OMNISTAT_TRACE_MAX_INTERVAL` at or above 15 drop the oldest records
of every batch. Timestamps that land in the future instead point at clock skew.
Dropping a record affects the recorded time series only, not the running
application.

<hr style="border: 1px solid black;">

(combining-counters-and-tracing)=
## Combining counters and tracing

Hardware counters and kernel tracing can be collected in the same user-mode
run. Enable both collectors in the configuration file, and list both libraries
in `ROCP_TOOL_LIBRARIES`, separated by colons:

```shell
export ROCP_TOOL_LIBRARIES=/path/to/libomnistat_count.so:/path/to/libomnistat_trace.so:
```

```{warning}
The trailing colon is required. ROCProfiler-SDK drops the last entry while
parsing this variable, so without it the final library is never loaded. Because
a library that is not loaded reports nothing, there is no error to go on: the
symptom is simply that one of the two data sources is silently absent.
```

<hr style="border: 1px solid black;">

## Older ROCm releases

In ROCm versions before 10, counter collection was enabled with the
ROCProfiler v1 tool library rather than `libomnistat_count.so`:

```shell
export HSA_TOOLS_LIB=/opt/rocm/lib/librocprofiler64.so
export HSA_TOOLS_ROCPROFILER_V1_TOOLS=1
```

The ROCProfiler v1 tool library mechanism these variables rely on is no longer
available in ROCm 10, so they have no effect there. Configurations carried over
from an older Omnistat or ROCm release should be updated to load
`libomnistat_count.so` via `ROCP_TOOL_LIBRARIES` instead.
