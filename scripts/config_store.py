#!/usr/bin/env python3
"""
config_store.py — v4.6 DB-Resident Configuration Store

New in v4.6: application configuration lives in SQLite alongside the data,
replacing scattered .env reads and rate_schedule.json parsing. This module
owns the v4.6 config schema and provides the four accessors:

    app_config    — typed key/value settings (scope-aware for future multi-aGate)
    solar_arrays  — per-array inventory (charges_battery is first-class)
    rate_plans    — rate plan / tiers / windows / seasons / holidays reads
    app_state     — internal state (schema version, migration stamps, wizard state)

Reuses db.py's connection layer (WAL, thread-local, _safe_write) — one
connection discipline for the whole app. Phase 1 is additive: nothing reads
these tables until the Phase 2 consumer refactor. .env remains authoritative
until then.

Secrets policy: values flagged is_secret are stored base64-encoded.
This is OBSCURED, NOT ENCRYPTED — it prevents shoulder-surfing and accidental
log/screenshot exposure, nothing more. The 3 Franklin credentials never
migrate into the DB at all; they stay in .env.

Usage:
    import config_store
    config_store.init_config_schema()           # idempotent, cheap

    cap = config_store.app_config.get('battery.capacity_kwh')        # -> 27.2 (float)
    arrays = config_store.solar_arrays.battery_connected()           # -> [house row]
    plan = config_store.rate_plans.get_active_plan()
    season = config_store.rate_plans.get_season_for_date(plan['id'], '2026-06-12')
"""

import base64
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import db

logger = logging.getLogger(__name__)


# =============================================================================
# Schema — v4.6 Configuration Tables
# =============================================================================

CONFIG_SCHEMA_SQL = """
-- Application configuration: typed key/value, the single source of truth
-- scope: 'global' for whole-system settings; reserved for per-gateway
--        overrides when multi-aGate support lands (scope = gateway/device_id)
CREATE TABLE IF NOT EXISTS app_config (
    scope TEXT NOT NULL DEFAULT 'global',
    key TEXT NOT NULL,
    value TEXT,
    value_type TEXT NOT NULL DEFAULT 'str',   -- str | int | float | bool | json
    category TEXT,
    description TEXT,
    is_secret INTEGER NOT NULL DEFAULT 0,     -- 1 = value is base64-obscured
    source TEXT NOT NULL DEFAULT 'migration', -- env | default | user | migration
    updated_at TEXT,
    PRIMARY KEY (scope, key)
);
CREATE INDEX IF NOT EXISTS idx_app_config_category ON app_config(category);

-- Solar array inventory: one row per array, battery relationship explicit
CREATE TABLE IF NOT EXISTS solar_arrays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    array_id TEXT NOT NULL UNIQUE,            -- 'house', 'barn'
    name TEXT,
    array_type TEXT,                          -- enphase | solaredge | ...
    charges_battery INTEGER,                  -- 1 = feeds the Franklin; 0 = separately metered; NULL = unreviewed
    exports INTEGER,                          -- 1 = exports to grid; NULL = unreviewed
    gateway_id TEXT,                          -- aGate this array feeds (device_id namespace); NULL if none
    capacity_kw REAL,                         -- AC capacity
    capacity_kwp REAL,                        -- DC nameplate (forecast input)
    panel_count INTEGER,
    config_json TEXT,                         -- type-specific: ip/serial/site_id/credentials (secrets 'b64:'-prefixed)
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT
);

-- Rate plans: header row per utility plan
CREATE TABLE IF NOT EXISTS rate_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    export_capable INTEGER,
    net_metering TEXT,
    export_rates_json TEXT,
    default_tier TEXT,
    holiday_tier TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    source TEXT DEFAULT 'rate_schedule.json',
    imported_at TEXT
);

-- Rate seasons: month sets with optional per-season tier-rate overrides
CREATE TABLE IF NOT EXISTS rate_seasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    months_json TEXT NOT NULL,                -- e.g. [6,7,8,9]
    comment TEXT,
    UNIQUE(plan_id, name)
);

-- Rate tiers: season_id NULL = base/year-round rate; season row = override
CREATE TABLE IF NOT EXISTS rate_tiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,
    season_id INTEGER,                        -- NULL = base tier rate
    tier TEXT NOT NULL,                       -- peak | partial_peak | off_peak | ...
    rate_cents REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rate_tiers_plan ON rate_tiers(plan_id, season_id);

-- Rate windows: season_id NULL = year-round layout (fallback);
-- per-season layouts carry their season_id. The legacy per-window 'months'
-- key is NOT representable here by design — the importer rejects it (#18).
CREATE TABLE IF NOT EXISTS rate_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,
    season_id INTEGER,                        -- NULL = year-round / fallback
    tier TEXT NOT NULL,
    days_json TEXT NOT NULL,                  -- e.g. ["mon","tue",...]
    start_time TEXT NOT NULL,                 -- 'HH:MM'
    end_time TEXT NOT NULL,                   -- 'HH:MM' ('00:00' end = midnight)
    UNIQUE(plan_id, season_id, tier, days_json, start_time, end_time)
);
CREATE INDEX IF NOT EXISTS idx_rate_windows_plan ON rate_windows(plan_id, season_id);

-- Rate holidays: explicit dates that resolve to holiday_tier
CREATE TABLE IF NOT EXISTS rate_holidays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL,
    holiday_date TEXT NOT NULL,               -- 'YYYY-MM-DD'
    name TEXT,
    UNIQUE(plan_id, holiday_date)
);

-- Application state: internal bookkeeping (schema version, migration stamps,
-- future wizard state machine: new/validating/review_pending/complete)
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);
"""


