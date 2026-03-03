# -*- coding: utf-8 -*-

# Notes
# Added by https://github.com/marcelrv
# https://github.com/Louisvdw/dbus-serialbattery/pull/1016
#
# Refactored to use granular per-register reads instead of large block reads.
# This drastically reduces CRC errors on noisy RS485 links and ensures that
# stale data is NEVER reported as fresh data.

import math
import struct
import time
from typing import Union, Optional, List

import ext.minimalmodbus as minimalmodbus
import serial
from battery import Battery, Cell, Protection
from utils import get_connection_error_message, logger, USE_BMS_DVCC_VALUES

# Retry & timing constants
RETRYCNT = 5
RETRY_SLEEP_S = 0.15
SERIAL_TIMEOUT_S = 0.9


class Seplosv3(Battery):
    def __init__(self, port, baud, address):
        super(Seplosv3, self).__init__(port, baud, address)
        self.type = "Seplos v3"
        self.serialnumber = ""
        self.mbdev: Union[minimalmodbus.Instrument, None] = None
        if address is not None and len(address) > 0:
            self.slaveaddress: int = int(address)
            self.slaveaddresses: list[int] = [self.slaveaddress]
        else:
            self.slaveaddress: int = 0
            self.slaveaddresses = list(range(16))
        self.history.exclude_values_to_calculate = ["charge_cycles", "total_ah_drawn"]

        # Tiered polling: not every register needs to be read every cycle.
        # _poll_cycle counts up and determines which group is read.
        self._poll_cycle: int = 0

    # How often slow/very-slow groups are read (in multiples of poll_interval).
    # With POLL_INTERVAL=5 s:  MEDIUM→every 15 s,  SLOW→every 60 s
    POLL_EVERY_MEDIUM = 3   # cell voltages, temperatures
    POLL_EVERY_SLOW = 12    # alarms, balancing, cycles, BMS current limits

    # ---------------------------------------------------------------------------
    #  Low-level helpers
    # ---------------------------------------------------------------------------

    @staticmethod
    def to_signed_int(value: int) -> int:
        """Converts an unsigned 16-bit value to a signed 16-bit value."""
        packval = struct.pack("<H", value)
        return struct.unpack("<h", packval)[0]

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """Return True for typical RS485 transport errors (CRC / timeout / truncated)."""
        msg = str(exc).lower()
        return (
            "checksum" in msg
            or "crc" in msg
            or "no answer" in msg
            or "no communication" in msg
            or "timed out" in msg
            or "timeout" in msg
            or "too short" in msg
        )

    def _read_registers(
        self,
        name: str,
        register: int,
        count: int,
        fc: int = 4,
        tries: int = RETRYCNT,
    ) -> Optional[List[int]]:
        """
        Read *count* holding/input registers starting at *register*.
        Retries up to *tries* times on retryable errors.
        Returns the list of register values, or None on failure.
        """
        mb = self.get_modbus(self.slaveaddress)
        last_exc = None
        for attempt in range(1, tries + 1):
            try:
                result = mb.read_registers(
                    registeraddress=register,
                    number_of_registers=count,
                    functioncode=fc,
                )
                logger.debug(f"{name}(0x{register:04X}, {count}): {result}")
                return result
            except Exception as e:
                last_exc = e
                if not self._is_retryable(e):
                    logger.info(f"{name}: non-retryable error: {e}")
                    return None
                logger.info(f"{name}: retry {attempt}/{tries}: {e}")
                time.sleep(RETRY_SLEEP_S + min(0.25, 0.05 * attempt))
        logger.info(f"{name}: failed after {tries} tries: {last_exc}")
        return None

    def _read_register(
        self,
        name: str,
        register: int,
        fc: int = 4,
        tries: int = RETRYCNT,
    ) -> Optional[int]:
        """Read a single register. Returns the value or None."""
        result = self._read_registers(name, register, 1, fc, tries)
        if result is not None:
            return result[0]
        return None

    def _read_bits(
        self,
        name: str,
        address: int,
        count: int,
        fc: int = 1,
        tries: int = RETRYCNT,
    ) -> Optional[List[int]]:
        """
        Read *count* coil bits starting at *address*.
        Retries up to *tries* times on retryable errors.
        Returns the list of bit values, or None on failure.
        """
        mb = self.get_modbus(self.slaveaddress)
        last_exc = None
        for attempt in range(1, tries + 1):
            try:
                result = mb.read_bits(address, number_of_bits=count, functioncode=fc)
                logger.debug(f"{name}(0x{address:04X}, {count}): {result}")
                return result
            except Exception as e:
                last_exc = e
                if not self._is_retryable(e):
                    logger.info(f"{name}: non-retryable error: {e}")
                    return None
                logger.info(f"{name}: retry {attempt}/{tries}: {e}")
                time.sleep(RETRY_SLEEP_S + min(0.25, 0.05 * attempt))
        logger.info(f"{name}: failed after {tries} tries: {last_exc}")
        return None

    # ---------------------------------------------------------------------------
    #  Modbus instrument
    # ---------------------------------------------------------------------------

    def get_modbus(self, slaveaddress=0) -> minimalmodbus.Instrument:
        # hack to allow communication to the Seplos BMS using minimodbus which uses slaveaddress 0 as broadcast
        if slaveaddress == 0:
            minimalmodbus._SLAVEADDRESS_BROADCAST = 0xF0
        else:
            minimalmodbus._SLAVEADDRESS_BROADCAST = 0

        if self.mbdev is not None and slaveaddress == self.slaveaddress:
            return self.mbdev

        mbdev = minimalmodbus.Instrument(
            self.port,
            slaveaddress=slaveaddress,
            mode="rtu",
            close_port_after_each_call=True,
            debug=False,
        )
        mbdev.serial.parity = minimalmodbus.serial.PARITY_NONE
        mbdev.serial.stopbits = serial.STOPBITS_ONE
        mbdev.serial.baudrate = 19200
        mbdev.serial.timeout = SERIAL_TIMEOUT_S
        try:
            mbdev.serial.write_timeout = SERIAL_TIMEOUT_S
        except Exception:
            pass
        return mbdev

    # ---------------------------------------------------------------------------
    #  Connection & identification
    # ---------------------------------------------------------------------------

    def test_connection(self):
        """
        Identify the BMS on the bus. Cycles through slave addresses.
        Return True if a Seplos v3 was found, False otherwise.
        """
        found = False

        for self.slaveaddress in self.slaveaddresses:
            mbdev = self.get_modbus(self.slaveaddress)
            if len(self.slaveaddresses) > 1:
                logger.info(f"  |- on slave address {self.slaveaddress}")

            for n in range(1, RETRYCNT + 1):
                try:
                    factory = mbdev.read_string(
                        registeraddress=0x1700,
                        number_of_registers=10,
                        functioncode=4,
                    )
                    if "XZH-ElecTech Co.,Ltd" in factory:
                        logger.info(f"Identified Seplos v3 by '{factory}' on slave address {self.slaveaddress}")
                        model = mbdev.read_string(
                            registeraddress=0x170A,
                            number_of_registers=10,
                            functioncode=4,
                        )
                        logger.info(f"Model: {model}")
                        self.model = model.rstrip("\x00")
                        self.hardware_version = model.rstrip("\x00")

                        sn = mbdev.read_string(
                            registeraddress=0x1715,
                            number_of_registers=15,
                            functioncode=4,
                        )
                        self.serialnumber = sn.rstrip("\x00")
                        logger.info(f"Serial nr: {self.serialnumber}")

                        sw_version = mbdev.read_string(
                            registeraddress=0x1714,
                            number_of_registers=1,
                            functioncode=4,
                        )
                        sw_version = sw_version.rstrip("\x00")
                        self.version = sw_version[0] + "." + sw_version[1]
                        logger.info(f"Firmware Version: {self.version}")
                        found = True
                        self.mbdev = mbdev

                except Exception as e:
                    time.sleep(RETRY_SLEEP_S)
                    logger.debug(
                        f"Seplos v3 testing failed ({e}) {n}/{RETRYCNT}"
                        f" for {self.port}({str(self.slaveaddress)})"
                    )
                    continue
                break
            if found:
                break

        if not found:
            get_connection_error_message(self.online)
        else:
            result = self.get_settings()
            result = result and self.refresh_data()

        return found

    def unique_identifier(self) -> str:
        return self.serialnumber

    # ---------------------------------------------------------------------------
    #  Settings – read once at startup (static config registers)
    # ---------------------------------------------------------------------------

    def get_settings(self) -> bool:
        self.charger_connected = True
        self.load_connected = True

        # Cell count – register 0x1301 (spa offset 1)
        cell_count = self._read_register("cell_count", 0x1301)
        if cell_count is None:
            logger.error("Cannot read cell_count from BMS")
            return False
        self.cell_count = cell_count

        # Capacity – register 0x1359 (spa offset 0x59)
        capacity = self._read_register("capacity", 0x1359)
        if capacity is None:
            logger.error("Cannot read capacity from BMS")
            return False
        self.capacity = capacity / 100

        # Initialise cells array
        self.cells = [Cell(False) for _ in range(self.cell_count)]

        # DVCC values from BMS (spa area – single register reads)
        if USE_BMS_DVCC_VALUES:
            max_v = self._read_register("max_voltage", 0x1305)
            min_v = self._read_register("min_voltage", 0x1311)
            ctrl_v = self._read_register("ctrl_voltage", 0x1365)
            ctrl_cc = self._read_register("ctrl_charge_current", 0x1366)
            ctrl_dc = self._read_register("ctrl_discharge_current", 0x1367)

            if None in (max_v, min_v, ctrl_v, ctrl_cc, ctrl_dc):
                logger.error("Cannot read DVCC registers from BMS")
                return False

            self.max_battery_voltage = max_v / 100
            self.min_battery_voltage = min_v / 100
            self.control_voltage = ctrl_v / 100
            self.control_charge_current = ctrl_cc
            self.control_discharge_current = ctrl_dc

        logger.info(
            f"Settings: cell_count={self.cell_count}, capacity={self.capacity}"
        )
        return True

    # ---------------------------------------------------------------------------
    #  Refresh – called every poll cycle. Reads ONLY fresh data, never stale.
    # ---------------------------------------------------------------------------

    def refresh_data(self) -> bool:
        """
        Read dynamic battery data with tiered polling to reduce bus load.

        FAST   (every cycle):  voltage, current, SoC, FET status
        MEDIUM (every N):      cell voltages, temperatures
        SLOW   (every M):      alarms, balancing, cycles, BMS current limits

        First call (_poll_cycle == 0) always reads everything.
        If a read fails, return False – no stale data is ever served.
        """
        cycle = self._poll_cycle
        is_first = cycle == 0
        do_medium = is_first or (cycle % self.POLL_EVERY_MEDIUM == 0)
        do_slow = is_first or (cycle % self.POLL_EVERY_SLOW == 0)
        self._poll_cycle += 1

        # ==================================================================
        #  FAST – every cycle (5 bus reads)
        # ==================================================================

        # 1) Pack info – voltage, current, remaining capacity
        pia_core = self._read_registers("pia_core", 0x1000, 3)
        if pia_core is None:
            logger.info("refresh_data FAILED: cannot read voltage/current/capacity_remain")
            return False
        self.voltage = pia_core[0] / 100
        self.current = self.to_signed_int(pia_core[1]) / 100
        self.capacity_remain = pia_core[2] / 100

        # 2) Total Ah drawn + SOC
        pia_soc = self._read_registers("pia_soc", 0x1004, 2)
        if pia_soc is None:
            logger.info("refresh_data FAILED: cannot read soc/ah_drawn")
            return False
        self.history.total_ah_drawn = pia_soc[0] * 10
        self.soc = pia_soc[1] / 10

        # 3) FET status – safety critical, always read
        fet_bits = self._read_bits("fet_bits", 0x1278, 9, tries=3)
        if fet_bits is None:
            logger.info("refresh_data FAILED: cannot read FET status")
            return False
        self.discharge_fet = bool(fet_bits[0])
        self.charge_fet = bool(fet_bits[1])
        self.balancing = bool(fet_bits[8])

        # ==================================================================
        #  MEDIUM – cell voltages & temperatures (drives CVL/CCL/DCL calc)
        # ==================================================================
        if do_medium:
            # 4) Cell voltages
            cell_volts = self._read_registers("cell_volts", 0x1100, self.cell_count)
            if cell_volts is None:
                logger.info("refresh_data FAILED: cannot read cell voltages")
                return False

            # 5) Cell temperatures (4 sensors)
            cell_temps = self._read_registers("cell_temps", 0x1110, 4)
            if cell_temps is None:
                logger.info("refresh_data FAILED: cannot read cell temperatures")
                return False

            # 6) MOS temperature
            mos_temp = self._read_register("mos_temp", 0x1119)
            if mos_temp is None:
                logger.info("refresh_data FAILED: cannot read MOS temperature")
                return False

            # Apply cell data
            for i in range(self.cell_count):
                self.cells[i].voltage = cell_volts[i] / 1000.0
                self.cells[i].temperature = cell_temps[math.floor(i / 4)] / 10 - 273.0

            self.temperature_1 = cell_temps[0] / 10 - 273.0
            self.temperature_2 = cell_temps[1] / 10 - 273.0
            self.temperature_3 = cell_temps[2] / 10 - 273.0
            self.temperature_4 = cell_temps[3] / 10 - 273.0
            self.temperature_mos = mos_temp / 10 - 273.0

        # ==================================================================
        #  SLOW – alarms, balancing, cycles, BMS limits
        # ==================================================================
        if do_slow:
            # 7) Charge cycles
            cycles = self._read_register("cycles", 0x1007)
            if cycles is None:
                logger.info("refresh_data FAILED: cannot read charge_cycles")
                return False
            self.history.charge_cycles = cycles

            # 8) Max charge / discharge current from BMS
            pia_limits = self._read_registers("pia_limits", 0x100F, 2)
            if pia_limits is None:
                logger.info("refresh_data FAILED: cannot read current limits")
                return False
            self.max_battery_discharge_current = pia_limits[0]
            self.max_battery_charge_current = pia_limits[1]

            # 9) Balance status (nice-to-have)
            balance_lo = self._read_bits("balance_lo", 0x1228, 8, tries=3)
            balance_hi = self._read_bits("balance_hi", 0x1230, 8, tries=3)
            if balance_lo is not None and balance_hi is not None:
                for i in range(self.cell_count):
                    if i < 8:
                        self.cells[i].balance = bool(balance_lo[i])
                    else:
                        self.cells[i].balance = bool(balance_hi[i - 8])

            # 10) Alarm flags
            sfa = self._read_bits("sfa", 0x1400, 0x50, tries=3)
            if sfa is None:
                logger.info("refresh_data FAILED: cannot read alarm flags")
                return False
            if not self._update_alarms(sfa):
                return False

        logger.debug(
            f"Seplos v3 {self.hardware_version} {self.serialnumber} OK"
            f" (cycle={cycle}, medium={do_medium}, slow={do_slow})"
        )
        return True

    # ---------------------------------------------------------------------------
    #  Alarm processing (unchanged logic, just extracted)
    # ---------------------------------------------------------------------------

    def _update_alarms(self, sfa) -> bool:
        try:
            self.protection = Protection()
            # ALARM = 2 , WARNING = 1 , OK = 0
            self.protection.high_voltage = 2 if sfa[0x05] == 0 else 1 if sfa[0x04] == 0 else 0
            self.protection.low_voltage = 2 if sfa[0x06] == 0 else 1 if sfa[0x06] == 0 else 0
            self.protection.high_cell_voltage = 1 if sfa[0x00] == 0 else 0 + 1 if sfa[0x01] == 0 else 0
            self.protection.low_cell_voltage = 2 if sfa[0x03] == 0 else 1 if sfa[0x02] == 0 else 0
            self.protection.low_soc = 2 if sfa[0x30] == 0 else 0
            self.protection.high_charge_current = 2 if sfa[0x21] == 0 else 1 if sfa[0x20] == 0 else 0
            self.protection.high_discharge_current = 2 if sfa[0x24] == 0 else 1 if sfa[0x23] == 0 else 0
            self.protection.internal_failure = (
                2 if (sfa[0x48] + sfa[0x49] + sfa[0x4A] + sfa[0x4B] + sfa[0x4D] + sfa[0x35]) < 5 else 0
            )
            self.protection.high_charge_temperature = 2 if sfa[0x09] == 0 else 1 if sfa[0x08] == 0 else 0
            self.protection.low_charge_temperature = 2 if sfa[0x0B] == 0 else 1 if sfa[0x0A] == 0 else 0
            self.protection.high_temperature = 2 if sfa[0x0D] == 0 else 1 if sfa[0x0C] == 0 else 0
            self.protection.low_temperature = 2 if sfa[0x0F] == 0 else 1 if sfa[0x0E] == 0 else 0
            self.protection.high_internal_temperature = 2 if sfa[0x15] == 0 else 1 if sfa[0x14] == 0 else 0
        except Exception as e:
            logger.info(f"Error updating alarm info {e}")
            return False
        return True
