## Environment to view local Omnistat data

This Docker Compose environment is meant to be used as a tool to visualize
data collected with usermode Omnistat without the need to use external
resources.

Two different services are required to view Omnistat data:
 - **Victoria Metrics**: used to read and query data.
 - **Grafana**: used as visualization platform to display time series and
   other metrics.

This environment will automatically launch containers, configure both services,
connect Grafana and Victoria Metrics, and pre-load two dashboards:
 - `omnistat-index`: List all jobs in the stored database.
 - `omnistat-job`: View details for a single job.

For more information on how to use this Docker Compose environment, please
refer to the [Exploring results with a local Docker
environment](https://rocm.github.io/omnistat/installation/user-mode.html#exploring-results-locally).

section of the documentation.

### Quick Deployment Guide

1. Copy Omnistat database collected in usermode to a local directory.
   ```
   data/cache/
   data/data/
   data/flock.lock
   data/indexdb/
   data/metadata/
   data/snapshots/
   data/tmp/
   ```
2. Start services:
   ```
   DATADIR=./path/to/data docker compose up
   ```
3. Access Grafana dashboard at http://localhost:3000.

### Combining Omnistat databases

To work with multiple Omnistat databases at the same time: create a directory,
copy the desired Omnistat databases as subdirectories, and start services with
the `MULTIDIR` variable pointing to the new directory.

1. As an example, the following `collection` directory contains two Omnistat
   databases under the `data-{0,1}` subdirectories:
   ```
   collection/data-0/
   collection/data-1/
   ```
2. Start the services with the `MULTIDIR` variable to merge multiple
   databases:
   ```
   MULTIDIR=./path/to/collection docker compose up
   ```
3. While the services are started, a new database named `_merged` will be
   created:
   ```
   collection/data-0/
   collection/data-1/
   collection/_merged/
   ```
   Once the merged database is ready, all the information from `data-0`
   and `data-1` will be visible in the local Grafana dashboard at
   http://localhost:3000.
