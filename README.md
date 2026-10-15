[![Documentation](https://github.com/ROCm/omnistat/actions/workflows/docs.yml/badge.svg)](https://rocm.github.io/omnistat/)
[![System mode](https://github.com/ROCm/omnistat/actions/workflows/test.yml/badge.svg)](https://github.com/ROCm/omnistat/actions/workflows/test.yml)
[![User mode - Push](https://github.com/ROCm/omnistat/actions/workflows/test-user-push.yml/badge.svg)](https://github.com/ROCm/omnistat/actions/workflows/test-user-push.yml)
[![GPU collectors](https://github.com/ROCm/omnistat/actions/workflows/test-gpus.yml/badge.svg)](https://github.com/ROCm/omnistat/actions/workflows/test-gpus.yml)
[![MI325](https://github.com/ROCm/omnistat/actions/workflows/test-mi325.yml/badge.svg)](https://github.com/ROCm/omnistat/actions/workflows/test-mi325.yml)
[![RDNA4](https://github.com/ROCm/omnistat/actions/workflows/test-rdna4.yml/badge.svg)](https://github.com/ROCm/omnistat/actions/workflows/test-rdna4.yml)
![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/ROCm/omnistat/badges/coverage.json)
[![DOI](https://img.shields.io/badge/DOI-10.1145%2F3837730.3837748-blue.svg)](https://doi.org/10.1145/3837730.3837748)



# Omnistat

## General

Omnistat provides a set of utilities to aid cluster administrators or
individual application developers to aggregate scale-out system
metrics via low-overhead sampling across all hosts in a cluster or,
alternatively on a subset of hosts associated with a specific user
job. Omnistat infrastructure can aid in the collection of key
telemetry from AMD Instinct™ accelerators (on a per-GPU
basis). Relevant target metrics include:

* GPU utilization
* High-bandwidth memory (HBM) usage
* GPU power
* GPU temperature
* GPU clock frequency
* GPU memory clock frequency
* Inventory information
  * ROCm driver version
  * GPU type
  * GPU vBIOS version

#### Additional optional metrics:

* RAS information (error counts per GPU block)
* GPU power caps
* GPU throttling events
* XGMI traffic
* GPU hardware counters
* Host network traffic (rx and tx)
* Host resource utilization (CPU, memory, and I/O)


The data can be scraped for detailed visualization and analysis via a
combination of [Prometheus](https://prometheus.io/) /
[VictoriaMetrics](https://github.com/VictoriaMetrics/VictoriaMetrics)
and [Grafana](https://github.com/grafana/grafana). Users can also
generate PDF reports summarizing resource utilization on a per job
basis with SLURM entirely in user-space.


For more information on available features and installation steps
please refer to the online [documentation](https://rocm.github.io/omnistat/).

## Citation

If you use Omnistat on your systems or as part of your research, please
consider citing:

> J. Polo and K. W. Schulz, "Omnistat: Low-overhead Application Scale-out
> Telemetry and Performance Analysis," in *Proceedings of the Cray User Group
> (CUG 2026)*, 2026, pp. 199-210. https://doi.org/10.1145/3837730.3837748

```bibtex
@inproceedings{polo2026omnistat,
  author    = {Polo, Jord{\`a} and Schulz, Karl W.},
  title     = {Omnistat: Low-overhead Application Scale-out Telemetry and Performance Analysis},
  booktitle = {Proceedings of the Cray User Group (CUG 2026)},
  year      = {2026},
  pages     = {199--210},
  publisher = {Association for Computing Machinery},
  doi       = {10.1145/3837730.3837748},
}
```

---
Omnistat is an AMD open source research project and is not supported
as part of the ROCm software stack. We welcome contributions and
feedback from the community.

Licensing information can be found in the [LICENSE](LICENSE) file.
