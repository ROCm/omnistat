# ORNL Lux

This section provides instructions for running user-mode Omnistat on Oak Ridge National Laboratory's (ORNL) Lux supercomputer with pre-installed versions from AMD Research. For ORNL's
Frontier, see the [Frontier instructions](site.frontier.md) instead.

**Prerequisites**:
- User account on Lux
- Familiarity with SLURM job submission

## Running jobs on Lux

Omnistat is preinstalled on Lux so users only need to setup their module
environment appropriately and add several commands to their SLURM job scripts.

Lux requires every job to name a partition and request accelerators
explicitly. Submissions without both `-p batch` and a `--gpus-per-node` (or
equivalent `--gpus`/`--gpus-per-task`) request are rejected at submit time.

The version of Omnistat installed on Lux includes a convenience wrapper for
teams running their own Python stack who might want to avoid any pollution with
the application's Python environment. A SLURM job script example highlighting
use of the wrapper utility before and after executing a GPU application is
highlighted below:

```{eval-rst}
.. code-block:: bash
   :caption: Example Lux SLURM job using wrapper, highlighting changes needed to run Omnistat
   :emphasize-lines: 10-12,18-20

   #!/bin/bash
   #SBATCH -A <project_id>
   #SBATCH -p batch
   #SBATCH -J omnistat
   #SBATCH -N 2
   #SBATCH --gpus-per-node=8
   #SBATCH -t 01:00:00

   # Setup and launch Omnistat (wrapper version)
   ml use /sw/lux/amdsw/modulefiles
   ml omnistat-wrapper
   ${OMNISTAT_WRAPPER} usermode --start --interval 1.0

   # Your GPU application here
   srun ./your_application

   # Tear down Omnistat
   ${OMNISTAT_WRAPPER} usermode --stopexporters
   ${OMNISTAT_WRAPPER} query --interval 1.0 --job ${SLURM_JOB_ID} --pdf omnistat.${SLURM_JOB_ID}.pdf
   ${OMNISTAT_WRAPPER} usermode --stopserver
```

```{note}
The wrapper sets up its own Python and ROCm environment internally, in a
subshell. The job script's own module environment is left untouched, so an
application built against a specific `rocm` module continues to run against
that version.
```

## Storage

By default, Omnistat databases are stored in Lustre, under the
`/lustre/orion/$(SLURM_JOB_ACCOUNT)/scratch/$(USER)/omnistat/$(SLURM_JOB_ID)`
directory. It's possible to override the default path using the
`OMNISTAT_VICTORIA_DATADIR` environment variable as highlighted in the
following example.

```{eval-rst}
.. code-block:: bash
   :caption: Storing data under /tmp and copying it after running
   :emphasize-lines: 12,22

   #!/bin/bash
   #SBATCH -A <project_id>
   #SBATCH -p batch
   #SBATCH -J omnistat
   #SBATCH -N 2
   #SBATCH --gpus-per-node=8
   #SBATCH -t 01:00:00

   # Setup and launch Omnistat (wrapper version)
   ml use /sw/lux/amdsw/modulefiles
   ml omnistat-wrapper
   export OMNISTAT_VICTORIA_DATADIR=/tmp/omnistat/${SLURM_JOB_ID}
   ${OMNISTAT_WRAPPER} usermode --start --interval 1.0

   # Your GPU application here
   srun ./your_application

   # Tear down Omnistat
   ${OMNISTAT_WRAPPER} usermode --stopexporters
   ${OMNISTAT_WRAPPER} query --interval 1.0 --job ${SLURM_JOB_ID} --pdf omnistat.${SLURM_JOB_ID}.pdf
   ${OMNISTAT_WRAPPER} usermode --stopserver
   mv /tmp/omnistat/${SLURM_JOB_ID} data_omnistat.${SLURM_JOB_ID}
```

```{note}
Omnistat databases require `flock` support, which is available in Lustre and
local filesystems like `/tmp`. Data can't be stored directly under Lux's
`$HOME`, but it can be moved there after running.
```

## Data analysis

