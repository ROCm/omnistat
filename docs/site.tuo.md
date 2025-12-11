# LLNL

```eval_rst
.. toctree::
   :glob:
   :maxdepth: 4
```

This section provides instructions for running user-mode Omnistat on LLNL's
Tuolumne supercomputer with pre-installed versions from AMD Research.

**Prerequisites**:
- User account on Tuolumne
- Familiarity with Flux job submission
- Be a member of the `omnistat-dev` Unix group (*shared install currently limited to these users*)

## Running jobs on Tuolumne

Omnistat is preinstalled on Tuolumne so users only need to setup their module environment appropriately and add
several commands to their Flux job scripts.  There are two primary options for setup which we highlight below with example Flux job scripts.

### Standard usage

In the standard case, users load the `omnistat` module and include relevant commands in their job script to initiate data collection at job startup and then tear-down the process after application executions have completed.  A simple job example demonstrating this approach is as follows:

```eval_rst
.. code-block:: bash
   :caption: Example Tuolumne Flux job using standard Omnistat module to sample at 1s intervals
   :emphasize-lines: 10-12,18-20

   #!/bin/bash
   #flux: -N 2
   #flux: -n 16
   #flux: -t 1h
   #flux: -q pdebug

   jobid=`flux getattr jobid`

   # Beginning of job - load Omnistat environment and enable data collection
   ml use /usr/workspace/omnistat-dev/modulefiles/
   ml omnistat
   ${OMNISTAT_DIR}/omnistat-usermode --start --interval 1.0

   # Run your snazzy application
   flux run -N 2 sleep 120

   # Tear down job and generate summary
   ${OMNISTAT_DIR}/omnistat-usermode --stopexporters
   ${OMNISTAT_DIR}/omnistat-query --job ${jobid} --interval 1.0 --pdf omnistat.${jobid}.pdf
   ${OMNISTAT_DIR}/omnistat-usermode --stopserver
```

If all runs well, the `omnistat-query` command above will include a short summary output directly within the Flux job output. Representative output from an example job is as follows:

```eval_rst

.. code-block:: none
   :caption: Example telemetry summary report card in user-mode from Tuolomne.

   ----------------------------------------------------------------------
   Omnistat Report Card for Job Id: f2cZ3xxxxxx
   ----------------------------------------------------------------------

   Job Overview (Num Nodes = 2, Machine = LLNL Tuolumne)
   --> Start time = 2025-12-11 13:32:48
   --> End   time = 2025-12-11 13:35:49
   --> Duration   = 181 secs

   GPU Statistics:

              | Utilization (%)  |  Memory Use (%)  | Temperature (C)  |    Power (W)     | Energy (kWh) |
        GPU # |    Max     Mean  |    Max     Mean  |    Max     Mean  |    Max     Mean  |    Total     |
       ---------------------------------------------------------------------------------------------------
            0 |  100.00   26.48  |   63.42   20.03  |   63.00   51.78  |  549.00  227.39  |   2.27e-02   |
            1 |  100.00   27.00  |   63.41   20.03  |   67.00   54.95  |  549.00  229.38  |   2.29e-02   |
            2 |  100.00   25.46  |   63.41   19.94  |   60.00   49.92  |  549.00  212.08  |   2.12e-02   |
            3 |  100.00   25.54  |   63.41   19.95  |   64.00   51.66  |  549.00  222.37  |   2.22e-02   |

   Approximate Total GPU Energy Consumed = 8.90e-02 kWh

   Host Statistics:

      # CPUs | CPU Utilization (%)  |    Memory Use (%)    |    I/O Data Totals   |
             |    Max       Mean    |    Max       Mean    |    Read      Write   |
      -----------------------------------------------------------------------------
         192 |   26.21      5.71    |   69.44     26.56    |  143.3 GB   138.2 GB |

   --
   Query interval = 1.0 secs
   Query execution time = 1.0 secs
   Version = {__VERSION__}
```

### Wrapper usage

The `omnistat` module used in the previous example relies on the system provided `cray-python` module along with extending the python environment to resolve necessary dependencies.  Teams maintaining and deploying their own custom Python stack for application execution may wish to avoid any environment pollution and the local install also includes a convenience wrapper named `omnistat-wrapper`. This wrapper variant can be used as an alternative to run desired Omnistat commands in a sandbox without changing the remaining execution environment.  

 The following Flux job script example highlights use of the wrapper utility before and after executing a GPU application:

```eval_rst
.. code-block:: bash
   :caption: Example Tuolumne Flux job using omnistat-wrapper, highlighting changes needed to run Omnistat
   :emphasize-lines: 10-12,18-20

   #!/bin/bash
   #flux: -N 2
   #flux: -n 16
   #flux: -t 1h
   #flux: -q pdebug

   jobid=`flux getattr jobid`

   # Setup and launch Omnistat (wrapper version)
   ml use /usr/workspace/omnistat-dev/modulefiles/
   ml omnistat-wrapper
   ${OMNISTAT_WRAPPER} usermode --start --interval 1.0

   # Your fine GPU application here
   flux run -N 2 ./your_application

   # Tear down Omnistat
   ${OMNISTAT_WRAPPER} usermode --stopexporters
   ${OMNISTAT_WRAPPER} query --job ${jobid} --interval 1.0 --pdf omnistat.${jobid}.pdf
   ${OMNISTAT_WRAPPER} usermode --stopserver
```

## Storage

By default, Omnistat databases are stored in the user's Lustre file system under an `omnistat` directory, organized by Flux id as follows:
`/p/lustre5/${USER}/omnistat/${FLUX_ENCLOSING_ID}`

It is possible to override the default path using the `OMNISTAT_VICTORIA_DATADIR` environment variable prior to starting up Omnistat. If saving to a node-local storage location, please ensure to move the data after Omnistat teardown to a shared file system to avoid losing the data after job release.

```{note}
Omnistat databases require `flock` support, which is available in Lustre and
local filesystems like `/tmp`.
```

## Data Analysis

After job completion, transfer the archived Omnistat data to your local machine
for analysis using the Docker environment described in the [user-mode
guide](installation/user-mode.md#exploring-results-locally).