def init_config_schema():
    """Create v4.6 config tables if they don't exist. Safe to call repeatedly."""
    try:
        conn = db._get_connection()
        conn.executescript(CONFIG_SCHEMA_SQL)
        conn.commit()
    except Exception as e:
        logger.warning(f"Config schema init failed: {e}")


def _now_iso() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# =============================================================================
# Value Encoding
# =============================================================================

def _serialize(value: Any, value_type: str) -> Optional[str]:
    """Python value -> stored text."""
    if value is None:
        return None
    if value_type == 'bool':
        if isinstance(value, str):
            return 'true' if value.strip().lower() in ('true', '1', 'yes', 'on') else 'false'
        return 'true' if value else 'false'
    if value_type == 'json':
        return value if isinstance(value, str) else json.dumps(value)
    return str(value)


def _deserialize(text: Optional[str], value_type: str) -> Any:
    """Stored text -> typed Python value."""
    if text is None:
        return None
    try:
        if value_type == 'int':
            return int(float(text))
        if value_type == 'float':
            return float(text)
        if value_type == 'bool':
            return text.strip().lower() in ('true', '1', 'yes', 'on')
        if value_type == 'json':
            return json.loads(text)
    except (ValueError, TypeError, json.JSONDecodeError):
        logger.warning(f"Config value failed {value_type} coercion: {text!r}")
        return None
    return text


def obscure(text: str) -> str:
    """Base64-obscure a secret value. OBSCURED, NOT ENCRYPTED."""
    if text is None:
        return None
    return base64.b64encode(text.encode('utf-8')).decode('ascii')


def reveal(text: str) -> str:
    """Decode a base64-obscured value. Returns input unchanged on failure."""
    if text is None:
        return None
    try:
        return base64.b64decode(text.encode('ascii')).decode('utf-8')
    except Exception:
        return text


# =============================================================================
# Accessor 1 — app_config
# =============================================================================

class _AppConfig:
    """Typed key/value settings. Secrets transparently obscured/revealed."""

    def get(self, key: str, default: Any = None, scope: str = 'global') -> Any:
        rows = db.query(
            "SELECT value, value_type, is_secret FROM app_config WHERE scope = ? AND key = ?",
            (scope, key))
        if not rows:
            return default
        row = rows[0]
        raw = row['value']
        if row['is_secret'] and raw is not None:
            raw = reveal(raw)
        value = _deserialize(raw, row['value_type'])
        return default if value is None else value

    def get_row(self, key: str, scope: str = 'global') -> Optional[Dict[str, Any]]:
        """Full row including metadata (value left as stored; secrets stay obscured)."""
        rows = db.query(
            "SELECT * FROM app_config WHERE scope = ? AND key = ?", (scope, key))
        return rows[0] if rows else None

    def set(self, key: str, value: Any, *, value_type: str = None,
            category: str = None, description: str = None,
            is_secret: bool = None, source: str = 'user',
            scope: str = 'global') -> bool:
        """Insert or update a config key. Metadata args only override when given."""
        existing = self.get_row(key, scope=scope)
        vt = value_type or (existing['value_type'] if existing else 'str')
        cat = category if category is not None else (existing['category'] if existing else None)
        desc = description if description is not None else (existing['description'] if existing else None)
        secret = is_secret if is_secret is not None else bool(existing['is_secret']) if existing else False
        text = _serialize(value, vt)
        if secret and text is not None:
            text = obscure(text)
        try:
            with db._safe_write() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO app_config
                    (scope, key, value, value_type, category, description,
                     is_secret, source, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (scope, key, text, vt, cat, desc, 1 if secret else 0,
                      source, _now_iso()))
            return True
        except Exception as e:
            logger.warning(f"app_config set failed for {key}: {e}")
            return False

    def get_all(self, category: str = None, scope: str = 'global',
                include_secrets: bool = False) -> List[Dict[str, Any]]:
        """All rows (optionally one category), values typed. Secret values are
        redacted unless include_secrets=True (settings page wants redacted)."""
        if category:
            rows = db.query(
                "SELECT * FROM app_config WHERE scope = ? AND category = ? ORDER BY key",
                (scope, category))
        else:
            rows = db.query(
                "SELECT * FROM app_config WHERE scope = ? ORDER BY category, key",
                (scope,))
        out = []
        for r in rows:
            raw = r['value']
            if r['is_secret']:
                if include_secrets:
                    raw = reveal(raw)
                else:
                    raw = '********' if raw else None
                    r = dict(r)
                    r['value'] = raw
                    out.append(r)
                    continue
            r = dict(r)
            r['value'] = _deserialize(raw, r['value_type'])
            out.append(r)
        return out

    def get_prefix(self, prefix: str, scope: str = 'global') -> Dict[str, Any]:
        """Typed dict of all keys under a prefix, e.g. 'battery.' -> {...}."""
        rows = db.query(
            "SELECT key, value, value_type, is_secret FROM app_config "
            "WHERE scope = ? AND key LIKE ? ORDER BY key",
            (scope, prefix + '%'))
        out = {}
        for r in rows:
            raw = reveal(r['value']) if (r['is_secret'] and r['value'] is not None) else r['value']
            out[r['key']] = _deserialize(raw, r['value_type'])
        return out


