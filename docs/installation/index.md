# Overview

As mentioned in the project {ref}`overview <user-vs-system>`, Omnistat has two primary modes of operation: system-wide or user-mode.  The __system-wide__ mode is generally intended for permanent installations across an entire cluster and is typically performed by a system administrator with access to elevated credentials. The __user-mode__ use case does not require elevated credentials and targets temporary data collection directly within a user's batch job under a supported resource manager (e.g. SLURM).

The installation/usage process differs based on the desired usage mode and additional installation details are provided for each case:

* {doc}`System-mode deployment </system-mode/system-install>`
* {doc}`User-mode execution </user-mode/user-install>`
* {doc}`Building optional components </installation/extensions>`
