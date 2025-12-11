# Grafana Dashboards

Omnistat provides several sample Grafana dashboards for cluster-wide
deployments that vary depending on the target environment. For example,
whether resource manager integration is desired or not.

Dashboard variants are automatically generated from *source* available under
the `source` directory. *Source* dashboards include supersets of panels, which
can be filtered out using the `filter-dashboard` script.

The following table shows a list of the sample pre-generated dashboards
distributed with Omnistat, along with their supported metrics:

| Dashboard           | Omnistat and ROCm      | RMS                |
| :---:               | :---:                  | :---:              |
| `source`            | :heavy_check_mark:[^1] | :heavy_check_mark: |
| `system/rms`        | :heavy_check_mark:     | :heavy_check_mark: |
| `system/standalone` | :heavy_check_mark:     |                    |
| `docker`[^3]        | :heavy_check_mark:     | :heavy_check_mark: |

[^1]: Includes throttling events (experimental).
[^2]: Includes CPU, memory, IO, ethernet, and IB panels.
[^3]: Docker dashboards are meant to be used in local Docker-based Grafana
      instances to access local data collected in usermode.

## Generate Custom Dashboards

The `filter-dashboard` script can also be used to generate new customized
dashboards with different subsets of panels. For example, the following command
can be used to generate a dashboard with support for ROCm and RMS metrics,
excluding panels that use metrics from host collector:

```
./scripts/filter-dashboard -i json-models/system/rms-global.json --exclude-metrics omnistat_host_
```

Note that dashboard variants cannot be imported into the same Grafana instance
with the same name.  If loading multiple dashboard variants is important, use
the `--replace-name` flag as follows:

```
./scripts/filter-dashboard -i json-models/system/rms-global.json --exclude-metrics omnistat_host_ --replace-name RMS:RMSNoHost
```

## Synchronize Dashboards

The `sync-dashboards` script can be used to keep track of remote dashboards and
update them when new versions are available. It can perform two operations:
 1. **Download** dashboards from a remote Grafana server to a local directory.
 2. **Upload** dashboards from a local directory to a remote Grafana server.

The script requires a Grafana API token, which can be generated using a [Grafana
Service Account](https://grafana.com/docs/grafana/latest/administration/service-accounts/).
Set the `GRAFANA_API_TOKEN` environment variable with the token before running
the script.

For example, to download dashboards from a remote Grafana server, use the
following command:
```
GRAFANA_API_TOKEN=$(command-to-get-secret-token) ./scripts/sync-dashboards --grafana-url "https://grafana.example.com" download
```

> [!NOTE]
> - Ensure the `GRAFANA_API_TOKEN` is securely stored and accessible only to
>   authorized users.
> - Replace `command-to-get-secret-token` with the actual command or method to
>   retrieve your API token.

By default, `sync-dashboards` will synchronize the `source` directory. To
synchronize a different directory, use the `--dashboards-dir` flag:
```
GRAFANA_API_TOKEN=$(command-to-get-secret-token) ./scripts/sync-dashboards --grafana-url "https://grafana.example.com" --dashboards-dir ./json-models/system/ download
```