# =============================================================================
# Accessor 2 — solar_arrays
# =============================================================================

class _SolarArrays:
    """Per-array inventory. config_json secrets carry a 'b64:' prefix."""

    @staticmethod
    def _decode_config(row: Dict[str, Any]) -> Dict[str, Any]:
        row = dict(row)
        cfg = row.get('config_json')
        if cfg:
            try:
                parsed = json.loads(cfg)
                for k, v in list(parsed.items()):
                    if isinstance(v, str) and v.startswith('b64:'):
                        parsed[k] = reveal(v[4:])
                row['config'] = parsed
            except (json.JSONDecodeError, TypeError):
                row['config'] = {}
        else:
            row['config'] = {}
        return row

    def all(self, enabled_only: bool = True) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM solar_arrays"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY array_id"
        return [self._decode_config(r) for r in db.query(sql)]

    def get(self, array_id: str) -> Optional[Dict[str, Any]]:
        rows = db.query("SELECT * FROM solar_arrays WHERE array_id = ?", (array_id,))
        return self._decode_config(rows[0]) if rows else None

    def battery_connected(self) -> List[Dict[str, Any]]:
        """Arrays that physically charge the Franklin battery."""
        rows = db.query(
            "SELECT * FROM solar_arrays WHERE charges_battery = 1 AND enabled = 1 "
            "ORDER BY array_id")
        return [self._decode_config(r) for r in rows]

    def upsert(self, array_id: str, **fields) -> bool:
        """Insert or update an array row. Only provided fields are written;
        existing values are preserved for omitted fields."""
        existing = db.query("SELECT * FROM solar_arrays WHERE array_id = ?", (array_id,))
        current = dict(existing[0]) if existing else {}
        allowed = ('name', 'array_type', 'charges_battery', 'exports',
                   'gateway_id', 'capacity_kw', 'capacity_kwp', 'panel_count',
                   'config_json', 'enabled')
        merged = {k: fields.get(k, current.get(k)) for k in allowed}
        if merged.get('enabled') is None:
            merged['enabled'] = 1
        try:
            with db._safe_write() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO solar_arrays
                    (id, array_id, name, array_type, charges_battery, exports,
                     gateway_id, capacity_kw, capacity_kwp, panel_count,
                     config_json, enabled, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (current.get('id'), array_id, merged['name'],
                      merged['array_type'], merged['charges_battery'],
                      merged['exports'], merged['gateway_id'],
                      merged['capacity_kw'], merged['capacity_kwp'],
                      merged['panel_count'], merged['config_json'],
                      merged['enabled'], _now_iso()))
            return True
        except Exception as e:
            logger.warning(f"solar_arrays upsert failed for {array_id}: {e}")
            return False


# =============================================================================
# Accessor 3 — rate_plans
# =============================================================================

