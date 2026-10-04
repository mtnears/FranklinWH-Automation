#!/usr/bin/env python3
"""
FranklinWH Data Source Manager - v3.5.1

Unified data collection from Modbus TCP, Enphase local solar, and Franklin
Cloud API. Designed to minimize cloud API usage by sourcing data locally
wherever possible.

Data Source Hierarchy (v4.7):
  Readings          → Local connection → Modbus TCP → Cloud API
  Solar Production  → Enphase local JSON (from collect_enphase.py) → aGate PV reading
  Mode Switching    → Local connection → Cloud API (saved-token reuse)
  Mode Confirmation → Modbus 15507 → local reading → Cloud API status

Architecture:
  ModbusDataSource    - Fast local readings via SunSpec registers (SOC, grid)
  SolarDataSource     - Reads solar_house.json written by collect_enphase.py
  CloudDataSource     - Franklin cloud API (fallback for data, required for switching)
  SOCTrendTracker     - Derives solar-to-battery rate from SOC changes over time
  ModeStateTracker    - Tracks mode locally, reduces cloud API calls
  DataSourceManager   - Orchestrates all sources with fallback logic
"""

import asyncio
import time
import logging
import settings
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from pathlib import Path
import json

import channel_stats

# Modbus imports (optional)
try:
    from pymodbus.client import ModbusTcpClient
    MODBUS_AVAILABLE = True
except ImportError:
    MODBUS_AVAILABLE = False

# Franklin Cloud API
from franklinwh import Client, TokenFetcher

# Local config
from config import config, configure_logging

# Setup logging
logger = logging.getLogger(__name__)


# =============================================================================
# Mode-switch reserve preservation (v4.6.2)
# =============================================================================

# Per-mode reserve fields returned by the cloud switch-status call
# (Client._switch_status), keyed by our canonical mode names.
_MODE_RESERVE_FIELD = {
    'emergency_backup': 'backupMaxSoc',
    'self_consumption': 'selfMinSoc',
    'time_of_use': 'touMinSoc',
}


def preserved_mode_soc(mode: str, sw_status: Optional[dict]) -> tuple:
    """Reserve SOC to send with a mode switch, and where it came from.

    The cloud mode-switch call always carries a reserve (soc) that the aGate
    adopts for that mode. Before v4.6.2 we always sent RESERVE_SOC_HOME /
    RESERVE_SOC_BACKUP (defaults 20 / 100), so every engine switch silently
    reset a reserve the user had set in the Franklin app.

    Precedence:
      1. RESERVE_SOC_HOME / RESERVE_SOC_BACKUP explicitly set in the environment
         — the user asked the engine to enforce it
      2. The aGate's current reserve for the target mode (echoed back unchanged)
      3. The config default, only if the device value can't be read

    Returns (soc: int, source: 'env' | 'agate' | 'default').
    """
    import os
    is_backup = mode == 'emergency_backup'
    env_key = 'RESERVE_SOC_BACKUP' if is_backup else 'RESERVE_SOC_HOME'
    default = config.RESERVE_SOC_BACKUP if is_backup else config.RESERVE_SOC_HOME

    if settings.is_set(env_key):
        return int(default), 'env'

    field_name = _MODE_RESERVE_FIELD.get(mode)
    if sw_status and field_name:
        try:
            value = int(float(sw_status.get(field_name)))
            if 0 <= value <= 100:
                return value, 'agate'
        except (TypeError, ValueError):
            pass

    logger.warning(f'Could not read aGate {mode} reserve — using config default {default}%')
    return int(default), 'default'


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class BatteryData:
    """Standardized battery data structure from any source."""
    # Core metrics
    soc_percent: float
    battery_power_kw: float  # Positive = discharge, negative = charge
    grid_power_kw: float     # Positive = import, negative = export
    solar_power_kw: float    # Always positive
    home_load_kw: float      # Always positive

    # Additional data
    grid_status: str = "Unknown"
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = "unknown"

    # Per-battery breakdown (if available)
    per_battery_soc: list = field(default_factory=list)

    # Environmental data (if available)
    ambient_temp_c: Optional[float] = None
    cabinet_temp_c: Optional[float] = None
    cell_signal: Optional[int] = None

    # System status
    run_status: Optional[int] = None
    mode_name: str = "Unknown"

    # Charging breakdown (if available)
    grid_to_battery_kw: float = 0.0
    solar_to_battery_kw: float = 0.0

    # Voltage/frequency (if available)
    grid_voltage_v: Optional[float] = None
    grid_frequency_hz: Optional[float] = None

    # Cumulative energy totals (from cloud API stats.totals)
    battery_charge_total_kwh: float = 0.0
    battery_discharge_total_kwh: float = 0.0
    grid_import_total_kwh: float = 0.0
    solar_total_kwh: float = 0.0


@dataclass
class ConnectionStats:
    """Track connection performance and reliability."""
    total_attempts: int = 0
    successful_reads: int = 0
    failed_reads: int = 0
    total_response_time_ms: float = 0.0
    last_success: Optional[datetime] = None
    last_failure: Optional[datetime] = None
    consecutive_failures: int = 0

    @property
    def success_rate(self) -> float:
        if self.total_attempts == 0:
            return 0.0
        return (self.successful_reads / self.total_attempts) * 100.0

    @property
    def avg_response_time_ms(self) -> float:
        if self.successful_reads == 0:
            return 0.0
        return self.total_response_time_ms / self.successful_reads

    def record_success(self, response_time_ms: float):
        self.total_attempts += 1
        self.successful_reads += 1
        self.total_response_time_ms += response_time_ms
        self.last_success = datetime.now()
        self.consecutive_failures = 0

    def record_failure(self):
        self.total_attempts += 1
        self.failed_reads += 1
        self.last_failure = datetime.now()
        self.consecutive_failures += 1


# =============================================================================
# SOC Trend Tracker - derives solar_to_battery_kw from SOC changes
# =============================================================================

