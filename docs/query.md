# Querying job telemetry

The `omnistat-query` utility reads a job's metrics from Prometheus or
VictoriaMetrics and produces a report card. The job ID is required:

```bash
omnistat-query --job "${jobid}"
```

Use `--interval` to set the resolution, in seconds, of the range queries sent
to the metrics server:

```bash
omnistat-query --job "${jobid}" --interval 5
```

The query interval defaults to 30 seconds. When the collection interval is
known, pass the same value to preserve the available resolution. A query
interval shorter than the collection interval cannot recover samples that
were never collected.

## Query duration limits

`omnistat-query` requires at least five query intervals between the detected
start and end of a job. As a result, the approximate minimum supported job
duration is:

```text
minimum duration = 5 * query interval
```

At the other end of the range, VictoriaMetrics limits the number of data
points returned for each time series. The upstream default for
[`-search.maxPointsPerTimeseries`](https://docs.victoriametrics.com/victoriametrics/#list-of-command-line-flags)
is 30,000 points. Omnistat user-mode starts its bundled VictoriaMetrics server
with a 90,000-point limit.

For a given server configuration, estimate the longest queryable job as:

```text
maximum duration = query interval * search.maxPointsPerTimeseries
```

The table below applies those formulas to common query intervals. Maximum
durations are estimates because a range query includes both endpoints.

| Query interval | Minimum duration | Maximum at 30,000 points | Maximum at 90,000 points |
| -------------- | ---------------- | ------------------------ | ------------------------ |
| 0.01 seconds   | 0.05 seconds     | 5 minutes                | 15 minutes               |
| 0.1 seconds    | 0.5 seconds      | 50 minutes               | 2 hours 30 minutes       |
| 1 second       | 5 seconds        | 8 hours 20 minutes       | 25 hours                 |
| 5 seconds      | 25 seconds       | 1 day 17 hours 40 minutes | 5 days 5 hours          |
| 15 seconds     | 75 seconds       | 5 days 5 hours           | 15 days 15 hours         |
| 30 seconds (default) | 150 seconds | 10 days 10 hours        | 31 days 6 hours          |

The server's actual `-search.maxPointsPerTimeseries` setting is authoritative.
System installations can use the VictoriaMetrics default or an
administrator-selected value, so they do not necessarily have the same limit
as Omnistat user-mode.

The minimum sample count and CLI default are defined in
`omnistat/query.py`. The user-mode VictoriaMetrics limit is defined in
`omnistat/omni_util.py`; update this page if those source defaults change.

## Working around the point limit

Choose one of the following approaches when a job is too long for the selected
query interval:

1. Increase `--interval` for this query. The smallest interval that fits is
   approximately `job duration / search.maxPointsPerTimeseries`; round it up
   to avoid crossing the limit. This reduces the number of returned points and
   query cost, but the coarser resolution can hide short peaks and transitions.
2. Ask the VictoriaMetrics administrator to increase
   `-search.maxPointsPerTimeseries`. This preserves finer resolution, but
   larger responses consume more server and client memory and can take longer
   to process.

Changing `--interval` only changes query resolution. It does not change the
sampling interval used when the telemetry was collected.
