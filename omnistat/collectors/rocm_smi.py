# -------------------------------------------------------------------------------
# MIT License
#
# Copyright (c) 2023 - 2025 Advanced Micro Devices, Inc. All Rights Reserved.
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

"""ROCM-smi based data collector

Implements a number of prometheus gauge metrics based on GPU data collected from
rocm smi library.  The ROCm runtime must be pre-installed to use this data
collector. This data collector gathers statistics on a per GPU basis and exposes
metrics with a "rocm" prefix with individual cards denotes by labels. The
following example highlights example metrics for card 0:

rocm_temperature_celsius{card="0",location="edge"} 41.0
rocm_temperature_memory_celsius{card="0",location="hbm_0"} 46.0
rocm_average_socket_power_watts{card="0"} 35.0
rocm_sclk_clock_mhz{card="0"} 1502.0
rocm_mclk_clock_mhz{card="0"} 1200.0
rocm_vram_busy_percentage{card="0"} 22.0
rocm_vram_total_bytes{card="0"} 3.4342961152e+010
rocm_vram_used_percentage{card="0"} 0.0198
rocm_utilization_percentage{card="0"} 0.0
"""

import ctypes
import logging
import os
import sys
from enum import IntEnum
from pathlib import Path

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from omnistat.collector_base import Collector
from omnistat.utils import (
    count_compute_units,
    get_occupancy,
    gpu_index_mapping_based_on_guids,
)

rsmi_clk_names_dict = {"sclk": 0x0, "fclk": 0x1, "dcefclk": 0x2, "socclk": 0x3, "mclk": 0x4}


def get_rsmi_frequencies_type(rsmiVersion):
    """
    Instantiates and returns a struct for use with RSMI frequency queries.
    This data structure is library version dependent. Mimics definitions in
    rsmiBindings.py.

    Args:
        rsmiVersion (dict): ROCm SMI library version info

    Returns:
        C Struct: struct for use with rsmi_dev_power_get() and rsmi_dev_power_ave_get()
    """
    if rsmiVersion["major"] < 6:
        logging.debug("SMI version < 6")
        RSMI_MAX_NUM_FREQUENCIES = 32

        class rsmi_frequencies_t(ctypes.Structure):
            _fields_ = [
                ("num_supported", ctypes.c_int32),
                ("current", ctypes.c_uint32),
                ("frequency", ctypes.c_uint64 * RSMI_MAX_NUM_FREQUENCIES),
            ]

        return rsmi_frequencies_t()
    else:
        logging.debug("SMI version >= 6")
        RSMI_MAX_NUM_FREQUENCIES = 33

        class rsmi_frequencies_t(ctypes.Structure):
            _fields_ = [
                ("has_deep_sleep", ctypes.c_bool),
                ("num_supported", ctypes.c_int32),
                ("current", ctypes.c_uint32),
                ("frequency", ctypes.c_uint64 * RSMI_MAX_NUM_FREQUENCIES),
            ]

        return rsmi_frequencies_t()


class rsmi_power_type_t(ctypes.c_int):
    RSMI_AVERAGE_POWER = (0,)
    RSMI_CURRENT_POWER = (1,)
    RSMI_INVALID_POWER = 0xFFFFFFFF


class rsmi_version_t(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_uint32),
        ("minor", ctypes.c_uint32),
        ("patch", ctypes.c_uint32),
        ("build", ctypes.c_char_p),
    ]


class rsmi_sw_component_t(ctypes.c_int):
    RSMI_SW_COMP_FIRST = 0x0
    RSMI_SW_COMP_DRIVER = RSMI_SW_COMP_FIRST
    RSMI_SW_COMP_LAST = RSMI_SW_COMP_DRIVER


class rsmi_temperature_type_t(IntEnum):
    RSMI_TEMP_TYPE_EDGE = 0
    RSMI_TEMP_TYPE_JUNCTION = 1
    RSMI_TEMP_TYPE_VRAM = 2
    RSMI_TEMP_TYPE_HBM_0 = 3
    RSMI_TEMP_TYPE_HBM_1 = 4
    RSMI_TEMP_TYPE_HBM_2 = 5
    RSMI_TEMP_TYPE_HBM_3 = 6


