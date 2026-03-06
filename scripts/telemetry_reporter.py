#!/usr/bin/env python3
"""
Telemetry Reporter — FranklinWH Automation v4.0

Anonymous, opt-in telemetry that sends aggregate usage data to a
hosted collection endpoint. No personally identifiable information
is ever collected.

Collection endpoint: https://mtnears.com/telemetry/submit
Submission: HTTP POST with JSON payload, API key in header.
API key and endpoint URL are shipped with the Docker image —
users just click "Enable" on the dashboard. No configuration needed.

What IS collected:
  - System size (battery kWh, panel count, engine version)
  - Config flags (modbus, adaptive engine, forecast, CARE rate, etc.)
  - Aggregate performance (peak protection %, self-consumption %, error rates)
  - Region (state-level only, user-provided via .env)

What is NOT collected:
  - IP addresses, gateway IDs, serial numbers, MAC addresses
  - API keys, tokens, credentials
  - Exact location (no city/zip — state only, from .env TELEMETRY_REGION)
  - Specific rate dollar amounts
  - Hourly usage patterns or time-series data
  - Any raw data from the Franklin cloud API

Consent:
  1. .env TELEMETRY_ENABLED=true|false — always wins if set
  2. /data/telemetry_consent.json — set by dashboard modal
  3. Default: telemetry OFF
"""

import json
import logging
import os
import platform
import sys
import uuid
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional

# Add script directory to path
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

from config import configure_logging

logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────
# /data/ is on the persistent Docker volume (survives rebuilds)
# /app/logs/ has the intelligence log CSV
DATA_DIR = Path(os.getenv('DATA_DIR', '/app/data'))
LOG_DIR = Path(os.getenv('LOG_DIR', '/app/logs'))
CONSENT_FILE = DATA_DIR / 'telemetry_consent.json'

# ── Telemetry Endpoint ─────────────────────────────────────────────
# HTTP endpoint hosted at mtnears.com — receives telemetry POSTs.
# API key is shipped with the image — users don't need to configure anything.
# Advanced users can override both via .env for forked setups.
TELEMETRY_ENDPOINT = os.getenv('TELEMETRY_ENDPOINT', 'https://mtnears.com/telemetry/submit.php')
TELEMETRY_API_KEY = os.getenv('TELEMETRY_API_KEY', 'fwh_telem_87bba18ff738325790b60cffd9487013f2c30065')

# ── Schema ─────────────────────────────────────────────────────────
SCHEMA_VERSION = 1


# ═══════════════════════════════════════════════════════════════════
#  CONSENT MANAGEMENT
# ═══════════════════════════════════════════════════════════════════

def get_consent_status() -> Dict[str, Any]:
    """Return the current consent status.

    Returns dict with:
      status: 'unknown' | 'opted_in' | 'opted_out' | 'env_override'
      consented: bool | None
      source: 'env' | 'dashboard' | None
      install_uuid: str | None
    """
    # 1. .env override always wins
    env_val = os.getenv('TELEMETRY_ENABLED', '').strip().lower()
    if env_val in ('true', '1', 'yes'):
        return {
            'status': 'env_override',
            'consented': True,
            'source': 'env',
            'install_uuid': _get_or_create_uuid(),
        }
    if env_val in ('false', '0', 'no'):
        return {
            'status': 'env_override',
            'consented': False,
            'source': 'env',
            'install_uuid': _get_or_create_uuid() if CONSENT_FILE.exists() else None,
        }

    # 2. Consent file
    if CONSENT_FILE.exists():
        try:
            with open(CONSENT_FILE, 'r') as f:
                data = json.load(f)
            consented = data.get('consented', False)
            return {
                'status': 'opted_in' if consented else 'opted_out',
                'consented': consented,
                'source': data.get('source', 'dashboard'),
                'install_uuid': data.get('install_uuid'),
            }
        except (json.JSONDecodeError, OSError):
            pass

    # 3. No file, no env → unknown (show modal)
    return {
        'status': 'unknown',
        'consented': None,
        'source': None,
        'install_uuid': None,
    }


