#!/usr/bin/env python3
"""
db.py — SQLite Central Data Store for FranklinWH Automation

Thin database layer that any collector can import to persist readings.
All writes are fire-and-forget: if the DB fails, the caller keeps running.

Usage:
    from db import store, init_db

    init_db()  # creates tables if needed (safe to call repeatedly)

    store.system_reading(
        soc_pct=85.0, grid_kw=0.12, solar_kw=3.4,
        battery_kw=-2.1, home_load_kw=1.5, mode='self_consumption',
        device_id='agate_main'
    )

Database: /app/data/franklin.db  (WAL mode for concurrent readers)
"""

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default DB path — overridable for testing
_DB_DIR = Path('/app/data')
_DB_PATH = _DB_DIR / 'franklin.db'

# Thread-local connections for safety
_local = threading.local()


# =============================================================================
# Schema — Core Tables
# =============================================================================

SCHEMA_SQL = """
-- System readings: parsed, usable data from any source
-- Covers: collect_modbus, collect_franklin_cloud, smart_decision context
CREATE TABLE IF NOT EXISTS system_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    device_id TEXT NOT NULL DEFAULT 'agate_main',

    -- Core power flow (all sources)
    soc_pct REAL,
    grid_kw REAL,
    solar_kw REAL,
    battery_kw REAL,
    home_load_kw REAL,

    -- Charging breakdown (observer + smart_decision)
    solar_to_battery_kw REAL,
    grid_to_battery_kw REAL,

    -- Per-battery (cloud API — JSON array, e.g. [85.0, 84.2])
    per_battery_soc_json TEXT,
    per_battery_power_json TEXT,

    -- Mode
    mode TEXT,
    mode_detail TEXT,
    run_status INTEGER,

    -- Grid quality (Modbus model 701)
    grid_voltage_v REAL,
    grid_frequency_hz REAL,
    grid_status TEXT,
    grid_connected INTEGER,
    conn_state INTEGER,

    -- Environment (Modbus + cloud API)
    ambient_temp_c REAL,
    cabinet_temp_c REAL,
    batt_dc_voltage_v REAL,
    cell_signal INTEGER,
    wifi_signal INTEGER,

    -- Reserves (Modbus extension)
    self_reserve_pct INTEGER,
    tou_reserve_pct INTEGER,

    -- Cumulative daily energy totals (cloud API)
    kwh_solar REAL,
    kwh_grid_import REAL,
    kwh_grid_export REAL,
    kwh_load REAL,
    kwh_battery_charge REAL,
    kwh_battery_discharge REAL,
    kwh_generator REAL,

    -- Decision engine context (smart_decision / adaptive_engine)
    hours_to_peak REAL,
    engine_priority TEXT,
    curtailed_kwh REAL,
    grid_price_cents REAL,

    -- Data provenance
    source TEXT DEFAULT 'modbus',
    UNIQUE(timestamp, device_id)
);

-- Enphase readings: per-array solar snapshots
CREATE TABLE IF NOT EXISTS enphase_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    array_id TEXT NOT NULL DEFAULT 'house',
    inverter_sum_w REAL,
    meter_w REAL,
    curtailed_w REAL,
    panel_count INTEGER,
    panels_reporting INTEGER,
    panels_json TEXT,
    meter_wh_today REAL,
    meter_wh_lifetime REAL,
    inverter_wh_today REAL,
    inverter_wh_lifetime REAL,
    consumption_w REAL,
    net_consumption_w REAL,
    meter_voltage_v REAL,
    meter_frequency_hz REAL,
    meter_power_factor REAL,
    meter_voltage_l1 REAL,
    meter_voltage_l2 REAL,
    UNIQUE(timestamp, array_id)
);

-- Raw Modbus register dumps: every register, every cycle
CREATE TABLE IF NOT EXISTS modbus_raw_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    device_id TEXT NOT NULL DEFAULT 'agate_main',
    block_name TEXT NOT NULL,
    register_base INTEGER,
    register_count INTEGER,
    values_json TEXT,
    UNIQUE(timestamp, device_id, block_name)
);

-- SolarEdge per-optimizer readings (portal scraper + history imports)
CREATE TABLE IF NOT EXISTS solaredge_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    site_id TEXT NOT NULL DEFAULT '1241660',
    serial_number TEXT NOT NULL,
    inverter TEXT,
    inverter_sn TEXT,
    string TEXT,
    position TEXT,
    today_wh REAL,
    week_wh REAL,
    month_wh REAL,
    lifetime_wh REAL,
    current_power_w REAL,
    health_status TEXT,
    health_ratio_vs_string REAL,
    health_ratio_vs_array REAL,
    UNIQUE(timestamp, site_id, serial_number)
);

-- SolarEdge inverter-level readings (Modbus TCP, when available)
CREATE TABLE IF NOT EXISTS solaredge_inverter_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    inverter_id TEXT NOT NULL,
    site_id TEXT NOT NULL DEFAULT '1241660',

    -- AC output
    ac_power_w REAL,
    ac_energy_wh REAL,
    ac_voltage_v REAL,
    ac_current_a REAL,
    ac_frequency_hz REAL,
    ac_va REAL,
    ac_var REAL,
    ac_pf REAL,

    -- DC input
    dc_power_w REAL,
    dc_voltage_v REAL,
    dc_current_a REAL,

    -- Inverter status
    status TEXT,
    status_vendor TEXT,
    temperature_c REAL,

    -- Meter data (if available via Modbus)
    meter_power_w REAL,
    meter_energy_exported_wh REAL,
    meter_energy_imported_wh REAL,
    meter_voltage_v REAL,
    meter_current_a REAL,
    meter_frequency_hz REAL,

    source TEXT DEFAULT 'modbus_tcp',
    UNIQUE(timestamp, inverter_id, site_id)
);

-- PVOutput daily summaries (site-level)
CREATE TABLE IF NOT EXISTS pvoutput_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    array_id TEXT NOT NULL,
    energy_wh REAL,
    peak_power_w REAL,
    peak_time TEXT,
    efficiency REAL,
    exported_wh REAL,
    used_wh REAL,
    condition TEXT,
    UNIQUE(date, array_id)
);

-- Device inventory: firmware and model tracking
CREATE TABLE IF NOT EXISTS device_inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    system TEXT NOT NULL,
    device_type TEXT,
    serial_number TEXT NOT NULL,
    model TEXT,
    firmware TEXT,
    parent_serial TEXT,
    extra_json TEXT,
    UNIQUE(timestamp, system, serial_number)
);

-- Weather observations: raw 15-min readings from WU
CREATE TABLE IF NOT EXISTS weather_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    station_id TEXT NOT NULL,
    obs_time_local TEXT,
    temp_f REAL,
    heat_index_f REAL,
    dewpoint_f REAL,
    wind_chill_f REAL,
    humidity REAL,
    pressure_inhg REAL,
    wind_speed_mph REAL,
    wind_gust_mph REAL,
    wind_dir_degrees INTEGER,
    precip_rate_in_hr REAL,
    precip_total_in REAL,
    solar_radiation_wm2 REAL,
    uv_index REAL,
    neighborhood TEXT,
    source TEXT DEFAULT 'wu_api',
    UNIQUE(timestamp, station_id)
);

-- Weather daily aggregates
CREATE TABLE IF NOT EXISTS weather_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    station_id TEXT NOT NULL,
    temp_high REAL,
    temp_low REAL,
    temp_avg REAL,
    dewpoint_high REAL,
    dewpoint_low REAL,
    dewpoint_avg REAL,
    heat_index_high REAL,
    windchill_low REAL,
    humidity_high REAL,
    humidity_low REAL,
    humidity_avg REAL,
    pressure_max REAL,
    pressure_min REAL,
    wind_speed_avg REAL,
    wind_speed_high REAL,
    wind_gust_high REAL,
    precip_total REAL,
    solar_radiation_high REAL,
    uv_index_high REAL,
    observation_count INTEGER DEFAULT 0,
    source TEXT DEFAULT 'aggregated',
    UNIQUE(date, station_id)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_system_ts ON system_readings(timestamp);
CREATE INDEX IF NOT EXISTS idx_system_device_ts ON system_readings(device_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_enphase_ts ON enphase_readings(timestamp);
CREATE INDEX IF NOT EXISTS idx_enphase_array_ts ON enphase_readings(array_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_modbus_ts ON modbus_raw_readings(timestamp);
CREATE INDEX IF NOT EXISTS idx_modbus_block_ts ON modbus_raw_readings(device_id, block_name, timestamp);
CREATE INDEX IF NOT EXISTS idx_solaredge_ts ON solaredge_readings(timestamp);
CREATE INDEX IF NOT EXISTS idx_solaredge_serial_ts ON solaredge_readings(serial_number, timestamp);
CREATE INDEX IF NOT EXISTS idx_solaredge_site_ts ON solaredge_readings(site_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_se_inverter_ts ON solaredge_inverter_readings(timestamp);
CREATE INDEX IF NOT EXISTS idx_se_inverter_id_ts ON solaredge_inverter_readings(inverter_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_pvoutput_date ON pvoutput_daily(date);
CREATE INDEX IF NOT EXISTS idx_pvoutput_array ON pvoutput_daily(array_id, date);
CREATE INDEX IF NOT EXISTS idx_device_inv_ts ON device_inventory(timestamp);
CREATE INDEX IF NOT EXISTS idx_device_inv_sys ON device_inventory(system, serial_number);
CREATE INDEX IF NOT EXISTS idx_weather_obs_ts ON weather_observations(timestamp);
CREATE INDEX IF NOT EXISTS idx_weather_obs_station_ts ON weather_observations(station_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_weather_daily_date ON weather_daily(date);
CREATE INDEX IF NOT EXISTS idx_weather_daily_station ON weather_daily(station_id, date);
"""

