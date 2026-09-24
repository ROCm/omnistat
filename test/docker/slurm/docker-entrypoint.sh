#!/bin/bash
#
# This entrypoint script performs the final installation of Omnistat and
# executes all the appropriate daemons. Valid commands for this script are:
#  - controller-system
#  - node-system
#  - controller-user
#  - node-user
#
# Installation is the same both modes: system and user. However, some
# daemons like the Prometheus Server and Omnistat Monitor are only configured
# and started when running in system mode.

set -e

TEST_OMNISTAT_EXECUTION=${TEST_OMNISTAT_EXECUTION:-source}

if [[ "$1" =~ ^controller ]]; then
    service munge start
    service slurmctld start

    # Configure SSH for compute nodes
    if [[ ! -f $HOME/.ssh/id_rsa ]]; then
        ssh-keygen -t rsa -f $HOME/.ssh/id_rsa -N ""
    fi
    cp $HOME/.ssh/id_rsa.pub $HOME/.ssh/authorized_keys
    echo "Host node*" > $HOME/.ssh/config
    echo "StrictHostKeyChecking no" >> $HOME/.ssh/config
fi

if [[ "$1" == controller-system ]]; then
    cp /host-source/test/docker/slurm/prometheus.yml /etc/prometheus/
    service prometheus start
fi

if [[ "$1" =~ ^node ]]; then
    service munge start
    service slurmd start
    service ssh start

    # Install omnistat based on the current working copy of the repository;
    # this docker compose environment is meant to be used for development and
    # testing. Copy entire directory to avoid polluting the host with files
    # generated in the container.
    mkdir -p /source
    cp -R /host-source/. /source
    rm -rf /source/build /source/omnistat.egg-info

    # Create a Python virtual environment to install Omnistat and/or its
    # dependencies.
    python3 -m venv /opt/omnistat
    . /opt/omnistat/bin/activate

    cd /source

    case "$TEST_OMNISTAT_EXECUTION" in
        "source")
            echo "Executing Omnistat from uninstalled source"
            pip install -r requirements.txt
            ;;
        "package")
            echo "Executing Omnistat from installed package"
            pip install .[query]
            cd && rm -rf /source
            ;;
        *)
            echo "Unknown TEST_OMNISTAT_EXECUTION value"
            exit 1
            ;;
    esac
fi

if [[ "$1" == node-system ]]; then
    # Enable access from the controller container, which is running the
    # prometheus scraper.
    ip=$(dig +short controller)
    sed "s/127.0.0.1/127.0.0.1, $ip/" \
        /host-source/test/docker/slurm/omnistat-system.config \
        > /etc/omnistat.config

    path=/source
    if [[ ! -d /source ]]; then
        path=/opt/omnistat/bin
    fi

    if [[ ! -f $path/omnistat-monitor ]]; then
        echo "Failed to locate omnistat-monitor"
        exit 1
    fi

    OMNISTAT_CONFIG=/etc/omnistat.config $path/omnistat-monitor
fi

if [[ "$1" == node-user ]]; then
    # Enable access from all nodes.
    sed "s/127.0.0.1/127.0.0.1, $(dig +short node1), $(dig +short node2)/" \
        /host-source/test/docker/slurm/omnistat-user.config \
        > /etc/omnistat-user.config
fi

# When there is no service available for health checks, e.g. user-level
# execution, the following log message can be used to make sure the
# installation has completed.
echo "READY"

sleep infinity