def set_consent(consented: bool, source: str = 'dashboard', region: str = None) -> Dict[str, Any]:
    """Write consent choice to persistent file.

    Called by the dashboard modal or CLI. Generates a UUID on first call.
    Region is the country code from the dashboard dropdown (e.g., "US", "CA").
    Returns the written consent record.
    """
    install_uuid = _get_or_create_uuid()
    record = {
        'consented': consented,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'source': source,
        'install_uuid': install_uuid,
    }

    # Store region if provided (from dashboard country picker)
    if region:
        record['region'] = region

    # Preserve existing region if not being updated
    if not region and CONSENT_FILE.exists():
        try:
            with open(CONSENT_FILE, 'r') as f:
                existing = json.load(f)
            if existing.get('region'):
                record['region'] = existing['region']
        except (json.JSONDecodeError, OSError):
            pass

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONSENT_FILE, 'w') as f:
        json.dump(record, f, indent=2)

    logger.info(f"Telemetry consent set: {'opted in' if consented else 'opted out'} (source={source})")
    return record


def is_telemetry_enabled() -> bool:
    """Quick check: should we send telemetry?"""
    status = get_consent_status()
    return status.get('consented', False) is True


def _get_or_create_uuid() -> str:
    """Get existing install UUID or generate a new one.

    UUID is random — no tie to gateway ID, MAC, or any system identifier.
    Persists in the consent file on the Docker volume.
    """
    if CONSENT_FILE.exists():
        try:
            with open(CONSENT_FILE, 'r') as f:
                data = json.load(f)
            if 'install_uuid' in data and data['install_uuid']:
                return data['install_uuid']
        except (json.JSONDecodeError, OSError):
            pass
    return str(uuid.uuid4())


# ═══════════════════════════════════════════════════════════════════
#  MODBUS DATA ENRICHMENT (optional — only if Modbus is enabled)
# ═══════════════════════════════════════════════════════════════════

def _read_modbus_telemetry() -> Optional[Dict[str, Any]]:
    """Read hardware identity and grid characteristics from Modbus.

    Returns dict with inverter model, firmware, grid frequency/voltage,
    or None if Modbus is not available. These are all static or
    slow-changing values — safe to read once for telemetry.

    IMPORTANT: No serial numbers or gateway IDs are included in
    the returned data. Model 1 serial is read but NOT passed through.
    """
    try:
        from config import config
        if not getattr(config, 'MODBUS_ENABLED', False):
            return None

        modbus_host = getattr(config, 'MODBUS_HOST', '')
        modbus_port = getattr(config, 'MODBUS_PORT', 502)
        if not modbus_host:
            return None

        from pymodbus.client import ModbusTcpClient

        client = ModbusTcpClient(
            host=modbus_host,
            port=int(modbus_port),
            timeout=5.0
        )

        if not client.connect():
            return None

        data = {}

        try:
            # Model 1 (Common) — base address 4, length 66
            # Manufacturer: regs 0-15, Model: regs 16-31, Firmware: regs 40-47
            # Serial: regs 48-63 — READ BUT NOT INCLUDED (PII)
            common = client.read_holding_registers(4, count=48)
            if not common.isError() and len(common.registers) >= 48:
                def _decode_str(regs):
                    raw = b''
                    for r in regs:
                        raw += r.to_bytes(2, 'big')
                    return raw.decode('ascii', errors='replace').rstrip('\x00').strip()

                mfr = _decode_str(common.registers[0:16])
                model = _decode_str(common.registers[16:32])
                firmware = _decode_str(common.registers[40:48])

                if mfr and mfr != '\x00' * len(mfr):
                    data['inverter_manufacturer'] = mfr
                if model and model != '\x00' * len(model):
                    data['inverter_model_name'] = model
                if firmware and firmware != '\x00' * len(firmware):
                    data['firmware_version'] = firmware

            # Model 701 (AC Measurement) — base address 72
            # Register 85: line voltage ÷10, Register 88: frequency ÷1000
            ac = client.read_holding_registers(72, count=20)
            if not ac.isError() and len(ac.registers) >= 17:
                # Offset 13 = register 85: voltage × 10
                voltage_raw = ac.registers[13]
                if 1000 < voltage_raw < 5000:  # Sanity: 100V-500V range
                    data['grid_voltage'] = round(voltage_raw / 10.0, 1)

                # Offset 16 = register 88: frequency × 1000
                freq_raw = ac.registers[16]
                if 45000 < freq_raw < 65000:  # Sanity: 45-65 Hz range
                    data['grid_frequency_hz'] = round(freq_raw / 1000.0, 1)

            # Model 702 (DC/Nameplate) — base address 227
            # Offset 0: rated power in W
            dc = client.read_holding_registers(227, count=10)
            if not dc.isError() and len(dc.registers) >= 6:
                rated_w = dc.registers[0]
                if 1000 < rated_w < 100000:  # Sanity: 1-100 kW
                    data['inverter_rated_kw'] = round(rated_w / 1000.0, 1)

        finally:
            client.close()

        return data if data else None

    except (ImportError, Exception) as e:
        logger.debug(f"Modbus telemetry read skipped: {e}")
        return None


