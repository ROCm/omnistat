import os
import shutil

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--require-rocprofiler", action="store_true", help="Fail (don't skip) if rocprofiler SDK is unavailable"
    )
    parser.addoption(
        "--require-tsdb",
        action="store_true",
        help="Fail (don't skip) if VictoriaMetrics/Prometheus TSDB is unavailable",
    )
    parser.addoption("--require-docker", action="store_true", help="Fail (don't skip) if Docker is unavailable")


def pytest_configure(config):
    for marker in ("rocprofiler", "tsdb", "docker"):
        config.addinivalue_line("markers", f"{marker}: requires {marker} infrastructure")


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


def _is_required(config, marker_name):
    # --require-<marker> is only registered when this conftest loads at startup
    # (running by path, e.g. `pytest test/foo.py`). Under `pytest --pyargs
    # omnistat_tests` the conftest loads too late for pytest_addoption, so
    # getoption uses a default and forcing falls back to the env var.
    flag = config.getoption(f"--require-{marker_name}", default=False)
    env = os.environ.get(f"OMNISTAT_REQUIRE_{marker_name.upper()}") == "1"
    return flag or env


def pytest_collection_modifyitems(config, items):
    checks = {
        "rocprofiler": (_rocprofiler_available, "rocprofiler SDK not available"),
        "tsdb": (_tsdb_available, "TSDB config not available"),
        "docker": (_docker_available, "Docker not available"),
    }

    for item in items:
        for marker_name, (check_fn, reason) in checks.items():
            if marker_name in item.keywords:
                if check_fn():
                    continue
                if _is_required(config, marker_name):
                    continue  # forced: let it run and fail hard
                item.add_marker(pytest.mark.skip(reason=reason))
