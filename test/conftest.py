import os
import shutil

import pytest


def pytest_addoption(parser):
    parser.addoption("--require-rocm", action="store_true",
                     help="Fail (don't skip) if ROCm is unavailable")
    parser.addoption("--require-rocprofiler", action="store_true",
                     help="Fail (don't skip) if rocprofiler SDK is unavailable")
    parser.addoption("--require-tsdb", action="store_true",
                     help="Fail (don't skip) if VictoriaMetrics/Prometheus TSDB is unavailable")
    parser.addoption("--require-docker", action="store_true",
                     help="Fail (don't skip) if Docker is unavailable")


def _rocm_available():
    return shutil.which("rocminfo") is not None


def _rocprofiler_available():
    return _rocm_available() and "ROCP_TOOL_LIBRARIES" in os.environ


def _docker_available():
    return shutil.which("docker") is not None


def _tsdb_available():
    """Check if the TSDB config file has a valid omnistat.query section."""
    from pathlib import Path

    config_file = Path(__file__).resolve().parent / "docker" / "victoriametrics" / "omnistat-query.config"
    if not config_file.exists():
        return False
    try:
        from omnistat.utils import readConfig
        cfg = readConfig(str(config_file))
        return "omnistat.query" in cfg
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    checks = {
        "rocm": (_rocm_available, "--require-rocm", "ROCm not available"),
        "rocprofiler": (_rocprofiler_available, "--require-rocprofiler", "rocprofiler SDK not available"),
        "tsdb": (_tsdb_available, "--require-tsdb", "TSDB config not available"),
        "docker": (_docker_available, "--require-docker", "Docker not available"),
    }

    for item in items:
        for marker_name, (check_fn, cli_flag, reason) in checks.items():
            if marker_name in item.keywords:
                if check_fn():
                    continue
                if config.getoption(cli_flag):
                    continue  # let it run and fail hard
                item.add_marker(pytest.mark.skip(reason=reason))
