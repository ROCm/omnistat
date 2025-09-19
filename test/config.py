import os
import shutil

# Variable used to skip tests that depend on a ROCm installation; assume
# ROCm is installed if we can find `rocminfo' in the host.
rocm_host = True if shutil.which("rocminfo") else False

# List of available nodes in the test environment.
nodes = ["node1", "node2"]

# Prometheus URL and query configuration.
prometheus_url = "http://localhost:9090/"
victoria_url = "http://localhost:9090/"
time_range = "30m"

# Omnistat monitor port; same port is used for system and user tests.
port = "8002"

rocm_path = os.getenv("ROCM_PATH", "/opt/rocm")

# Path to prometheus data for user-level executions; needs to match datadir
# as defined in docker/slurm/omnistat-user.config.
prometheus_data_user = "/jobs/prometheus-data"
victoria_data_user = "/jobs/vic-data"
