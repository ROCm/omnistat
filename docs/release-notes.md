---
description: ROCm Omnistat release notes
keywords: Omnistat, ROCm, release notes, AMD
---

# ROCm Omnistat 1.16.0 release notes

These release notes describe notable changes since the previous ROCm Omnistat release.

## Release highlights

This release introduces optional RCCL communication tracing with accompanying Grafana job
dashboard panels, expands network collector capabilities with direct driver-level port counter
reading, and adds site-specific documentation and runtime configuration for the Lux HPC site.

## Added

* Optional RCCL communication tracing with accompanying job dashboard panels.
* Network collector option to read port counters directly from the driver on NICs where
  sysfs totals are unreliable.
* Documentation page and companion runtime configuration for the Lux HPC site.
* Citation information for the project.

## Changed

* Updated production wheel staging workflow.
* Expanded security scanning, workflow token scoping, and automated dependency updates.

## Resolved issues

* Fixed stale extension build paths and lingering references to the removed `query`
  install extra.
* Adapted collector tests to amd-smi-only hardware environments.
* Fixed `allowed_ips` substring matching in the standalone collector, along with two
  latent bugs in the same file.
* Fixed non-functional documentation search field with the ROCm documentation theme.
* Capped `reportlab` on Python 3.9 to avoid an import failure introduced by the 5.1
  release.
