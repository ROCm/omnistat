import configparser
import time
from collections import OrderedDict
from unittest.mock import MagicMock, Mock, patch

import pytest

from omnistat.collector_kernel_trace import KernelTrace


class TestKernelTrace:
    """Unit tests for KernelTrace, focusing on the __process_dispatches() method."""

    @pytest.fixture
    def mock_time(self):
        """Fixture to mock time functions for deterministic tests."""
        with patch("time.time_ns") as mock_time_ns, patch("time.clock_gettime_ns") as mock_clock_gettime_ns:
            # Set up time values. In seconds, current unix time is t=1 and
            # system was booted at (t=0). Offset for GPU timestamps is 1s.
            mock_time_ns.return_value = 1_000_000_000
            mock_clock_gettime_ns.return_value = 1_000_000_000
            yield {"time_ns": mock_time_ns, "clock_gettime_ns": mock_clock_gettime_ns, "offset_ns": 1_000_000_000}

    @pytest.fixture
    def collector_instance(self, mock_time):
        """Create a KernelTrace instance for testing."""
        config = configparser.ConfigParser()
        route_mock = Mock()
        interval = 1.0
        kt = KernelTrace(config, route_mock, interval)
        return kt

    def test_empty_dispatches(self, collector_instance, mock_time):
        """Test processing when no dispatches are pending."""
        # Given an empty dispatch queue
        assert len(collector_instance._KernelTrace__dispatches) == 0

        # Process dispatches once every second for 10 seconds
        duration = 10
        start_seconds = (mock_time["time_ns"].return_value // 1_000_000_000) + 1
        end_seconds = start_seconds + duration
        for i in range(start_seconds, end_seconds):
            mock_time["time_ns"].return_value = i * 1_000_000_000
            last_bin = collector_instance._KernelTrace__process_dispatches()

            # Should return current bin (next interval bounday in ms), no values accumulated
            assert last_bin == (mock_time["time_ns"].return_value // 1_000_000) + 1_000
            assert len(collector_instance._KernelTrace__values) == 0

            # Verify bins were extended but empty
            bins = collector_instance._KernelTrace__ts
            assert last_bin in bins
            assert len(bins[last_bin]) == 0

        # Verify total number of bins
        assert len(collector_instance._KernelTrace__ts) == duration + 1

    def test_single_dispatch(self, collector_instance, mock_time):
        """Test processing a single dispatch."""
        # Given one dispatch of kernel_a lasting 5ms and ending at t=3s
        gpu_end_ns = 3_000_000_000 - mock_time["offset_ns"]
        duration_ns = 5_000_000
        dispatch = ("0", "kernel_a", gpu_end_ns, duration_ns)
        collector_instance._KernelTrace__dispatches.append(dispatch)

        # Process dispatches at t=3s
        mock_time["time_ns"].return_value = 3 * 1_000_000_000
        last_bin = collector_instance._KernelTrace__process_dispatches()

        values = collector_instance._KernelTrace__values
        ts = collector_instance._KernelTrace__ts

        key = ("0", "kernel_a")
        assert key in values
        assert values[key] == (1, duration_ns)

        expected_bin = mock_time["time_ns"].return_value // 1_000_000
        assert expected_bin in ts
        assert key in ts[expected_bin]
        assert ts[expected_bin][key] == (1, duration_ns)

        # Verify dispatch queue was cleared
        assert len(collector_instance._KernelTrace__dispatches) == 0
