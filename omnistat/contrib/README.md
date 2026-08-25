
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

## Lustre Service Time Collector

The Lustre data collector reports per-RPC **server service time**: how long the
storage servers take to service each I/O request, aggregated over every OST
(Object Storage Target) the node has issued I/O to. The values come from
counters the servers report back to the Lustre client.

**Collector**: `enable_lustre`
<br/>
**Collector options**: `sampling_interval`, `idle_filter`

| Node Metric | Description |
| :---------- | :---------- |
| `omnistat_lustre_read_service_seconds` | Cumulative server service time for bulk read RPCs. |
| `omnistat_lustre_write_service_seconds` | Cumulative server service time for bulk write RPCs. |
| `omnistat_lustre_read_rpcs` | Cumulative bulk read RPCs issued by this client. |
| `omnistat_lustre_write_rpcs` | Cumulative bulk write RPCs issued by this client. |
| `omnistat_lustre_samples_total` | Successful collector samples since startup; used to gate the latency query. |
| `omnistat_lustre_collection_errors_total` | Cumulative procfs files the collector could not read. A *monitoring* failure, not a Lustre I/O error. |

All values are cumulative, so service time per RPC is the ratio of two rates,
gated on the collector still sampling:

```
rate(omnistat_lustre_write_service_seconds[5m])
  / rate(omnistat_lustre_write_rpcs[5m])
  and on(instance) (increase(omnistat_lustre_samples_total[30s]) > 0)
```

Configuration file example with settings related to the Lustre collector:
```ini
[omnistat.collectors]
enable_lustre = True

[omnistat.collectors.contrib.lustre]
sampling_interval = 10
idle_filter = True
```

`sampling_interval` is the cadence of the background sampling thread, which is
independent of the polling interval; it also sets the idle-filter cutoff. The
`idle_filter` skips reading per-target RPC statistics for OSTs with no recent
traffic, roughly a 3x reduction in sampling cost.