class SOCTrendTracker:
    """
    Track SOC readings over time to derive charging rates.

    When Modbus gives us SOC and Enphase gives us solar production,
    we can derive how much solar is actually reaching the battery by
    watching how fast SOC rises.

    Formula:
        charging_rate_kw = (delta_soc% / 100) * battery_capacity_kwh / delta_hours

    If solar is producing and SOC is rising, we attribute the rise to solar.
    If solar is NOT producing and SOC is rising, it's grid charging.
    """

    def __init__(self, battery_capacity_kwh: float = 30.0, max_samples: int = 12,
                 state_file: Path = None):
        self.battery_capacity_kwh = battery_capacity_kwh
        self.max_samples = max_samples
        self.readings = deque(maxlen=max_samples)  # (timestamp, soc_percent)
        self.state_file = state_file or (config.DATA_DIR / "soc_trend.json")
        self._load_state()

    def _load_state(self):
        """Load persisted SOC readings from file."""
        try:
            if self.state_file.exists():
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                for entry in state.get("readings", []):
                    ts = datetime.fromisoformat(entry["timestamp"])
                    soc = entry["soc_percent"]
                    self.readings.append((ts, soc))
                logger.debug(f"Loaded {len(self.readings)} SOC trend readings from {self.state_file}")
        except Exception as e:
            logger.debug(f"Could not load SOC trend state: {e}")

    def _save_state(self):
        """Persist SOC readings to file."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "battery_capacity_kwh": self.battery_capacity_kwh,
                "readings": [
                    {"timestamp": ts.isoformat(), "soc_percent": soc}
                    for ts, soc in self.readings
                ],
                "updated_at": datetime.now().isoformat(),
                "summary": self.get_trend_summary() if len(self.readings) >= 2 else None,
            }
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.debug(f"Could not save SOC trend state: {e}")

    def record(self, soc_percent: float, timestamp: datetime = None):
        """Record a SOC reading and persist to disk."""
        ts = timestamp or datetime.now()
        self.readings.append((ts, soc_percent))
        self._save_state()

    def get_charging_rate_kw(self) -> Optional[float]:
        """
        Calculate current charging rate from recent SOC trend.

        Returns positive value if charging, negative if discharging.
        Returns None if insufficient data.
        """
        if len(self.readings) < 2:
            return None

        # Use the two most recent readings
        ts_prev, soc_prev = self.readings[-2]
        ts_now, soc_now = self.readings[-1]

        delta_hours = (ts_now - ts_prev).total_seconds() / 3600
        if delta_hours < 0.01:  # Less than 36 seconds apart
            return None

        delta_soc = soc_now - soc_prev
        rate_kw = (delta_soc / 100.0) * self.battery_capacity_kwh / delta_hours
        return rate_kw

    def estimate_solar_to_battery(self, solar_kw: float, grid_power_kw: float) -> float:
        """
        Estimate solar-to-battery power based on SOC trend and other inputs.

        Logic:
        - If SOC is rising and solar is producing: solar is likely charging
        - The charging rate from SOC trend tells us total charging power
        - Subtract any grid contribution to isolate solar contribution
        """
        charging_rate = self.get_charging_rate_kw()
        if charging_rate is None or charging_rate <= 0:
            return 0.0

        if solar_kw <= 0.1:
            # No solar — all charging is from grid
            return 0.0

        # If grid is importing (positive), some charging may be from grid
        # If grid is exporting (negative), all charging is from solar
        if grid_power_kw <= 0:
            # Grid exporting or zero — all charging is solar
            return charging_rate
        else:
            # Grid importing — estimate grid's contribution to charging
            # Conservative: attribute what we can to solar
            solar_contribution = max(0, charging_rate - grid_power_kw)
            return min(solar_contribution, solar_kw)  # Can't exceed solar production

    def get_trend_summary(self) -> Dict[str, Any]:
        """Get a summary of recent SOC trend for logging."""
        if len(self.readings) < 2:
            return {"status": "insufficient_data", "samples": len(self.readings)}

        ts_first, soc_first = self.readings[0]
        ts_last, soc_last = self.readings[-1]
        delta_hours = (ts_last - ts_first).total_seconds() / 3600

        return {
            "status": "tracking",
            "samples": len(self.readings),
            "soc_start": soc_first,
            "soc_end": soc_last,
            "delta_soc": soc_last - soc_first,
            "span_minutes": delta_hours * 60,
            "rate_kw": self.get_charging_rate_kw(),
        }


# =============================================================================
# Mode State Tracker - track mode locally to reduce cloud API calls
# =============================================================================

class ModeStateTracker:
    """
    Track battery mode locally instead of querying cloud API every cycle.

    We are the ones issuing mode switches, so we know what mode we set.
    Periodically verify against cloud API to stay honest.
    """

    def __init__(self, state_file: Path = None, verify_interval_minutes: int = 15):
        self.state_file = state_file or (config.LOG_DIR / "mode_state.json")
        self.verify_interval = timedelta(minutes=verify_interval_minutes)
        self.current_mode = "unknown"
        self.mode_name = "Unknown"
        self.last_switch_time = None
        self.last_verified = None
        self._load_state()

    def _load_state(self):
        """Load persisted mode state."""
        try:
            if self.state_file.exists():
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                self.current_mode = state.get("current_mode", "unknown")
                self.mode_name = state.get("mode_name", "Unknown")
                if state.get("last_switch_time"):
                    self.last_switch_time = datetime.fromisoformat(state["last_switch_time"])
                if state.get("last_verified"):
                    self.last_verified = datetime.fromisoformat(state["last_verified"])
        except Exception as e:
            logger.debug(f"Could not load mode state: {e}")

    def _save_state(self):
        """Persist mode state to file."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "current_mode": self.current_mode,
                "mode_name": self.mode_name,
                "last_switch_time": self.last_switch_time.isoformat() if self.last_switch_time else None,
                "last_verified": self.last_verified.isoformat() if self.last_verified else None,
                "updated_at": datetime.now().isoformat(),
            }
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.debug(f"Could not save mode state: {e}")

    def record_switch(self, mode: str):
        """Record that we switched to a mode."""
        self.last_switch_time = datetime.now()
        if mode in ['emergency_backup', 'backup']:
            self.current_mode = "emergency_backup"
            self.mode_name = "Emergency Backup"
        elif mode == 'self_consumption':
            self.current_mode = "self_consumption"
            self.mode_name = "Self Consumption"
        else:
            self.current_mode = config.HOME_MODE
            self.mode_name = "Time of Use" if config.HOME_MODE == "tou" else "Self Consumption"
        self._save_state()
        logger.info(f"Mode state recorded: {self.current_mode} ({self.mode_name})")

    def record_verification(self, mode: str, mode_name: str):
        """Record cloud API verification result."""
        self.current_mode = mode
        self.mode_name = mode_name
        self.last_verified = datetime.now()
        self._save_state()

    def needs_verification(self) -> bool:
        """Check if we should verify mode against cloud API."""
        if self.current_mode == "unknown":
            return True
        if self.last_verified is None:
            return True
        return datetime.now() - self.last_verified > self.verify_interval

    def get_mode(self) -> str:
        return self.current_mode

    def get_mode_name(self) -> str:
        return self.mode_name


# =============================================================================
# Abstract Data Source
# =============================================================================

class DataSource(ABC):
    """Abstract base class for data sources."""

    def __init__(self):
        self.stats = ConnectionStats()
        self.enabled = True
        self.name = "Unknown"

    @abstractmethod
    async def read_battery_data(self) -> Optional[BatteryData]:
        pass

    @abstractmethod
    async def switch_mode(self, mode: str) -> bool:
        pass

    def is_healthy(self) -> bool:
        return self.stats.consecutive_failures < 5

    @property
    def consecutive_failures(self) -> int:
        return self.stats.consecutive_failures


# =============================================================================
# Modbus TCP Data Source
# =============================================================================

