# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2026 Advanced Micro Devices, Inc. All Rights Reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# -------------------------------------------------------------------------------

import time
import uuid
from collections.abc import Iterator


class KernelGenerator:
    """Generate synthetic kernel traces for testing the KernelTrace collector.

    Produces individual GPU kernel dispatch records (gpu_id, name, start_ns,
    end_ns) and groups them into time-interval batches that mirror how the
    collector receives and processes data.  Dispatches are anchored at the
    current time; the simulated process clock advances past each bin so that
    the collector's buffer window can be exercised explicitly.
    """

    def __init__(self, duration_s: int, interval_s: float):
        """Create a generator for a job of *duration_s* seconds sampled every *interval_s*.

        Args:
            duration_s: Total simulated job length in seconds.
            interval_s: Collection interval in seconds (bin width).
        """
        self.interval_ms = int(interval_s * 1_000)
        self.num_intervals = int(duration_s / interval_s)
        self.job_id = job_id = uuid.uuid4()

        # Anchor base bin at the current time (aligned to interval boundary)
        now_ms = int(time.time() * 1000)
        self.base_bin_ms = (now_ms // self.interval_ms) * self.interval_ms

        self._dispatches = []  # [(gpu_id, name, start_ns, end_ns)]

    def add_dispatch(self, gpu_id: int, name: str, start_ns: int, duration_ns: int) -> None:
        """Add a single dispatch at a time relative to the job start.

        Args:
            gpu_id: GPU device index.
            name: Kernel name.
            start_ns: Dispatch start time in nanoseconds relative to base_bin_ms.
            duration_ns: Dispatch duration in nanoseconds.
        """
        start_ns = self.base_bin_ms * 1_000_000 + start_ns
        self._dispatches.append((gpu_id, name, start_ns, start_ns + duration_ns))

    def add_periodic_kernel(self, gpu_id: int, name: str, duration_ns: int, period_ns: int) -> None:
        """Add a kernel that fires at a fixed period for the entire job.

        Dispatches are generated starting from the job start time, each
        lasting *duration_ns* nanoseconds, spaced *period_ns* apart.  Only
        dispatches that fit entirely within the job window are created.

        Args:
            name: Kernel name as it appears in trace records.
            gpu_id: GPU device index.
            duration_ns: Duration of each dispatch in nanoseconds.
            period_ns: Time between successive dispatch starts in nanoseconds.
        """
        job_start_ns = self.base_bin_ms * 1_000_000
        job_end_ns = (self.base_bin_ms + self.num_intervals * self.interval_ms) * 1_000_000
        t = job_start_ns
        while t + duration_ns <= job_end_ns:
            self._dispatches.append((gpu_id, name, t, t + duration_ns))
            t += period_ns

    def generate_batches(self, advance_ms: int = 0) -> Iterator[tuple[int, int, list]]:
        """Yield ``(interval_index, process_time_ns, records)`` for each interval.

        A dispatch belongs to the interval whose time range contains its
        *end* timestamp (in milliseconds).  ``process_time_ns`` is the
        simulated wall-clock time at which the collector should process the
        batch.

        Args:
            advance_ms: Milliseconds past each bin end to set the process
                        clock.  Should exceed the collector's buffer window
                        to allow bins to be emitted.
        """
        for i in range(self.num_intervals):
            bin_start_ms = self.base_bin_ms + i * self.interval_ms
            bin_end_ms = bin_start_ms + self.interval_ms
            process_time_ns = (bin_end_ms + advance_ms) * 1_000_000

            records = []
            for gpu_id, name, start_ns, end_ns in self._dispatches:
                end_ms = end_ns // 1_000_000
                if bin_start_ms <= end_ms < bin_end_ms:
                    records.append([gpu_id, name, start_ns, end_ns])

            yield i, process_time_ns, records

    @property
    def label_defaults(self) -> str:
        """Prometheus label string identifying this job's instance."""
        return f'instance="node.{self.job_id}"'

    def query_labels(self, gpu_id: int, kernel: str) -> str:
        """Build a full Prometheus label selector for a specific GPU and kernel."""
        return f'{self.label_defaults},card="{gpu_id}",kernel="{kernel}"'

    def expected_counts(self, gpu_id: int, kernel: str) -> list[int]:
        """Return the expected dispatch count per interval for a given GPU/kernel.

        The returned list has one entry per interval; each entry is the number
        of dispatches whose end time falls within that interval.
        """
        counts = []
        for _, _, records in self.generate_batches():
            n = sum(1 for r in records if r[0] == gpu_id and r[1] == kernel)
            counts.append(n)
        return counts

    def bin_timestamp_ms(self, interval_index: int) -> int:
        """Return the bin-edge timestamp (ms) for the given interval index.

        This is the *end* of the interval — the timestamp at which the
        cumulative metric value should be queried in VictoriaMetrics.
        """
        return self.base_bin_ms + (interval_index + 1) * self.interval_ms
