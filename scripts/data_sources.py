#!/usr/bin/env python3
"""
FranklinWH Data Source Manager - v3.5.1

Unified data collection from Modbus TCP, Enphase local solar, and Franklin
Cloud API. Designed to minimize cloud API usage by sourcing data locally
wherever possible.

Data Source Hierarchy:
  SOC + Grid Power  → Modbus TCP (26ms) → Cloud API fallback
  Solar Production  → Enphase local JSON (from collect_enphase.py) → PVOutput → Cloud API
  Mode Switching    → Cloud API only (no local alternative)
  Mode Detection    → Local state tracking → Cloud API verification

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
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from pathlib import Path
import json

# Modbus imports (optional)
try:
    from pymodbus.client import ModbusTcpClient
    MODBUS_AVAILABLE = True
except ImportError:
    MODBUS_AVAILABLE = False

# Franklin Cloud API
from franklinwh import Client, TokenFetcher

# Local config
from config import config

# Setup logging
logger = logging.getLogger(__name__)


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

    def __init__(self, state_file: Path = None, verify_interval_minutes: int = 60):
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
    """Modbus TCP data source using SunSpec DER models."""

    MODELS = {
        "Model 701": {"addr": 72, "length": 153},   # AC Measurement
        "Model 713": {"addr": 1035, "length": 7},    # DER Status
        "Model 702": {"addr": 227, "length": 50},    # DC Measurement
    }

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
        """Read battery data via Modbus TCP. Returns SOC + grid power."""
        start_time = time.time()

        try:
            if not await self._connect():
                self.stats.record_failure()
                return None

            # Read key SunSpec models
            model_713 = self._read_registers(1035, 7)   # DER Status (SOC)
            model_701 = self._read_registers(72, 50)     # AC Measurement

            if not model_713:
                logger.warning("Failed to read Model 713 (DER Status)")
                self.stats.record_failure()
                return None

            # SOC from Model 713 offset 2 (confirmed: value / 10)
            soc_raw = model_713[2] if len(model_713) > 2 else 0
            soc_percent = soc_raw / 10.0

            # Build battery data with what Modbus provides
            battery_data = BatteryData(
                soc_percent=soc_percent,
                battery_power_kw=0.0,   # Not reliably available via Modbus yet
                grid_power_kw=0.0,
                solar_power_kw=0.0,     # Will be filled from Enphase
                home_load_kw=0.0,       # Not reliably available via Modbus yet
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

            # Record success
            response_time = (time.time() - start_time) * 1000
            self.stats.record_success(response_time)

            logger.debug(f"Modbus read: SOC={soc_percent:.1f}%, "
                        f"Grid={battery_data.grid_power_kw:.3f}kW, "
                        f"Time={response_time:.0f}ms")

            return battery_data

        except Exception as e:
            logger.error(f"Modbus read failed: {e}")
            self.stats.record_failure()
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
                import os
                array_type = os.getenv(f'SOLAR_ARRAY_{array_id.upper()}_TYPE', '')
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
        self.enabled = bool(config.FRANKLIN_USERNAME and
                           config.FRANKLIN_PASSWORD and
                           config.FRANKLIN_GATEWAY_ID)

    async def _get_client(self) -> Optional[Client]:
        try:
            if not self.client:
                self.token_fetcher = TokenFetcher(
                    config.FRANKLIN_USERNAME,
                    config.FRANKLIN_PASSWORD
                )
                self.client = Client(self.token_fetcher, config.FRANKLIN_GATEWAY_ID)
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

            logger.debug(f"Cloud API read: SOC={battery_data.soc_percent:.1f}%, "
                        f"Time={response_time:.0f}ms")

            return battery_data

        except Exception as e:
            logger.error(f"Cloud API read failed: {e}")
            self.stats.record_failure()
            # Reset client on failure to force re-auth
            self.client = None
            self.token_fetcher = None
            return None

    async def switch_mode(self, mode: str) -> bool:
        """Switch battery mode via Cloud API."""
        try:
            from franklinwh import TokenFetcher, Client, Mode

            # Create fresh client for mode switching (ensures fresh auth)
            fetcher = TokenFetcher(config.FRANKLIN_USERNAME, config.FRANKLIN_PASSWORD)
            client = Client(fetcher, config.FRANKLIN_GATEWAY_ID)

            if mode in ['emergency_backup', 'backup']:
                mode_obj = Mode.emergency_backup(soc=config.RESERVE_SOC_BACKUP)
            elif mode == 'self_consumption':
                # v4 engine explicitly requests self_consumption — honor it directly
                mode_obj = Mode.self_consumption(soc=config.RESERVE_SOC_HOME)
            else:
                # v3.5 legacy "home" target — use config.HOME_MODE to decide
                if config.HOME_MODE == 'self_consumption':
                    mode_obj = Mode.self_consumption(soc=config.RESERVE_SOC_HOME)
                else:
                    mode_obj = Mode.time_of_use(soc=config.RESERVE_SOC_HOME)

            await client.set_mode(mode_obj)
            logger.info(f'Mode switch successful: {mode}')
            return True

        except Exception as e:
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
# Data Source Manager - orchestrates everything
# =============================================================================

class DataSourceManager:
    """Manages all data sources with automatic fallback and enrichment."""

    def __init__(self):
        self.modbus_source = ModbusDataSource()
        self.solar_source = SolarDataSource()
        self.cloud_source = CloudDataSource()

        self.soc_tracker = SOCTrendTracker(
            battery_capacity_kwh=config.BATTERY_CAPACITY_KWH
        )
        self.mode_tracker = ModeStateTracker()

        self.preferred_source = "modbus" if config.MODBUS_ENABLED else "cloud"
        self.last_data = None
        self.last_fallback = None

        # Connection counters for dashboard
        self.connection_stats = {
            "modbus": self.modbus_source.stats,
            "cloud": self.cloud_source.stats,
            "solar": self.solar_source.stats,
        }

    async def read_battery_data(self) -> Optional[BatteryData]:
        """
        Read battery data with local-first strategy.

        Priority:
        1. Modbus for SOC + grid power (26ms)
        2. Enphase JSON for solar production (file read, <1ms)
        3. SOC trend for solar_to_battery estimate
        4. Mode from local state tracker
        5. Cloud API only as fallback if Modbus fails
        """
        battery_data = None

        # === Strategy 1: Modbus primary ===
        if self.preferred_source == "modbus" and self.modbus_source.enabled:
            battery_data = await self.modbus_source.read_battery_data()

            if battery_data:
                # Enrich with solar data from Enphase collector
                solar_kw = self.solar_source.read_solar_production()
                if solar_kw is not None:
                    battery_data.solar_power_kw = solar_kw
                    battery_data.source = "modbus+enphase"
                else:
                    # No solar data available — not critical at night
                    logger.debug("No solar data available (may be nighttime)")

                # Track SOC for trend analysis
                self.soc_tracker.record(battery_data.soc_percent)

                # Derive solar-to-battery rate from SOC trend
                solar_to_bat = self.soc_tracker.estimate_solar_to_battery(
                    battery_data.solar_power_kw,
                    battery_data.grid_power_kw
                )
                battery_data.solar_to_battery_kw = solar_to_bat

                # Derive grid-to-battery: if SOC rising and grid importing
                charging_rate = self.soc_tracker.get_charging_rate_kw()
                if charging_rate and charging_rate > 0 and battery_data.grid_power_kw > 0.1:
                    battery_data.grid_to_battery_kw = max(0, charging_rate - solar_to_bat)

                # Apply mode from local tracker
                battery_data.mode_name = self.mode_tracker.get_mode_name()

                # Periodic cloud verification for mode + per-battery SOC
                if self.mode_tracker.needs_verification():
                    await self._verify_mode_from_cloud(battery_data)

                self.last_data = battery_data
                return battery_data

            # Modbus failed — fall back to cloud
            logger.warning("Modbus read failed, falling back to Cloud API")

        # === Strategy 2: Cloud API (fallback or primary if no Modbus) ===
        battery_data = await self.cloud_source.read_battery_data()
        if battery_data:
            # Track SOC even from cloud reads
            self.soc_tracker.record(battery_data.soc_percent)

            # Update mode tracker from cloud data
            if battery_data.mode_name and battery_data.mode_name != "Unknown":
                mode = self._detect_mode_from_name(battery_data.mode_name)
                self.mode_tracker.record_verification(mode, battery_data.mode_name)

            self.last_data = battery_data
            self.last_fallback = datetime.now()
            return battery_data

        # Both sources failed
        logger.error("All data sources failed")
        return None

    async def _verify_mode_from_cloud(self, battery_data: BatteryData):
        """Periodically verify mode and get per-battery SOC from cloud."""
        try:
            verification = await self.cloud_source.verify_mode()
            if verification:
                mode_name = verification.get("mode_name", "Unknown")
                mode = self._detect_mode_from_name(mode_name)
                self.mode_tracker.record_verification(mode, mode_name)
                battery_data.mode_name = mode_name

                # Also grab per-battery SOC while we're here
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
        # Anything else (TOU-B, TOU-Summer, custom schedule, etc.) is home mode
        return config.HOME_MODE

    async def switch_mode(self, mode: str) -> bool:
        """Switch mode via cloud API and record locally."""
        success = await self.cloud_source.switch_mode(mode)
        if success:
            self.mode_tracker.record_switch(mode)
        return success

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
        status["sources"]["modbus"]["enabled"] = self.modbus_source.enabled
        status["sources"]["cloud"]["enabled"] = self.cloud_source.enabled
        status["sources"]["solar"]["enabled"] = self.solar_source.enabled

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