class ModbusDataSource(DataSource):
    """Modbus TCP data source using SunSpec DER models + Franklin extensions."""

    MODELS = {
        "Model 701": {"addr": 72, "length": 153},   # AC Measurement
        "Model 713": {"addr": 1035, "length": 7},    # DER Status
        "Model 702": {"addr": 227, "length": 50},    # DC Measurement
    }

    # Franklin extension registers (15500+ range, proprietary)
    EXT_BASE = 15500
    EXT_READ_COUNT = 17          # 15500-15516
    EXT_PV_OFFSET = 2            # 15502: PV output power (W)
    EXT_HOME_LOAD_OFFSET = 6     # 15506: Home load power (W)
    EXT_MODE_OFFSET = 7          # 15507: Operating mode
    EXT_SELF_RESERVE_OFFSET = 8  # 15508: Self-consumption reserve %
    EXT_TOU_RESERVE_OFFSET = 9   # 15509: TOU reserve %
    EXT_TOU_DISPATCH_OFFSET = 16 # 15516: TOU dispatch state

    EXT_MODE_MAP = {
        0: ("standby", "Standby"),
        1: ("emergency_backup", "Emergency Backup"),
        2: ("self_consumption", "Self Consumption"),
        3: ("time_of_use", "Time of Use"),
    }

    TOU_DISPATCH_MAP = {
        0: "Idle", 1: "Home Loads", 2: "Standby",
        3: "Solar Charging", 4: "Grid Charging", 5: "Grid Discharge",
        6: "Self Consumption", 7: "Grid Export", 8: "Grid Charge",
    }

    # Sanity bounds (watts). Values near 0xFFFF (e.g. 65531) pass the
    # != 0xFFFF check but are clearly erroneous for residential systems.
    MAX_PLAUSIBLE_SOLAR_W = 25000   # 25 kW
    MAX_PLAUSIBLE_LOAD_W = 50000    # 50 kW

    def __init__(self):
        super().__init__()
        self.name = "Modbus TCP"
        self.client = None
        self.enabled = config.MODBUS_ENABLED and MODBUS_AVAILABLE

        if config.MODBUS_ENABLED and not MODBUS_AVAILABLE:
            logger.warning("Modbus enabled in config but pymodbus not installed")
            self.enabled = False

    async def _connect(self) -> bool:
        if not self.enabled:
            return False
        try:
            if self.client and self.client.connected:
                return True

            self.client = ModbusTcpClient(
                host=config.MODBUS_HOST,
                port=config.MODBUS_PORT,
                timeout=config.MODBUS_TIMEOUT
            )
            connected = self.client.connect()
            if connected:
                logger.debug(f"Modbus connected to {config.MODBUS_HOST}:{config.MODBUS_PORT}")
            else:
                logger.warning(f"Modbus connection failed to {config.MODBUS_HOST}:{config.MODBUS_PORT}")
            return connected
        except Exception as e:
            logger.error(f"Modbus connection error: {e}")
            return False

    def _read_registers(self, addr: int, count: int) -> Optional[list]:
        """Read holding registers with error handling."""
        try:
            response = self.client.read_holding_registers(addr, count=count)
            if response.isError():
                logger.error(f"Modbus read error at {addr}: {response}")
                return None
            return response.registers
        except Exception as e:
            logger.error(f"Modbus read exception at {addr}: {e}")
            return None

    def _uint16_to_int16(self, val: int) -> int:
        """Convert unsigned 16-bit to signed 16-bit."""
        return val - 0x10000 if val >= 0x8000 else val

    async def read_battery_data(self) -> Optional[BatteryData]:
        """Read battery data via Modbus TCP: SunSpec models + Franklin extensions."""
        start_time = time.time()

        try:
            if not await self._connect():
                self.stats.record_failure()
                channel_stats.record('modbus', 'read', False, None, 'could not connect')
                return None

            # Read key SunSpec models
            model_713 = self._read_registers(1035, 7)   # DER Status (SOC)
            model_701 = self._read_registers(72, 50)     # AC Measurement

            if not model_713:
                logger.warning("Failed to read Model 713 (DER Status)")
                self.stats.record_failure()
                channel_stats.record('modbus', 'read', False, None, 'model 713 read failed')
                return None

            # SOC from Model 713 offset 2 (confirmed: value / 10)
            soc_raw = model_713[2] if len(model_713) > 2 else 0
            soc_percent = soc_raw / 10.0

            # Build battery data with what Modbus provides
            battery_data = BatteryData(
                soc_percent=soc_percent,
                battery_power_kw=0.0,
                grid_power_kw=0.0,
                solar_power_kw=0.0,
                home_load_kw=0.0,
                source="modbus",
                timestamp=datetime.now()
            )

            # Grid power from Model 701 offset 8 (signed watts, confirmed r=0.998)
            if model_701 and len(model_701) > 8:
                grid_power_raw = self._uint16_to_int16(model_701[8])
                battery_data.grid_power_kw = grid_power_raw / 1000.0

            # Frequency from offset 16
            if model_701 and len(model_701) > 16:
                freq_raw = model_701[16]
                battery_data.grid_frequency_hz = freq_raw / 1000.0 if freq_raw > 0 else None

            # Temperature data
            if model_701 and len(model_701) > 33:
                amb_temp_raw = model_701[33]
                if amb_temp_raw != 0xFFFF and amb_temp_raw != 0x8000:
                    battery_data.ambient_temp_c = self._uint16_to_int16(amb_temp_raw) / 10.0

            if model_701 and len(model_701) > 34:
                cab_temp_raw = model_701[34]
                if cab_temp_raw != 0xFFFF and cab_temp_raw != 0x8000:
                    battery_data.cabinet_temp_c = self._uint16_to_int16(cab_temp_raw) / 10.0

            # Voltage
            if model_701 and len(model_701) > 44:
                voltage_raw = model_701[44]
                if voltage_raw > 0:
                    battery_data.grid_voltage_v = voltage_raw / 10.0

            # Grid connection state from offset 3
            if model_701 and len(model_701) > 3:
                battery_data.grid_status = "Connected" if model_701[3] == 1 else "Disconnected"

            # --- Franklin Extension Registers (15500-15516) ---
            ext = self._read_registers(self.EXT_BASE, self.EXT_READ_COUNT)
            if ext and len(ext) >= 10:
                # Home load (15506) - watts
                load_val = ext[self.EXT_HOME_LOAD_OFFSET]
                load_valid = load_val != 0xFFFF and load_val < self.MAX_PLAUSIBLE_LOAD_W
                if load_valid:
                    battery_data.home_load_kw = load_val / 1000.0

                # PV/solar from Franklin (15502) - watts
                # Store as ext_solar for cross-check; Enphase will override in merge
                pv_val = ext[self.EXT_PV_OFFSET]
                pv_valid = pv_val != 0xFFFF and pv_val < self.MAX_PLAUSIBLE_SOLAR_W
                if pv_valid:
                    battery_data._ext_solar_kw = pv_val / 1000.0

                # Derive battery power from energy balance:
                # solar + grid + battery = home_load
                # battery = home_load - solar - grid
                if load_valid:
                    ext_solar_w = pv_val if pv_valid else 0
                    battery_data.battery_power_kw = (load_val - ext_solar_w - (battery_data.grid_power_kw * 1000)) / 1000.0

                # Mode from 15507 - direct hardware state
                mode_raw = ext[self.EXT_MODE_OFFSET]
                if mode_raw in self.EXT_MODE_MAP:
                    mode_id, mode_label = self.EXT_MODE_MAP[mode_raw]
                    battery_data._ext_mode = mode_id
                    battery_data._ext_mode_name = mode_label

                    # TOU dispatch sub-state from 15516
                    if mode_raw == 3 and len(ext) > self.EXT_TOU_DISPATCH_OFFSET:
                        dispatch = ext[self.EXT_TOU_DISPATCH_OFFSET]
                        dispatch_text = self.TOU_DISPATCH_MAP.get(dispatch, f"Unknown({dispatch})")
                        battery_data._ext_tou_dispatch = dispatch
                        battery_data._ext_tou_dispatch_text = dispatch_text
                        battery_data._ext_mode_name = f"TOU-{dispatch_text}"

                # Reserves
                battery_data._ext_self_reserve = ext[self.EXT_SELF_RESERVE_OFFSET]
                battery_data._ext_tou_reserve = ext[self.EXT_TOU_RESERVE_OFFSET]
            else:
                logger.debug("Franklin extension registers not available")

            # Record success
            response_time = (time.time() - start_time) * 1000
            self.stats.record_success(response_time)
            channel_stats.record('modbus', 'read', True, response_time)

            logger.debug(f"Modbus read: SOC={soc_percent:.1f}%, "
                        f"Grid={battery_data.grid_power_kw:.3f}kW, "
                        f"Load={battery_data.home_load_kw:.3f}kW, "
                        f"Time={response_time:.0f}ms")

            return battery_data

        except Exception as e:
            logger.error(f"Modbus read failed: {e}")
            self.stats.record_failure()
            channel_stats.record('modbus', 'read', False, None, str(e))
            return None

    async def switch_mode(self, mode: str) -> bool:
        """Mode switching not available via Modbus."""
        logger.warning("Mode switching not supported via Modbus (read-only)")
        return False

    def disconnect(self):
        if self.client and self.client.connected:
            self.client.close()
            logger.debug("Modbus connection closed")