class _RatePlans:
    """Read helpers for the rate tables. The full date->rate resolver that
    consumers call lands in Phase 2 (rate_config); these are its primitives."""

    def get_active_plan(self) -> Optional[Dict[str, Any]]:
        rows = db.query("SELECT * FROM rate_plans WHERE active = 1 ORDER BY id LIMIT 1")
        return rows[0] if rows else None

    def get_plan_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        rows = db.query("SELECT * FROM rate_plans WHERE name = ?", (name,))
        return rows[0] if rows else None

    def get_seasons(self, plan_id: int) -> List[Dict[str, Any]]:
        rows = db.query(
            "SELECT * FROM rate_seasons WHERE plan_id = ? ORDER BY id", (plan_id,))
        for r in rows:
            try:
                r['months'] = json.loads(r['months_json'])
            except (json.JSONDecodeError, TypeError):
                r['months'] = []
        return rows

    def get_season_for_date(self, plan_id: int, date_str: str) -> Optional[Dict[str, Any]]:
        """Season whose months contain the given 'YYYY-MM-DD' date, else None."""
        try:
            month = int(date_str[5:7])
        except (ValueError, IndexError):
            return None
        for season in self.get_seasons(plan_id):
            if month in season.get('months', []):
                return season
        return None

    def get_tier_rates(self, plan_id: int, season_id: int = None) -> Dict[str, float]:
        """Tier -> rate_cents. Base rates overlaid by season overrides when
        season_id is given."""
        rates = {}
        for r in db.query(
                "SELECT tier, rate_cents FROM rate_tiers "
                "WHERE plan_id = ? AND season_id IS NULL", (plan_id,)):
            rates[r['tier']] = r['rate_cents']
        if season_id is not None:
            for r in db.query(
                    "SELECT tier, rate_cents FROM rate_tiers "
                    "WHERE plan_id = ? AND season_id = ?", (plan_id, season_id)):
                rates[r['tier']] = r['rate_cents']
        return rates

    def get_windows(self, plan_id: int, season_id: int = None) -> List[Dict[str, Any]]:
        """Window layout for a season. Per-season layouts win; the year-round
        (season_id NULL) layout is the fallback when the season has none."""
        if season_id is not None:
            rows = db.query(
                "SELECT * FROM rate_windows WHERE plan_id = ? AND season_id = ? "
                "ORDER BY start_time", (plan_id, season_id))
            if rows:
                for r in rows:
                    r['days'] = json.loads(r['days_json'])
                return rows
        rows = db.query(
            "SELECT * FROM rate_windows WHERE plan_id = ? AND season_id IS NULL "
            "ORDER BY start_time", (plan_id,))
        for r in rows:
            r['days'] = json.loads(r['days_json'])
        return rows

    def get_holidays(self, plan_id: int) -> List[Dict[str, Any]]:
        return db.query(
            "SELECT * FROM rate_holidays WHERE plan_id = ? ORDER BY holiday_date",
            (plan_id,))


# =============================================================================
# Accessor 4 — app_state
# =============================================================================

class _AppState:
    """Internal bookkeeping: schema version, migration stamps, wizard state."""

    def get(self, key: str, default: str = None) -> Optional[str]:
        rows = db.query("SELECT value FROM app_state WHERE key = ?", (key,))
        return rows[0]['value'] if rows else default

    def set(self, key: str, value: str) -> bool:
        try:
            with db._safe_write() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO app_state (key, value, updated_at) "
                    "VALUES (?, ?, ?)", (key, str(value), _now_iso()))
            return True
        except Exception as e:
            logger.warning(f"app_state set failed for {key}: {e}")
            return False

    def all(self) -> List[Dict[str, Any]]:
        return db.query("SELECT * FROM app_state ORDER BY key")


# Module-level accessor instances
app_config = _AppConfig()
solar_arrays = _SolarArrays()
rate_plans = _RatePlans()
app_state = _AppState()


if __name__ == '__main__':
    # Quick diagnostic dump when run directly
    import sys
    db.init_db()
    init_config_schema()
    print(f"schema_version: {app_state.get('schema_version', '(not set)')}")
    rows = app_config.get_all()
    print(f"app_config: {len(rows)} keys")
    for r in rows:
        print(f"  [{r['category']}] {r['key']} = {r['value']!r} ({r['value_type']}, {r['source']})")
    arrays = solar_arrays.all(enabled_only=False)
    print(f"solar_arrays: {len(arrays)} rows")
    for a in arrays:
        print(f"  {a['array_id']}: type={a['array_type']} charges_battery={a['charges_battery']} "
              f"exports={a['exports']} kw={a['capacity_kw']} kwp={a['capacity_kwp']}")
    plan = rate_plans.get_active_plan()
    if plan:
        seasons = rate_plans.get_seasons(plan['id'])
        print(f"rate plan: {plan['name']} (export_capable={plan['export_capable']}, "
              f"{plan['net_metering']}), {len(seasons)} seasons")
        for s in seasons:
            rates = rate_plans.get_tier_rates(plan['id'], s['id'])
            print(f"  {s['name']} months={s['months']} rates={rates}")
    else:
        print("rate plan: none imported")
    sys.exit(0)
