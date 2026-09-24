# System-mode installation

As mentioned in the project {ref}`overview <user-vs-system>`, Omnistat has two primary modes of
operation and this section highlights installation of the **system-mode** variant that is intended
for permanent installations across an entire cluster and is typically performed by a system
administrator with access to elevated credentials. 

There are different ways to deploy and configure Omnistat in a data center, and each system will
generally require a certain level of customization. Here, we provide the basic steps to install the
Omnistat data collector, Prometheus server, and provide an example of how to deploy Omnistat in a
data center using Ansible. Finally, an approach for integrating with the SLURM workload manager to
track user jobs is discussed.

For system-wide installation, we recommend creation and usage of a dedicated Linux user that will be
used to run the data collector daemon of Omnistat (`omnistat-monitor`).  In addition, per the
architecture highlighted in {numref}`fig-system-mode`, a separate server (or VM/container) is needed
to support installations of a Prometheus server and Grafana instance.  These services can be hosted
on your cluster head-node, or via a separate administrative host. Note that if the host chosen to
support the Prometheus server can route out externally, you can also leverage public Grafana cloud
infrastructure and
[forward](https://grafana.com/docs/agent/latest/flow/tasks/collect-prometheus-metrics/) system
telemetry data to an external Grafana instance.

The following assumptions are made throughout the rest of this system-wide installation discussion:

__Assumptions__:
* Installer has `sudo` or elevated credentials to install software system-wide, enable systemd services, and optionally modify the local SLURM configuration
* [ROCm](https://rocm.docs.amd.com/en/latest/) v{__ROCM_MIN_VERSION__} or newer is pre-installed on all GPU hosts
* Installer has provisioned a dedicated user (eg. `omnidc`) across all desired compute nodes of their system
* Installer has identified a location to host a Prometheus server (if not present already) that has network access to all compute nodes.

Different installation options exist depending on whether you want to install Omnistat from a
released wheel package (recommended), or prefer to use a development version using git.
Both options are highlighted below for a basic installation that enables standard GPU and host-level
telemetry.  Depending on your local environment, you may also wish to augment the examples that follow
to install Omnistat within a dedicated Python virtual environment (e.g. using `venv` or `conda`). 

(system-install)= 
## Standard install

::::{tab-set}
:::{tab-item} Install latest release using pip
:sync: release

Install the latest released version of Omnistat from AMD's ROCm package repository:

```bash
[omnidc]$ pip install --extra-index-url https://stable.repo.amd.com/rocm/extras/omnistat/whl-next/ omnistat
```
:::

:::{tab-item} Use latest development from git source
:sync: git

Clone the repository (`dev` branch is default) and install python dependencies:

```bash
[omnidc]$ git clone https://github.com/ROCm/omnistat.git
[omnidc]$ cd omnistat
[omnidc]$ pip install -r requirements.txt
```
:::
::::

### Runtime configuration


The Omnistat data collector has a number of runtime configuration options housed within an
[omnistat/config/omnistat.default](https://github.com/ROCm/omnistat/blob/main/omnistat/config/omnistat.default)
file including use of port `8001` for the Prometheus client. Assuming that port is available
locally, the main additional option to confirm after installation is the location of your local ROCm
install. The default configuration assumes a standard install path of `/opt/rocm` - if that matches
your local setup, no additional change is necessary. If, however, your environment supports multiple
ROCm installs in versioned paths, the version used by Omnistat data collection can be changed via
the _rocm_path_ setting, e.g.

```
rocm_path = /opt/rocm-7.14.0
```
```{note}
You can override the default runtime configuration file above by setting an `OMNISTAT_CONFIG` environment variable or by using the `omnistat-monitor --configfile` option.
```

### Test the installation

Omnistat includes a test suite you can exercise to confirm a locally functional installation. Test execution requires a host with valid ROCm installation and one or more GPUs present and will run a short set of tests. 

::::{tab-set}
:::{tab-item} Install latest release using pip
:sync: release

Install the companion `omnistat-tests` package and run the suite with `pytest`:

```shell-session
[omnidc]$ pip install --extra-index-url https://stable.repo.amd.com/rocm/extras/omnistat/whl-next/ omnistat-tests
[omnidc]$ pytest --pyargs omnistat_tests
```
:::

:::{tab-item} Use latest development from git source
:sync: git

Install test dependencies and run the test suite directly from the cloned repository:

```shell-session
[omnidc]$ pip install -r test/requirements.txt
[omnidc]$ pytest
```
:::
::::

Example output of a successful run (including a few skipped) tests is as follows:

```{raw} html
<div class="terminal-output">
<div class="t-dim">=============================== test session starts ================================</div>
<div>platform linux -- Python 3.12.12, pytest-9.1.1, pluggy-1.6.0</div>
<div>collected 444 items / 2 skipped</div>
<div>&nbsp;</div>
<div class="t-row"><span>omnistat_tests/test_collectors.py <span class="t-pass">..............................</span></span><span class="t-pct">[&nbsp;&nbsp;8%]</span></div>
<div class="t-row"><span><span class="t-pass">.......................</span><span class="t-skip">sss</span><span class="t-pass">......</span><span class="t-skip">ssssssss</span><span class="t-pass">.</span></span><span class="t-pct">[&nbsp;54%]</span></div>
<div class="t-row"><span>omnistat_tests/test_rms.py <span class="t-pass">............</span></span><span class="t-pct">[&nbsp;62%]</span></div>
<div class="t-row"><span>omnistat_tests/test_unit_kernel_trace.py <span class="t-pass">...............................</span></span><span class="t-pct">[&nbsp;98%]</span></div>
<div class="t-row"><span><span class="t-pass">........</span></span><span class="t-pct">[100%]</span></div>
<div>&nbsp;</div>
<div><span class="t-pass" style="font-weight:bold">======================== 414 passed, 32 skipped in 40.08s =========================</span></div>
</div>
```


### Enable systemd service

Now that the software is installed under a dedicated user and basic functionality has been confirmed, the data collector can be enabled for permanent service. The recommended approach for this is to leverage `systemd` and an example service file named [omnistat.service](https://github.com/ROCm/omnistat/blob/main/omnistat.service) is included in the distribution. The contents of the file are shown below with four lines highlighted in yellow that are most likely to require local customization.

<!-- * `User` set to the local Linux user created to run Omnistat
* `OMNISTAT_DIR` set to the local path where you downloaded the source tree
* `OMNISTAT_CONFIG` set to the path of desired runtime config file
* `CPUAffinity` set to the CPU core index where omnistat-monitor will be pinned -->


```{eval-rst}
.. literalinclude:: omnistat.service
   :language: ini
   :emphasize-lines: 8-11
```

Using elevated credentials, install the omnistat.service file across all desired compute nodes (e.g. `/etc/systemd/system/omnistat.service` and enable using systemd (`systemctl enable omnistat`).

### Access restriction configuration

By default, the omnistat data collector will only respond to queries initiated from the local host where the service is running.  This functionality is controlled by a runtime configuration and generally needs to be updated to include the IP address of a companion Prometheus server in order to gather system-wide metrics (see follow-on [discussion](#prometheus-server) for additional details on configuring a Prometheus server).  For example, if your locally configured Prometheus instance has an IP address of `10.0.0.42`, update the `omnistat/config/omnistat.default` runtime file (or equivalent if using a custom configfile) to include the following setting:

```{eval-rst}
.. code-block:: ini
   :emphasize-lines: 3

    [omnistat.collectors]

    allowed_ips = 127.0.0.1, 10.0.0.42
```

```{note}
Alternatively, you can specify a value of `allowed_ips = 0.0.0.0` to disable any access restrictions.
```


---

(optional-components)=
## Optional component(s)

Beyond the standard data collector, Omnistat provides **optional** components that unlock
additional telemetry, most notably a GPU hardware counter collector built on
ROCProfiler-SDK. These steps are optional and only required to enable support for hardware
counter collection — the standard install above already enables GPU and host-level
monitoring. These components are compiled from C++ sources and require a local build step.
The examples below build and install the counter collector alongside the data collector.

::::{tab-set}
:::{tab-item} Install latest release using pip
:sync: release

After completing the standard release install, build the optional ROCProfiler-SDK
counter extension using the bundled helper:

```bash
[omnidc]$ omnistat-build-extras --counters
```
:::

:::{tab-item} Use latest development from git source
:sync: git

From within a cloned copy of the repository, build the two pieces of hardware
counter support in place. First, build the **collector extension** that samples
counters from the GPUs:

```bash
[omnidc]$ pip install cmake-build-extension nanobind
[omnidc]$ BUILD_ROCPROFILER_SDK_EXTENSION=1 python setup.py build_ext --inplace
```

Then build the **counter enablement library** (`libomnistat_count.so`), a
standalone C++ shared library loaded into monitored applications to enable
counter collection for their queues:

```bash
[omnidc]$ cmake -S rocprofiler-sdk/ -B build-count/ -DBUILD_COUNT_LIB=ON
[omnidc]$ cmake --build build-count/
```
:::
::::

```{note}
The optional components rely on `cmake` and HIP C++ compiler.
```

The resulting library is located at `build-count/libomnistat_count.so`. See
[Advanced Profiling](../advanced-profiling.md#hardware-counters) for usage
instructions.

---

## Prometheus server

Once the `omnistat-monitor` daemon is configured and running system-wide, we next install and configure a [Prometheus](https://prometheus.io/) server to enable automatic telemetry collection. This server typically runs on an administrative host and can be installed via OS package manager, by downloading a [precompiled binary](https://prometheus.io/download/), or using a [Docker image](https://hub.docker.com/u/prom). The install steps below highlight installation via package manager followed by a simple scrape configuration.

1. Install: Prometheus server (via package manager)

   ::::{tab-set}
   :::{tab-item} Debian-based
   ```shell-session
   # apt-get install prometheus
   ```
   :::
   :::{tab-item} RHEL
   ```shell-session
   # dnf install golang-github-prometheus
   ```
   :::
   :::{tab-item} SUSE
   ```shell-session
   #  zypper install golang-github-prometheus-prometheus
   ```
   :::
   ::::

2. Configuration: add a scrape configuration to Prometheus to enable telemetry collection. This configuration stanza typically resides in the `/etc/prometheus/prometheus.yml` runtime config file and controls which nodes to poll and at what frequency. The example below highlights configuration of a Prometheus job to poll Omnistat data at 30 second intervals from four separate compute nodes. We recommend keeping the `scrape_interval` setting at 5 seconds or larger.

   ```yaml
   scrape_configs:
     - job_name: "omnistat"
       scrape_interval: 30s
       scrape_timeout: 5s
       static_configs:
         - targets:
           - compute-00:8001
           - compute-01:8001
           - compute-02:8001
           - compute-03:8001
   ```

Edit your server's prometheus.yml file using the snippet above as a guide and restart the Prometheus server to enable automatic data collection. Please also ensure that the target hosts configured for the Omnistat data collection allow queries initiated from this Prometheus server as discussed in the [access restriction](#access-restriction-configuration) section.

```{note}
You may want to adjust the Prometheus server default storage retention policy in order to retain telemetry data longer than the default (which is typically 15 days). Assuming you are using a distro-provided version of Prometheus, you can modify the systemd launch process to include a `--storage.tsdb.retention.time` option as shown in the snippet below:

```ini
[Service]
Restart=on-failure
User=prometheus
EnvironmentFile=/etc/default/prometheus
ExecStart=/usr/bin/prometheus $ARGS --storage.tsdb.retention.time=3y
ExecReload=/bin/kill -HUP $MAINPID
TimeoutStopSec=20s
SendSIGKILL=no
```

---

## Ansible example

For production cluster or data center deployments, configuration management tools like [Ansible](https://github.com/ansible/ansible) may be useful to automate installation of Omnistat. To aid in this process, the following example highlights key elements of an Ansible role to install necessary Python dependencies and configure the Omnistat Prometheus client. These RHEL9-based example files are provided as a starting reference for system administrators and can be adjusted to suit per local conventions.

Note that this recipe assumes existence of a dedicated non-root user to run the Omnistat exporter, templated as `{{ omnistat_user }}`.  It also assumes that an Omnistat release has been downloaded into a local path, templated to be in the `{{ omnistat_dir }}`.

```{eval-rst}
.. code-block:: yaml
   :caption: roles/omnistat/tasks/main.yml

    - name: Set omnistat_dir
      set_fact:
        omnistat_dir: "/path/to/omnistat-repo"
        omnistat_user: "omnidc"

    - name: Show omnistat dir
      debug:
        msg: "Omnistat directory -> {{ omnistat_dir }}"
        verbosity: 0

    - name: Install python package dependencies
      ansible.builtin.pip:
        requirements: "{{ omnistat_dir }}/requirements.txt"
      become_user: "{{ omnistat_user }}"

    #--
    # omnistat service file
    #--

    - name: install omnistat service file
      ansible.builtin.template:
        src: templates/omnistat.service.j2
        dest: /etc/systemd/system/omnistat.service
        mode: '0644'

    - name: omnistat service enabled
      ansible.builtin.service:
        name: omnistat
        enabled: yes
        state: started
```

```{eval-rst}
.. code-block:: ini
   :caption: roles/omnistat/templates/omnistat.service.j2

    [Unit]
    Description=Prometheus exporter for HPC/GPU oriented metrics
    Documentation=https://rocm.github.io/omnistat/
    Requires=network-online.target
    After=network-online.target

    [Service]
    User={{ omnistat_user }}
    Environment="OMNISTAT_CONFIG={{ omnistat_dir }}/omnistat/config/omnistat.default"
    CPUAffinity=0
    SyslogIdentifier=omnistat
    ExecStart={{ omnistat_dir }}/omnistat-monitor
    ExecReload=/bin/kill -HUP $MAINPID
    TimeoutStopSec=20s
    SendSIGKILL=no
    Nice=19
    Restart=on-failure

    [Install]
    WantedBy=multi-user.target
  ```

---

## SLURM Integration

An optional info metric capability exists within Omnistat to allow collected telemetry data to be mapped to individual jobs as they are scheduled by the resource manager.  Multiple options exist to implements this integration, but the recommended approach for large-scale production resources is to leverage prolog/epilog functionality within SLURM to expose relevant job information to the Omnistat data collector. This remaining portion of this section highlights basic steps for implementing this particular strategy.

<u>Note/Assumption</u>: the architecture of the resource manager integration assumes that compute nodes on the cluster are allocated **exclusively** (ie, multiple SLURM jobs do not share the same host).

1. To enable resource manager tracking on the Omnistat client side, edit the chosen runtime config file and update the `[omnistat.collectors]` and `[omnistat.collectors.rms]` sections to have the following settings highlighted in yellow.

```{eval-rst}
.. code-block:: ini
   :caption: omnistat.default
   :emphasize-lines: 4,7-8

   [omnistat.collectors]
   port = 8001
   enable_amd_smi = True
   enable_rms = True

   [omnistat.collectors.rms]
   job_detection_mode = file-based
   job_detection_file = /tmp/omni_rmsjobinfo
```
The settings above enable the resource manager collector and configures Omnistat to query the `/tmp/omni_rmsjobinfo` file to derive dynamic job information.  This file can be generated using the `omnistat-rms-env` utility from within an actively running job, or during prolog execution.  The resulting file contains a simple JSON format as follows:

```{eval-rst}
.. code-block:: json
   :caption: /tmp/omni_rmsjobinfo

    {
        "RMS_TYPE": "slurm",
        "RMS_JOB_ID": "74129",
        "RMS_JOB_USER": "auser",
        "RMS_JOB_PARTITION": "devel",
        "RMS_JOB_NUM_NODES": "2",
        "RMS_JOB_BATCHMODE": 1,
        "RMS_STEP_ID": -1
    }
```

2. SLURM configuration update(s)

The second step to enable resource manager integration is to augment the prolog/epilog scripts configured for your local SLURM environment to create and tear-down the `/tmp/omni_rmsjobinfo` file. Below are example snippets that can be added to the scripts. Note that in these examples, we assume a local `slurm.conf` configuration where Prolog and Epilog are enabled as follows:


```
Prolog=/etc/slurm/slurm.prolog
Epilog=/etc/slurm/slurm.epilog
```

```{eval-rst}
.. code-block:: bash
   :caption: /etc/slurm/slurm.prolog snippet

    # cache job data for omnistat
    OMNISTAT_DIR="/home/omnidc/omnistat"
    OMNISTAT_USER=omnidc
    if [ -e ${OMNISTAT_DIR}/omnistat-rms-env ];then
        su ${OMNISTAT_USER} -c ${OMNISTAT_DIR}/omnistat-rms-env    
    fi
```

```{eval-rst}
.. code-block:: bash
   :caption: /etc/slurm/slurm.epilog snippet

    # remove cached job info to indicate end of job
    if [ -e "/tmp/omni_rmsjobinfo" ];then
        rm -f /tmp/omni_rmsjobinfo
    fi
```

```{note}
To make sure the cached job data file is created immediately upon on allocation of a user job (instead of the first `srun` invocation), be sure to include the following setting in your local SLURM configuration:
```text
PrologFlags=Alloc
```
