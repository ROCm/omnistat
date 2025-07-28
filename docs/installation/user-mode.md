# User-mode execution

```eval_rst
.. toctree::
   :glob:
   :maxdepth: 4
```

In user-mode executions, Omnistat data collectors and a companion VictoriaMetrics
server are deployed temporarily on hosts assigned to a user's job, as
highlighted in {numref}`fig-user-mode`. The following assumptions are made
throughout the rest of this user-mode installation discussion:

__Assumptions__:
* [ROCm](https://rocm.docs.amd.com/en/latest/) v6.1 or newer is pre-installed
  on all GPU hosts.
* Installer has access to a distributed file-system; if no distributed
  file-system is present, installation steps need to be repeated across all nodes.


## Omnistat software installation

To begin, we download the Omnistat software and install necessary Python
dependencies. Per the assumptions above, we download and install Omnistat in a
path accessible from all nodes.

1. Download and expand latest release version.
   ```shell-session
   [user@login]$ REPO=https://github.com/ROCm/omnistat
   [user@login]$ curl -OLJ ${REPO}/archive/refs/tags/v{__VERSION__}.tar.gz
   [user@login]$ tar xfz omnistat-{__VERSION__}.tar.gz
   ```

2. Install dependencies.
   ```shell-session
   [user@login]$ cd omnistat-v{__VERSION__}
   [user@login]$ pip install --user -r requirements.txt
   [user@login]$ pip install --user -r requirements-query.txt
   ```

```{note}
Omnistat can also be installed as a Python package. Create a virtual
environment, and install Omnistat and its dependencies from the top
directory of the release.
```shell-session
[user@login]$ cd omnistat-v{__VERSION__}
[user@login]$ python -m venv ~/venv/omnistat
[user@login]$ ~/venv/omnistat/bin/python -m pip install .[query]
```

3. Download a **single-node** VictoriaMetrics server. Assuming a `victoria-metrics` server is not already present on the system,
   download and extract a [precompiled binary](https://github.com/VictoriaMetrics/VictoriaMetrics/releases/latest) from upstream. This binary can generally be stored in any directory accessible by the user, but the path to the binary will need to be known during the next section when configuring user-mode execution. Note that VictoriaMetrics provides a larger number binary releases and we typically use the `victoria-metrics-linux-amd64` variant on x86_64 clusters.

## Configuring user-mode Omnistat

For user-mode execution, Omnistat includes additional options in the `[omnistast.usermode]` section of the runtime configuration file. A portion of the [default](https://github.com/ROCm/omnistat/blob/main/omnistat/config/omnistat.default) config file is highlighted below with the lines in yellow indicating settings to confirm or customize for your local environment.

```eval_rst
.. code-block:: ini
   :caption: Sample Omnistat configuration file
   :emphasize-lines: 2,4,8,11,12,13

    [omnistat.collectors]
    port = 8001
    enable_rocm_smi = True
    enable_rms = True

    [omnistat.collectors.rms]
    job_detection_mode = file-based
    job_detection_file = /tmp/omni_rmsjobinfo_user

    [omnistat.usermode]
    victoria_binary = /path/to/victoria-metrics
    victoria_datadir = data_prom
    victoria_logfile = vic_server.log
  ```

## Running Jobs

To enable user-mode data collection for a specifid job, add logic within your job script to start and stop the collection mechanism before and after running your desired application(s).  Omnistat includes an `omnistat-usermode` utility to help automate this process and the examples below highlight the steps for simple SLURM and Flux job scripts.  Note that the lines highlighted in
yellow need to be customized for the local installation path.


   ### SLURM example
```eval_rst
.. code-block:: bash
   :emphasize-lines: 6-7
   :caption: Example SLURM job file using user-mode Omnistat with a 10 second sampling interval

   #!/bin/bash
   #SBATCH -N 8
   #SBATCH -n 16
   #SBATCH -t 02:00:00

    export OMNISTAT_CONFIG=/path/to/omnistat.config
    export OMNISTAT_DIR=/path/to/omnistat

    # Beginning of job - start data collector
    ${OMNISTAT_DIR}/omnistat-usermode --start --interval 10

    # Run application(s) as normal
    srun <options> ./a.out

    # End of job -  stop data collection, generate summary and store collected data by jobid
    ${OMNISTAT_DIR}/omnistat-usermode --stopexporters
    ${OMNISTAT_DIR}/omnistat-query --job ${SLURM_JOB_ID} --interval 10
    ${OMNISTAT_DIR}/omnistat-usermode --stopserver
    mv data_prom data_prom_${SLURM_JOB_ID}
  ```

  ### Flux example

```eval_rst
.. code-block:: bash
   :emphasize-lines: 8-9
   :caption: Example FLUX job file using user-mode Omnistat with a 1 second sampling interval

   #!/bin/bash
   #flux: -N 8
   #flux: -n 16
   #flux: -t 2h

   jobid=`flux getattr jobid`

   export OMNISTAT_CONFIG=/path/to/omnistat.config
   export OMNISTAT_DIR=/path/to/omnistat

   # Beginning of job - start data collector
   ${OMNISTAT_DIR}/omnistat-usermode --start --interval 1

   # Run application(s) as normal
   flux run <options> ./a.out

   # End of job -  stop data collection, generate summary and store collected data by jobid
   ${OMNISTAT_DIR}/omnistat-usermode --stopexporters
   ${OMNISTAT_DIR}/omnistat-query --job ${jobid} --interval 1
   ${OMNISTAT_DIR}/omnistat-usermode --stopserver
   mv data_prom data_prom.${jobid}
  ```

 In both examples above, the `omnistat-query` utility is used at the end of the job to query collected telemetry (prior to shutting down the server) for the assigned jobid. This should embed an ascii summary for the job similar to the [report card](query_report_card) example mentioned in the Overview directly within the recorded job output.

<!-- ## Exploring results with a local Docker environment -->
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
   [user@login]$ cd omnistat-v{__VERSION__}/docker
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

| File                    | Collector               | Description                          |
| :---------------------- | :---------------------: | :----------------------------------- |
| `omnistat-rocm.gpu.csv` | `rocm_smi` or `amd_smi` | GPU-level utilization and telemetry. |
| `omnistat-network.csv`  | `network`               | Network bandwidth.                   |

Exported data can be easily loaded as a data frame using tools like Pandas for
further processing.

```eval_rst
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

```eval_rst
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
