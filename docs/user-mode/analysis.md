# Analyzing results

## Exploring results locally

To explore results previously gathered via Omnistat user-mode execution, we provide
a Docker environment that will automatically launch the required data exploration services
locally. This containerized environment includes Victoria Metrics to read and query
the stored data, and Grafana as a visualization platform to display time series and
other metrics. The following steps outline the general process to visualize user-mode results locally:

1. Download the latest Omnistat release and proceed to the `docker` directory
   within Omnistat.
   ```shell-session
   [user@login]$ REPO=https://github.com/ROCm/omnistat
   [user@login]$ curl -OLJ ${REPO}/archive/refs/tags/v{__VERSION__}.tar.gz
   [user@login]$ tar xfz omnistat-{__VERSION__}.tar.gz
   [user@login]$ cd omnistat-{__VERSION__}/docker
   ```
2. Copy an Omnistat database collected in usermode to the local `./data` directory.
   Note that all the contents of the `victoria_datadir` configuration option (or
   `OMNISTAT_VICTORIA_DATADIR` environment variable) need to be copied recursively,
   typically resulting in the following hierarchy:
   ```text
   ./data/cache/
   ./data/data/
   ./data/flock.lock
   ./data/indexdb/
   ./data/metadata/
   ./data/snapshots/
   ./data/tmp/
   ```