# =============================================================================
# Solar Data Source - reads from existing Enphase collector output
# =============================================================================

class SolarDataSource:
    """
    Read solar production from the Enphase collector's JSON output.

    The collect_enphase.py script runs every 5 minutes and writes
    solar_house.json to both data/ and web/ directories. We just
    read that file — no additional API calls needed.

    For users without Enphase, falls back to PVOutput data if available.
    """

    def __init__(self):
        self.stats = ConnectionStats()
        self.name = "Solar (Local)"
        self.last_solar_kw = 0.0
        self.last_read_time = None

        # Determine solar data file path
        # The collect_enphase.py writes to data/solar_{array_id}.json
        self.solar_file = config.DATA_DIR / "solar_house.json"

        # Check if SOLAR_ARRAYS is configured for a different array ID
        solar_arrays = getattr(config, 'SOLAR_ARRAYS', '')
        if solar_arrays:
            # Use first Enphase array found
            for array_id in [a.strip() for a in solar_arrays.split(',') if a.strip()]:
                array_type = settings.raw(f'SOLAR_ARRAY_{array_id.upper()}_TYPE', '')
                if array_type == 'enphase':
                    self.solar_file = config.DATA_DIR / f"solar_{array_id}.json"
                    break

        self.enabled = self.solar_file.parent.exists()

    def read_solar_production(self) -> Optional[float]:
        """
        Read current solar production in kW from the Enphase JSON file.

        Returns total_kw from the most recent collection, or None if
        the data is stale (>15 min old) or unavailable.
        """
        start_time = time.time()

        try:
            if not self.solar_file.exists():
                logger.debug(f"Solar data file not found: {self.solar_file}")
                self.stats.record_failure()
                return None

            with open(self.solar_file, 'r') as f:
                data = json.load(f)

            # Check freshness — data older than 15 minutes is stale
            timestamp_str = data.get("timestamp", "")
            if timestamp_str:
                try:
                    data_time = datetime.fromisoformat(timestamp_str)
                    age_minutes = (datetime.now() - data_time).total_seconds() / 60
                    if age_minutes > 15:
                        logger.warning(f"Solar data is {age_minutes:.0f} min old (stale)")
                        # Still return the value but log the staleness
                except (ValueError, TypeError):
                    pass

            # Check collection status
            if data.get("collection_status") == "error":
                logger.warning(f"Solar collector reported error: {data.get('error', 'unknown')}")
                self.stats.record_failure()
                return None

            # Extract production — prefer meter reading, fall back to inverter sum
            summary = data.get("summary", {})
            production = data.get("production", {})

            # meter_w_now is more accurate (includes CT measurement)
            meter_w = production.get("meter_w_now")
            if meter_w is not None and meter_w >= 0:
                solar_kw = meter_w / 1000.0
            else:
                # Fall back to sum of inverter readings
                solar_kw = summary.get("total_kw", 0.0)

            response_time = (time.time() - start_time) * 1000
            self.stats.record_success(response_time)
            self.last_solar_kw = solar_kw
            self.last_read_time = datetime.now()

            logger.debug(f"Solar read: {solar_kw:.3f}kW from {self.solar_file.name} "
                        f"({response_time:.0f}ms)")

            return solar_kw

        except json.JSONDecodeError as e:
            logger.error(f"Solar JSON parse error: {e}")
            self.stats.record_failure()
            return None
        except Exception as e:
            logger.error(f"Solar read failed: {e}")
            self.stats.record_failure()
            return None


# =============================================================================
# Cloud API Data Source (fallback + mode switching)
# =============================================================================