After job completion, transfer the archived Omnistat data to your local machine
for analysis using the Docker environment described in the [user-mode
guide](user-mode/analysis.md#exploring-results-locally).

## Additional features

Beyond the metrics collected by default, Omnistat offers two [advanced
profiling](advanced-profiling.md) capabilities on Lux: sampling of GPU [hardware
counters](advanced-profiling.md#hardware-counters), and
[tracing](advanced-profiling.md#tracing) of kernel dispatches and RCCL
communication. These advanced capabilities
are disabled in the default runtime configuration but can be overridden using the `OMNISTAT_CONFIG`
environment variable and a custom config file based on the default [Lux configuration
file](https://github.com/ROCm/omnistat/blob/main/omnistat/config/omnistat.lux).

### Hardware counters

Hardware counters are sampled device-wide on each GPU at the Omnistat sampling interval. In
user-mode, sampling requires a counter enablement library (`libomnistat_count.so`) loaded into the
application's runtime environment, which registers the application's GPU queues with ROCProfiler-SDK
so a separate Omnistat instance can read their counters. A pre-built counter enablement library is
provided with the Omnistat installation.

To enable hardware counters:
1. Create a custom configuration file with `enable_rocprofiler = True`, using
   the default
   [Lux configuration file](https://github.com/ROCm/omnistat/blob/main/omnistat/config/omnistat.lux)
   as a starting point, and select a {ref}`counter profile <counter-profiles>`
   under `[omnistat.collectors.rocprofiler]`.
2. Set `OMNISTAT_CONFIG` to point to your custom file in the job script.
3. Point `ROCP_TOOL_LIBRARIES` at the pre-built counter enablement library.
   This must be set before launching the application:

   ```bash
   export ROCP_TOOL_LIBRARIES=${OMNISTAT_DIR}/build-count/libomnistat_count.so
   ```

### Tracing

Tracing works by loading a tracing library (`libomnistat_trace.so`) into the
application's runtime environment to intercept GPU kernel dispatches and RCCL
calls (RCCL tracing requires Omnistat **1.16.0**). Pre-built tracing libraries
are provided for each ROCm release available on the system.

To enable tracing:
1. Create a custom configuration file with `enable_kernel_trace = True`,
   `enable_rccl_trace = True`, or both, using the default
   [Lux configuration file](https://github.com/ROCm/omnistat/blob/main/omnistat/config/omnistat.lux)
   as a starting point.
2. Set `OMNISTAT_CONFIG` to point to your custom file in the job script.
3. Set `ROCP_TOOL_LIBRARIES` to the pre-built tracing library matching the
   ROCm version used to build your application. Pre-built libraries are
   available under
   `${OMNISTAT_DIR}/build-trace-rocm-<version>/`.
   This variable must be set before launching the application.

```{eval-rst}
.. code-block:: bash
   :caption: Example Lux SLURM job with tracing enabled
   :emphasize-lines: 12,16

   #!/bin/bash
   #SBATCH -A <project_id>
   #SBATCH -p batch
   #SBATCH -J omnistat
   #SBATCH -N 2
   #SBATCH --gpus-per-node=8
   #SBATCH -t 01:00:00

   # Setup and launch Omnistat (wrapper version)
   ml use /sw/lux/amdsw/modulefiles
   ml omnistat-wrapper
   export OMNISTAT_CONFIG=/path/to/omnistat.config
   ${OMNISTAT_WRAPPER} usermode --start --interval 1.0

   # Enable Omnistat tracing (ROCm version derived from ROCM_PATH)
   export ROCP_TOOL_LIBRARIES=${OMNISTAT_DIR}/build-trace-$(basename ${ROCM_PATH})/libomnistat_trace.so

   # Your GPU application here
   srun ./your_application

   # Tear down Omnistat
   ${OMNISTAT_WRAPPER} usermode --stopexporters
   ${OMNISTAT_WRAPPER} query --job ${SLURM_JOB_ID} --interval 1.0
   ${OMNISTAT_WRAPPER} usermode --stopserver
```

```{note}
Tracing libraries are built per ROCm release, so the derived path only resolves
if a matching `build-trace-rocm-<version>` directory exists. If Lux's default
ROCm is updated ahead of the Omnistat installation, load a `rocm` module for
which a tracing library is available and rebuild your application against it.
```

Hardware counters and tracing can also be enabled together by listing
both libraries in `ROCP_TOOL_LIBRARIES`; see {ref}`combining counters and
tracing <combining-counters-and-tracing>` for the required syntax.