# =============================================================================
# Schema — Billing Tables
# =============================================================================

BILLING_SCHEMA_SQL = """
-- Billing periods: utility billing cycle summaries
CREATE TABLE IF NOT EXISTS billing_periods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    billing_period INTEGER,
    billing_days INTEGER,
    true_up_year TEXT,
    total_usage_kwh REAL,
    total_generation_kwh REAL,
    total_net_kwh REAL,
    total_charges REAL,
    cumulative_total REAL,
    ytd_nem_charges REAL,
    ytd_minimum_delivery REAL,
    ytd_estimated_true_up REAL,
    notes TEXT,
    source TEXT DEFAULT 'manual',
    UNIQUE(period_start, period_end)
);
CREATE INDEX IF NOT EXISTS idx_billing_start ON billing_periods(period_start);
CREATE INDEX IF NOT EXISTS idx_billing_trueup ON billing_periods(true_up_year);

-- Billing meters: per-meter detail for each billing period
CREATE TABLE IF NOT EXISTS billing_meters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    meter_id TEXT NOT NULL,
    meter_label TEXT,
    usage_kwh REAL,
    generation_kwh REAL,
    net_kwh REAL,
    charges REAL,
    ytd_nem REAL,
    care_active INTEGER DEFAULT 0,
    rate_schedule TEXT,
    notes TEXT,
    UNIQUE(period_start, period_end, meter_id),
    FOREIGN KEY (period_start, period_end)
        REFERENCES billing_periods(period_start, period_end)
);
CREATE INDEX IF NOT EXISTS idx_billing_meter ON billing_meters(meter_id);
CREATE INDEX IF NOT EXISTS idx_billing_meter_period ON billing_meters(period_start, period_end);

-- Daily savings: battery automation savings per day
CREATE TABLE IF NOT EXISTS daily_savings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    solar_ratio REAL,
    total_charged_kwh REAL,
    solar_charged_kwh REAL,
    grid_charged_kwh REAL,
    peak_discharge_kwh REAL,
    post_peak_discharge_kwh REAL,
    peak_savings REAL,
    post_peak_savings REAL,
    total_savings REAL,
    rate_type TEXT,
    peak_rate REAL,
    off_peak_rate REAL,
    solar_discharge_kwh REAL,
    solar_discharge_savings REAL,
    source TEXT DEFAULT 'calculated',
    UNIQUE(date)
);
CREATE INDEX IF NOT EXISTS idx_savings_date ON daily_savings(date);

-- Daily energy summary: nightly rollup of system_readings for fast dashboard/report queries
CREATE TABLE IF NOT EXISTS daily_energy_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    device_id TEXT NOT NULL DEFAULT 'agate_main',
    solar_kwh REAL DEFAULT 0,
    grid_import_kwh REAL DEFAULT 0,
    grid_export_kwh REAL DEFAULT 0,
    battery_charge_kwh REAL DEFAULT 0,
    battery_discharge_kwh REAL DEFAULT 0,
    home_load_kwh REAL DEFAULT 0,
    generator_kwh REAL DEFAULT 0,
    peak_solar_kw REAL DEFAULT 0,
    peak_load_kw REAL DEFAULT 0,
    peak_grid_kw REAL DEFAULT 0,
    peak_battery_kw REAL DEFAULT 0,
    soc_min REAL,
    soc_max REAL,
    soc_avg REAL,
    soc_end REAL,
    reading_count INTEGER DEFAULT 0,
    source TEXT DEFAULT 'rollup',
    UNIQUE(date, device_id)
);
CREATE INDEX IF NOT EXISTS idx_daily_energy_date ON daily_energy_summary(date);

-- Scheduler log: structured log entries from scheduler.py
CREATE TABLE IF NOT EXISTS scheduler_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scheduler_log_ts ON scheduler_log(timestamp);

-- Intelligence log: structured entries from smart_decision.py / adaptive_engine
CREATE TABLE IF NOT EXISTS intelligence_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    level TEXT,
    logger TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_intelligence_log_ts ON intelligence_log(timestamp);

-- Rate history: rate schedule changes over time
CREATE TABLE IF NOT EXISTS rate_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_date TEXT NOT NULL,
    rate_name TEXT NOT NULL,
    peak_rate REAL,
    off_peak_rate REAL,
    care_peak_rate REAL,
    care_off_peak_rate REAL,
    retail_peak_rate REAL,
    retail_off_peak_rate REAL,
    min_delivery_per_day REAL,
    care_delivery_discount_pct REAL,
    nbc_rate REAL,
    notes TEXT,
    UNIQUE(effective_date, rate_name)
);
CREATE INDEX IF NOT EXISTS idx_rate_date ON rate_history(effective_date);
"""


