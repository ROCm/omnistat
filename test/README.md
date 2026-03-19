## Test Environment with SLURM

Test environment based on a containerized SLURM cluster.

The provided Docker Compose environment creates a containerized SLURM cluster,
and installs the working copy of Omnistat in the container at run time. It is
meant to help make development easier, and enables testing without relying on
access to real clusters.

### Deploy and Test System-level Omnistat

From the root directory of the project:

1. Start containers.
   ```
   docker compose -f test/docker/slurm/compose.yaml up -d
   ```
   By default the containers will be launched with an uninstalled deployment,
   executing directly from a working copy of the Omnistat repository. In order
   to test the Omnistat package and its installation, set the
   `TEST_OMNISTAT_EXECUTION` variable as follows:
   ```
   TEST_OMNISTAT_EXECUTION=package docker compose -f test/docker/slurm/compose.yaml up -d
   ```

2. Run tests with `pytest`:
   ```
   pytest test/test_integration.py test/test_job_system.py
   ```

3. Stop containers.
   ```
   docker compose -f test/docker/slurm/compose.yaml down -v
   ```

### Deploy and Test User-level Omnistat

User-level deployments are very similar and only require passing an additional
file to `docker compose`:

1. Start containers.
   ```
   docker compose -f test/docker/slurm/compose.yaml -f test/docker/slurm/compose-user.yaml up -d
   ```

2. Run tests with `pytest`:
   ```
   pytest test/test_job_user_pull.py test/test_job_user_push.py
   ```

3. Stop containers.
   ```
   docker compose -f test/docker/slurm/compose.yaml -f test/docker/slurm/compose-user.yaml down -v
   ```

### Deploy and Test Database

1. Start containers.
   ```
   docker compose -f test/docker/victoriametrics/compose.yaml up -d
   ```

2. Run tests with `pytest`:
   ```
   pytest test/test_tsdb_query.py test/test_tsdb_kernel_trace.py
   ```

3. Stop containers.
   ```
   docker compose -f test/docker/victoriametrics/compose.yaml down -v
   ```

### Additional Information for Testing and Debugging

The test environment includes a controller node (`controller`) and two compute
nodes (`node1` and `node2`). These nodes are launched as different containers
with Docker Compose. All containers use the same base image and are configured
at run time by launching the container with different commands. Currently
supported commands include:
- `controller-system`
- `node-system`
- `controller-user`
- `node-user`

The main difference between `-system` and `-user` variants is that the latter
won't start the Prometheus server and the Omnistat monitor.

Inside of the container, these are the most relevant paths for testing:
- `/host-source`: Omnistat source in the host exposed to the containers.
- `/source`: Copy of the Omnistat source used for installation and/or
  execution. When `TEST_OMNISTAT_EXECUTION` is set to `package`, this directory
  will be removed after the installation completes.
- `/jobs`: Shared directory across all nodes. Executing jobs from this
  directory is recommended to make sure job logs and data is easily accessible
  from the controller.
- `/opt/omnistat`: Python virtual environment containing Omnistat dependencies.
  When `TEST_OMNISTAT_EXECUTION` is set to `package`, this virtual environment
  will also include Omnistat.

Jobs can be submitted from the controller:
```
docker exec slurm-controller bash -c "cd /jobs; sbatch --wrap='sleep 10'"
```

Compute nodes are reachable using SSH from any of the containers in the
network; the controller is not reachable using SSH.

In system-level deployments, Prometheus data is exposed to the host and can be
accessed at [http://localhost:9090](http://localhost:9090). Omnistat monitor is
only exposed to the internal network.