class rsmi_temperature_type_t(IntEnum):
    RSMI_TEMP_TYPE_EDGE = 0
    RSMI_TEMP_TYPE_JUNCTION = 1
    RSMI_TEMP_TYPE_VRAM = 2
    RSMI_TEMP_TYPE_HBM_0 = 3
    RSMI_TEMP_TYPE_HBM_1 = 4
    RSMI_TEMP_TYPE_HBM_2 = 5
    RSMI_TEMP_TYPE_HBM_3 = 6


class rsmi_gpu_block_t(IntEnum):
    RSMI_GPU_BLOCK_UMC = 0x0000000000000001
    RSMI_GPU_BLOCK_SDMA = 0x0000000000000002
    RSMI_GPU_BLOCK_GFX = 0x0000000000000004
    RSMI_GPU_BLOCK_MMHUB = 0x0000000000000008
    RSMI_GPU_BLOCK_ATHUB = 0x0000000000000010
    RSMI_GPU_BLOCK_PCIE_BIF = 0x0000000000000020
    RSMI_GPU_BLOCK_HDP = 0x0000000000000040
    RSMI_GPU_BLOCK_XGMI_WAFL = 0x0000000000000080
    RSMI_GPU_BLOCK_DF = 0x0000000000000100
    RSMI_GPU_BLOCK_SMN = 0x0000000000000200
    RSMI_GPU_BLOCK_SEM = 0x0000000000000400
    RSMI_GPU_BLOCK_MP0 = 0x0000000000000800
    RSMI_GPU_BLOCK_MP1 = 0x0000000000001000
    RSMI_GPU_BLOCK_FUSE = 0x0000000000002000


class rsmi_ras_err_state_t(ctypes.c_int):
    RSMI_RAS_ERR_STATE_NONE = 0
    RSMI_RAS_ERR_STATE_DISABLED = 1
    RSMI_RAS_ERR_STATE_PARITY = 2
    RSMI_RAS_ERR_STATE_SING_C = 3
    RSMI_RAS_ERR_STATE_MULT_UC = 4
    RSMI_RAS_ERR_STATE_POISON = 5
    RSMI_RAS_ERR_STATE_ENABLED = 6
    RSMI_RAS_ERR_STATE_LAST = RSMI_RAS_ERR_STATE_ENABLED
    RSMI_RAS_ERR_STATE_INVALID = 0xFFFFFFFF


class rsmi_error_count_t(ctypes.Structure):
    _fields_ = [("correctable_err", ctypes.c_uint64), ("uncorrectable_err", ctypes.c_uint64)]


# --