# =============================================================================
# Connection Management
# =============================================================================

def set_db_path(path: Path):
    """Override default DB path (call before init_db)."""
    global _DB_PATH, _DB_DIR
    _DB_PATH = Path(path)
    _DB_DIR = _DB_PATH.parent


def _get_connection() -> sqlite3.Connection:
    """Get or create a thread-local connection."""
    if not hasattr(_local, 'conn') or _local.conn is None:
        _DB_DIR.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(_DB_PATH), timeout=10)
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


@contextmanager
def _safe_write():
    """Context manager that commits on success, logs on failure, never crashes caller."""
    conn = _get_connection()
    try:
        yield conn
        conn.commit()
    except Exception as e:
        logger.warning(f"DB write failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass


def init_db():
    """Create tables and indexes if they don't exist. Safe to call repeatedly."""
    try:
        conn = _get_connection()
        conn.executescript(SCHEMA_SQL)
        conn.executescript(BILLING_SCHEMA_SQL)
        conn.commit()
        logger.info(f"Database initialized: {_DB_PATH}")
        # v4.6 / PR #25: ensure new system_readings columns exist before any
        # collector writes. Idempotent — re-runs are no-ops. Keeps the new
        # collector safe even if migrate_v46.py hasn't been run yet.
        try:
            added = _ensure_v46_columns(conn)
            if added:
                logger.info(f"v4.6 column migration: added {', '.join(added)}")
        except Exception as e:
            logger.warning(f"v4.6 column migration failed: {e}")
        # One-time historical data cleanup. Idempotent — re-runs are no-ops
        # once the data is canonical. Wrapped in try/except so a migration
        # hiccup never blocks startup.
        try:
            with _safe_write() as wconn:
                n = _backfill_normalize_modes(wconn)
            if n:
                logger.info(f"Mode normalization migration: {n} rows updated")
        except Exception as e:
            logger.warning(f"Mode normalization migration failed: {e}")
    except Exception as e:
        logger.warning(f"Database init failed: {e}")


def close():
    """Close thread-local connection."""
    if hasattr(_local, 'conn') and _local.conn:
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None


# =============================================================================
# Timestamp Helper
# =============================================================================

def _now_iso() -> str:
    """Current time as ISO string."""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# =============================================================================
# v4.6 Column Migration (PR #25)
# =============================================================================

def _ensure_v46_columns(conn) -> list:
    """Add v4.6 / PR #25 columns to system_readings if missing. Idempotent.

    battery_kw_direct  — battery power read directly from register 1048,
                         parallel-logged alongside the derived battery_kw
                         (load - solar - grid) until validated, then swapped
                         to authoritative in a follow-up release.
    active_reserve_pct — the reserve that applies to the CURRENT mode,
                         replacing the self_reserve_pct/tou_reserve_pct pair.
                         Old columns retained until the post-soak cleanup.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(system_readings)").fetchall()]
    added = []
    if 'battery_kw_direct' not in cols:
        conn.execute("ALTER TABLE system_readings ADD COLUMN battery_kw_direct REAL")
        added.append('battery_kw_direct')
    if 'active_reserve_pct' not in cols:
        conn.execute("ALTER TABLE system_readings ADD COLUMN active_reserve_pct INTEGER")
        added.append('active_reserve_pct')
    if added:
        conn.commit()
    return added


# =============================================================================
# Mode Normalization
# =============================================================================
#
# system_readings.mode is written by multiple collectors:
#   - collect_modbus.py writes canonical engine form: 'self_consumption',
#     'time_of_use', 'emergency_backup', 'manual'
#   - collect_franklin_cloud.py used to pass through the cloud API's raw
#     display string ('Self-Consumption', 'TOU-B', 'Emergency Backup')
#
# When both collectors write to the same row (cloud UPDATEs an existing
# Modbus row, which is the common case on hybrid systems), the mode value
# flipped format every cycle. Analytics tab mode bands rendered as a
# barcode of alternating per-cycle stripes instead of solid per-mode blocks.
#
# normalize_mode() collapses all known variants to the canonical engine form.
# It is called automatically by system_reading() and system_reading_update_cloud()
# so any caller writing to the mode column gets normalization for free.
# Collectors may also call it explicitly (recommended) to self-document intent.

_MODE_NORMALIZE_MAP = {
    'self_consumption':  'self_consumption',
    'Self-Consumption':  'self_consumption',
    'Self Consumption':  'self_consumption',
    'time_of_use':       'time_of_use',
    'TOU':               'time_of_use',
    'TOU-B':             'time_of_use',
    'Time of Use':       'time_of_use',
    'Time-of-Use':       'time_of_use',
    'emergency_backup':  'emergency_backup',
    'Emergency Backup':  'emergency_backup',
    'Emergency-Backup':  'emergency_backup',
    'Backup':            'emergency_backup',
    'manual':            'manual',
    'Manual':            'manual',
}


def normalize_mode(raw):
    """Collapse known mode display strings to canonical engine form.

    Returns None for empty / unrecognized values. Canonical forms pass through
    unchanged. Unknown values fall through to a lower-snake-case best-effort
    so future format drift degrades gracefully instead of barcoding.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raw = str(raw)
    raw = raw.strip()
    if not raw:
        return None
    if raw in _MODE_NORMALIZE_MAP:
        return _MODE_NORMALIZE_MAP[raw]
    # Best-effort fallback: 'Some New Mode' -> 'some_new_mode'
    return raw.lower().replace('-', '_').replace(' ', '_') or None


def _backfill_normalize_modes(conn) -> int:
    """One-time migration that rewrites historical non-canonical mode values
    to canonical form. Idempotent — re-running is a no-op.

    Run automatically on init_db() if any non-canonical rows are present.
    Returns the number of rows updated.
    """
    canonical = ('self_consumption', 'time_of_use', 'emergency_backup',
                 'manual', None)
    placeholders = ','.join(['?'] * (len(canonical) - 1))
    # Find distinct non-canonical mode values to migrate
    rows = conn.execute(
        f"SELECT DISTINCT mode FROM system_readings "
        f"WHERE mode NOT IN ({placeholders}) AND mode IS NOT NULL",
        canonical[:-1]
    ).fetchall()
    if not rows:
        return 0

    total = 0
    for r in rows:
        raw = r[0]
        normalized = normalize_mode(raw)
        if normalized == raw:
            continue
        if normalized is None:
            cur = conn.execute(
                "UPDATE system_readings SET mode = NULL WHERE mode = ?",
                (raw,)
            )
        else:
            cur = conn.execute(
                "UPDATE system_readings SET mode = ? WHERE mode = ?",
                (normalized, raw)
            )
        n = cur.rowcount or 0
        if n:
            logger.info(f"Mode normalization: {raw!r} -> {normalized!r} ({n} rows)")
        total += n
    return total


# =============================================================================
# Store Functions
# =============================================================================

class _Store:
    """Namespace for all store operations."""

    def system_reading(self, *,
                       soc_pct: float = None,
                       grid_kw: float = None,
                       solar_kw: float = None,
                       battery_kw: float = None,
                       battery_kw_direct: float = None,
                       home_load_kw: float = None,
                       solar_to_battery_kw: float = None,
                       grid_to_battery_kw: float = None,
                       per_battery_soc_json: str = None,
                       per_battery_power_json: str = None,
                       mode: str = None,
                       mode_detail: str = None,
                       run_status: int = None,
                       grid_voltage_v: float = None,
                       grid_frequency_hz: float = None,
                       grid_status: str = None,
                       grid_connected: int = None,
                       conn_state: int = None,
                       ambient_temp_c: float = None,
                       cabinet_temp_c: float = None,
                       batt_dc_voltage_v: float = None,
                       cell_signal: int = None,
                       wifi_signal: int = None,
                       self_reserve_pct: int = None,
                       tou_reserve_pct: int = None,
                       active_reserve_pct: int = None,
                       kwh_solar: float = None,
                       kwh_grid_import: float = None,
                       kwh_grid_export: float = None,
                       kwh_load: float = None,
                       kwh_battery_charge: float = None,
                       kwh_battery_discharge: float = None,
                       kwh_generator: float = None,
                       hours_to_peak: float = None,
                       engine_priority: str = None,
                       curtailed_kwh: float = None,
                       grid_price_cents: float = None,
                       source: str = 'modbus',
                       device_id: str = 'agate_main',
                       timestamp: str = None):
        """Store a parsed system reading."""
        ts = timestamp or _now_iso()
        # Normalize mode at write time — defense in depth. Cleans any collector
        # that forgot to normalize, guarantees the column stays canonical.
        mode = normalize_mode(mode)
        with _safe_write() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO system_readings
                (timestamp, device_id, soc_pct, grid_kw, solar_kw, battery_kw,
                 battery_kw_direct,
                 home_load_kw, solar_to_battery_kw, grid_to_battery_kw,
                 per_battery_soc_json, per_battery_power_json,
                 mode, mode_detail, run_status,
                 grid_voltage_v, grid_frequency_hz, grid_status, grid_connected, conn_state,
                 ambient_temp_c, cabinet_temp_c, batt_dc_voltage_v, cell_signal, wifi_signal,
                 self_reserve_pct, tou_reserve_pct, active_reserve_pct,
                 kwh_solar, kwh_grid_import, kwh_grid_export, kwh_load,
                 kwh_battery_charge, kwh_battery_discharge, kwh_generator,
                 hours_to_peak, engine_priority, curtailed_kwh, grid_price_cents,
                 source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts, device_id, soc_pct, grid_kw, solar_kw, battery_kw,
                  battery_kw_direct,
                  home_load_kw, solar_to_battery_kw, grid_to_battery_kw,
                  per_battery_soc_json, per_battery_power_json,
                  mode, mode_detail, run_status,
                  grid_voltage_v, grid_frequency_hz, grid_status, grid_connected, conn_state,
                  ambient_temp_c, cabinet_temp_c, batt_dc_voltage_v, cell_signal, wifi_signal,
                  self_reserve_pct, tou_reserve_pct, active_reserve_pct,
                  kwh_solar, kwh_grid_import, kwh_grid_export, kwh_load,
                  kwh_battery_charge, kwh_battery_discharge, kwh_generator,
                  hours_to_peak, engine_priority, curtailed_kwh, grid_price_cents,
                  source))

    def system_reading_update_cloud(self, *,
                                     timestamp: str,
                                     device_id: str = 'agate_main',
                                     # Primary fields (Modbus is source of truth when both present;
                                     # cloud fills only NULLs in UPDATE branch, inserts freely
                                     # in cloud-only INSERT branch):
                                     soc_pct: float = None,
                                     solar_kw: float = None,
                                     grid_kw: float = None,
                                     battery_kw: float = None,
                                     home_load_kw: float = None,
                                     grid_voltage_v: float = None,
                                     grid_frequency_hz: float = None,
                                     grid_status: str = None,
                                     grid_connected: int = None,
                                     # Cloud-enrichment fields (always overwritten — Modbus
                                     # doesn't populate these):
                                     per_battery_soc_json: str = None,
                                     per_battery_power_json: str = None,
                                     kwh_solar: float = None,
                                     kwh_grid_import: float = None,
                                     kwh_grid_export: float = None,
                                     kwh_load: float = None,
                                     kwh_battery_charge: float = None,
                                     kwh_battery_discharge: float = None,
                                     kwh_generator: float = None,
                                     cell_signal: int = None,
                                     wifi_signal: int = None,
                                     ambient_temp_c: float = None,
                                     solar_to_battery_kw: float = None,
                                     grid_to_battery_kw: float = None,
                                     run_status: int = None,
                                     mode: str = None,
                                     mode_detail: str = None):
        """Update cloud-only fields on an existing system_readings row.

        Finds the nearest Modbus row within 5 minutes of the given timestamp
        and fills in the NULL cloud fields. If no matching row exists,
        inserts a new row with source='cloud'.

        For PRIMARY fields (soc_pct, solar_kw, grid_kw, battery_kw, home_load_kw,
        grid_voltage_v, grid_frequency_hz, grid_status, grid_connected, mode,
        mode_detail) the UPDATE branch uses COALESCE semantics — only fills the
        field if the existing row has NULL. Modbus is the source of truth for
        these on Modbus-enabled systems. The cloud-only INSERT path populates
        them unconditionally so cloud-only users get a fully-populated row.

        mode is normalized via normalize_mode() before storage regardless of
        path — cloud API returns display strings like 'Self-Consumption' that
        must collapse to canonical 'self_consumption' so historical analytics
        and engine logic compare cleanly.
        """
        # Normalize cloud-API mode strings to canonical engine form.
        # Cloud returns 'Self-Consumption' / 'TOU-B' / 'Emergency Backup';
        # Modbus and engine write 'self_consumption' / 'time_of_use' /
        # 'emergency_backup'. Without normalization, alternating Modbus and
        # cloud writes to the same row produce a barcode mode column.
        mode = normalize_mode(mode)

        # Columns where Modbus wins ties — cloud only fills NULLs in UPDATE branch.
        # mode and mode_detail are Modbus-authoritative: the engine reads register
        # 15507 (instant, local) and writes canonical form. Cloud mode arrives as
        # a display string ~15 min later and would overwrite the live value if
        # treated as enrichment.
        primary_fields = [
            ('soc_pct', soc_pct),
            ('solar_kw', solar_kw),
            ('grid_kw', grid_kw),
            ('battery_kw', battery_kw),
            ('home_load_kw', home_load_kw),
            ('grid_voltage_v', grid_voltage_v),
            ('grid_frequency_hz', grid_frequency_hz),
            ('grid_status', grid_status),
            ('grid_connected', grid_connected),
            ('mode', mode),
            ('mode_detail', mode_detail),
        ]
        # Columns where cloud is authoritative — overwrite freely
        enrichment_fields = [
            ('per_battery_soc_json', per_battery_soc_json),
            ('per_battery_power_json', per_battery_power_json),
            ('kwh_solar', kwh_solar),
            ('kwh_grid_import', kwh_grid_import),
            ('kwh_grid_export', kwh_grid_export),
            ('kwh_load', kwh_load),
            ('kwh_battery_charge', kwh_battery_charge),
            ('kwh_battery_discharge', kwh_battery_discharge),
            ('kwh_generator', kwh_generator),
            ('cell_signal', cell_signal),
            ('wifi_signal', wifi_signal),
            ('ambient_temp_c', ambient_temp_c),
            ('solar_to_battery_kw', solar_to_battery_kw),
            ('grid_to_battery_kw', grid_to_battery_kw),
            ('run_status', run_status),
        ]

        with _safe_write() as conn:
            row = conn.execute("""
                SELECT timestamp FROM system_readings
                WHERE device_id = ? AND source = 'modbus'
                  AND timestamp BETWEEN datetime(?, '-5 minutes') AND datetime(?, '+5 minutes')
                ORDER BY ABS(strftime('%s', timestamp) - strftime('%s', ?))
                LIMIT 1
            """, (device_id, timestamp, timestamp, timestamp)).fetchone()

            if row:
                target_ts = row[0]
                sets = []
                vals = []
                # Primary fields use COALESCE so existing Modbus values aren't clobbered
                for col, val in primary_fields:
                    if val is not None:
                        sets.append(f"{col} = COALESCE({col}, ?)")
                        vals.append(val)
                # Enrichment fields overwrite directly
                for col, val in enrichment_fields:
                    if val is not None:
                        sets.append(f"{col} = ?")
                        vals.append(val)
                if sets:
                    vals.extend([target_ts, device_id])
                    conn.execute(
                        f"UPDATE system_readings SET {', '.join(sets)} "
                        f"WHERE timestamp = ? AND device_id = ?",
                        vals
                    )
            else:
                # Cloud-only INSERT — populate everything we have
                self.system_reading(
                    timestamp=timestamp, device_id=device_id, source='cloud',
                    soc_pct=soc_pct, solar_kw=solar_kw, grid_kw=grid_kw,
                    battery_kw=battery_kw, home_load_kw=home_load_kw,
                    grid_voltage_v=grid_voltage_v,
                    grid_frequency_hz=grid_frequency_hz,
                    grid_status=grid_status, grid_connected=grid_connected,
                    per_battery_soc_json=per_battery_soc_json,
                    per_battery_power_json=per_battery_power_json,
                    kwh_solar=kwh_solar, kwh_grid_import=kwh_grid_import,
                    kwh_grid_export=kwh_grid_export, kwh_load=kwh_load,
                    kwh_battery_charge=kwh_battery_charge,
                    kwh_battery_discharge=kwh_battery_discharge,
                    kwh_generator=kwh_generator, cell_signal=cell_signal,
                    wifi_signal=wifi_signal, ambient_temp_c=ambient_temp_c,
                    solar_to_battery_kw=solar_to_battery_kw,
                    grid_to_battery_kw=grid_to_battery_kw,
                    run_status=run_status, mode=mode, mode_detail=mode_detail,
                )

    def enphase_reading(self, *,
                        array_id: str = 'house',
                        inverter_sum_w: float = None,
                        meter_w: float = None,
                        curtailed_w: float = None,
                        panel_count: int = None,
                        panels_reporting: int = None,
                        panels_json: str = None,
                        meter_wh_today: float = None,
                        meter_wh_lifetime: float = None,
                        inverter_wh_today: float = None,
                        inverter_wh_lifetime: float = None,
                        consumption_w: float = None,
                        net_consumption_w: float = None,
                        meter_voltage_v: float = None,
                        meter_frequency_hz: float = None,
                        meter_power_factor: float = None,
                        meter_voltage_l1: float = None,
                        meter_voltage_l2: float = None,
                        timestamp: str = None):
        """Store an Enphase array reading."""
        ts = timestamp or _now_iso()
        with _safe_write() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO enphase_readings
                (timestamp, array_id, inverter_sum_w, meter_w, curtailed_w,
                 panel_count, panels_reporting, panels_json,
                 meter_wh_today, meter_wh_lifetime,
                 inverter_wh_today, inverter_wh_lifetime,
                 consumption_w, net_consumption_w,
                 meter_voltage_v, meter_frequency_hz, meter_power_factor,
                 meter_voltage_l1, meter_voltage_l2)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts, array_id, inverter_sum_w, meter_w, curtailed_w,
                  panel_count, panels_reporting, panels_json,
                  meter_wh_today, meter_wh_lifetime,
                  inverter_wh_today, inverter_wh_lifetime,
                  consumption_w, net_consumption_w,
                  meter_voltage_v, meter_frequency_hz, meter_power_factor,
                  meter_voltage_l1, meter_voltage_l2))

    def modbus_raw(self, *,
                   block_name: str,
                   register_base: int,
                   register_count: int,
                   values: List[int],
                   device_id: str = 'agate_main',
                   timestamp: str = None):
        """Store a raw Modbus register block."""
        ts = timestamp or _now_iso()
        values_json = json.dumps(values)
        with _safe_write() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO modbus_raw_readings
                (timestamp, device_id, block_name, register_base,
                 register_count, values_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (ts, device_id, block_name, register_base,
                  register_count, values_json))

    def solaredge_reading(self, *,
                          serial_number: str,
                          site_id: str = '1241660',
                          inverter: str = None,
                          inverter_sn: str = None,
                          string: str = None,
                          position: str = None,
                          today_wh: float = None,
                          week_wh: float = None,
                          month_wh: float = None,
                          lifetime_wh: float = None,
                          current_power_w: float = None,
                          health_status: str = None,
                          health_ratio_vs_string: float = None,
                          health_ratio_vs_array: float = None,
                          timestamp: str = None):
        """Store a SolarEdge per-optimizer reading."""
        ts = timestamp or _now_iso()
        with _safe_write() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO solaredge_readings
                (timestamp, site_id, serial_number, inverter, inverter_sn, string, position,
                 today_wh, week_wh, month_wh, lifetime_wh, current_power_w,
                 health_status, health_ratio_vs_string, health_ratio_vs_array)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts, site_id, serial_number, inverter, inverter_sn, string, position,
                  today_wh, week_wh, month_wh, lifetime_wh, current_power_w,
                  health_status, health_ratio_vs_string, health_ratio_vs_array))

    def solaredge_inverter_reading(self, *,
                                    inverter_id: str,
                                    site_id: str = '1241660',
                                    ac_power_w: float = None,
                                    ac_energy_wh: float = None,
                                    ac_voltage_v: float = None,
                                    ac_current_a: float = None,
                                    ac_frequency_hz: float = None,
                                    ac_va: float = None,
                                    ac_var: float = None,
                                    ac_pf: float = None,
                                    dc_power_w: float = None,
                                    dc_voltage_v: float = None,
                                    dc_current_a: float = None,
                                    status: str = None,
                                    status_vendor: str = None,
                                    temperature_c: float = None,
                                    meter_power_w: float = None,
                                    meter_energy_exported_wh: float = None,
                                    meter_energy_imported_wh: float = None,
                                    meter_voltage_v: float = None,
                                    meter_current_a: float = None,
                                    meter_frequency_hz: float = None,
                                    source: str = 'modbus_tcp',
                                    timestamp: str = None):
        """Store a SolarEdge inverter-level reading (from Modbus TCP)."""
        ts = timestamp or _now_iso()
        with _safe_write() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO solaredge_inverter_readings
                (timestamp, inverter_id, site_id,
                 ac_power_w, ac_energy_wh, ac_voltage_v, ac_current_a, ac_frequency_hz,
                 ac_va, ac_var, ac_pf,
                 dc_power_w, dc_voltage_v, dc_current_a,
                 status, status_vendor, temperature_c,
                 meter_power_w, meter_energy_exported_wh, meter_energy_imported_wh,
                 meter_voltage_v, meter_current_a, meter_frequency_hz,
                 source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts, inverter_id, site_id,
                  ac_power_w, ac_energy_wh, ac_voltage_v, ac_current_a, ac_frequency_hz,
                  ac_va, ac_var, ac_pf,
                  dc_power_w, dc_voltage_v, dc_current_a,
                  status, status_vendor, temperature_c,
                  meter_power_w, meter_energy_exported_wh, meter_energy_imported_wh,
                  meter_voltage_v, meter_current_a, meter_frequency_hz,
                  source))

    def pvoutput_daily(self, *,
                       date: str,
                       array_id: str,
                       energy_wh: float = None,
                       peak_power_w: float = None,
                       peak_time: str = None,
                       efficiency: float = None,
                       exported_wh: float = None,
                       used_wh: float = None,
                       condition: str = None):
        """Store a PVOutput daily summary."""
        with _safe_write() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO pvoutput_daily
                (date, array_id, energy_wh, peak_power_w, peak_time,
                 efficiency, exported_wh, used_wh, condition)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (date, array_id, energy_wh, peak_power_w, peak_time,
                  efficiency, exported_wh, used_wh, condition))

    def device_inventory(self, *,
                         system: str,
                         serial_number: str,
                         device_type: str = None,
                         model: str = None,
                         firmware: str = None,
                         parent_serial: str = None,
                         extra_json: str = None,
                         timestamp: str = None):
        """Store a device inventory entry."""
        ts = timestamp or _now_iso()
        with _safe_write() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO device_inventory
                (timestamp, system, device_type, serial_number, model, firmware, parent_serial, extra_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (ts, system, device_type, serial_number, model, firmware, parent_serial, extra_json))

    def daily_savings(self, *,
                      date: str,
                      solar_ratio: float = None,
                      total_charged_kwh: float = None,
                      solar_charged_kwh: float = None,
                      grid_charged_kwh: float = None,
                      peak_discharge_kwh: float = None,
                      post_peak_discharge_kwh: float = None,
                      peak_savings: float = None,
                      post_peak_savings: float = None,
                      total_savings: float = None,
                      rate_type: str = None,
                      peak_rate: float = None,
                      off_peak_rate: float = None,
                      solar_discharge_kwh: float = None,
                      solar_discharge_savings: float = None,
                      source: str = 'calculated'):
        """Store a daily savings calculation."""
        with _safe_write() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO daily_savings
                (date, solar_ratio, total_charged_kwh, solar_charged_kwh, grid_charged_kwh,
                 peak_discharge_kwh, post_peak_discharge_kwh,
                 peak_savings, post_peak_savings, total_savings,
                 rate_type, peak_rate, off_peak_rate,
                 solar_discharge_kwh, solar_discharge_savings, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (date, solar_ratio, total_charged_kwh, solar_charged_kwh, grid_charged_kwh,
                  peak_discharge_kwh, post_peak_discharge_kwh,
                  peak_savings, post_peak_savings, total_savings,
                  rate_type, peak_rate, off_peak_rate,
                  solar_discharge_kwh, solar_discharge_savings, source))


    def scheduler_log(self, *, timestamp: str = None, message: str):
        """Store a scheduler log entry."""
        ts = timestamp or _now_iso()
        with _safe_write() as conn:
            conn.execute(
                "INSERT INTO scheduler_log (timestamp, message) VALUES (?, ?)",
                (ts, message))

    def intelligence_log(self, *, timestamp: str = None, level: str = None,
                         logger: str = None, message: str):
        """Store an intelligence log entry."""
        ts = timestamp or _now_iso()
        with _safe_write() as conn:
            conn.execute(
                "INSERT INTO intelligence_log (timestamp, level, logger, message) VALUES (?, ?, ?, ?)",
                (ts, level, logger, message))