class CloudDataSource(DataSource):
    """Franklin Cloud API data source — fallback for data, required for mode switching."""

    def __init__(self):
        super().__init__()
        self.name = "Cloud API"
        self.client = None
        self.token_fetcher = None
        self.last_error = None
        self.enabled = bool(config.FRANKLIN_USERNAME and
                           config.FRANKLIN_PASSWORD and
                           config.FRANKLIN_GATEWAY_ID)

    async def _get_client(self) -> Optional[Client]:
        try:
            if not self.client:
                from cloud_token import make_client
                self.client = make_client()
                self.token_fetcher = getattr(self.client, 'fetcher', None)
            return self.client
        except Exception as e:
            logger.error(f"Cloud API client creation failed: {e}")
            return None

    async def read_battery_data(self) -> Optional[BatteryData]:
        """Read battery data via Franklin Cloud API (full data set)."""
        start_time = time.time()

        try:
            client = await self._get_client()
            if not client:
                self.stats.record_failure()
                return None

            # Get stats with retry logic
            stats = None
            for attempt in range(config.API_MAX_RETRIES):
                try:
                    stats = await client.get_stats()
                    break
                except Exception as e:
                    if attempt < config.API_MAX_RETRIES - 1:
                        await asyncio.sleep(config.API_RETRY_DELAY)
                        logger.debug(f"Cloud API retry {attempt + 1}/{config.API_MAX_RETRIES}")
                    else:
                        raise e

            if not stats:
                self.stats.record_failure()
                return None

            # Get detailed status for mode info
            status = None
            try:
                status = await client._status()
            except Exception as e:
                logger.debug(f"Could not get detailed status: {e}")

            battery_data = BatteryData(
                soc_percent=stats.current.battery_soc,
                battery_power_kw=stats.current.battery_use,
                grid_power_kw=stats.current.grid_use,
                solar_power_kw=stats.current.solar_production,
                home_load_kw=stats.current.home_load,
                grid_status=stats.current.grid_status.name,
                source="cloud_api",
                timestamp=datetime.now()
            )

            # Populate cumulative totals from cloud API
            battery_data.battery_charge_total_kwh = getattr(stats.totals, 'battery_charge', 0.0)
            battery_data.battery_discharge_total_kwh = getattr(stats.totals, 'battery_discharge', 0.0)
            battery_data.grid_import_total_kwh = getattr(stats.totals, 'grid_import', 0.0)
            battery_data.solar_total_kwh = getattr(stats.totals, 'solar', 0.0)

            if status and isinstance(status, dict):
                battery_data.run_status = status.get("run_status")
                battery_data.mode_name = status.get("name", "Unknown")

                if "fhpSoc" in status:
                    battery_data.per_battery_soc = status["fhpSoc"]
                if "t_amb" in status:
                    battery_data.ambient_temp_c = status["t_amb"]
                if "signal" in status:
                    battery_data.cell_signal = status["signal"]
                if "gridChBat" in status:
                    battery_data.grid_to_battery_kw = status["gridChBat"]
                if "soChBat" in status:
                    battery_data.solar_to_battery_kw = status["soChBat"]

                # Clean up stale charging data during discharge
                if battery_data.battery_power_kw > 0.1:
                    battery_data.grid_to_battery_kw = 0.0
                    battery_data.solar_to_battery_kw = 0.0

            response_time = (time.time() - start_time) * 1000
            self.stats.record_success(response_time)
            channel_stats.record('cloud', 'read', True, response_time)
            try:
                from cloud_token import note_success
                note_success(client)
            except Exception:
                pass

            logger.debug(f"Cloud API read: SOC={battery_data.soc_percent:.1f}%, "
                        f"Time={response_time:.0f}ms")

            return battery_data

        except Exception as e:
            logger.error(f"Cloud API read failed: {e}")
            self.stats.record_failure()
            channel_stats.record('cloud', 'read', False, (time.time() - start_time) * 1000, str(e))
            # Reset client on failure to force re-auth
            self.client = None
            self.token_fetcher = None
            return None

    async def switch_mode(self, mode: str) -> bool:
        """Switch battery mode via Cloud API."""
        try:
            from franklinwh import TokenFetcher, Client, Mode
            # Patch Mode.payload to echo back the user's current Storm Hedge
            # setting rather than the library's hardcoded "1". The current
            # value is read from the cloud before set_mode() and stashed on
            # the Mode object as _stromen_preserve. See richo/franklinwh-python#8.
            _stromen_orig = getattr(Mode.payload, "_stromen_patched", None) or Mode.payload
            def _stromen_fix(self, gateway, _o=_stromen_orig):
                p = _o(self, gateway)
                p["stromEn"] = getattr(self, "_stromen_preserve", "0")
                return p
            _stromen_fix._stromen_patched = _stromen_orig
            Mode.payload = _stromen_fix

            # Client pre-seeded with the saved session token; it logs in
            # again only if the server rejects the token.
            from cloud_token import make_client, note_success
            client = make_client()
            if client is None:
                self.last_error = 'cloud credentials not configured'
                return False

            # Read current Storm Hedge state and per-mode reserves from the
            # cloud so the switch preserves the user's app settings.
            sw_status = None
            try:
                sw_status = await client._switch_status()
                current_stromen = str(sw_status.get("stromEn", 0))
            except Exception as e:
                logger.warning(f'Could not read current stromEn, defaulting to "0": {e}')
                current_stromen = "0"

            if mode in ['emergency_backup', 'backup']:
                target = 'emergency_backup'
            elif mode in ('self_consumption', 'time_of_use'):
                # v4: SC for peak hours; TOU is the resting state (requires the
                # Franklin app TOU tariff configured with "aPower charges from solar")
                target = mode
            else:
                # v3.5 legacy "home" target — use config.HOME_MODE to decide
                target = 'self_consumption' if config.HOME_MODE == 'self_consumption' else 'time_of_use'

            soc, soc_source = preserved_mode_soc(target, sw_status)
            factory = {
                'emergency_backup': Mode.emergency_backup,
                'self_consumption': Mode.self_consumption,
                'time_of_use': Mode.time_of_use,
            }[target]
            mode_obj = factory(soc=soc)

            mode_obj._stromen_preserve = current_stromen
            await client.set_mode(mode_obj)
            note_success(client)
            self.last_error = None
            logger.info(f'Mode switch successful: {mode} (reserve {soc}% from {soc_source}, '
                        f'stromEn preserved as {current_stromen})')
            return True

        except Exception as e:
            self.last_error = f'{type(e).__name__}: {e}'
            logger.error(f'Mode switch error: {e}')
            return False

    async def verify_mode(self) -> Optional[Dict[str, Any]]:
        """Query cloud API just for mode verification (lighter than full read)."""
        try:
            client = await self._get_client()
            if not client:
                return None

            status = await client._status()
            if status and isinstance(status, dict):
                return {
                    "run_status": status.get("run_status"),
                    "mode_name": status.get("name", "Unknown"),
                    "per_battery_soc": status.get("fhpSoc", []),
                }
            return None
        except Exception as e:
            logger.debug(f"Mode verification failed: {e}")
            # Reset client on failure
            self.client = None
            self.token_fetcher = None
            return None


# =============================================================================
# Local connection (v4.7) - primary channel for readings and mode changes
# =============================================================================

class LocalDataSource(DataSource):
    """aGate local connection: live readings and operating-mode changes."""

    def __init__(self):
        super().__init__()
        self.name = "Local"
        self.gw = None
        self.enabled = False
        try:
            import local_gateway
            self.gw = local_gateway.LocalGateway()
            self.enabled = self.gw.enabled
            if not local_gateway.LIB_AVAILABLE:
                logger.info("Local connection package not installed — local channel off")
        except Exception as e:
            logger.warning(f"Local connection unavailable: {e}")

    async def read_flow(self) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        t0 = time.time()
        try:
            flow = await asyncio.to_thread(self.gw.read)
            if flow.get('soc_pct') is None:
                raise RuntimeError('reading had no SOC')
            self.stats.record_success((time.time() - t0) * 1000)
            return flow
        except Exception as e:
            self.stats.record_failure()
            logger.warning(f"Local connection read failed: {e}")
            return None

    async def read_battery_data(self) -> Optional[BatteryData]:
        flow = await self.read_flow()
        return flow_to_battery_data(flow) if flow else None

    async def switch_mode(self, mode: str) -> Dict[str, Any]:
        if not self.enabled:
            return {'ok': False, 'error': 'local connection not enabled'}
        try:
            return await asyncio.to_thread(self.gw.set_mode, mode)
        except Exception as e:
            return {'ok': False, 'error': f'{type(e).__name__}: {e}'}


def flow_to_battery_data(flow: Dict[str, Any]) -> BatteryData:
    """Local reading dict (local_gateway.read) -> BatteryData."""
    def kw(k):
        v = flow.get(k)
        return (v / 1000.0) if v is not None else 0.0
    bd = BatteryData(
        soc_percent=float(flow['soc_pct']),
        battery_power_kw=kw('battery_w'),
        grid_power_kw=kw('grid_w'),
        solar_power_kw=kw('solar_w'),
        home_load_kw=kw('load_w'),
        source="local",
        timestamp=datetime.now(),
    )
    bd.run_status = flow.get('run_status')
    bd.per_battery_soc = list(flow.get('per_battery_soc') or [])
    bd.ambient_temp_c = flow.get('ambient_temp_c')
    bd._ext_solar_kw = kw('solar_w')
    bd._local_mode = flow.get('mode')
    bd._local_flow = flow
    return bd


# Display labels used for mode_name when the reading carries no richer name
_MODE_LABELS = {
    'time_of_use': 'Time of Use',
    'self_consumption': 'Self Consumption',
    'emergency_backup': 'Emergency Backup',
}

CONTROL_ALERT_FILE = 'control_alert.json'


