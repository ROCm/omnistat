FROM --platform=linux/amd64 rocm/dev-ubuntu-22.04:10.1.0-full

ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update -y && \
    apt-get install -y --no-install-recommends \
        bind9-dnsutils \
        build-essential \
        git \
        munge \
        numactl \
        openssh-server \
        prometheus \
        python3-venv \
        slurm-client \
        slurmctld \
        slurmd \
        vim \
        victoria-metrics \
        wget

RUN mkdir /var/spool/slurmctld && \
    mkdir /var/spool/slurmd && \
    chown slurm.slurm /var/spool/slurm*

COPY test/docker/slurm/slurm.conf /etc/slurm/
COPY test/docker/slurm/slurm-prolog.sh /etc/slurm/prolog.sh
COPY test/docker/slurm/slurm-epilog.sh /etc/slurm/epilog.sh

COPY test/docker/slurm/docker-entrypoint.sh /usr/local/bin/entrypoint

ENTRYPOINT ["/usr/local/bin/entrypoint"]