class rocm_smi(Collector):
    def __init__(
        self,
        enable_ecc_ras=True,
        enable_power_capping=False,
        enable_cu_occupancy=False,
        rocm_path="/opt/rocm",
        **kwargs,
    ):
        logging.debug("Initializing ROCm SMI data collector")

        self.__prefix = "rocm_"
        self.__schema = 1.0
        self.__minSMIVersionRequired = (7, 0, 0)
        self.__minROCmVersion = "6.1.0"
        self.__ecc_ras_monitoring = enable_ecc_ras
        self.__power_cap_monitoring = enable_power_capping
        self.__cu_occupancy_monitoring = enable_cu_occupancy
        self.__eccBlocks = {}

        # load smi runtime
        smi_lib = rocm_path + "/lib/librocm_smi64.so"
        if os.path.isfile(smi_lib):
            self.__libsmi = ctypes.CDLL(smi_lib)
            logging.info("Runtime library loaded from %s" % smi_lib)

            # initialize smi library
            ret_init = self.__libsmi.rsmi_init(0)
            assert ret_init == 0

            # cache smi library version
            verInfo = rsmi_version_t()
            ret = self.__libsmi.rsmi_version_get(ctypes.byref(verInfo))
            self.__smiVersion = {"major": verInfo.major, "minor": verInfo.minor, "patch": verInfo.patch}
            logging.info(
                "SMI library API initialized --> version %s.%s.%s loaded"
                % (verInfo.major, verInfo.minor, verInfo.patch)
            )

            # verify minimum version requirement
            verlocal = (verInfo.major, verInfo.minor, verInfo.patch)
            if verlocal < self.__minSMIVersionRequired:
                logging.error("")
                logging.error(
                    "[ERROR]: Minimum SMI version not met: please install ROCm version %s or higher."
                    % self.__minROCmVersion
                )
                logging.error('         Alternatively, update the "rocm_path" setting in the runtime')
                logging.error("         configuration file if multiple versions are available locally.")
                logging.error("")
                sys.exit(4)

            self.__rsmi_frequencies_type = get_rsmi_frequencies_type(self.__smiVersion)

            # driver version
            ver_str = ctypes.create_string_buffer(256)
            self.__libsmi.rsmi_version_str_get(rsmi_sw_component_t.RSMI_SW_COMP_DRIVER, ver_str, 256)
            assert ret_init == 0
            self.__gpuDriverVer = ver_str.value.decode()

        else:
            logging.error("")
            logging.error("ERROR: Unable to load SMI library.")
            logging.error("--> looking for %s" % smi_lib)
            logging.error('--> please verify path and set "rocm_path" in runtime config file if necesssary.')
            logging.error("")
            sys.exit(4)

        self.__GPUmetrics = {}

    # --------------------------------------------------------------------------------------
    # Required child methods

    def registerMetrics(self):
        """Query number of devices and register metrics of interest"""

        numDevices = ctypes.c_uint32(0)
        ret = self.__libsmi.rsmi_num_monitor_devices(ctypes.byref(numDevices))
        assert ret == 0
        logging.info("Number of GPU devices = %i" % numDevices.value)

        # register number of GPUs
        numGPUs_metric = Gauge(self.__prefix + "num_gpus", "# of GPUS available on host")
        numGPUs_metric.set(numDevices.value)
        self.__num_gpus = numDevices.value

        # determine GPU index mapping (ie. map kfd indices used by SMI lib to that of HIP_VISIBLE_DEVICES)
        guidMapping = {}
        nodeMapping = {}
        guid = ctypes.c_int64(0)
        node = ctypes.c_int32(0)
        for i in range(self.__num_gpus):
            device = ctypes.c_uint32(i)

            ret = self.__libsmi.rsmi_dev_guid_get(device, ctypes.byref(guid))
            assert ret == 0
            guidMapping[i] = guid.value

            ret = self.__libsmi.rsmi_dev_node_id_get(device, ctypes.byref(node))
            assert ret == 0
            nodeMapping[i] = node.value

        self.__guidMapping = guidMapping
        self.__indexMapping = gpu_index_mapping_based_on_guids(guidMapping, self.__num_gpus)

        # version info metric
        version_metric = Gauge(
            self.__prefix + "version_info",
            "GPU versioning information",
            labelnames=["card", "driver_ver", "vbios", "type", "schema"],
        )
        for i in range(self.__num_gpus):
            gpuLabel = self.__indexMapping[i]
            ver_str = ctypes.create_string_buffer(256)
            device = ctypes.c_uint32(i)

            self.__libsmi.rsmi_dev_vbios_version_get(device, ver_str, 256)
            vbios = ver_str.value.decode()

            self.__libsmi.rsmi_dev_name_get(device, ver_str, 256)
            devtype = ver_str.value.decode()

            version_metric.labels(
                card=gpuLabel, driver_ver=self.__gpuDriverVer, vbios=vbios, type=devtype, schema=self.__schema
            ).set(1)

        # register desired metric names
        self.__GPUmetrics = {}

        # temperature: note that temperature queries require a location index to be supplied that can
        # vary depending on hardware (e.g. RSMI_TEMP_TYPE_EDGE vs RSMI_TEMP_TYPE_JUNCTION). During init,
        # check which is available and cache index/location of first non-zero response.

        maxTempLocations = 4
        temperature = ctypes.c_int64(0)
        temp_metric = ctypes.c_int32(0)  # 0=RSMI_TEMP_CURRENT
        device = ctypes.c_uint32(0)

        # primary temperature location
        for temp_type in rsmi_temperature_type_t:
            temp_location = ctypes.c_int32(temp_type.value)
            ret = self.__libsmi.rsmi_dev_temp_metric_get(device, temp_location, temp_metric, ctypes.byref(temperature))
            if ret == 0 and temperature.value > 0:
                self.__temp_location_index = temp_location
                self.__temp_location_name = temp_type.name.removeprefix("RSMI_TEMP_TYPE_").lower()
                logging.info("--> Using primary temperature location at %s" % self.__temp_location_name)
                break

        # Cache valid memory temperature location
        self.__temp_memory_location_index = None
        for temp_type in rsmi_temperature_type_t:
            temp_location = ctypes.c_int32(temp_type.value)
            if "HBM" in temp_type.name or "VRAM" in temp_type.name:
                ret = self.__libsmi.rsmi_dev_temp_metric_get(
                    device, temp_location, temp_metric, ctypes.byref(temperature)
                )
                if ret == 0 and temperature.value > 0:
                    self.__temp_memory_location_index = temp_location
                    self.__temp_memory_location_name = temp_type.name.removeprefix("RSMI_TEMP_TYPE_").lower()
                    logging.info("--> Using HBM temperature location at %s" % self.__temp_memory_location_name)
                    break
            else:
                continue

        self.registerGPUMetric(
            self.__prefix + "temperature_celsius",
            "gauge",
            "Temperature (C)",
            labelExtra=["location"],
        )

        if self.__temp_memory_location_index:
            self.registerGPUMetric(
                self.__prefix + "temperature_memory_celsius",
                "gauge",
                "Memory Temperature (C)",
                labelExtra=["location"],
            )

        # power
        self.registerGPUMetric(
            self.__prefix + "average_socket_power_watts", "gauge", "Average Graphics Package Power (W)"
        )
        # clock speeds
        self.registerGPUMetric(self.__prefix + "sclk_clock_mhz", "gauge", "current sclk clock speed (Mhz)")
        self.registerGPUMetric(self.__prefix + "mclk_clock_mhz", "gauge", "current mclk clock speed (Mhz)")
        # memory
        self.registerGPUMetric(self.__prefix + "vram_total_bytes", "gauge", "VRAM Total Memory (B)")
        self.registerGPUMetric(self.__prefix + "vram_used_percentage", "gauge", "VRAM Memory in Use (%)")
        self.registerGPUMetric(self.__prefix + "vram_busy_percentage", "gauge", "Memory controller activity (%)")
        # utilization
        self.registerGPUMetric(self.__prefix + "utilization_percentage", "gauge", "GPU use (%)")
        # RAS counts
        if self.__ecc_ras_monitoring:
            state = rsmi_ras_err_state_t()
            ras_counts = rsmi_error_count_t()
            for block in rsmi_gpu_block_t:
                # check if RAS enabled for this block
                self.__libsmi.rsmi_dev_ecc_status_get(device, block.value, ctypes.byref(state))
                if state.value == rsmi_ras_err_state_t.RSMI_RAS_ERR_STATE_ENABLED:
                    # check if RAS counts available for this block
                    ret = self.__libsmi.rsmi_dev_ecc_count_get(device, block.value, ctypes.byref(ras_counts))
                    if ret == 0:
                        key = block.name.removeprefix("RSMI_GPU_BLOCK_").lower()
                        self.__eccBlocks[key] = block.value
                        metric = self.__prefix + "ras_%s_correctable_count" % key
                        self.registerGPUMetric(
                            metric, "gauge", "number of correctable RAS events for %s block (count)" % key
                        )
                        metric = self.__prefix + "ras_%s_uncorrectable_count" % key
                        self.registerGPUMetric(
                            metric, "gauge", "number of uncorrectable RAS events for %s block (count)" % key
                        )
        # power cap
        if self.__power_cap_monitoring:
            self.registerGPUMetric(self.__prefix + "power_cap_watts", "gauge", "Max power cap of device (W)")

        if self.__cu_occupancy_monitoring:
            # Measure the number CUs in each GPU node ID (KFD internal GPU index),
            # and map it to KFD GPU indices.
            counts = count_compute_units(nodeMapping.values())
            self.__num_compute_units = {i: counts[node] for i, node in nodeMapping.items()}
            self.registerGPUMetric(self.__prefix + "num_compute_units", "gauge", "Number of compute units")
            self.registerGPUMetric(self.__prefix + "compute_unit_occupancy", "gauge", "Compute unit occupancy")

        return

    def updateMetrics(self):
        self.collect_data_incremental()
        return

    # --------------------------------------------------------------------------------------
    # Additional custom methods unique to this collector

    def registerGPUMetric(self, metricName, type, description, labelExtra=None):
        if metricName in self.__GPUmetrics:
            logging.error("Ignoring duplicate metric name addition: %s" % (metricName))
            return
        if type == "gauge":
            labelnames = ["card"]
            if labelExtra:
                for entry in labelExtra:
                    labelnames.append(entry)
            self.__GPUmetrics[metricName] = Gauge(metricName, description, labelnames=labelnames)

            logging.info("--> [registered] %s -> %s (gauge)" % (metricName, description))
        else:
            logging.error("Ignoring unknown metric type -> %s" % type)
        return

    def collect_data_incremental(self):
        # ---
        # Collect and parse latest GPU metrics from rocm SMI library
        # ---

        temperature = ctypes.c_int64(0)
        temp_metric = ctypes.c_int32(0)  # 0=RSMI_TEMP_CURRENT
        temp_location = ctypes.c_int32(0)  # 0=RSMI_TEMP_TYPE_EDGE
        power = ctypes.c_uint64(0)
        power_type = rsmi_power_type_t()
        freq = self.__rsmi_frequencies_type
        freq_system_clock = 0  # 0=RSMI_CLK_TYPE_SYS
        freq_mem_clock = 4  # 4=RSMI_CLK_TYPE_MEM
        vram_total = ctypes.c_uint64(0)
        vram_used = ctypes.c_uint64(0)
        vram_busy = ctypes.c_uint32(0)
        utilization = ctypes.c_uint32(0)
        pcie_sent = ctypes.c_uint64(0)
        pcie_received = ctypes.c_uint64(0)
        pcie_max_pkt_sz = ctypes.c_uint64(0)
        ras_counts = rsmi_error_count_t()

        for i in range(self.__num_gpus):

            device = ctypes.c_uint32(i)
            guid = self.__guidMapping[i]
            gpuLabel = self.__indexMapping[i]

            # --
            # temperature [millidegrees Celcius, converted to degrees Celcius]
            metric = self.__prefix + "temperature_celsius"
            ret = self.__libsmi.rsmi_dev_temp_metric_get(
                device, self.__temp_location_index, temp_metric, ctypes.byref(temperature)
            )
            self.__GPUmetrics[metric].labels(card=gpuLabel, location=self.__temp_location_name).set(
                temperature.value / 1000.0
            )

            # --
            # HBM temperature [millidegrees Celcius, converted to degrees Celcius]
            if self.__temp_memory_location_index:
                metric = self.__prefix + "temperature_memory_celsius"
                ret = self.__libsmi.rsmi_dev_temp_metric_get(
                    device, self.__temp_memory_location_index, temp_metric, ctypes.byref(temperature)
                )
                self.__GPUmetrics[metric].labels(card=gpuLabel, location=self.__temp_memory_location_name).set(
                    temperature.value / 1000.0
                )

            # --
            # average socket power [micro Watts, converted to Watts]
            metric = self.__prefix + "average_socket_power_watts"
            if self.__smiVersion["major"] < 6:
                ret = self.__libsmi.rsmi_dev_power_ave_get(device, 0, ctypes.byref(power))
            else:
                ret = self.__libsmi.rsmi_dev_power_get(device, ctypes.byref(power), ctypes.byref(power_type))
            if ret == 0:
                self.__GPUmetrics[metric].labels(card=gpuLabel).set(power.value / 1000000.0)
            else:
                self.__GPUmetrics[metric].labels(card=gpuLabel).set(0.0)

            # --
            # clock speeds [Hz, converted to megaHz]
            metric = self.__prefix + "sclk_clock_mhz"
            ret = self.__libsmi.rsmi_dev_gpu_clk_freq_get(device, freq_system_clock, ctypes.byref(freq))
            self.__GPUmetrics[metric].labels(card=gpuLabel).set(freq.frequency[freq.current] / 1000000.0)

            metric = self.__prefix + "mclk_clock_mhz"
            ret = self.__libsmi.rsmi_dev_gpu_clk_freq_get(device, freq_mem_clock, ctypes.byref(freq))
            self.__GPUmetrics[metric].labels(card=gpuLabel).set(freq.frequency[freq.current] / 1000000.0)

            # --
            # gpu memory [total_vram in bytes]
            metric = self.__prefix + "vram_total_bytes"
            ret = self.__libsmi.rsmi_dev_memory_total_get(device, 0x0, ctypes.byref(vram_total))
            self.__GPUmetrics[metric].labels(card=gpuLabel).set(vram_total.value)

            metric = self.__prefix + "vram_used_percentage"
            ret = self.__libsmi.rsmi_dev_memory_usage_get(device, 0x0, ctypes.byref(vram_used))
            percentage = round(100.0 * vram_used.value / vram_total.value, 4)
            self.__GPUmetrics[metric].labels(card=gpuLabel).set(percentage)

            metric = self.__prefix + "vram_busy_percentage"
            ret = self.__libsmi.rsmi_dev_memory_busy_percent_get(device, ctypes.byref(vram_busy))
            self.__GPUmetrics[metric].labels(card=gpuLabel).set(vram_busy.value)

            # --
            # utilization
            metric = self.__prefix + "utilization_percentage"
            ret = self.__libsmi.rsmi_dev_busy_percent_get(device, ctypes.byref(utilization))
            self.__GPUmetrics[metric].labels(card=gpuLabel).set(utilization.value)

            # --
            # RAS counts
            if self.__ecc_ras_monitoring:
                for key, block in self.__eccBlocks.items():
                    ret = self.__libsmi.rsmi_dev_ecc_count_get(device, block, ctypes.byref(ras_counts))
                    self.__GPUmetrics[self.__prefix + "ras_%s_correctable_count" % key].labels(card=gpuLabel).set(
                        ras_counts.correctable_err
                    )
                    self.__GPUmetrics[self.__prefix + "ras_%s_uncorrectable_count" % key].labels(card=gpuLabel).set(
                        ras_counts.uncorrectable_err
                    )
            # --
            # power cap
            if self.__power_cap_monitoring:
                metric = self.__prefix + "power_cap_watts"
                ret = self.__libsmi.rsmi_dev_power_cap_get(device, 0x0, ctypes.byref(power))
                # rsmi value in microwatts -> convert to watt
                self.__GPUmetrics[metric].labels(card=gpuLabel).set(power.value / 1000000)

            # --
            # CU occupancy
            if self.__cu_occupancy_monitoring:
                metric = self.__prefix + "num_compute_units"
                self.__GPUmetrics[metric].labels(card=gpuLabel).set(self.__num_compute_units[i])

                metric = self.__prefix + "compute_unit_occupancy"
                cu_occupancy = get_occupancy(guid)
                self.__GPUmetrics[metric].labels(card=gpuLabel).set(cu_occupancy)

        return