3. Start Docker environment.
   ```shell-session
   [user@login]$ docker compose up
   ```
   This command will download the appropriate Docker images and prepare the
   environment to visualize Omnistat data. If everything works as expected, the startup
   process will conclude with output similar to the following indicating the Omnistat dashboard is ready:
   ```text
   Attaching to omnistat
   omnistat  | Executing as user 1000:1000
   omnistat  | Starting Victoria Metrics using ./data
   omnistat  | Scanned database in 0.19 seconds
   omnistat  | .. Number of jobs in the last 365 days: 1
   omnistat  | Omnistat dashboard ready: http://localhost:3000
   ```
   <!-- Services run with the same user and group ID as the owner and group of
   the `./data` directory. -->


   ```{note}
   You can also override the default database directory by setting the
   `DATADIR` variable when starting the Docker containers, e.g:

   ```shell-session
   [user@login]$ DATADIR=/path/to/data docker compose up
   ```

4. Access Grafana dashboard at [http://localhost:3000](http://localhost:3000).

**Teardown**: when finished with local data exploration, you can press `Ctrl+C` to stop the Docker environment.  To completely remove the containers, issue:

```shell-session
[user@login]$ docker compose down
```

### Video demonstration

The following video demonstrates how to interactively explore user-mode
Omnistat traces using the provided Docker environment. The demonstration
covers downloading and loading traces, and showcases the key features of the
job dashboard when displaying data from a multi-node job.

<video width="640" controls>
  <source src="https://github.com/user-attachments/assets/9870d611-a163-459b-9e6c-70aa4b9c7294" type="video/mp4">
</video>
<p></p>


### Combining Omnistat databases

To work with multiple Omnistat collections at the same time (e.g to explore telemetry collected from different jobs), these first need to be
merged into a single database. Omnistat's Docker environment provides an
option to trigger a merge operation by providing a `MULTIDIR` path (instead of
`DATADIR`). When starting the Docker environment with this option, all databases residing
under the directory pointed to by `MULTIDIR` will be loaded into a common database that will be used
to support visualization of multiple jobs.

1. As an example, the following `collection` directory contains two Omnistat
   databases under the `data-{0,1}` subdirectories:
   ```text
   ./collection/data-0/
   ./collection/data-1/
   ```
2. Start the services with the `MULTIDIR` variable to merge multiple
   databases:
   ```shell-session
   [user@login]$ MULTIDIR=./collection docker compose up
   ```
3. While the services are started, a new database named `_merged` will be
   created automatically:
   ```text
   ./collection/data-0/
   ./collection/data-1/
   ./collection/_merged/
   ```
   Once the merged database is ready, all the information from `data-0`
   and `data-1` will be visible in the local Grafana dashboard at
   [http://localhost:3000](http://localhost:3000).

Note that it is also possible to copy new databases to the same `MULTIDIR` directory at a later
time. To merge a new database, simply stop the Docker Compose environment and
start it again with the same `docker compose up`. Only newly copied
directories will be loaded into the merged database.


## Exporting time series data

To explore and process raw Omnistat data without relying on the Docker
environment or a Prometheus/VictoriaMetrics server, the `omnistat-query` tool
has an option to export all time series data to CSV files.
```bash
${OMNISTAT_DIR}/omnistat-query --job ${jobid} --interval 1 --export
```

Exported CSV files are stored in the current directory by default. The
`--export` flag accepts an optional argument to write CSV files to a different
location. For example, `--export export-data` will store exported CSV files
under the `export-data` directory.

The export functionality will generate one or more CSV files, depending on
which collectors are enabled in the Omnistat configuration, as outlined in the
following table.

| File                            | Collector                    | Description                                               |
| :------------------------------ | :--------------------------: | :-------------------------------------------------------- |
| `omnistat-rocm.gpu.csv`         | `rocm_smi` or `amd_smi`      | GPU-level utilization and telemetry.                      |
| `omnistat-network.csv`          | `network`                    | Network interface rx/tx bytes.                            |
| `omnistat-host.csv`             | `host_metrics`               | Host CPU, memory, and local I/O metrics.                  |
| `omnistat-host-proc-io.csv`     | `host_metrics`               | Per-process I/O (includes network I/O).                   |
| `omnistat-host-proc-io-inventory.csv` | `host_metrics`         | PID inventory with per-process start/end times and total bytes read/written. |
| `omnistat-rocprofiler.gpu.csv`  | `rocprofiler`                | GPU hardware performance counters.                        |
| `omnistat-fom.csv`              | `fom`                        | Figures of merit.                                         |
| `omnistat-vendor.csv`           | `vendor_counters`            | Node-level vendor PM counters (energy, power).            |
| `omnistat-vendor.gpu.csv`       | `vendor_counters`            | Per-GPU vendor PM counters (accelerator energy, power).   |
| `omnistat-xgmi.gpu.csv`         | `xgmi`                       | GPU-to-GPU xGMI interconnect read/write data.             |
| `omnistat-kernel-trace.gpu.csv` | `kernel_trace`               | Per-kernel dispatch counts and execution durations.       |

Exported data can be easily loaded as a data frame using tools like Pandas for
further processing.

```{eval-rst}
.. code-block:: python
   :caption: Python script to read exported time series as a Pandas data frame

   import pandas

   df = pandas.read_csv("omnistat-rocm.gpu.csv", header=[0, 1, 2], index_col=0)

   # Select a single metric
   df["rocm_utilization_percentage"]

   # Select a single metric and node
   df["rocm_utilization_percentage"]["node01"]

   # Select a single metric, node, and GPU
   df["rocm_utilization_percentage"]["node01"]["0"]

   # Select GPU Utilization and GPU Memory Utilization for GPU ID 0 in all nodes
   df.loc[:, pandas.IndexSlice[["rocm_utilization_percentage", "rocm_vram_used_percentage"], :, ["0"]]]

  ```

```{eval-rst}
.. code-block:: python
   :caption: Python script to plot average GPU Utilization per node

   import pandas
   import matplotlib.pyplot as plt

   df = pandas.read_csv("omnistat-rocm.gpu.csv", header=[0, 1, 2], index_col=0)
   df.index = pandas.to_datetime(df.index)

   # Create a new dataframe with node averages
   node_mean_df = df["rocm_utilization_percentage"].T.groupby(level=['instance']).mean().T

   node_mean_df.plot(linewidth=1)
   plt.title("Mean utilization per node")
   plt.xlabel("Time")
   plt.ylabel("GPU Utilization (%)")
   plt.show()
  ```