# Module-level store instance
store = _Store()


# =============================================================================
# Query Helpers
# =============================================================================

def query(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Run a read query and return list of dicts. Returns empty list on error."""
    try:
        conn = _get_connection()
        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.warning(f"DB query failed: {e}")
        return []


def get_latest_system(device_id: str = 'agate_main') -> Optional[Dict[str, Any]]:
    """Get the most recent system reading for a device."""
    rows = query(
        "SELECT * FROM system_readings WHERE device_id = ? ORDER BY timestamp DESC LIMIT 1",
        (device_id,)
    )
    return rows[0] if rows else None


def get_latest_device_firmware(system: str, serial_number: str) -> Optional[Dict[str, Any]]:
    """Get the most recent device inventory entry for a specific device."""
    rows = query(
        "SELECT * FROM device_inventory WHERE system = ? AND serial_number = ? "
        "ORDER BY timestamp DESC LIMIT 1",
        (system, serial_number)
    )
    return rows[0] if rows else None


def get_register_at(block_name: str, offset: int, *,
                    device_id: str = 'agate_main',
                    limit: int = 100) -> List[Dict[str, Any]]:
    """Query a specific register offset from raw dumps."""
    rows = query(f"""
        SELECT timestamp, 
               json_extract(values_json, '$[{offset}]') as register_value,
               values_json
        FROM modbus_raw_readings
        WHERE device_id = ? AND block_name = ?
        ORDER BY timestamp DESC LIMIT ?
    """, (device_id, block_name, limit))
    return rows


def db_stats() -> Dict[str, Any]:
    """Return row counts for all tables."""
    stats = {}
    tables = query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    for t in tables:
        name = t['name']
        rows = query(f"SELECT COUNT(*) as cnt FROM [{name}]")
        stats[name] = rows[0]['cnt'] if rows else 0
    stats['db_path'] = str(_DB_PATH)
    stats['db_size_mb'] = round(_DB_PATH.stat().st_size / 1024 / 1024, 2) if _DB_PATH.exists() else 0
    return stats


# =============================================================================
# Query Helpers — Consumer Rewiring
# =============================================================================

def get_readings_for_date(date_str: str, device_id: str = 'agate_main') -> List[Dict[str, Any]]:
    """Get all system_readings for a specific date. Returns list of dicts."""
    return query(
        "SELECT * FROM system_readings WHERE date(timestamp) = ? AND device_id = ? ORDER BY timestamp",
        (date_str, device_id)
    )


def get_readings_range(start_date: str, end_date: str, device_id: str = 'agate_main') -> List[Dict[str, Any]]:
    """Get system_readings between two dates (inclusive). Returns list of dicts."""
    return query(
        "SELECT * FROM system_readings WHERE date(timestamp) >= ? AND date(timestamp) <= ? "
        "AND device_id = ? ORDER BY timestamp",
        (start_date, end_date, device_id)
    )


def get_recent_readings(limit: int = 50, device_id: str = 'agate_main') -> List[Dict[str, Any]]:
    """Get the N most recent system_readings. Returns list of dicts, newest first."""
    return query(
        "SELECT * FROM system_readings WHERE device_id = ? ORDER BY timestamp DESC LIMIT ?",
        (device_id, limit)
    )


def get_readings_since(hours_ago: float, device_id: str = 'agate_main') -> List[Dict[str, Any]]:
    """Get system_readings from the last N hours. Returns list of dicts."""
    cutoff = (datetime.now() - timedelta(hours=hours_ago)).strftime('%Y-%m-%d %H:%M:%S')
    return query(
        "SELECT * FROM system_readings WHERE timestamp >= ? AND device_id = ? ORDER BY timestamp",
        (cutoff, device_id)
    )


def get_daily_savings_range(start_date: str, end_date: str) -> List[Dict[str, Any]]:
    """Get daily_savings between two dates (inclusive)."""
    return query(
        "SELECT * FROM daily_savings WHERE date >= ? AND date <= ? ORDER BY date",
        (start_date, end_date)
    )


def get_daily_savings_recent(limit: int = 90) -> List[Dict[str, Any]]:
    """Get the N most recent daily_savings rows."""
    return query(
        "SELECT * FROM daily_savings ORDER BY date DESC LIMIT ?",
        (limit,)
    )


def get_daily_energy_summary(date_str: str, device_id: str = 'agate_main') -> Optional[Dict[str, Any]]:
    """Get daily_energy_summary for a specific date."""
    rows = query(
        "SELECT * FROM daily_energy_summary WHERE date = ? AND device_id = ?",
        (date_str, device_id)
    )
    return rows[0] if rows else None


def get_daily_energy_range(start_date: str, end_date: str, device_id: str = 'agate_main') -> List[Dict[str, Any]]:
    """Get daily_energy_summary between two dates (inclusive)."""
    return query(
        "SELECT * FROM daily_energy_summary WHERE date >= ? AND date <= ? AND device_id = ? ORDER BY date",
        (start_date, end_date, device_id)
    )


def get_weather_daily_all(station_id: str = None) -> List[Dict[str, Any]]:
    """Get all weather_daily rows, ordered by date. For calibration model building."""
    if station_id:
        return query(
            "SELECT * FROM weather_daily WHERE station_id = ? ORDER BY date",
            (station_id,)
        )
    return query("SELECT * FROM weather_daily ORDER BY date")


def get_weather_daily_range(start_date: str, end_date: str, station_id: str = None) -> List[Dict[str, Any]]:
    """Get weather_daily between two dates (inclusive)."""
    if station_id:
        return query(
            "SELECT * FROM weather_daily WHERE date >= ? AND date <= ? AND station_id = ? ORDER BY date",
            (start_date, end_date, station_id)
        )
    return query(
        "SELECT * FROM weather_daily WHERE date >= ? AND date <= ? ORDER BY date",
        (start_date, end_date)
    )


def get_weather_daily_recent(limit: int = 30, station_id: str = None) -> List[Dict[str, Any]]:
    """Get the N most recent weather_daily rows."""
    if station_id:
        return query(
            "SELECT * FROM weather_daily WHERE station_id = ? ORDER BY date DESC LIMIT ?",
            (station_id, limit)
        )
    return query("SELECT * FROM weather_daily ORDER BY date DESC LIMIT ?", (limit,))


def get_recent_scheduler_logs(limit: int = 100) -> List[Dict[str, Any]]:
    """Get the N most recent scheduler_log entries, newest first."""
    return query(
        "SELECT timestamp, message FROM scheduler_log ORDER BY id DESC LIMIT ?",
        (limit,))


def get_recent_intelligence_logs(limit: int = 100) -> List[Dict[str, Any]]:
    """Get the N most recent intelligence_log entries, newest first."""
    return query(
        "SELECT timestamp, level, logger, message FROM intelligence_log ORDER BY id DESC LIMIT ?",
        (limit,))


def get_intelligence_log_stats(days: int = 7) -> Dict[str, Any]:
    """Compute decision/mode stats from intelligence_log for telemetry.

    Returns dict with daily_decisions_avg, mode_switches_avg, api_error_rate_pct.
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    stats = {
        'daily_decisions_avg': None,
        'mode_switches_avg': None,
        'api_error_rate_pct': None,
    }
    try:
        rows = query(
            "SELECT timestamp, message FROM intelligence_log "
            "WHERE timestamp >= ? ORDER BY timestamp",
            (cutoff,))
        if not rows:
            return stats

        decisions = 0
        mode_switches = 0
        api_errors = 0
        api_total = 0
        days_seen = set()
        prev_mode = None

        for row in rows:
            ts_str = row.get('timestamp', '')
            msg = row.get('message', '')
            if not ts_str:
                continue
            day_key = ts_str[:10]
            days_seen.add(day_key)
            decisions += 1
            api_total += 1

            for prefix in ('Switching to ', 'Mode: ', 'Setting mode: '):
                if prefix in msg:
                    current_mode = msg.split(prefix, 1)[1].split()[0].strip('.,')
                    if prev_mode and current_mode != prev_mode:
                        mode_switches += 1
                    prev_mode = current_mode
                    break

            if 'error' in msg.lower() or 'fail' in msg.lower():
                api_errors += 1

        num_days = max(len(days_seen), 1)
        stats['daily_decisions_avg'] = round(decisions / num_days, 1)
        stats['mode_switches_avg'] = round(mode_switches / num_days, 1)
        if api_total > 0:
            stats['api_error_rate_pct'] = round((api_errors / api_total) * 100, 2)
    except Exception:
        pass
    return stats


def prune_old_logs(days: int = 30):
    """Delete scheduler_log and intelligence_log entries older than N days."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
    try:
        with _safe_write() as conn:
            conn.execute("DELETE FROM scheduler_log WHERE timestamp < ?", (cutoff,))
            conn.execute("DELETE FROM intelligence_log WHERE timestamp < ?", (cutoff,))
    except Exception as e:
        logger.warning(f"Log pruning failed: {e}")


def get_weather_observations_daily_agg(date_str: str, station_id: str = None) -> Optional[Dict[str, Any]]:
    """Aggregate weather_observations for a single date into daily summary format.
    Useful for getting today's partial-day summary from raw observations."""
    station_filter = "AND station_id = ?" if station_id else ""
    params = [date_str] + ([station_id] if station_id else [])
    rows = query(f"""
        SELECT
            MAX(temp_f) as temp_high,
            MIN(temp_f) as temp_low,
            AVG(temp_f) as temp_avg,
            AVG(humidity) as humidity_avg,
            MAX(humidity) as humidity_high,
            MIN(humidity) as humidity_low,
            MAX(precip_total_in) as precip_total,
            MAX(pressure_inhg) as pressure_max,
            MIN(pressure_inhg) as pressure_min,
            MAX(solar_radiation_wm2) as solar_radiation_high,
            MAX(uv_index) as uv_index_high,
            COUNT(*) as observation_count
        FROM weather_observations
        WHERE date(timestamp) = ? {station_filter}
        AND temp_f IS NOT NULL
    """, tuple(params))
    if rows and rows[0].get('observation_count', 0) > 0:
        return rows[0]
    return None


def store_daily_energy_summary(*, date: str, device_id: str = 'agate_main', **kwargs):
    """Insert or replace a daily_energy_summary row."""
    fields = ['date', 'device_id'] + list(kwargs.keys())
    placeholders = ', '.join(['?'] * len(fields))
    columns = ', '.join(fields)
    values = [date, device_id] + list(kwargs.values())
    try:
        with _safe_write() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO daily_energy_summary ({columns}) VALUES ({placeholders})",
                values
            )
        return True
    except Exception as e:
        logger.warning(f"Failed to store daily energy summary: {e}")
        return False
