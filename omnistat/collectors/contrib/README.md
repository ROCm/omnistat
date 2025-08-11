
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
