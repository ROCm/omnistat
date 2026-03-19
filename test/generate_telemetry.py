import time

import numpy as np

GPU_METRIC_NAMES = [
    "rocm_utilization_percentage",
    "rocm_vram_used_percentage",
    "rocm_temperature_celsius",
    "rocm_sclk_clock_mhz",
    "rocm_average_socket_power_watts",
]


class TelemetryGenerator:
    """
    Generate synthetic Omnistat traces. A trace consists of one or more
    "loads". Loads represent collections of nodes exhibiting identical
    behavior, allowing spatial partitioning of the trace. Each load can
    optionally define define one or more "steps", which allow for temporal
    partitioning of the trace.
    """

    def __init__(self, duration: int, interval: float, job_id: str, num_nodes: int):
        self.duration = duration
        self.interval = interval
        self.job_id = job_id
        self.num_nodes = num_nodes

        # Simulate time progression starting from roughly the current time:
        # generate metrics as if they happened in the past leading up to now.
        self.start_time = int(time.time()) - duration
        self.times = []
        self.to_time = {}
        for i in list(np.arange(0.0, duration + interval, interval)):
            t = int((self.start_time + i) * 1000)
            self.times.append(t)
            if i.is_integer():
                self.to_time[int(i)] = t

        # For convenience, keep a list of sample timestamps while the job is running.
        self.sample_times = self.times[0 : len(self.times) - 1]

        self.loads = {}
        self.steps = {}

    def add_constant_load(self, load_id: str, num_nodes: int, gpu_values: list[float]):
        """
        Adds a load with constant values. Values can be defined for each GPU
        ID, and will remain the same for all metrics and samples in all the
        nodes that are part of the load.

        Args:
            load_id (str): Unique string to identify load within the trace.
            num_nodes (int): Number of nodes in this load.
            gpu_values (list): List of values to use, indexed by GPU ID.
        """
        self.loads[load_id] = (num_nodes, gpu_values)

    def add_constant_step(self, load_id: str, step_id: str, start: int, end: int, gpu_values: list[float]):
        """
        Adds a step with constant values.

        Args:
            load_id (str): Load identifier for the step.
            step_id (str): Unique string to identify the step.
            start (int): Step start time.
            end (int): Step end time.
            gpu_values (list): List of values to use, indexed by GPU ID.
        """
        if not load_id in self.steps:
            self.steps[load_id] = []
        self.steps[load_id].append((step_id, self.to_time[start], self.to_time[end], gpu_values))

    def generate(self) -> list[str]:
        total_num_nodes = 0
        for _, (num_nodes, _) in self.loads.items():
            total_num_nodes += num_nodes
        assert self.num_nodes == total_num_nodes, "Number of nodes doesn't match load"

        samples = []
        for load_id, (num_nodes, gpu_values) in self.loads.items():
            nodes = [f"node-{i}.{load_id}.{self.job_id}" for i in range(num_nodes)]

            for node in nodes:
                common_info_labels = [
                    f'instance="{node}"',
                    f'nodes="{self.num_nodes}"',
                    f'jobid="{self.job_id}"',
                    f'partition="test"',
                ]

                info_labels = common_info_labels + [f'jobstep="-1"']
                info_samples = [(f'rmsjob_info{{{",".join(info_labels)}}}', "1", t) for t in self.sample_times]

                for step_id, start, end, _ in self.steps.get(load_id, []):
                    step_labels = common_info_labels + [f'jobstep="{step_id}"']
                    step_metric = f'rmsjob_info{{{",".join(step_labels)}}}'
                    info_samples = self._override_metric(info_samples, start, end, step_metric)

                samples.extend(info_samples)

                num_gpus = len(gpu_values)
                num_gpus_labels = [f'instance="{node}"']
                num_gpus_samples = [
                    (f'rocm_num_gpus{{{",".join(num_gpus_labels)}}}', num_gpus, t) for t in self.sample_times
                ]
                samples.extend(num_gpus_samples)

                for gpu_id, value in enumerate(gpu_values):
                    for name in GPU_METRIC_NAMES:
                        gpu_samples = [
                            (f'{name}{{instance="{node}",card="{gpu_id}"}}', value, t) for t in self.sample_times
                        ]

                        for _, start, end, step_gpu_values in self.steps.get(load_id, []):
                            step_value = step_gpu_values[gpu_id]
                            gpu_samples = self._override_value(gpu_samples, start, end, step_value)

                        samples.extend(gpu_samples)

        metrics = self._format(samples)
        return metrics

    def _override_metric(self, samples: list[tuple], start: int, end: int, metric: str) -> list[tuple]:
        overridden = []
        for m, v, t in samples:
            if t >= start and t < end:
                overridden.append((metric, v, t))
            else:
                overridden.append((m, v, t))
        return overridden

    def _override_value(self, samples: list[tuple], start: int, end: int, value: str) -> list[tuple]:
        overridden = []
        for m, v, t in samples:
            if t >= start and t < end:
                overridden.append((m, value, t))
            else:
                overridden.append((m, v, t))
        return overridden

    def _format(self, samples: list[tuple]) -> list[str]:
        """
        Convert a list of samples to a list of strings compatible with the
        Prometheus metrics format.
        """
        formatted = []
        for sample in samples:
            m, v, t = sample
            formatted.append(f"{m} {v} {t}")
        return formatted