def _infer_grid_region(frequency_hz: float, voltage: float) -> Optional[str]:
    """Infer broad geographic region from grid frequency and voltage.

    Returns a region code like 'north_america', 'europe', 'asia_pacific'.
    This does NOT replace TELEMETRY_REGION (which is state-level, user-set).
    It provides an automatic continent-level fallback.
    """
    if frequency_hz is None:
        return None

    freq = round(frequency_hz)

    if freq == 60:
        # North America, parts of South America, Japan (eastern)
        if voltage and voltage > 200:
            return 'north_america'  # 240V split-phase
        elif voltage and voltage < 150:
            return 'north_america'  # 120V service
        return 'americas_60hz'
    elif freq == 50:
        if voltage and 220 <= voltage <= 240:
            return 'europe_asia_50hz'  # Europe, Australia, most of Asia
        return 'other_50hz'

    return None



def build_payload() -> Dict[str, Any]:
    """Build the telemetry payload from local system state.

    Uses only cached/local data — no Franklin cloud API calls needed.
    Optionally reads Modbus for hardware identity and grid characteristics.
    """
    consent = get_consent_status()
    install_uuid = consent.get('install_uuid', 'unknown')

    # Read Modbus data once (if available) — used by multiple builders
    modbus_data = _read_modbus_telemetry()

    payload = {
        'schema_version': SCHEMA_VERSION,
        'install_uuid': install_uuid,
        'submitted_at': datetime.now(timezone.utc).isoformat(),

        'system': _build_system_info(modbus_data),
        'hardware': _build_hardware_info(modbus_data),
        'config_flags': _build_config_flags(),
        'utility': _build_utility_info(modbus_data),
        'performance_7d': _build_performance_stats(days=7),
        'performance_30d': _build_performance_stats(days=30),
        'meta': _build_meta(install_uuid),
    }

    return payload


