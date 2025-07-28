# System-wide installation

```eval_rst
.. toctree::
   :glob:
   :maxdepth: 4
```

There are different ways to deploy and install Omnistat in a data center, and
each system will generally require a certain level of customization. Here, we
provide the basic manual steps to install the Omnistat client
and server, and then provide an example of how to deploy Omnistat in a data
center using Ansible. Finally, an approach for integrating with the SLURM workload manager to track user jobs is discussed.

For system-wide installation, we recommend creation and usage of a dedicated Linux user that will be used to run the data collector daemon of Omnistat (`omnistat-monitor`).  In addition, per the architecture highlighted in {numref}`fig-system-mode`, a separate server (or VM/container) is needed to support installations of a Prometheus server and Grafana instance.  These services can be hosted on your cluster head-node, or via a separate administrative host. Note that if the host chosen to support the Prometheus server can route out externally, you can also leverage public Grafana cloud infrastructure and [forward](https://grafana.com/docs/agent/latest/flow/tasks/collect-prometheus-metrics/) system telemetry data to an external Grafana instance.

To reiterate, the following assumptions are made throughout the rest of this system-wide installation discussion:

__Assumptions__:
* Installer has `sudo` or elevated credentials to install software system-wide, enable systemd services, and optionally modify the local SLURM configuration
* [ROCm](https://rocm.docs.amd.com/en/latest/) v6.1 or newer is pre-installed on all GPU hosts
* Installer has provisioned a dedicated user (eg. `omnidc`) across all desired compute nodes of their system
* Installer has identified a location to host a Prometheus server (if not present already) that has network access to all compute nodes.

(system-install)=
## Omnistat software installation

To begin, we download the Omnistat software and install necessary Python dependencies. Per the assumptions above, we leverage a dedicated user to house the software install.

1. Download and expand latest release version.
   ```shell-session
   [omnidc@login]$ REPO=https://github.com/ROCm/omnistat
   [omnidc@login]$ curl -OLJ ${REPO}/archive/refs/tags/v{__VERSION__}.tar.gz
   [omnidc@login]$ tar xfz omnistat-{__VERSION__}.tar.gz
   ```

2. Install dependencies.
   ```shell-session
   [omnidc@login]$ cd omnistat-v{__VERSION__}
   [omnidc@login]$ pip install --user -r requirements.txt
   ```

<!-- ```{note}
Omnistat can also be installed as a Python package. How cool is that? Add more snazzy text here to get folks pointed in the
right direction.
``` -->

At this point, we can verify basic functionality of the data collector and launch the client by hand.

3. Launch data collector (`omnistat-monitor`) interactively.
   ```shell-session
   [omnidc@login]$ ./omnistat-monitor
   ```

<!-- ### Option B. Install package

1. Clone repository.
   ```
   $ git clone https://github.com/ROCm/omnistat.git
   ```

2. Create a virtual environment, with Python 3.8, 3.9, or 3.10.
   ```
   $ cd omnistat
   $ python -m venv /opt/omnistat
   ```

3. Install omnistat in a virtual environment. The virtual environment can
   also be used by sourcing the `./opt/omnistat/bin/activate` file, and that
   way there is no need to keep using the complete `./venv/bin` path every
   time. This guide uses the complete path for clarity. Needs to be
   executed from the root directory of the Omnistat repository.
   ```
   $ /opt/omnistat/bin/python -m pip install .
   ```
   Alternatively, use the following line to install Omnistat with the
   optional dependencies for the `omnistat-query` tool.
   ```
   $ /opt/omnistat/bin/python -m pip install .[query]
   ```

4. Launch the client with `gunicorn`. To make sure the installed version of
   Omnistat is being used, this shouldn't be executed from the root directory
   of the project.
   ```
   $ /opt/omnistat/bin/gunicorn -b 0.0.0.0:8000 "omnistat.node_monitoring:app"
   ``` -->

<!-- ### Configure client -->

Launching the data collector client as described above will use a set of default
configuration options housed within an [omnistat/config/omnistat.default](https://github.com/ROCm/omnistat/blob/main/omnistat/config/omnistat.default) file including use of port `8001` for the Prometheus client. If all went well, example output from running `omnistat-monitor` is highlighted below:

```shell-session
Reading configuration from /home1/omnidc/omnistat/omnistat/config/omnistat.default
Allowed query IPs = ['127.0.0.1']
Runtime library loaded from /opt/rocm-6.2.1/lib/librocm_smi64.so
SMI library API initialized
SMI version >= 6
Number of GPU devices = 4
GPU topology indexing: Scanning devices from /sys/class/kfd/kfd/topology/nodes
--> Mapping: {0: '3', 1: '2', 2: '1', 3: '0'}
--> Using primary temperature location at edge
--> Using HBM temperature location at hbm_0
--> [registered] rocm_temperature_celsius -> Temperature (C) (gauge)
--> [registered] rocm_temperature_hbm_celsius -> HBM Temperature (C) (gauge)
--> [registered] rocm_average_socket_power_watts -> Average Graphics Package Power (W) (gauge)
--> [registered] rocm_sclk_clock_mhz -> current sclk clock speed (Mhz) (gauge)
--> [registered] rocm_mclk_clock_mhz -> current mclk clock speed (Mhz) (gauge)
--> [registered] rocm_vram_total_bytes -> VRAM Total Memory (B) (gauge)
--> [registered] rocm_vram_used_percentage -> VRAM Memory in Use (%) (gauge)
--> [registered] rocm_vram_busy_percentage -> Memory controller activity (%) (gauge)
--> [registered] rocm_utilization_percentage -> GPU use (%) (gauge)
[2024-07-09 13:19:33 -0500] [2995880] [INFO] Starting gunicorn 21.2.0
[2024-07-09 13:19:33 -0500] [2995880] [INFO] Listening at: http://0.0.0.0:8001 (2995880)
[2024-07-09 13:19:33 -0500] [2995880] [INFO] Using worker: sync
[2024-07-09 13:19:33 -0500] [2995881] [INFO] Booting worker with pid: 2995881
```

```{note}
You can override the default runtime configuration file above by setting an `OMNISTAT_CONFIG` environment variable or by using the `./omnistat-monitor --configfile` option.
```

While the client is running interactively, we can use a _separate_ command shell to query the client to further confirm functionality. The output below highlights an example query response on a system with four GPUs installed (note that the metrics include unique card labels to differentiate specific GPU measurements):

```shell-session
[omnidc@login]$ curl localhost:8001/metrics | grep rocm | grep -v "^#"
rocm_num_gpus 4.0
rocm_temperature_celsius{card="3",location="edge"} 38.0
rocm_temperature_celsius{card="2",location="edge"} 43.0
rocm_temperature_celsius{card="1",location="edge"} 40.0
rocm_temperature_celsius{card="0",location="edge"} 54.0
rocm_average_socket_power_watts{card="3"} 35.0
rocm_average_socket_power_watts{card="2"} 33.0
rocm_average_socket_power_watts{card="1"} 35.0
rocm_average_socket_power_watts{card="0"} 35.0
...
```

Once local functionality has been established, you can terminate the interactive test (ctrl-c) and proceed with an automated startup procedure.


### Enable systemd service

Now that the software is installed under a dedicated user and basic functionality has been confirmed, the data collector can be enabled for permanent service. The recommended approach for this is to leverage `systemd` and an example service file named [omnistat.service](https://github.com/ROCm/omnistat/blob/main/omnistat.service) is included in the distribution. The contents of the file are shown below with four lines highlighted in yellow that are most likely to require local customization.

<!-- * `User` set to the local Linux user created to run Omnistat
* `OMNISTAT_DIR` set to the local path where you downloaded the source tree
* `OMNISTAT_CONFIG` set to the path of desired runtime config file
* `CPUAffinity` set to the CPU core index where omnistat-monitor will be pinned -->


```eval_rst
.. literalinclude:: omnistat.service
   :language: ini
   :emphasize-lines: 8-11
```

Using elevated credentials, install the omnistat.service file across all desired compute nodes (e.g. `/etc/systemd/system/omnistat.service` and enable using systemd (`systemctl enable omnistat`).

### Access restriction configuration

By default, the omnistat data collector will only respond to queries initiated from the local host where the service is running.  This functionality is controlled by a runtime configuration and generally needs to be updated to include the IP address of a companion Prometheus server in order to gather system-wide metrics (see follow-on [discussion](#prometheus-server) for additional details on configuring a Prometheus server).  For example, if your locally configured Prometheus instance has an IP address of `10.0.0.42`, update the `omnistat/config/omnistat.default` runtime file (or equivalent if using a custom configfile) to include the following setting:

```eval_rst
.. code-block:: ini
   :emphasize-lines: 3

    [omnistat.collectors]

    allowed_ips = 127.0.0.1, 10.0.0.42
```

```{note}
Alternatively, you can specify a value of `allowed_ips = 0.0.0.0` to disable any access restrictions.
```


---

## Host telemetry

As mentioned in the [intro](../introduction.md) discussion, we recommend enablement of the popular [node-exporter](https://github.com/prometheus/node_exporter) client for host-level monitoring including CPU load, host memory usage, I/O, and network traffic.  This client is available in most standard distros and we highlight common package manager installs below. Alternatively, you can download binary distributions from [here](https://prometheus.io/download/#node_exporter).

For Debian-based systems:
```shell-session
# apt-get install prometheus-node-exporter
```
For RHEL:
```shell-session
# dnf install golang-github-prometheus-node-exporter
```
For SUSE:
```shell-session
#  zypper install golang-github-prometheus-node_exporter
```

The relevant OS package should be installed on all desired cluster hosts and enabled for execution (e.g. `systemctl enable prometheus-node-exporter` on a RHEL9-based system).

```{note}
The default node-exporter configuration can enable a significantly large number of metrics per host and the example Grafana [dashboards](../grafana.md) included with Omnistat are restricted to rely on a modest number of available metrics. In addition, if desiring to monitor InfiniBand traffic an additional module needs to be enabled. The configuration below highlights  example node-exporter arguments for the `/etc/default/prometheus-node-exporter` file to enable InfiniBand and metrics referenced in example Omnistat dashboards.
```text
ARGS='--collector.disable-defaults --collector.loadavg --collector.diskstats
      --collector.meminfo --collector.stat --collector.netdev
      --collector.infiniband'
```

---

## Prometheus server

Once the `omnistat-monitor` daemon is configured and running system-wide, we next install and configure a [Prometheus](https://prometheus.io/) server to enable automatic telemetry collection. This server typically runs on an administrative host and can be installed via package manager, by downloading a [precompiled binary](https://prometheus.io/download/), or using a [Docker image](https://hub.docker.com/u/prom). The install steps below highlight installation via package manager followed by a simple scrape configuration.

<!-- On a separate server with access to compute nodes, install and configure
[Prometheus](https://prometheus.io/). -->

1. Install: Prometheus server (via package manager)

   For Debian-based systems:
   ```shell-session
   # apt-get install prometheus
   ```
   For RHEL:
   ```shell-session
   # dnf install golang-github-prometheus
   ```
   For SUSE:
   ```shell-session
   #  zypper install golang-github-prometheus-prometheus
   ```

2. Configuration: add a scrape configuration to Prometheus to enable telemetry collection. This configuration stanza typically resides in the `/etc/prometheus/prometheus.yml` runtime config file and controls which nodes to poll and at what frequency. The example below highlights configuration of two Prometheus jobs.  The first enables an omnistat job to poll GPU data at 30 second intervals from four separate compute nodes.  The second job enables collection of the recommended node-exporter to collect host-level data at a similar frequency (default node-exporter port is). We recommend keeping the `scrape_interval` setting at 5 seconds or larger.

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

     - job_name: "node"
       scrape_interval: 30s
       scrape_timeout: 5s
       static_configs:
         - targets:
           - compute-00:9100
           - compute-01:9100
           - compute-02:9100
           - compute-03:9100
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

For production cluster or data center deployments, configuration management tools like [Ansible](https://github.com/ansible/ansible) may be useful to automate installation of Omnistat. To aid in this process, the following example highlights key elements of an Ansible role to install necessary Python dependencies and configure the Omnistat and node-exporter Prometheus clients. These RHEL9-based example files are provided as a starting reference for system administrators and can be adjusted to suit per local conventions.

Note that this recipe assumes existence of a dedicated non-root user to run the Omnistat exporter, templated as `{{ omnistat_user }}`.  It also assumes that an Omnistat release has been downloaded into a local path, templated to be in the `{{ omnistat_dir }}`.

```eval_rst
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

    - name: Install python package dependencies to support query tool
      ansible.builtin.pip:
        requirements: "{{ omnistat_dir }}/requirements-query.txt"
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

    #--
    #  prometheus node exporter
    #--

    - name: node-exporter package
      ansible.builtin.yum:
        name: golang-github-prometheus-node-exporter
        state: installed

    - name: /etc/default/prometheus-node-exporter
      ansible.builtin.template:
        src:  prometheus-node-exporter.j2
        dest:  /etc/default/prometheus-node-exporter
        owner: root
        group: root
        mode: '0644'

    - name: node-exporter service enabled
      ansible.builtin.service:
        name: prometheus-node-exporter
        enabled: yes
        state: started
```

```eval_rst
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
```eval_rst
.. code-block:: bash
   :caption: roles/omnistat/templates/prometheus-node-exporter.j2

    ARGS='--collector.disable-defaults --collector.loadavg --collector.diskstats --collector.meminfo --collector.stat --collector.netdev --collector.infiniband'
```

---

(slurm-integration)=
## SLURM Integration

An optional info metric capability exists within Omnistat to allow collected telemetry data to be mapped to individual jobs as they are scheduled by the resource manager.  Multiple options exist to implements this integration, but the recommended approach for large-scale production resources is to leverage prolog/epilog functionality within SLURM to expose relevant job information to the Omnistat data collector. This remaining portion of this section highlights basic steps for implementing this particular strategy.

<u>Note/Assumption</u>: the architecture of the resource manager integration assumes that compute nodes on the cluster are allocated **exclusively** (ie, multiple SLURM jobs do not share the same host).

1. To enable resource manager tracking on the Omnistat client side, edit the chosen runtime config file and update the `[omnistat.collectors]` and `[omnistat.collectors.rms]` sections to have the following settings highlighted in yellow.

```eval_rst
.. code-block:: ini
   :caption: omnistat.default
   :emphasize-lines: 4,7-8

   [omnistat.collectors]
   port = 8001
   enable_rocm_smi = True
   enable_rms = True

   [omnistat.collectors.rms]
   job_detection_mode = file-based
   job_detection_file = /tmp/omni_rmsjobinfo
```
The settings above enable the resource manager collector and configures Omnistat to query the `/tmp/omni_rmsjobinfo` file to derive dynamic job information.  This file can be generated using the `omnistat-rms-env` utility from within an actively running job, or during prolog execution.  The resulting file contains a simple JSON format as follows:

```eval_rst
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

```eval_rst
.. code-block:: bash
   :caption: /etc/slurm/slurm.prolog snippet

    # cache job data for omnistat
    OMNISTAT_DIR="/home/omnidc/omnistat"
    OMNISTAT_USER=omnidc
    if [ -e ${OMNISTAT_DIR}/omnistat-rms-env ];then
        su ${OMNISTAT_USER} -c ${OMNISTAT_DIR}/omnistat-rms-env    
    fi
```

```eval_rst
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
