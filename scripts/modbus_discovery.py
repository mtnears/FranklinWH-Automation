#!/usr/bin/env python3
"""
modbus_discovery.py — Modbus Auto-Discovery for FranklinWH Battery Automation v4.0

Reads system configuration and live data directly from the aGate via Modbus TCP.
Eliminates the need for manual configuration of battery count, capacity, reserve,
and provides real-time readings for SOC, power flows, mode, and health.

Register Map (confirmed on Ken's 2-battery system, Feb 2026):
  Standard SunSpec:
    M701 (AC Measurement) base=72:  grid power, voltage, frequency, temps
    M702 (DC/Nameplate)   base=227: max charge/discharge ratings
    M713 (DER Status)     base=1035: SOC, SoH, battery voltage, rated power
  Franklin Extended (proprietary):
    15502: PV total power (watts)
    15506: Home load (watts)
    15507: On-grid mode (0=Backup, 1=TOU, 2=Self-Consumption, 3=Manual)
    15508: Self-consumption reserve (%)
    15509: TOU reserve (%)

Requires: pymodbus (pip install pymodbus)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional, Tuple

logger = logging.getLogger('modbus_discovery')

# ---------------------------------------------------------------------------
# Register Addresses
# ---------------------------------------------------------------------------

# SunSpec Model 701 (AC Measurement) — base address 72
M701_BASE = 72
M701_GRID_POWER = 80         # offset 8: AC active power, watts, signed
M701_VOLTAGE_LN = 85         # offset 13: Line-to-neutral voltage, ÷10 = volts
M701_VOLTAGE_LL = 86         # offset 14: Line-to-line voltage, ÷10 = volts
M701_FREQUENCY = 88          # offset 16: Frequency, ÷1000 = Hz
M701_TEMP_AMB = 105          # offset 33: Ambient temperature, ÷10 = °C
M701_TEMP_CAB = 106          # offset 34: Cabinet temperature, ÷10 = °C
M701_CONN_STATE = 75         # offset 3: Grid connection state

# SunSpec Model 702 (DC/Nameplate) — base address 227
M702_BASE = 227

# SunSpec Model 713 (DER Status) — base address 1035
M713_BASE = 1035
M713_RATED_POWER = 1035      # offset 0: Rated power in watts (30000 = 30kW)
M713_SOC = 1037              # offset 2: SOC, ÷10 = percent
M713_SOH = 1038              # offset 3: SoH, ÷10 = percent

# Franklin Extended Registers (proprietary, 15000+ range)
EXT_BASE = 15500
EXT_PV_TOTAL = 15502         # Total PV/solar power in watts
EXT_HOME_LOAD = 15506        # Home load in watts
EXT_ONGRID_MODE = 15507      # 0=Backup, 1=TOU, 2=Self-Consumption, 3=Manual
EXT_SELF_RESERVE = 15508     # Self-consumption reserve percent
EXT_TOU_RESERVE = 15509      # TOU reserve percent

# Constants
APOWER_CAPACITY_KWH = 13.6   # Single FranklinWH aPower battery capacity
APOWER_RATED_W = 15000        # Single aPower rated power (watts)
MODBUS_DEFAULT_PORT = 502
MODBUS_DEFAULT_UNIT = 2       # aGate uses unit ID 2

# Mode mapping
MODE_MAP = {
    0: 'emergency_backup',
    1: 'time_of_use',
    2: 'self_consumption',
    3: 'manual',
}
MODE_MAP_REVERSE = {v: k for k, v in MODE_MAP.items()}


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ModbusSystemInfo:
    """Static system configuration discovered from Modbus."""
    battery_count: int = 1
    total_capacity_kwh: float = APOWER_CAPACITY_KWH
    capacity_per_battery_kwh: float = APOWER_CAPACITY_KWH
    rated_power_w: int = APOWER_RATED_W
    max_charge_w: int = 5000
    max_discharge_w: int = 5000
    soh_percent: float = 0.0
    discovery_source: str = 'defaults'

    @property
    def max_charge_kw(self) -> float:
        return self.max_charge_w / 1000.0

    @property
    def max_discharge_kw(self) -> float:
        return self.max_discharge_w / 1000.0


@dataclass
class ModbusLiveData:
    """Real-time readings from Modbus."""
    soc_percent: float = 0.0
    soh_percent: float = 0.0
    grid_power_w: int = 0
    solar_power_w: int = 0
    home_load_w: int = 0
    battery_power_w: int = 0          # From ext register if available
    voltage_v: float = 0.0
    frequency_hz: float = 0.0
    temp_ambient_c: float = 0.0
    temp_cabinet_c: float = 0.0
    grid_connected: bool = True
    mode: str = 'unknown'
    mode_raw: int = -1
    reserve_pct: float = 20.0         # Active mode's reserve
    self_reserve_pct: float = 20.0
    tou_reserve_pct: float = 20.0
    read_ok: bool = False
    read_time_ms: float = 0.0
    errors: list = field(default_factory=list)

    @property
    def solar_kw(self) -> float:
        return self.solar_power_w / 1000.0

    @property
    def grid_kw(self) -> float:
        return self.grid_power_w / 1000.0

    @property
    def home_load_kw(self) -> float:
        return self.home_load_w / 1000.0

    @property
    def battery_kw(self) -> float:
        return self.battery_power_w / 1000.0


# ---------------------------------------------------------------------------
# Modbus Client Wrapper
# ---------------------------------------------------------------------------

class ModbusDiscovery:
    """Reads system configuration and live data from the aGate via Modbus TCP.

    Usage:
        discovery = ModbusDiscovery('192.168.5.149')

        # One-time: discover system parameters
        info = discovery.discover_system()

        # Every cycle: read live data
        live = discovery.read_live()
    """

    def __init__(self, host: str, port: int = MODBUS_DEFAULT_PORT,
                 unit: int = MODBUS_DEFAULT_UNIT, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.unit = unit
        self.timeout = timeout
        self._client = None

    def _connect(self) -> bool:
        """Connect to the aGate. Returns True if successful."""
        if self._client is not None:
            try:
                if self._client.is_socket_open():
                    return True
            except Exception:
                pass

        try:
            from pymodbus.client import ModbusTcpClient
            self._client = ModbusTcpClient(self.host, port=self.port, timeout=self.timeout)
            if self._client.connect():
                logger.debug(f"Modbus connected to {self.host}:{self.port}")
                return True
            else:
                logger.warning(f"Modbus connection failed to {self.host}:{self.port}")
                return False
        except ImportError:
            logger.error("pymodbus not installed — run: pip install pymodbus")
            return False
        except Exception as e:
            logger.warning(f"Modbus connection error: {e}")
            return False

    def _disconnect(self):
        """Close the Modbus connection."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _read_registers(self, address: int, count: int) -> Optional[list]:
        """Read holding registers. Returns list of uint16 values or None on error."""
        try:
            result = self._client.read_holding_registers(address, count=count)
            if result and not result.isError():
                return result.registers
            return None
        except Exception as e:
            logger.debug(f"Modbus read error at {address}: {e}")
            return None

    def _read_signed(self, address: int) -> Optional[int]:
        """Read a single register as a signed 16-bit value."""
        regs = self._read_registers(address, 1)
        if regs is None:
            return None
        val = regs[0]
        if val >= 0x8000:
            val -= 0x10000
        return val

    # -------------------------------------------------------------------
    # System Discovery (run once at startup)
    # -------------------------------------------------------------------

    def discover_system(self) -> ModbusSystemInfo:
        """Discover static system parameters from Modbus.

        Reads:
          - M713: rated power → battery count
          - M713: SoH
          - M702: max charge/discharge ratings
          - EXT: reserves

        Returns ModbusSystemInfo with discovered values, or defaults on failure.
        """
        info = ModbusSystemInfo()

        if not self._connect():
            logger.warning("Modbus discovery failed — using defaults")
            return info

        try:
            import time
            start = time.time()

            # --- M713: Rated power, SOC, SoH ---
            m713 = self._read_registers(M713_BASE, 10)
            if m713:
                rated_w = m713[0]
                soh_raw = m713[3]

                if rated_w > 0 and rated_w != 0xFFFF:
                    info.rated_power_w = rated_w
                    info.battery_count = max(1, round(rated_w / APOWER_RATED_W))
                    info.capacity_per_battery_kwh = APOWER_CAPACITY_KWH
                    info.total_capacity_kwh = info.battery_count * APOWER_CAPACITY_KWH

                if soh_raw > 0 and soh_raw != 0xFFFF:
                    info.soh_percent = soh_raw / 10.0

            # --- M702: Max charge/discharge ratings ---
            m702 = self._read_registers(M702_BASE, 10)
            if m702:
                # M702 nameplate: check first few registers for ratings
                # David's code reads WChaRteMax and WDisChaRteMax
                # From your --status output: Max Power=20000, Charge=16000, Discharge=20000
                # These are at specific offsets in M702 — scan for plausible values
                for i, val in enumerate(m702):
                    if val > 1000 and val != 0xFFFF:
                        logger.debug(f"M702 offset {i}: {val}")

                # Try the standard nameplate positions
                # From Ken's M702 dump: offset 0=20000(max power), 8=16000(charge), 9=20000(discharge)
                # Matches David's --status: Max Power=20000, Charge=16000, Discharge=20000
                if len(m702) > 9:
                    max_charge = m702[8] if m702[8] > 1000 and m702[8] != 0xFFFF else None
                    max_discharge = m702[9] if m702[9] > 1000 and m702[9] != 0xFFFF else None
                    max_power = m702[0] if m702[0] > 1000 and m702[0] != 0xFFFF else None

                    if max_charge:
                        info.max_charge_w = max_charge
                    if max_discharge:
                        info.max_discharge_w = max_discharge
                    elif max_power:
                        info.max_discharge_w = max_power

            elapsed_ms = (time.time() - start) * 1000
            info.discovery_source = 'modbus'
            logger.info(
                f"Modbus discovery: {info.battery_count}x {info.capacity_per_battery_kwh} kWh "
                f"= {info.total_capacity_kwh} kWh, SoH={info.soh_percent:.1f}%, "
                f"charge_max={info.max_charge_kw:.1f} kW, "
                f"discharge_max={info.max_discharge_kw:.1f} kW "
                f"({elapsed_ms:.0f}ms)"
            )

        except Exception as e:
            logger.warning(f"Modbus discovery error: {e}")

        return info

    # -------------------------------------------------------------------
    # Live Data Reading (run every cycle)
    # -------------------------------------------------------------------

    def read_live(self) -> ModbusLiveData:
        """Read all live data points from Modbus in a single session.

        Reads:
          - M713: SOC, SoH
          - M701: grid power, voltage, frequency, temps, grid status
          - EXT: solar, home load, mode, reserves

        Returns ModbusLiveData with all available readings.
        """
        data = ModbusLiveData()

        if not self._connect():
            data.errors.append("connection_failed")
            return data

        try:
            import time
            start = time.time()

            # --- M713: SOC, SoH (registers 1035-1044) ---
            m713 = self._read_registers(M713_BASE, 10)
            if m713:
                soc_raw = m713[2]   # offset 2
                soh_raw = m713[3]   # offset 3
                if soc_raw > 0 and soc_raw != 0xFFFF:
                    data.soc_percent = soc_raw / 10.0
                if soh_raw > 0 and soh_raw != 0xFFFF:
                    data.soh_percent = soh_raw / 10.0
            else:
                data.errors.append("m713_read_failed")

            # --- M701: Grid power, voltage, frequency, temps ---
            # Read grid connection state (register 75)
            conn = self._read_registers(M701_CONN_STATE, 1)
            if conn:
                # Value 1 = connected, 0 = disconnected (confirmed in grid disconnect testing)
                data.grid_connected = (conn[0] == 1)

            # Grid power (register 80, signed)
            grid_signed = self._read_signed(M701_GRID_POWER)
            if grid_signed is not None:
                data.grid_power_w = grid_signed

            # Voltage (register 85, ÷10)
            voltage_regs = self._read_registers(M701_VOLTAGE_LN, 2)
            if voltage_regs:
                data.voltage_v = voltage_regs[0] / 10.0

            # Frequency (register 88, ÷1000)
            freq_regs = self._read_registers(M701_FREQUENCY, 1)
            if freq_regs:
                data.frequency_hz = freq_regs[0] / 1000.0

            # Temperatures (registers 105-106, ÷10)
            temp_regs = self._read_registers(M701_TEMP_AMB, 2)
            if temp_regs:
                data.temp_ambient_c = temp_regs[0] / 10.0
                data.temp_cabinet_c = temp_regs[1] / 10.0

            # --- Extended Registers: solar, home load, mode, reserves ---
            ext = self._read_registers(EXT_BASE, 15)
            if ext:
                # PV total (offset 2 from base = register 15502)
                pv_val = ext[2]
                if pv_val != 0xFFFF:
                    data.solar_power_w = pv_val

                # Home load (offset 6 from base = register 15506)
                load_val = ext[6]
                if load_val != 0xFFFF:
                    data.home_load_w = load_val

                # Battery power (also at offset 6, but this IS the home load)
                # Battery power can be derived: home_load + grid_export - grid_import - solar
                # Or use the M701 values. For now, derive from power balance.
                # battery_power = home_load - solar - grid (when grid positive = import)
                # Actually: let's compute it
                # Power balance: solar + grid + battery = home_load
                # So: battery = home_load - solar - grid
                data.battery_power_w = data.home_load_w - data.solar_power_w - data.grid_power_w

                # Mode (offset 7 from base = register 15507)
                mode_raw = ext[7]
                data.mode_raw = mode_raw
                data.mode = MODE_MAP.get(mode_raw, f'unknown_{mode_raw}')

                # Reserves (offset 8-9 from base = registers 15508-15509)
                data.self_reserve_pct = float(ext[8])
                data.tou_reserve_pct = float(ext[9])

                # Active reserve based on current mode
                if data.mode in ('self_consumption', 'manual'):
                    data.reserve_pct = data.self_reserve_pct
                elif data.mode == 'time_of_use':
                    data.reserve_pct = data.tou_reserve_pct
                else:
                    data.reserve_pct = max(data.self_reserve_pct, data.tou_reserve_pct)
            else:
                data.errors.append("ext_read_failed")

            elapsed_ms = (time.time() - start) * 1000
            data.read_time_ms = elapsed_ms
            data.read_ok = len(data.errors) == 0

            if data.read_ok:
                logger.debug(
                    f"Modbus live: SOC={data.soc_percent:.1f}% "
                    f"Solar={data.solar_kw:.2f}kW Grid={data.grid_kw:.2f}kW "
                    f"Load={data.home_load_kw:.2f}kW Mode={data.mode} "
                    f"Reserve={data.reserve_pct}% ({elapsed_ms:.0f}ms)"
                )
            else:
                logger.warning(f"Modbus live read partial: errors={data.errors}")

        except Exception as e:
            data.errors.append(f"exception: {e}")
            logger.warning(f"Modbus live read error: {e}")

        return data

    def close(self):
        """Clean up the connection."""
        self._disconnect()


