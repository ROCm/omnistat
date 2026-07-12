# Cluster Rack View Dashboard Generator

Utilities in this directory can be used to auto generate a custom Grafana dashboard that renders a
physical rack-layout cluster view for visualizing a subset of Omnistat metrics. Each rack is drawn
as a vertical column with compute servers color-coded by metric value (e.g., temperature,
utilization, power) and users can specify rack sizes, locations, and include additional switch
locations. Metrics are presented as separate tabs within a single Grafana dashboard (requires
Grafana v12 or newer).

Cluster layout and metrics controls are defined in a YAML configuration file. See
[example_cluster.yaml](example_cluster.yaml) for a two-rack example.

## Quick Start

```shell
# Generate a Grafana dashboard JSON file
python3 generate_cluster_dashboard.py example_cluster.yaml -o dashboard.json

# Optionally generate a static SVG preview
python3 generate_cluster_dashboard.py example_cluster.yaml -o dashboard.json --preview
```

Import the resulting JSON file into Grafana via **Dashboards > Import**.

## Documentation

For additional documentation, including YAML schema details and example screenshots, please consult
 the [Grafana Dashboards](../../docs/grafana.md) section of the Omnistat documentation.
