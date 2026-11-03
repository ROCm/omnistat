# Running Jobs

To enable user-mode data collection for a specific job, add logic within your job script to start and stop the collection mechanism before and after running your desired application(s).  Omnistat includes an `omnistat-usermode` utility to help automate this process and the examples below highlight the steps for simple SLURM and Flux job scripts.  Note that the lines highlighted in
yellow need to be customized for the local installation path.


## SLURM example
```{eval-rst}
.. code-block:: bash
   :emphasize-lines: 6-7
   :caption: Example SLURM job file using user-mode Omnistat with a 10 second sampling interval

   #!/bin/bash
   #SBATCH -N 8
   #SBATCH -n 16
   #SBATCH -t 02:00:00

    export OMNISTAT_CONFIG=/path/to/omnistat.config
    export OMNISTAT_DIR=/path/to/omnistat

    # Beginning of job - start data collector
    ${OMNISTAT_DIR}/omnistat-usermode --start --interval 10

    # Run application(s) as normal
    srun <options> ./a.out

    # End of job -  stop data collection, generate summary and store collected data by jobid
    ${OMNISTAT_DIR}/omnistat-usermode --stopexporters
    ${OMNISTAT_DIR}/omnistat-query --job ${SLURM_JOB_ID} --interval 10
    ${OMNISTAT_DIR}/omnistat-usermode --stopserver
    mv data_prom data_prom_${SLURM_JOB_ID}
  ```

## Flux example

```{eval-rst}
.. code-block:: bash
   :emphasize-lines: 8-9
   :caption: Example FLUX job file using user-mode Omnistat with a 1 second sampling interval

   #!/bin/bash
   #flux: -N 8
   #flux: -n 16
   #flux: -t 2h

   jobid=`flux getattr jobid`

   export OMNISTAT_CONFIG=/path/to/omnistat.config
   export OMNISTAT_DIR=/path/to/omnistat

   # Beginning of job - start data collector
   ${OMNISTAT_DIR}/omnistat-usermode --start --interval 1

   # Run application(s) as normal
   flux run <options> ./a.out

   # End of job -  stop data collection, generate summary and store collected data by jobid
   ${OMNISTAT_DIR}/omnistat-usermode --stopexporters
   ${OMNISTAT_DIR}/omnistat-query --job ${jobid} --interval 1
   ${OMNISTAT_DIR}/omnistat-usermode --stopserver
   mv data_prom data_prom.${jobid}
  ```

 In both examples above, the `omnistat-query` utility is used at the end of the job to query collected telemetry (prior to shutting down the server) for the assigned jobid. This should embed an ascii summary for the job similar to the [report card](query_report_card) example mentioned in the Overview directly within the recorded job output.