def _control_alert_path() -> Path:
    return Path(str(config.DATA_DIR)) / CONTROL_ALERT_FILE


def write_control_alert(target: str, attempts: list, message: str):
    try:
        p = _control_alert_path()
        prior = {}
        if p.exists():
            try:
                prior = json.loads(p.read_text())
            except Exception:
                prior = {}
        data = {
            'active': True,
            'first_at': prior.get('first_at') or datetime.now().isoformat(timespec='seconds'),
            'last_at': datetime.now().isoformat(timespec='seconds'),
            'failures': int(prior.get('failures', 0)) + 1,
            'target': target,
            'message': message,
            'attempts': attempts[:8],
        }
        p.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.debug(f"Could not write control alert: {e}")


def clear_control_alert():
    try:
        p = _control_alert_path()
        if p.exists():
            p.unlink()
    except Exception:
        pass


def read_control_alert() -> Optional[Dict[str, Any]]:
    try:
        p = _control_alert_path()
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return None


# =============================================================================
# Data Source Manager - orchestrates everything
# =============================================================================

class DataSourceManager:
    """Manages all data sources with automatic fallback and enrichment.

    v4.7 channel order:
      Readings      local connection -> Modbus -> cloud
      Mode changes  local connection -> cloud (saved-token reuse)
      Confirmation  Modbus 15507 when configured, else the local reading,
                    else the cloud status
    When both local and Modbus are available every reading is cross-checked
    and the comparison recorded in channel_stats.
    """

    MODE_CONFIRM_TIMEOUT = 20.0

    def __init__(self):
        self.local_source = LocalDataSource()
        self.modbus_source = ModbusDataSource()
        self.solar_source = SolarDataSource()
        self.cloud_source = CloudDataSource()

        self.soc_tracker = SOCTrendTracker(
            battery_capacity_kwh=config.BATTERY_CAPACITY_KWH
        )
        self.mode_tracker = ModeStateTracker()

        if self.local_source.enabled:
            self.preferred_source = "local"
        elif self.modbus_source.enabled:
            self.preferred_source = "modbus"
        else:
            self.preferred_source = "cloud"
        self.last_data = None
        self.last_fallback = None

        # Connection counters for dashboard
        self.connection_stats = {
            "local": self.local_source.stats,
            "modbus": self.modbus_source.stats,
            "cloud": self.cloud_source.stats,
            "solar": self.solar_source.stats,
        }

    # ------------------------------------------------------------------ reads
    async def read_battery_data(self) -> Optional[BatteryData]:
        """
        Read battery data, local channels first.

        1. Local connection (cross-checked against Modbus when configured)
        2. Modbus TCP
        3. Cloud API
        Solar is enriched from the Enphase collector in every local path.
        """
        battery_data = None

        # === Strategy 1: local connection ===
        if self.local_source.enabled:
            flow = await self.local_source.read_flow()
            if flow:
                battery_data = flow_to_battery_data(flow)
                mb = None
                if self.modbus_source.enabled:
                    mb = await self.modbus_source.read_battery_data()
                    if mb:
                        self._merge_modbus(battery_data, mb)
                        battery_data.source = "local+modbus"
                self._enrich_local(battery_data, from_modbus=mb is not None)
                self.last_data = battery_data
                return battery_data
            logger.warning("Local connection read failed — trying Modbus")

        # === Strategy 2: Modbus ===
        if self.modbus_source.enabled:
            battery_data = await self.modbus_source.read_battery_data()
            if battery_data:
                self._enrich_local(battery_data, from_modbus=True)
                if self.local_source.enabled:
                    self.last_fallback = datetime.now()
                self.last_data = battery_data
                return battery_data

            logger.warning("Modbus read failed, falling back to Cloud API")

        # === Strategy 3: Cloud API ===
        battery_data = await self.cloud_source.read_battery_data()
        if battery_data:
            self.soc_tracker.record(battery_data.soc_percent)
            if battery_data.mode_name and battery_data.mode_name != "Unknown":
                mode = self._detect_mode_from_name(battery_data.mode_name)
                self.mode_tracker.record_verification(mode, battery_data.mode_name)
            self.last_data = battery_data
            self.last_fallback = datetime.now()
            return battery_data

        logger.error("All data sources failed")
        return None

    def _merge_modbus(self, bd: BatteryData, mb: BatteryData):
        """Fill fields only Modbus has, and record the local/Modbus comparison."""
        bd.grid_status = mb.grid_status
        bd.grid_voltage_v = mb.grid_voltage_v
        bd.grid_frequency_hz = mb.grid_frequency_hz
        if mb.cabinet_temp_c is not None:
            bd.cabinet_temp_c = mb.cabinet_temp_c
        if bd.ambient_temp_c is None:
            bd.ambient_temp_c = mb.ambient_temp_c
        for attr in ('_ext_mode', '_ext_mode_name', '_ext_tou_dispatch',
                     '_ext_tou_dispatch_text', '_ext_self_reserve', '_ext_tou_reserve'):
            if hasattr(mb, attr):
                setattr(bd, attr, getattr(mb, attr))
        local_mode = getattr(bd, '_local_mode', None)
        mb_mode = getattr(mb, '_ext_mode', None)
        channel_stats.record_crosscheck(
            bd.soc_percent, mb.soc_percent,
            bd.grid_power_kw * 1000.0, mb.grid_power_kw * 1000.0,
            local_mode, mb_mode)
        if local_mode and mb_mode and local_mode != mb_mode:
            logger.warning(f"Mode disagreement: local={local_mode} modbus={mb_mode}")

    def _enrich_local(self, battery_data: BatteryData, from_modbus: bool):
        """Solar enrichment, SOC trend, charge-source split, mode tracking."""
        solar_kw = self.solar_source.read_solar_production()
        if solar_kw is not None:
            battery_data.solar_power_kw = solar_kw
            if battery_data.source in ("modbus",):
                battery_data.source = "modbus+enphase"
            elif battery_data.source.startswith("local"):
                battery_data.source += "+enphase"
        elif getattr(battery_data, '_ext_solar_kw', 0) and battery_data._ext_solar_kw > 0:
            battery_data.solar_power_kw = battery_data._ext_solar_kw
            if battery_data.source == "modbus":
                battery_data.source = "modbus+ext"
        else:
            logger.debug("No solar data available (may be nighttime)")

        self.soc_tracker.record(battery_data.soc_percent)

        # battery_power_kw > 0 means discharging, < 0 means charging
        if battery_data.battery_power_kw < -0.05:
            charge_rate = abs(battery_data.battery_power_kw)
            if battery_data.solar_power_kw > 0.1:
                battery_data.solar_to_battery_kw = min(charge_rate, battery_data.solar_power_kw)
                battery_data.grid_to_battery_kw = max(0, charge_rate - battery_data.solar_to_battery_kw)
            else:
                battery_data.grid_to_battery_kw = charge_rate
                battery_data.solar_to_battery_kw = 0.0
        elif battery_data.battery_power_kw > 0.05:
            battery_data.solar_to_battery_kw = 0.0
            battery_data.grid_to_battery_kw = 0.0
        else:
            solar_to_bat = self.soc_tracker.estimate_solar_to_battery(
                battery_data.solar_power_kw,
                battery_data.grid_power_kw
            )
            battery_data.solar_to_battery_kw = solar_to_bat
            charging_rate = self.soc_tracker.get_charging_rate_kw()
            if charging_rate and charging_rate > 0 and battery_data.grid_power_kw > 0.1:
                battery_data.grid_to_battery_kw = max(0, charging_rate - solar_to_bat)

        # Mode: Modbus 15507 (with TOU dispatch detail) when present, else the
        # local reading, else the tracker with periodic cloud verification.
        ext_mode = getattr(battery_data, '_ext_mode', None)
        ext_mode_name = getattr(battery_data, '_ext_mode_name', None)
        local_mode = getattr(battery_data, '_local_mode', None)
        if ext_mode and ext_mode_name:
            battery_data.mode_name = ext_mode_name
            self.mode_tracker.record_verification(ext_mode, ext_mode_name)
        elif local_mode:
            battery_data.mode_name = _MODE_LABELS.get(local_mode, local_mode)
            self.mode_tracker.record_verification(local_mode, battery_data.mode_name)
        else:
            battery_data.mode_name = self.mode_tracker.get_mode_name()
            # (cloud verification only when no local channel can see the mode)

    async def _verify_mode_from_cloud(self, battery_data: BatteryData):
        """Periodically verify mode and get per-battery SOC from cloud."""
        try:
            verification = await self.cloud_source.verify_mode()
            if verification:
                mode_name = verification.get("mode_name", "Unknown")
                mode = self._detect_mode_from_name(mode_name)
                self.mode_tracker.record_verification(mode, mode_name)
                battery_data.mode_name = mode_name
                per_bat_soc = verification.get("per_battery_soc", [])
                if per_bat_soc:
                    battery_data.per_battery_soc = per_bat_soc
                logger.info(f"Cloud verification: mode={mode_name}, "
                          f"per_bat_soc={per_bat_soc}")
        except Exception as e:
            logger.debug(f"Cloud verification failed (non-critical): {e}")

    def _detect_mode_from_name(self, mode_name: str) -> str:
        """Convert mode name string to internal mode identifier."""
        if not mode_name:
            return "unknown"
        name_lower = mode_name.lower()
        if "emergency" in name_lower or "backup" in name_lower:
            return "emergency_backup"
        if "self" in name_lower and "consumption" in name_lower:
            return "self_consumption"
        if "tou" in name_lower or "time" in name_lower:
            return "time_of_use"
        return config.HOME_MODE

    # ------------------------------------------------------------ switching
    @staticmethod
    def _canonical_target(mode: str) -> str:
        if mode in ('emergency_backup', 'backup'):
            return 'emergency_backup'
        if mode in ('self_consumption', 'time_of_use'):
            return mode
        # v3.5 legacy "home" target
        return 'self_consumption' if config.HOME_MODE == 'self_consumption' else 'time_of_use'

    async def _observe_mode(self, prefer: str = 'modbus') -> tuple:
        """Read the current hardware mode. Returns (mode|None, via)."""
        if prefer == 'modbus' and self.modbus_source.enabled:
            mb = await self.modbus_source.read_battery_data()
            m = getattr(mb, '_ext_mode', None) if mb else None
            if m:
                return m, 'modbus'
        if self.local_source.enabled:
            flow = await self.local_source.read_flow()
            if flow and flow.get('mode'):
                return flow['mode'], 'local'
        if prefer != 'modbus' and self.modbus_source.enabled:
            mb = await self.modbus_source.read_battery_data()
            m = getattr(mb, '_ext_mode', None) if mb else None
            if m:
                return m, 'modbus'
        return None, None

    async def _confirm(self, target: str, timeout: float) -> tuple:
        """Poll the hardware mode until it matches target.

        Returns (True|False|None, via, observed, ms). None means no local
        channel could read the mode at all (caller may fall back to cloud).
        """
        t0 = time.time()
        observed, via = None, None
        delay = 2.0
        while True:
            observed, via = await self._observe_mode('modbus')
            if observed == target:
                ms = (time.time() - t0) * 1000
                channel_stats.record(via, 'confirm', True, ms)
                return True, via, observed, ms
            if time.time() - t0 + delay > timeout:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 5.0)
        ms = (time.time() - t0) * 1000
        if observed is None:
            return None, None, None, ms
        channel_stats.record(via, 'confirm', False, None, f'not confirmed: saw {observed}')
        return False, via, observed, ms

    async def switch_mode(self, mode: str) -> bool:
        """
        Switch operating mode and confirm it on the hardware.

        Local connection first (twice), then the cloud API (three attempts).
        Returns True only when the hardware confirms the requested mode. On
        total failure an alert is written for the dashboard and the next
        engine cycle tries again.
        """
        target = self._canonical_target(mode)
        t_start = time.time()
        attempts = []

        # --- already there? (cheap, avoids a needless write) ---
        current, via = await self._observe_mode('modbus')
        if current == target:
            logger.info(f"Mode already {target} (per {via}) — no switch needed")
            self.mode_tracker.record_verification(target, _MODE_LABELS.get(target, target))
            clear_control_alert()
            return True

        # --- 1. local connection ---
        if self.local_source.enabled:
            for i in range(2):
                res = await self.local_source.switch_mode(target)
                if not res.get('ok'):
                    attempts.append({'channel': 'local', 'ok': False,
                                     'error': (res.get('error') or '')[:160]})
                    logger.warning(f"Local mode change to {target} failed "
                                   f"(attempt {i + 1}/2): {res.get('error')}")
                    await asyncio.sleep(3)
                    continue
                # Independent confirmation through Modbus 15507 when configured
                ok, cvia, observed, cms = (True, 'local', target, res.get('confirm_ms'))
                if self.modbus_source.enabled:
                    mok, mvia, mobs, mms = await self._confirm(target, self.MODE_CONFIRM_TIMEOUT)
                    if mok is False and mvia == 'modbus':
                        ok, cvia, observed = False, 'modbus', mobs
                        logger.warning(f"Local mode change acknowledged but Modbus shows {mobs}")
                    elif mok:
                        cvia, cms = mvia, (res.get('confirm_ms') or 0) + mms
                if ok:
                    attempts.append({'channel': 'local', 'ok': True})
                    self.mode_tracker.record_verification(target, _MODE_LABELS.get(target, target))
                    channel_stats.record_switch(target, 'local', True, attempts, cvia, cms,
                                                (time.time() - t_start) * 1000)
                    clear_control_alert()
                    logger.info(f"Mode switch VERIFIED: {target} via local connection "
                                f"(confirmed by {cvia}, {res.get('before')} -> {target})")
                    return True
                attempts.append({'channel': 'local', 'ok': False,
                                 'error': f'not confirmed: {cvia} shows {observed}'})

        # --- 2. cloud API ---
        if self.cloud_source.enabled:
            MAX_RETRIES = 3
            VERIFY_DELAYS = [5, 8, 12]
            for attempt in range(MAX_RETRIES):
                t0 = time.time()
                success = await self.cloud_source.switch_mode(target)
                channel_stats.record('cloud', 'switch', success, (time.time() - t0) * 1000,
                                     None if success else (self.cloud_source.last_error or 'switch failed'))
                if not success:
                    attempts.append({'channel': 'cloud', 'ok': False,
                                     'error': (self.cloud_source.last_error or 'switch failed')[:160]})
                    logger.warning(f"Cloud mode switch failed (attempt {attempt + 1}/{MAX_RETRIES})")
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(2)
                    continue

                delay = VERIFY_DELAYS[attempt] if attempt < len(VERIFY_DELAYS) else 12
                logger.info(f"Cloud mode switch sent, verifying (attempt {attempt + 1})...")
                ok, cvia, observed, cms = await self._confirm(target, delay + 10)
                if ok is None:
                    # No local channel can see the mode — ask the cloud.
                    await asyncio.sleep(delay)
                    verification = await self.cloud_source.verify_mode()
                    hw_name = verification.get("mode_name", "Unknown") if verification else None
                    observed = self._detect_mode_from_name(hw_name) if hw_name else None
                    ok, cvia = (observed == target) if observed else False, 'cloud'
                if ok:
                    attempts.append({'channel': 'cloud', 'ok': True})
                    self.mode_tracker.record_verification(target, _MODE_LABELS.get(target, target))
                    channel_stats.record_switch(target, 'cloud', True, attempts, cvia, cms,
                                                (time.time() - t_start) * 1000)
                    clear_control_alert()
                    logger.info(f"Mode switch VERIFIED: {target} via cloud (confirmed by {cvia})")
                    return True
                attempts.append({'channel': 'cloud', 'ok': False,
                                 'error': f'not confirmed: {cvia} shows {observed}'})
                logger.warning(f"Cloud mode switch NOT confirmed (attempt {attempt + 1}): "
                               f"commanded={target}, hardware={observed}")

        channel_stats.record_switch(target, 'none', False, attempts, None, None,
                                    (time.time() - t_start) * 1000)
        msg = (f"Could not switch the battery to {_MODE_LABELS.get(target, target)} — "
               f"every connection failed. The engine will retry next cycle; "
               f"you can change the mode in the Franklin app meanwhile.")
        write_control_alert(target, attempts, msg)
        logger.error(f"MODE SWITCH FAILED: commanded={target}; attempts={attempts}")
        return False

    def get_current_mode(self) -> str:
        """Get current mode from local tracker."""
        return self.mode_tracker.get_mode()

    def get_health_status(self) -> Dict[str, Any]:
        """Get health status of all data sources."""
        now = datetime.now()

        status = {
            "timestamp": now.isoformat(),
            "preferred_source": self.preferred_source,
            "last_fallback": self.last_fallback.isoformat() if self.last_fallback else None,
            "mode": {
                "current": self.mode_tracker.get_mode(),
                "name": self.mode_tracker.get_mode_name(),
                "last_verified": self.mode_tracker.last_verified.isoformat() if self.mode_tracker.last_verified else None,
                "needs_verification": self.mode_tracker.needs_verification(),
            },
            "soc_trend": self.soc_tracker.get_trend_summary(),
            "sources": {}
        }

        for name, source_stats in [
            ("local", self.local_source.stats),
            ("modbus", self.modbus_source.stats),
            ("cloud", self.cloud_source.stats),
            ("solar", self.solar_source.stats),
        ]:
            status["sources"][name] = {
                "enabled": True,  # Will be refined below
                "healthy": source_stats.consecutive_failures < 5,
                "total_attempts": source_stats.total_attempts,
                "success_rate": source_stats.success_rate,
                "avg_response_time_ms": source_stats.avg_response_time_ms,
                "consecutive_failures": source_stats.consecutive_failures,
                "last_success": source_stats.last_success.isoformat() if source_stats.last_success else None,
                "last_failure": source_stats.last_failure.isoformat() if source_stats.last_failure else None,
            }

        # Set enabled flags accurately
        status["sources"]["local"]["enabled"] = self.local_source.enabled
        status["sources"]["modbus"]["enabled"] = self.modbus_source.enabled
        status["sources"]["cloud"]["enabled"] = self.cloud_source.enabled
        status["sources"]["solar"]["enabled"] = self.solar_source.enabled

        status["control_alert"] = read_control_alert()
        try:
            status["channels"] = channel_stats.channel_status()
        except Exception:
            pass
        return status

    def save_health_stats(self):
        """Save health statistics to file for dashboard."""
        try:
            stats_file = config.LOG_DIR / "data_source_health.json"
            with open(stats_file, 'w') as f:
                json.dump(self.get_health_status(), f, indent=2)
        except Exception as e:
            logger.debug(f"Could not save health stats: {e}")


