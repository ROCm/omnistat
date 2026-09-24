# User-mode installation

As mentioned in the project {ref}`overview <user-vs-system>`, Omnistat has two primary modes of
operation and this section highlights installation of the **user-mode** variant which does not
require elevated credentials and targets temporary data collection directly within a user's batch
job under a supported resource manager (e.g. SLURM, or Flux).

In user-mode executions, Omnistat data collectors and a companion VictoriaMetrics server are
deployed temporarily on hosts assigned to a user's job, as highlighted in {numref}`fig-user-mode`.
The following assumptions are made throughout the rest of this user-mode installation discussion:

__Assumptions__:
* [ROCm](https://rocm.docs.amd.com/en/latest/) v{__ROCM_MIN_VERSION__} or newer is pre-installed
  on all GPU hosts.
* Installer has access to a distributed file-system; if no distributed
  file-system is present, installation steps need to be repeated across all nodes.

Installation steps depend on whether you want to install Omnistat from a released wheel package
(recommended), or prefer to use a development version using git. Both options are highlighted below
for a basic installation that enables standard GPU and host-level telemetry.  Depending on your
local environment, you may also wish to augment the examples that follow to install Omnistat within
a dedicated Python virtual environment (e.g. using `venv` or `conda`). 

## Standard Omnistat install

::::{tab-set}
:::{tab-item} Install latest release using pip
:sync: release

Install the latest released version of Omnistat from AMD's ROCm package repository:

```bash
$ pip install --extra-index-url https://stable.repo.amd.com/rocm/extras/omnistat/whl-next/ omnistat
```
:::

:::{tab-item} Use latest development from git source
:sync: git

Clone the repository (`dev` branch is default) and install python dependencies:

```bash
$ git clone https://github.com/ROCm/omnistat.git
$ cd omnistat
$ pip install -r requirements.txt
```
:::
::::

(user-optional-components)=
## Optional component(s)

Beyond the standard data collector, Omnistat provides **optional** components that unlock
additional telemetry: a GPU hardware counter collector and a kernel tracing library, both
built on ROCProfiler-SDK. These steps are optional and only required to enable support for
hardware counter or kernel tracing collection — the standard install above already enables
GPU and host-level monitoring. These components are compiled from C++ sources and require a
local build step.

::::{tab-set}
:::{tab-item} Install latest release using pip
:sync: release

After completing the standard release install, build all optional extensions using the
bundled helper:

```bash
$ omnistat-build-extras
```
:::

:::{tab-item} Use latest development from git source
:sync: git

From within a cloned copy of the repository, build the optional components in place.

**Hardware counter support** consists of two pieces. First, build the collector
extension that samples counters from the GPUs:

```bash
$ pip install cmake-build-extension nanobind
$ BUILD_ROCPROFILER_SDK_EXTENSION=1 python setup.py build_ext --inplace
```

Then build the counter enablement library (`libomnistat_count.so`), a standalone C++
shared library loaded into monitored applications to enable counter collection for their
queues (required for user-mode collection):

```bash
$ cmake -S rocprofiler-sdk/ -B build-count/ -DBUILD_COUNT_LIB=ON
$ cmake --build build-count/
```

**Kernel tracing support** provides `libomnistat_trace.so`, a standalone C++ shared
library that intercepts GPU kernel dispatches at runtime:

```bash
$ cmake -S rocprofiler-sdk/ -B build-trace/ -DBUILD_KERNEL_TRACE_LIB=ON
$ cmake --build build-trace/
```
:::
::::

```{note}
The optional components rely on `cmake` and a HIP C++ compiler.
```

The resulting libraries are located at `build-count/libomnistat_count.so` and
`build-trace/libomnistat_trace.so`. See
[Advanced Profiling](../advanced-profiling.md) for usage instructions, covering
both [hardware counters](../advanced-profiling.md#hardware-counters) and
[kernel tracing](../advanced-profiling.md#kernel-tracing).

## Victoria Metrics Server

Download a **single-node** VictoriaMetrics server. Assuming a `victoria-metrics` server is not
already present on the system, download and extract a [precompiled
binary](https://github.com/VictoriaMetrics/VictoriaMetrics/releases/latest) from upstream. This
binary can generally be stored in any directory accessible by the user, but the path to the binary
will need to be known during the next section when configuring user-mode execution. Note that
VictoriaMetrics provides a larger number binary releases and we typically use the
`victoria-metrics-linux-amd64` variant on x86_64 clusters.

## Configuring user-mode Omnistat

For user-mode execution, Omnistat includes additional options in the `[omnistat.usermode]` section of the runtime configuration file. A portion of the [default](https://github.com/ROCm/omnistat/blob/main/omnistat/config/omnistat.default) config file is highlighted below with the lines in yellow indicating settings to confirm or customize for your local environment.

```{eval-rst}
.. code-block:: ini
   :caption: Sample Omnistat configuration file
   :emphasize-lines: 2,4,8,11,12,13

    [omnistat.collectors]
    port = 8001
    enable_amd_smi = True
    enable_rms = True

    [omnistat.collectors.rms]
    job_detection_mode = file-based
    job_detection_file = /tmp/omni_rmsjobinfo_user

    [omnistat.usermode]
    victoria_binary = /path/to/victoria-metrics
    victoria_datadir = data_prom
    victoria_logfile = vic_server.log
  ```