def _build_system_info(modbus_data: Optional[Dict] = None) -> Dict[str, Any]:
    """Engine version, python, docker, uptime, firmware."""
    version = 'unknown'
    try:
        from config import config
        version = getattr(config, 'ENGINE_VERSION', None) or 'unknown'
    except ImportError:
        pass

    # If still unknown, try VERSION file
    if version == 'unknown':
        for vf in [SCRIPT_DIR / 'VERSION', SCRIPT_DIR.parent / 'VERSION']:
            if vf.exists():
                version = vf.read_text().strip()
                break

    # If still unknown, read from engine_status.json
    if version == 'unknown':
        try:
            engine_status_path = DATA_DIR / 'engine_status.json'
            if engine_status_path.exists():
                with open(engine_status_path, 'r') as f:
                    es = json.load(f)
                v = es.get('engine_version') or es.get('version')
                if v:
                    version = v
        except Exception:
            pass

    uptime_days = 0
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_secs = float(f.read().split()[0])
            uptime_days = round(uptime_secs / 86400, 1)
    except (OSError, ValueError):
        pass

    # Git commit hash — short hash for version tracking across fleet
    git_commit = None
    try:
        import subprocess
        result = subprocess.run(
            ['git', '-C', str(SCRIPT_DIR.parent), 'rev-parse', '--short', 'HEAD'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            git_commit = result.stdout.strip()
    except Exception:
        pass

    info = {
        'engine_version': version,
        'git_commit': git_commit,
        'python_version': platform.python_version(),
        'docker': os.path.exists('/.dockerenv'),
        'os_arch': platform.machine(),
        'uptime_days': uptime_days,
    }

    # Add firmware version from Modbus if available
    if modbus_data and 'firmware_version' in modbus_data:
        info['firmware_version'] = modbus_data['firmware_version']

    return info


def _build_hardware_info(modbus_data: Optional[Dict] = None) -> Dict[str, Any]:
    """Battery capacity, solar array info, inverter identity from Modbus."""
    info = {
        'battery_capacity_kwh': 0,
        'battery_count': 1,
        'inverter_model': 'franklin_apower',
        'solar_arrays': 0,
        'solar_capacity_kw': 0,
    }

    try:
        from config import config
        info['battery_capacity_kwh'] = getattr(config, 'BATTERY_CAPACITY_KWH', 0)
        info['battery_count'] = getattr(config, 'BATTERY_COUNT', 1)

        # Count solar arrays
        solar_arrays_str = getattr(config, 'SOLAR_ARRAYS', '')
        if solar_arrays_str:
            arrays = [a.strip() for a in solar_arrays_str.split(',') if a.strip()]
            info['solar_arrays'] = len(arrays)

            # Sum capacity across arrays
            total_kw = 0
            for arr_id in arrays:
                cap_key = f'SOLAR_ARRAY_{arr_id.upper()}_CAPACITY_KW'
                cap = float(os.getenv(cap_key, 0))
                total_kw += cap
            if total_kw > 0:
                info['solar_capacity_kw'] = round(total_kw, 2)
    except ImportError:
        pass

    # Enrich from Modbus if available — inverter identity and rated power
    if modbus_data:
        if 'inverter_model_name' in modbus_data:
            info['inverter_model'] = modbus_data['inverter_model_name']
        if 'inverter_rated_kw' in modbus_data:
            info['inverter_rated_kw'] = modbus_data['inverter_rated_kw']
        if 'grid_voltage' in modbus_data:
            info['grid_voltage'] = modbus_data['grid_voltage']
        if 'grid_frequency_hz' in modbus_data:
            info['grid_frequency_hz'] = modbus_data['grid_frequency_hz']

    return info


def _build_config_flags() -> Dict[str, Any]:
    """Which features are enabled."""
    flags = {
        'adaptive_engine': False,
        'modbus_enabled': False,
        'emergency_prep_mode': False,
        'forecast_solar_enabled': False,
        'multi_meter': False,
        'care_rate': False,
        'solar_export': False,
    }

    try:
        from config import config
        flags['adaptive_engine'] = getattr(config, 'ADAPTIVE_ENGINE_ENABLED', False)
        flags['modbus_enabled'] = getattr(config, 'MODBUS_ENABLED', False)
        flags['emergency_prep_mode'] = getattr(config, 'EMERGENCY_PREP_MODE', False)
        flags['forecast_solar_enabled'] = getattr(config, 'FORECAST_ENABLED', False)
        flags['multi_meter'] = getattr(config, 'MULTI_METER', False) or bool(os.getenv('METER2_ACCOUNT', ''))
        flags['care_rate'] = getattr(config, 'CARE_RATE', False) or os.getenv('CARE_RATE', '').lower() in ('true', '1', 'yes')
        flags['solar_export'] = getattr(config, 'SOLAR_EXPORT', False)

        # Enrich forecast flag from engine_status.json runtime state
        if not flags['forecast_solar_enabled']:
            try:
                engine_status_path = DATA_DIR / 'engine_status.json'
                if engine_status_path.exists():
                    with open(engine_status_path, 'r') as f:
                        es = json.load(f)
                    flags['forecast_solar_enabled'] = bool(es.get('forecast_engine', False))
            except Exception:
                pass
    except ImportError:
        pass

    return flags


def _build_utility_info(modbus_data: Optional[Dict] = None) -> Dict[str, Any]:
    """Region, rate structure — no dollar amounts.

    Region priority:
      1. TELEMETRY_REGION env var (power users, state-level: "CA", "IL")
      2. Consent file region field (dashboard country picker: "US", "AU")
      3. Weather station ID prefix (e.g. KCASANTA123 → CA, KILCHIC456 → IL)
      4. TZ env var (e.g. America/Los_Angeles → US, Europe/London → GB)
      5. Grid frequency + voltage from Modbus (auto, continent-level)
      6. null
    """
    # Priority 1: .env override
    user_region = os.getenv('TELEMETRY_REGION', None)

    # Priority 2: consent file (dashboard country picker)
    consent_region = None
    if CONSENT_FILE.exists():
        try:
            with open(CONSENT_FILE, 'r') as f:
                consent_data = json.load(f)
            consent_region = consent_data.get('region')
        except (json.JSONDecodeError, OSError):
            pass

    # Priority 3: derive US state from weather station ID (ICAO format: K + 2-letter state + chars)
    station_region = None
    try:
        from config import config
        station_id = getattr(config, 'WEATHER_STATION_ID', '') or ''
        if station_id and station_id.upper().startswith('K') and len(station_id) >= 4:
            # Personal weather stations: KCASANTA123 → state = CA
            # ASOS/AWOS stations: KSFO → state = SF (not useful), skip 4-char
            if len(station_id) > 4:
                state_code = station_id[1:3].upper()
                us_states = {
                    'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID',
                    'IL','IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS',
                    'MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK',
                    'OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV',
                    'WI','WY','DC'
                }
                if state_code in us_states:
                    station_region = state_code
    except Exception:
        pass

    # Priority 4: derive country/region from TZ env var
    tz_region = None
    if not (user_region or consent_region or station_region):
        try:
            tz = os.getenv('TZ', '')
            if not tz:
                from config import config
                tz = getattr(config, 'TZ', '')
            if tz.startswith('America/'):
                tz_region = 'US'
            elif tz.startswith('Europe/'):
                tz_region = 'EU'
            elif tz.startswith('Australia/'):
                tz_region = 'AU'
            elif tz.startswith('Asia/'):
                tz_region = 'AS'
        except Exception:
            pass

    # Priority 5: auto-detect from Modbus grid characteristics
    grid_region = None
    if modbus_data:
        freq = modbus_data.get('grid_frequency_hz')
        volt = modbus_data.get('grid_voltage')
        if freq:
            grid_region = _infer_grid_region(freq, volt)

    info = {
        'region': user_region or consent_region or station_region or tz_region or 'n/a',
        'region_station': station_region or 'n/a',
        'region_tz': tz_region or 'n/a',
        'grid_region': grid_region or 'n/a',
        'rate_structure_type': 'unknown',
        'peak_window_hours': 0,
        'nem_version': None,
    }

    try:
        from config import config
        if getattr(config, 'DYNAMIC_PRICING_ENABLED', False):
            info['rate_structure_type'] = 'dynamic'
        elif getattr(config, 'TOU_ENABLED', False):
            info['rate_structure_type'] = 'tou'
        else:
            info['rate_structure_type'] = 'flat'

        if config.TOU_ENABLED:
            start = getattr(config, 'PEAK_START_HOUR', 0)
            end = getattr(config, 'PEAK_END_HOUR', 0)
            info['peak_window_hours'] = (end - start) if end > start else (24 - start + end)

        nem = os.getenv('NEM_VERSION', None)
        if nem:
            info['nem_version'] = nem.lower()
    except ImportError:
        pass

    return info


def _build_performance_stats(days: int) -> Dict[str, Any]:
    """Compute aggregate performance from intelligence log and DB.

    Reads the intelligence log CSV for decision/mode stats, and the
    daily_savings SQLite table for peak protection and self-consumption.
    Returns pre-aggregated stats — no raw time-series data.
    """
    stats = {
        'peak_protection_pct': None,
        'solar_self_consumption_pct': None,
        'daily_decisions_avg': None,
        'mode_switches_avg': None,
        'api_error_rate_pct': None,
        'grid_import_peak_kwh_avg': None,
        'curtailment_kwh_avg': None,
        'tou_drift_rate_kw': None,
        'tou_drift_rate_pct_per_hour': None,
        'solar_discharge_session_kwh': None,
        'solar_discharge_activations': None,
        'solar_discharge_kwh_avg': None,
        'solar_discharge_days': None,
    }

    cutoff = datetime.now() - timedelta(days=days)

    # Compute decision/mode stats from intelligence_log DB table
    try:
        import sqlite3 as _sqlite3
        db_path = DATA_DIR / 'franklin.db'
        conn = _sqlite3.connect(str(db_path))
        conn.row_factory = _sqlite3.Row
        cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')

        day_rows = conn.execute(
            "SELECT date(timestamp) as day, COUNT(*) as cnt FROM intelligence_log "
            "WHERE timestamp >= ? AND level != 'DEBUG' GROUP BY day", (cutoff_str,)
        ).fetchall()
        if day_rows:
            stats['daily_decisions_avg'] = round(sum(r['cnt'] for r in day_rows) / len(day_rows), 1)

        mode_rows = conn.execute(
            "SELECT date(timestamp) as day, COUNT(*) as cnt FROM intelligence_log "
            "WHERE timestamp >= ? AND message LIKE 'Action:%' GROUP BY day", (cutoff_str,)
        ).fetchall()
        if mode_rows:
            stats['mode_switches_avg'] = round(sum(r['cnt'] for r in mode_rows) / len(mode_rows), 1)

        total_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM intelligence_log WHERE timestamp >= ?", (cutoff_str,)
        ).fetchone()['cnt']
        error_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM intelligence_log WHERE timestamp >= ? AND level = 'ERROR'", (cutoff_str,)
        ).fetchone()['cnt']
        if total_count > 0:
            stats['api_error_rate_pct'] = round((error_count / total_count) * 100, 2)

        conn.close()
    except Exception as e:
        logger.debug(f"Intelligence log DB stats error: {e}")

    # Try daily_savings DB table for peak protection and solar self-consumption
    try:
        import sqlite3 as _sqlite3
        db_path = DATA_DIR / 'franklin.db'
        conn = _sqlite3.connect(str(db_path))
        conn.row_factory = _sqlite3.Row
        cutoff_date = cutoff.strftime('%Y-%m-%d')

        savings_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM daily_savings WHERE date >= ? ORDER BY date DESC LIMIT ?",
            (cutoff_date, days)
        ).fetchall()]
        conn.close()

        peak_protected = 0
        total_peak_days = 0
        solar_ratios = []

        for row in savings_rows:
            grid_charged = float(row.get('grid_charged_kwh') or 0)
            total_peak_days += 1
            if grid_charged <= 0.5:
                peak_protected += 1

            if stats['grid_import_peak_kwh_avg'] is None:
                stats['grid_import_peak_kwh_avg'] = 0
            stats['grid_import_peak_kwh_avg'] += grid_charged

            solar_ratio = row.get('solar_ratio')
            if solar_ratio is not None and str(solar_ratio) not in ('', 'None'):
                solar_ratios.append(float(solar_ratio))

        if total_peak_days > 0:
            stats['peak_protection_pct'] = round((peak_protected / total_peak_days) * 100, 1)
            if stats['grid_import_peak_kwh_avg'] is not None:
                stats['grid_import_peak_kwh_avg'] = round(stats['grid_import_peak_kwh_avg'] / total_peak_days, 2)

        if solar_ratios:
            stats['solar_self_consumption_pct'] = round(sum(solar_ratios) / len(solar_ratios) * 100, 1)

        # Post-peak solar discharge — column is post_peak_discharge_kwh
        solar_discharge_values = [
            float(row['post_peak_discharge_kwh'])
            for row in savings_rows
            if row.get('post_peak_discharge_kwh') and float(row['post_peak_discharge_kwh']) > 0
        ]
        if solar_discharge_values:
            stats['solar_discharge_kwh_avg'] = round(sum(solar_discharge_values) / len(solar_discharge_values), 2)
            stats['solar_discharge_days'] = len(solar_discharge_values)

    except Exception as e:
        logger.debug(f"Daily savings DB stats error: {e}")

    # TOU drift rate — from engine status if available
    try:
        engine_status_path = DATA_DIR / 'engine_status.json'
        if engine_status_path.exists():
            with open(engine_status_path, 'r') as f:
                es = json.load(f)
            tou_drift = es.get('tou_drift', {})
            if tou_drift.get('sample_count', 0) > 0:
                stats['tou_drift_rate_kw'] = tou_drift.get('drift_rate_kw')
                stats['tou_drift_rate_pct_per_hour'] = tou_drift.get('drift_rate_pct_per_hour')
            solar_discharge = es.get('solar_discharge', {})
            if solar_discharge.get('activations', 0) > 0:
                stats['solar_discharge_session_kwh'] = solar_discharge.get('session_kwh')
                stats['solar_discharge_activations'] = solar_discharge.get('activations')
    except Exception:
        pass

    return stats