# =============================================================================
# Global instances and convenience functions
# =============================================================================

data_manager = DataSourceManager()


async def get_battery_data() -> Optional[BatteryData]:
    """Convenience function to get battery data."""
    return await data_manager.read_battery_data()


async def switch_battery_mode(mode: str) -> bool:
    """Convenience function to switch battery mode."""
    return await data_manager.switch_mode(mode)


# =============================================================================
# Self-test when run directly
# =============================================================================

if __name__ == "__main__":
    """Test the data source manager."""
    configure_logging()
    import sys

    async def test():
        print("=" * 60)
        print("FranklinWH Data Source Manager v3.5.1 - Self Test")
        print("=" * 60)
        print(f"Modbus enabled: {config.MODBUS_ENABLED} (available: {MODBUS_AVAILABLE})")
        print(f"Cloud API enabled: {data_manager.cloud_source.enabled}")
        print(f"Solar file: {data_manager.solar_source.solar_file}")
        print(f"Solar file exists: {data_manager.solar_source.solar_file.exists()}")
        print(f"Mode tracker: {data_manager.mode_tracker.get_mode()} ({data_manager.mode_tracker.get_mode_name()})")
        print()

        # Test solar data read
        print("--- Solar Data (Enphase JSON) ---")
        solar_kw = data_manager.solar_source.read_solar_production()
        if solar_kw is not None:
            print(f"  Solar production: {solar_kw:.3f} kW")
        else:
            print("  Solar data: not available")
        print()

        # Test full data read
        print("--- Battery Data (combined sources) ---")
        data = await get_battery_data()

        if data:
            print(f"  SOC: {data.soc_percent:.1f}%")
            print(f"  Grid: {data.grid_power_kw:.3f} kW")
            print(f"  Solar: {data.solar_power_kw:.3f} kW")
            print(f"  Solar→Bat: {data.solar_to_battery_kw:.3f} kW")
            print(f"  Grid→Bat: {data.grid_to_battery_kw:.3f} kW")
            print(f"  Mode: {data.mode_name}")
            print(f"  Source: {data.source}")
            if data.ambient_temp_c is not None:
                temp_f = data.ambient_temp_c * 9 / 5 + 32
                print(f"  Temp: {data.ambient_temp_c:.1f}°C ({temp_f:.0f}°F)")
            if data.per_battery_soc:
                print(f"  Per-battery: {data.per_battery_soc}")
        else:
            print("  Failed to read data from any source")
        print()

        # Show SOC trend
        print("--- SOC Trend ---")
        trend = data_manager.soc_tracker.get_trend_summary()
        print(f"  {trend}")
        print()

        # Show health status
        print("--- Health Status ---")
        health = data_manager.get_health_status()
        for source, stats in health["sources"].items():
            enabled = "ON" if stats["enabled"] else "OFF"
            if stats["total_attempts"] > 0:
                print(f"  {source.title()} [{enabled}]: "
                      f"{stats['success_rate']:.1f}% success, "
                      f"{stats['avg_response_time_ms']:.0f}ms avg")
            else:
                print(f"  {source.title()} [{enabled}]: no reads yet")

        print()
        print("Self-test complete.")

    asyncio.run(test())
