# Grafana Dashboards

```eval_rst
.. toctree::
   :glob:
   :maxdepth: 4
```

Dashboards allow cluster telemetry data to be visualized interactively in near
real-time. Omnistat provides several sample [Grafana](https://grafana.com/)
dashboards for cluster-wide deployments that vary depending on whether resource
manager integration is desired or not (screenshots of the variants with
resource manager integration enabled are highlighted in [Example
Screenshots](#example-screenshots)). JSON sources for example dashboards that
can be used in local deployments are highlighted below.

- *RMS* dashboards provide integration with *Resource Managers* like
  SLURM.
  - [Global Dashboard](https://github.com/ROCm/omnistat/blob/main/grafana/json-models/system/rms-global.json):
    provides an overview of the system, cluster-level telemetry for allocated
    and unallocated nodes, RAS counters, and job indices.
  - [Node Dashboard](https://github.com/ROCm/omnistat/blob/main/grafana/json-models/system/rms-node.json):
    job allocation timeline and detailed metrics for a single node in the
    cluster.
  - [Job Dashboard](https://github.com/ROCm/omnistat/blob/main/grafana/json-models/system/rms-job.json):
    provides detailed time-series data, load distribution, and other metrics for
    a single job.

- *Standalone* dashboards are meant to work without a resource manager.
  - [Global Dashboard](https://github.com/ROCm/omnistat/blob/main/grafana/json-models/system/standalone-global.json):
    provides an overview of the system, cluster-level telemetry, and RAS
    counters.
  - [Node Dashboard](https://github.com/ROCm/omnistat/blob/main/grafana/json-models/system/standalone-node.json):
    detailed metrics for a single node in the cluster.


## Grafana server

To visualize Omnistat's monitoring data, Grafana needs to be installed and
configured to use the Prometheus server described in the [system-wide
installation](installation/system-install).

### Installation

The official Grafana documentation describes several ways to [install
Grafana](https://grafana.com/docs/grafana/latest/setup-grafana/installation/),
including using packages for major operating systems.

Connectivity between Grafana server and Prometheus server is required to
display Omnistat data, so Grafana is typically installed and runs on an
administrative host.  If the host chosen to support the Prometheus server can
route out externally, you can also leverage public Grafana Cloud infrastructure
and
[forward](https://grafana.com/docs/agent/latest/flow/tasks/collect-prometheus-metrics/)
system telemetry data to an external Grafana instance.


```{note}
We recommend the official documentation for production systems. However, if you
are only interested in testing the dashboards, you can use the [Grafana Docker
image](https://grafana.com/docs/grafana/latest/setup-grafana/installation/docker/).
For example, run a temporary Grafana container with the following command, load
[localhost:3000](http://localhost:3000/) in a browser, and then follow the
steps below to configure the Grafana server and import dashboards.

```shell-session
docker run -e GF_AUTH_ANONYMOUS_ENABLED=true -e GF_AUTH_ANONYMOUS_ORG_ROLE=Admin -e GF_USERS_DEFAULT_THEME=light -it --rm -p 3000:3000 grafana/grafana
```

### Data source

The Prometheus server configured as part of the [Omnistat
installation](installation/system-install) needs to be added to Grafana as a
new data source.

To add a data source to Grafana:
1. Click **Connections** in the left-side menu.
2. Enter "Prometheus" in the search dialog, and click the **Prometheus** button
   under the search box.
3. Configure the new Prometehus data source following instructions and provide
   the hostname and port where Omnistat's Prometheus server is running.
   ```eval_rst
   .. figure:: images/grafana-data-source.png

      Adding a data source in Grafana
   ```

## Import dashboards

To import a dashboard to an existing Grafana server:
1. Click **Dashboards** in the left-side menu.
2. Click **New** and select **New Dashboard** from the drop-down menu.
3. On the dashboard, click **+ Add visualization**.
4. Upload the dashboard JSON file.
   ```eval_rst
   .. figure:: images/grafana-import-dashboard.png

      Importing a dashboard in Grafana
   ```

Sample dashboards are configured using standard default values for settings
such as network ports, but may require changes depending on the environment.
The following variable is the most relevant dashboard setting that may require
changes:
- `source`: Name of the Prometheus data source where the data is stored.
   Defaults to `prometheus`, and may require filtering if the Grafana instance
   has several Prometheus data sources.

To configure a dashboard:
1. Open a dashboard in edit mode.
2. Click **Dashboard settings** located at the top of the page.
3. Click **Variables**.
4. Click the desired variable and update its value.

(example-screenshots)=
## Example screenshots

```eval_rst
.. figure:: images/dashboard-global.png

   Global dashboard screenshot
```

```eval_rst
.. figure:: images/dashboard-node.png

   Node dashboard screenshot
```

```eval_rst
.. figure:: images/dashboard-job.png

   Job dashboard screenshot
```

<hr style="border: 1px solid black;">

## Cluster Rack View

In addition to the pre-configured Grafana panels highlighted above, additional utilities are
provided to generate a custom Grafana dashboard that renders a physical rack-layout
view for visualizing a subset of Omnistat metrics. Each rack is drawn as a vertical column with
compute servers color-coded by metric value (e.g., temperature, utilization, power) and users can
specify rack sizes, locations, and include additional switch locations to match their local installation.


### YAML Configuration

The desired cluster layout is defined in a YAML configuration file, and a companion Python
generator script produces the Grafana dashboard JSON for import. To illustrate the configuration options, a simple two-rack example with different server types is provided in
[example_cluster.yaml](https://github.com/ROCm/omnistat/blob/main/misc/cluster_dashboard/example_cluster.yaml).

The top-level fields are:

| Field | Description |
| :--- | :--- |
| `cluster_id` | Short identifier used in the dashboard title and UID |
| `display_name` | Human-readable name shown in the dashboard header |
| `omnistat_port` | Omnistat exporter port (default: 8000) |
| `grafana_url` | Base URL of the Grafana instance (e.g., `https://grafana.mycluster.org`) |
| `node_dashboard_path` | URL path to a per-node dashboard; `{instance}` is replaced at render time |
| `metrics` | List of Prometheus metrics to visualize, each with `name`, `label`, `unit`, `min`, `max`, `color`, and `show_values` |
| `racks` | List of rack definitions |

Each entry in `racks` contains:

| Field | Description |
| :--- | :--- |
| `rack_id` | Unique rack identifier |
| `label` | Display label shown above the rack |
| `col` | Column position (1-indexed, left to right) |
| `height_u` | Total rack height in rack units |
| `servers` | List of server entries occupying the rack |

Each entry in `servers` supports the following types:

- **`compute`** — GPU-equipped server. Requires `hostname` and `gpu_count`,
  or a `vms` list for servers with individually-addressable GPUs. Each GPU
  is rendered as a color-coded box bound to the corresponding Prometheus
  metric field.
- **`storage`** — Rendered as a solid block with a text label; no GPU metrics.
- **`network`** — Rendered as a solid block with a text label; no GPU metrics.

Unoccupied rack unit slots are automatically filled with blank panels.

### Generating the Dashboard

The cluster dashboard generator utility is located at [misc/cluster_dashboard/generate_cluster_dashboard.py](https://github.com/ROCm/omnistat/blob/main/misc/cluster_dashboard/generate_cluster_dashboard.py)
within the Omnistat repository. The generator script requires Python 3 and additional dependencies outlined in [misc/cluster_dashboard/requirements.txt](https://github.com/ROCm/omnistat/blob/main/misc/cluster_dashboard/requirements.txt).

The following commands highlight basic usage. Note that a `--preview` option exists to generate a static preview (SVG format) allowing administrators to iterate on the configuration before importing a final version into Grafana. An example preview rendering for the example cluster definition is shown in {numref}`fig-cluster-preview`.

```shell
# Generate a Grafana dashboard JSON file
python3 generate_cluster_dashboard.py example_cluster.yaml -o dashboard.json

# Optionally generate a static SVG preview
python3 generate_cluster_dashboard.py example_cluster.yaml -o dashboard.json --preview
```

```eval_rst
.. _fig-cluster-preview:
.. figure:: images/cluster-rack-preview.svg

   Static SVG preview generated from the example cluster configuration
```

### Importing into Grafana

Import the generated JSON file into Grafana following the steps described in [Import
dashboards](#import-dashboards) above. The dashboard will display with one tab per configured metric
with GPUs in each server color-coded based on linked Omnistat telemetry data. Hover over a
specific GPU to see exact values and link to the associated node dashboard.


```eval_rst
.. figure:: images/cluster-rack-view.png

   Live cluster rack example rendering in Grafana
```