def _build_meta(install_uuid: str) -> Dict[str, Any]:
    """First report date, consecutive days, schema history."""
    meta = {
        'first_report': None,
        'consecutive_days': 0,
        'schema_version_history': [SCHEMA_VERSION],
    }

    telemetry_log = DATA_DIR / 'telemetry_log.json'
    if telemetry_log.exists():
        try:
            with open(telemetry_log, 'r') as f:
                tlog = json.load(f)
            meta['first_report'] = tlog.get('first_report')
            meta['consecutive_days'] = tlog.get('consecutive_days', 0)
            history = tlog.get('schema_version_history', [])
            if SCHEMA_VERSION not in history:
                history.append(SCHEMA_VERSION)
            meta['schema_version_history'] = history
        except (json.JSONDecodeError, OSError):
            pass

    return meta


# ═══════════════════════════════════════════════════════════════════
#  TELEMETRY SUBMISSION
# ═══════════════════════════════════════════════════════════════════

def submit_telemetry(payload: dict) -> bool:
    """Submit telemetry payload to the collection endpoint.

    POSTs JSON to the telemetry endpoint with API key auth.
    Returns True on success.
    """
    if not TELEMETRY_API_KEY:
        logger.warning("Telemetry: no TELEMETRY_API_KEY configured — skipping submission")
        return False

    if not TELEMETRY_ENDPOINT:
        logger.warning("Telemetry: no TELEMETRY_ENDPOINT configured — skipping submission")
        return False

    headers = {
        'Content-Type': 'application/json',
        'X-API-Key': TELEMETRY_API_KEY,
        'User-Agent': 'FranklinWH-Telemetry/1.0',
    }

    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(
        TELEMETRY_ENDPOINT,
        data=data,
        headers=headers,
        method='POST'
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_body = resp.read().decode('utf-8')
            status = resp.status
            result = json.loads(resp_body) if resp_body else {}

        if status in (200, 201):
            uuid_short = payload.get('install_uuid', 'unknown').split('-')[0]
            logger.info(f"Telemetry submitted successfully ({uuid_short})")
            return True
        else:
            logger.warning(f"Telemetry submission unexpected status: HTTP {status}")
            return False

    except urllib.error.HTTPError as e:
        resp_body = e.read().decode('utf-8') if e.fp else ''
        try:
            err_detail = json.loads(resp_body).get('error', resp_body)
        except json.JSONDecodeError:
            err_detail = resp_body

        if e.code == 429:
            logger.info(f"Telemetry: rate limited (already submitted recently)")
            return True  # Not a failure — data was already accepted
        else:
            logger.warning(f"Telemetry submission failed: HTTP {e.code} — {err_detail}")
            return False

    except Exception as e:
        logger.warning(f"Telemetry submission error: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════
#  TELEMETRY LOG (local tracking)
# ═══════════════════════════════════════════════════════════════════

def _update_telemetry_log(success: bool):
    """Track submission history locally for the meta block."""
    telemetry_log = DATA_DIR / 'telemetry_log.json'
    tlog = {}

    if telemetry_log.exists():
        try:
            with open(telemetry_log, 'r') as f:
                tlog = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    now = datetime.now(timezone.utc).isoformat()

    if success:
        if not tlog.get('first_report'):
            tlog['first_report'] = now
        tlog['last_success'] = now

        # Track consecutive days
        last_date = tlog.get('last_success_date', '')
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%d')

        if last_date == yesterday:
            tlog['consecutive_days'] = tlog.get('consecutive_days', 0) + 1
        elif last_date != today:
            tlog['consecutive_days'] = 1  # Reset
        # If same day, don't increment

        tlog['last_success_date'] = today
    else:
        tlog['last_failure'] = now

    tlog['last_attempt'] = now

    # Track schema versions
    history = tlog.get('schema_version_history', [])
    if SCHEMA_VERSION not in history:
        history.append(SCHEMA_VERSION)
    tlog['schema_version_history'] = history

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(telemetry_log, 'w') as f:
        json.dump(tlog, f, indent=2)


def _should_retry() -> bool:
    """Check if we should retry a failed submission (once after 1 hour)."""
    telemetry_log = DATA_DIR / 'telemetry_log.json'
    if not telemetry_log.exists():
        return False

    try:
        with open(telemetry_log, 'r') as f:
            tlog = json.load(f)

        last_failure = tlog.get('last_failure')
        last_success = tlog.get('last_success')

        if not last_failure:
            return False

        fail_dt = datetime.fromisoformat(last_failure)
        now = datetime.now(timezone.utc)

        # Only retry if failure was 1-2 hours ago and no success since
        if timedelta(hours=1) <= (now - fail_dt) <= timedelta(hours=2):
            if not last_success or datetime.fromisoformat(last_success) < fail_dt:
                return True

    except Exception:
        pass

    return False


# ═══════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

def run_telemetry():
    """Main function — called by scheduler.py daily.

    Checks consent, builds payload, submits to GitHub.
    Failure is always silent — never impacts automation.
    """
    try:
        if not is_telemetry_enabled():
            logger.debug("Telemetry: not enabled, skipping")
            return

        logger.info("Telemetry: building payload...")
        payload = build_payload()

        logger.info("Telemetry: submitting...")
        success = submit_telemetry(payload)

        _update_telemetry_log(success)

        if success:
            logger.info("Telemetry: daily report submitted ✓")
        else:
            logger.warning("Telemetry: submission failed — will retry in 1 hour")

    except Exception as e:
        logger.error(f"Telemetry error (non-fatal): {e}")
        try:
            _update_telemetry_log(False)
        except Exception:
            pass


def run_telemetry_retry():
    """Retry handler — called 1 hour after the daily run.

    Only actually retries if the daily run failed.
    """
    try:
        if not is_telemetry_enabled():
            return
        if not _should_retry():
            return

        logger.info("Telemetry: retrying failed submission...")
        payload = build_payload()
        success = submit_telemetry(payload)
        _update_telemetry_log(success)

        if success:
            logger.info("Telemetry: retry succeeded ✓")
        else:
            logger.info("Telemetry: retry also failed — will try again tomorrow")

    except Exception as e:
        logger.error(f"Telemetry retry error (non-fatal): {e}")


# ═══════════════════════════════════════════════════════════════════
#  CLI / TESTING
# ═══════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    configure_logging()

    print("=" * 60)
    print("FranklinWH Telemetry Reporter — Test Mode")
    print("=" * 60)

    # Show consent status
    consent = get_consent_status()
    print(f"\nConsent status: {consent['status']}")
    print(f"  Consented: {consent['consented']}")
    print(f"  Source: {consent['source']}")
    print(f"  UUID: {consent['install_uuid']}")

    # Build and display payload
    print(f"\n{'─' * 60}")
    print("Sample payload (what would be sent):")
    print(f"{'─' * 60}")
    payload = build_payload()
    print(json.dumps(payload, indent=2))

    # Size check
    payload_bytes = len(json.dumps(payload).encode('utf-8'))
    print(f"\nPayload size: {payload_bytes:,} bytes ({payload_bytes/1024:.1f} KB)")

    # Privacy check
    payload_str = json.dumps(payload).lower()
    sensitive = ['password', 'token', 'key', 'secret', 'email', 'gateway_id', 'serial']
    found = [t for t in sensitive if t in payload_str and t not in ('schema_version_history',)]
    if found:
        print(f"⚠  Potential sensitive terms found: {found}")
    else:
        print("✓  Privacy check passed — no sensitive terms detected")

    # Endpoint check
    if TELEMETRY_API_KEY and TELEMETRY_ENDPOINT:
        print(f"\nEndpoint: {TELEMETRY_ENDPOINT}")
        print(f"API key: configured ({len(TELEMETRY_API_KEY)} chars)")
        print("Run with --submit to actually send")
    else:
        missing = []
        if not TELEMETRY_ENDPOINT:
            missing.append('TELEMETRY_ENDPOINT')
        if not TELEMETRY_API_KEY:
            missing.append('TELEMETRY_API_KEY')
        print(f"\n⚠  Telemetry not fully configured — missing: {', '.join(missing)}")

    if len(sys.argv) > 1 and sys.argv[1] == '--submit':
        if not TELEMETRY_API_KEY or not TELEMETRY_ENDPOINT:
            print("\nERROR: Cannot submit — endpoint or API key not configured")
        else:
            print(f"\nSubmitting to {TELEMETRY_ENDPOINT}...")
            success = submit_telemetry(payload)
            print(f"Result: {'SUCCESS' if success else 'FAILED'}")
