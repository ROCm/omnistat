
# Omnistat Contrib Area

This `contrib` area houses features that are not currently part of
Omnistat proper and as a result, may be subject to significant changes
between releases and/or removals/additions. Items herein may include
technology previews, experimental features, or 3rd-party contributions
of general interest.

An overview of current contrib features is highlighted below.

---

## GPU Driver Message Collector 

The driver messages data collector monitors the Linux kernel message buffer
(`/dev/kmsg`) for driver-related messages, particularly those from AMD GPU
drivers. This collector helps track kernel-level events and errors that may
impact system stability and GPU functionality. The collector can be configured
to monitor different severity levels and can optionally include existing
messages in the buffer at startup.

> [!NOTE]
> This collector requires read access to `/dev/kmsg` to function properly.

The collector supports filtering messages by the following kernel log severity
levels (from most to least critical): `EMERGENCY`, `ALERT`, `CRITICAL`,
`ERROR`, `WARNING`, `NOTICE`, `INFO`, `DEBUG`. The `min_severity`
configuration option determines which severity levels are monitored. For
example, setting `min_severity = WARNING` will collect messages with severity
levels from `EMERGENCY` down to `WARNING`.

**Collector**: `enable_contrib_kmsg`
<br/>
**Collector options**: `min_severity`, `include_existing_messages`

| Node Metric                     | Description                          |
| :------------------------------ | :----------------------------------- |
| `omnistat_num_driver_messages`  | Number of driver messages in the kernel log buffer, counted by driver and severity level. Labels: `driver`, `severity`. |

Configuration file example with settings related to the GPU Driver Message
Collector:
```ini
[omnistat.collectors.contrib]
enable_kmsg = True

[omnistat.collectors.contrib.kmsg]
min_severity = ERROR
include_existing_messages = False
```

---

## Lustre RPC Latency Collector

The Lustre data collector measures how long each read and write request takes to
complete, across every OST (Object Storage Target) the node has issued I/O to.
Metadata operations are not included. Knowing the per-request latency separates
a congested filesystem from a job that simply asked for little, which is not
possible with throughput alone.

The collector keeps two counters per OST pool, the total time requests spent
outstanding and the number of requests that completed, and the ratio of the two
over any window is the mean latency across that window.
That mean covers every request in the window, and request size and queue depth
move it as much as the filesystem does. There is no latency histogram either,
so a long tail is averaged away rather than shown. Read absolute values with
that in mind: comparing unlike jobs mostly compares their I/O patterns.

Timing runs from just before a request goes on the wire to its reply arriving,
covering the round trip, the server, and the data transfer, but not client-side
setup. It is therefore neither server time alone nor the full latency the
application sees. Counters start when the collector does, not at boot.

The read and write metrics are reported per OST pool, labelled by `filesystem`,
the Lustre filesystem name, and `pool`, the pool the targets belong to.

**Collector**: `enable_lustre`
<br/>
**Collector options**: `sampling_interval`

| Node Metric | Description |
| :---------- | :---------- |
| `omnistat_lustre_read_seconds` | Cumulative seconds bulk read RPCs spent outstanding. Labels: `filesystem`, `pool`. |
| `omnistat_lustre_write_seconds` | Cumulative seconds bulk write RPCs spent outstanding. Labels: `filesystem`, `pool`. |
| `omnistat_lustre_read_rpcs` | Cumulative bulk read RPCs completed. Labels: `filesystem`, `pool`. |
| `omnistat_lustre_write_rpcs` | Cumulative bulk write RPCs completed. Labels: `filesystem`, `pool`. |
| `omnistat_lustre_read_uncertainty_seconds` | Bound, in seconds, on the reconstruction error in `read_seconds`. Labels: `filesystem`, `pool`. |
| `omnistat_lustre_write_uncertainty_seconds` | Bound, in seconds, on the reconstruction error in `write_seconds`. Labels: `filesystem`, `pool`. |
| `omnistat_lustre_read_congested_rpcs` | Read RPCs admitted with 31 or more already in flight to the same target. A congestion tripwire, and a marker for where queue-depth data is censored. Labels: `filesystem`, `pool`. |
| `omnistat_lustre_write_congested_rpcs` | Write RPCs admitted with 31 or more already in flight to the same target. Labels: `filesystem`, `pool`. |
| `omnistat_lustre_samples_total` | Sweeps that read at least one target since startup. Stops advancing if the collector stalls. |
| `omnistat_lustre_collection_errors_total` | Cumulative procfs files the collector could not read. A *monitoring* failure, not a Lustre I/O error. |
| `omnistat_lustre_sweep_seconds` | Duration of the most recent sweep. |

Three things have to be read together. First, the latency itself,
count-weighted so a busy second is not averaged against an idle one:

```
increase(omnistat_lustre_read_seconds[1m])
  / increase(omnistat_lustre_read_rpcs[1m])
```

Second, how many requests that average covers, since a handful of them is real
but not representative:

```
increase(omnistat_lustre_read_rpcs[1m])
```

And finally, how much of it is reconstruction error rather than signal, as a
fraction of the latency above. This is a conservative upper bound covering
rounding only, so a small value is reassuring, while one approaching 1 means the
window is too thin to read as a trend:

```
increase(omnistat_lustre_read_uncertainty_seconds[1m])
  / increase(omnistat_lustre_read_seconds[1m])
```

Configuration file example with settings related to the Lustre collector:
```ini
[omnistat.collectors]
enable_lustre = True

[omnistat.collectors.contrib.lustre]
sampling_interval = 10
```

`sampling_interval` is the cadence of the background sampling thread in
seconds, independent of the polling interval. A sweep costs roughly 140 ms
across 1350 OSTs, so it should stay well above the polling interval.