# ---------------------------------------------------------------------------
# Convenience: One-shot discovery without persistent connection
# ---------------------------------------------------------------------------

def discover_from_modbus(host: str, port: int = MODBUS_DEFAULT_PORT,
                         timeout: float = 5.0) -> Optional[ModbusSystemInfo]:
    """Quick one-shot system discovery. Returns None if Modbus unavailable."""
    try:
        disc = ModbusDiscovery(host, port, timeout=timeout)
        info = disc.discover_system()
        disc.close()
        if info.discovery_source == 'modbus':
            return info
        return None
    except Exception as e:
        logger.warning(f"Modbus discovery failed: {e}")
        return None


def read_live_from_modbus(host: str, port: int = MODBUS_DEFAULT_PORT,
                          timeout: float = 5.0) -> Optional[ModbusLiveData]:
    """Quick one-shot live data read. Returns None if Modbus unavailable."""
    try:
        disc = ModbusDiscovery(host, port, timeout=timeout)
        data = disc.read_live()
        disc.close()
        if data.read_ok:
            return data
        return None
    except Exception as e:
        logger.warning(f"Modbus live read failed: {e}")
        return None


# ---------------------------------------------------------------------------
# CLI — Run standalone for testing
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys
    import os

    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    # Get host from args or environment
    host = sys.argv[1] if len(sys.argv) > 1 else os.environ.get('MODBUS_HOST', '192.168.5.149')
    port = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get('MODBUS_PORT', '502'))

    print(f"\n{'=' * 60}")
    print(f"MODBUS DISCOVERY — {host}:{port}")
    print(f"{'=' * 60}")

    disc = ModbusDiscovery(host, port)

    # System discovery
    print(f"\n--- System Discovery ---")
    info = disc.discover_system()
    print(f"  Source:           {info.discovery_source}")
    print(f"  Battery count:    {info.battery_count}")
    print(f"  Capacity/battery: {info.capacity_per_battery_kwh} kWh")
    print(f"  Total capacity:   {info.total_capacity_kwh} kWh")
    print(f"  Rated power:      {info.rated_power_w} W")
    print(f"  Max charge:       {info.max_charge_w} W ({info.max_charge_kw:.1f} kW)")
    print(f"  Max discharge:    {info.max_discharge_w} W ({info.max_discharge_kw:.1f} kW)")
    print(f"  SoH:              {info.soh_percent:.1f}%")

    # Live data
    print(f"\n--- Live Data ---")
    live = disc.read_live()
    print(f"  Read OK:          {live.read_ok}")
    print(f"  Read time:        {live.read_time_ms:.0f}ms")
    if live.errors:
        print(f"  Errors:           {live.errors}")
    print(f"  SOC:              {live.soc_percent:.1f}%")
    print(f"  SoH:              {live.soh_percent:.1f}%")
    print(f"  Solar:            {live.solar_kw:.2f} kW ({live.solar_power_w} W)")
    print(f"  Grid:             {live.grid_kw:.2f} kW ({live.grid_power_w} W)")
    print(f"  Home load:        {live.home_load_kw:.2f} kW ({live.home_load_w} W)")
    print(f"  Battery:          {live.battery_kw:.2f} kW ({live.battery_power_w} W)")
    print(f"  Grid connected:   {live.grid_connected}")
    print(f"  Voltage:          {live.voltage_v:.1f} V")
    print(f"  Frequency:        {live.frequency_hz:.3f} Hz")
    print(f"  Temp (ambient):   {live.temp_ambient_c:.1f} °C")
    print(f"  Temp (cabinet):   {live.temp_cabinet_c:.1f} °C")
    print(f"  Mode:             {live.mode} (raw={live.mode_raw})")
    print(f"  Reserve (active): {live.reserve_pct}%")
    print(f"  Self reserve:     {live.self_reserve_pct}%")
    print(f"  TOU reserve:      {live.tou_reserve_pct}%")

    disc.close()
    print(f"\nDone.")
