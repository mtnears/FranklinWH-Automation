#!/usr/bin/env python3
"""
migrate_v46.py — v4.6 Phase 1 Configuration Migration

Manual, one-shot migration (run via docker exec — NOT auto-invoked at startup):

    python3 /app/scripts/migrate_v46.py --dry-run --battery-array house
    python3 /app/scripts/migrate_v46.py --battery-array house

What it does (purely ADDITIVE — never modifies .env or rate_schedule.json;
those remain authoritative until the Phase 2 consumer cutover):

  1. Creates the v4.6 config tables (app_config, solar_arrays, rate_plans,
     rate_seasons, rate_tiers, rate_windows, rate_holidays, app_state).
  2. Maps every .env setting EXCEPT the 3 Franklin credentials into app_config,
     recording whether each value came from the env ('env') or fell back to
     the code default ('default') — the settings page uses this to flag
     unreviewed defaults. Secrets are base64-OBSCURED (not encrypted).
  3. Builds solar_arrays rows from SOLAR_ARRAYS / SOLAR_ARRAY_<NAME>_* vars.
     --battery-array marks which array(s) physically charge the Franklin.
  4. Imports rate_schedule.json into the rate tables. Rejects the legacy
     per-window 'months' key (#18) instead of silently ignoring it; validates
     season month coverage and overlap.
  5. Applies the PR #25 schema additions to system_readings:
     battery_kw_direct (register 1048 parallel logging) and
     active_reserve_pct (single-reserve replacement for the
     self_reserve_pct/tou_reserve_pct pair — old columns retained until
     the post-soak cleanup release).
  6. Stamps app_state with schema_version and migration metadata.

Idempotent: re-runs refresh 'env'/'default' rows but never overwrite rows a
user has edited (source='user'), preserve manually-set solar_arrays fields,
and re-import the rate plan atomically.
"""

import argparse
import base64
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import db
import config_store

logging.basicConfig(level=logging.INFO, format='%(levelname)-7s %(message)s')
logger = logging.getLogger('migrate_v46')

SCHEMA_VERSION = '4.6.0-phase1'

# Env vars that NEVER migrate into the DB
EXCLUDED_ENV = {
    'FRANKLIN_USERNAME', 'FRANKLIN_PASSWORD', 'FRANKLIN_GATEWAY_ID',  # the 3 credentials — stay in .env
    'BASE_DIR', 'LOG_DIR', 'DATA_DIR', 'WEB_DIR',                     # deployment topology — can't live in the DB they locate
    'ENGINE_VERSION',                                                  # derived from VERSION file at runtime
    'EMERGENCY_PREP_MODE',                                             # confirmed dead — intentionally dropped
}

# =============================================================================
# .env -> app_config mapping
# (env_var, key, type, category, default, is_secret, description)
# Defaults mirror config.py except where v4.6 decisions reconcile them
# (battery.count -> 1, engine.taper_ceiling_pct -> 85).
# =============================================================================

MAPPINGS = [
    # --- battery ---
    ('BATTERY_CAPACITY_KWH', 'battery.capacity_kwh', 'float', 'battery', 30.0, 0,
     'Total usable battery capacity in kWh, all aPower units combined'),
    ('CHARGE_RATE_PER_HOUR', 'battery.charge_rate_per_hour', 'float', 'battery', 32.0, 0,
     'Battery charge rate in percent SOC per hour'),
    ('BATTERY_COUNT', 'battery.count', 'int', 'battery', 1, 0,
     'Number of aPower battery units'),
    ('BACKUP_RESERVE_PCT', 'battery.backup_reserve_pct', 'float', 'battery', 20.0, 0,
     'Minimum SOC reserved for outage backup'),
    ('RESERVE_SOC_BACKUP', 'battery.reserve_soc_backup', 'int', 'battery', 100, 0,
     'SOC target passed to franklinwh library in Emergency Backup mode'),
    ('RESERVE_SOC_HOME', 'battery.reserve_soc_home', 'int', 'battery', 20, 0,
     'Minimum SOC passed to franklinwh library in TOU/Self-Consumption mode'),

    # --- features ---
    ('SOLAR_ENABLED', 'features.solar', 'bool', 'features', True, 0,
     'Solar-aware decision logic enabled'),
    ('TOU_ENABLED', 'features.tou', 'bool', 'features', True, 0,
     'Time-of-use rate optimization enabled'),
    ('DYNAMIC_PRICING_ENABLED', 'features.dynamic_pricing', 'bool', 'features', False, 0,
     'Real-time dynamic pricing (e.g. ComEd hourly) enabled'),
    ('WEATHER_ENABLED', 'features.weather', 'bool', 'features', False, 0,
     'Local weather station collection enabled'),
    ('PVOUTPUT_ENABLED', 'features.pvoutput', 'bool', 'features', False, 0,
     'PVOutput.org daily uploads enabled'),
    ('ENPHASE_ENABLED', 'features.enphase_legacy', 'bool', 'features', False, 0,
     'Legacy single-array Enphase flag (superseded by solar_arrays table)'),
    ('ADAPTIVE_ENGINE_ENABLED', 'features.adaptive_engine', 'bool', 'features', False, 0,
     'V4.0 adaptive decision engine (falls back to v3.5 logic on error)'),
    ('SOLAREDGE_PANEL_MONITORING', 'features.solaredge_panel_monitoring', 'bool', 'features', False, 0,
     'SolarEdge portal per-optimizer scraping enabled'),
    ('FORECAST_ENABLED', 'features.forecast', 'bool', 'features', False, 0,
     'Weather-aware solar forecasting enabled'),

    # --- modbus ---
    ('MODBUS_ENABLED', 'modbus.enabled', 'bool', 'modbus', False, 0,
     'Local Modbus TCP polling of the aGate (authoritative data source)'),
    ('MODBUS_HOST', 'modbus.host', 'str', 'modbus', '192.168.5.149', 0,
     'aGate Modbus TCP host'),
    ('MODBUS_PORT', 'modbus.port', 'int', 'modbus', 502, 0,
     'aGate Modbus TCP port'),
    ('MODBUS_TIMEOUT', 'modbus.timeout', 'float', 'modbus', 5.0, 0,
     'Modbus request timeout in seconds'),
    ('MODBUS_RETRY_ATTEMPTS', 'modbus.retry_attempts', 'int', 'modbus', 3, 0,
     'Modbus retry attempts before giving up a cycle'),

    # --- telemetry ---
    ('TELEMETRY_ENABLED', 'telemetry.enabled', 'bool', 'telemetry', False, 0,
     'Anonymous opt-in usage telemetry'),
    ('TELEMETRY_ENDPOINT', 'telemetry.endpoint', 'str', 'telemetry',
     'https://telemetry.example.com/franklin-automation', 0,
     'Telemetry submission endpoint'),
    ('TELEMETRY_INTERVAL_HOURS', 'telemetry.interval_hours', 'int', 'telemetry', 24, 0,
     'Hours between telemetry submissions'),
    ('TELEMETRY_REGION', 'telemetry.region', 'str', 'telemetry', '', 0,
     'Coarse region code included in telemetry'),

    # --- solar (battery-system level) ---
    ('SOLAR_EXPORT', 'solar.export', 'bool', 'solar', False, 0,
     'Charging STRATEGY flag, not capability: false = self-consumption '
     '(continuous-target runs, grid charge capped at taper ceiling); '
     'true = export-friendly (CT skipped, surplus exports for credit)'),
    ('SOLAR_CAPACITY_KW', 'solar.capacity_kw', 'float', 'solar', 0.0, 0,
     'Capacity (kW) of the battery-connected solar array ONLY — exclude '
     'separately-metered arrays that cannot charge the battery'),
    ('MIN_SOLAR_FOR_WAIT', 'solar.min_for_wait', 'float', 'solar', 0.5, 0,
     'Minimum solar kW to consider production active'),
    ('NEM_VERSION', 'solar.nem_version', 'str', 'solar', '', 0,
     'Net metering version (nem1/nem2/nem3) — drives export economics'),

    # --- rates / billing ---
    ('CARE_RATE', 'rates.care_active', 'bool', 'rates', False, 0,
     'CARE discount active on the battery-system meter'),
    ('MULTI_METER', 'billing.multi_meter', 'bool', 'billing', False, 0,
     'Multiple utility meters aggregate-billed (e.g. separate solar meter)'),

    # --- tou (LEGACY — engine still reads these in v4.5.x; Phase 2 repoints
    #     the engine at the rate plan windows and drains these. See issue #26.) ---
    ('PEAK_START_HOUR', 'tou.peak_start_hour', 'int', 'tou_legacy', 17, 0,
     'LEGACY engine peak start hour — superseded by rate plan windows after '
     'Phase 2 repoint (#26)'),
    ('PEAK_END_HOUR', 'tou.peak_end_hour', 'int', 'tou_legacy', 20, 0,
     'LEGACY engine peak end hour — superseded by rate plan windows after '
     'Phase 2 repoint (#26)'),
    ('PEAK_DAYS', 'tou.peak_days', 'str', 'tou_legacy', 'weekdays', 0,
     'LEGACY peak days — superseded by rate plan windows'),
    ('PEAK2_START_HOUR', 'tou.peak2_start_hour', 'int', 'tou_legacy', None, 0,
     'LEGACY optional second peak start hour'),
    ('PEAK2_END_HOUR', 'tou.peak2_end_hour', 'int', 'tou_legacy', None, 0,
     'LEGACY optional second peak end hour'),
    ('PEAK2_DAYS', 'tou.peak2_days', 'str', 'tou_legacy', None, 0,
     'LEGACY optional second peak days'),

    # --- scheduling ---
    ('CHECK_INTERVAL_MINUTES', 'scheduling.check_interval_minutes', 'int', 'scheduling', 0, 0,
     'Polling interval override in minutes; 0 = auto-calculated from features'),
    ('PEAK_TRANSITION_BUFFER_MINUTES', 'scheduling.peak_transition_buffer_minutes', 'int',
     'scheduling', 10, 0, 'Minutes of buffer before/after peak transitions'),
    ('HOME_MODE', 'scheduling.home_mode', 'str', 'scheduling', 'tou', 0,
     "Default home mode: 'tou' or 'self_consumption'"),

    # --- pricing (dynamic) ---
    ('PRICING_PROVIDER', 'pricing.provider', 'str', 'pricing', 'comed', 0,
     'Dynamic pricing provider'),
    ('PRICE_THRESHOLD_CENTS', 'pricing.threshold_cents', 'float', 'pricing', 4.0, 0,
     'Grid charge threshold in cents/kWh'),
    ('PRICE_CEILING_CENTS', 'pricing.ceiling_cents', 'float', 'pricing', 10.0, 0,
     'Price ceiling in cents/kWh'),
    ('SOLAR_OVERRIDE_PRICE_CENTS', 'pricing.solar_override_cents', 'float', 'pricing', None, 0,
     'Grid price at/below which to charge even with solar producing; unset = disabled'),

    # --- weather ---
    ('WEATHER_PROVIDER', 'weather.provider', 'str', 'weather', 'wunderground', 0,
     'Weather data provider'),
    ('WEATHER_STATION_ID', 'weather.station_id', 'str', 'weather', '', 0,
     'Local weather station ID'),
    ('WEATHER_API_KEY', 'weather.api_key', 'str', 'weather', '', 1,
     'Weather provider API key'),
    ('CLOUDY_THRESHOLD_PERCENT', 'weather.cloudy_threshold_percent', 'int', 'weather', 50, 0,
     'Cloud cover percent above which a day is treated as cloudy'),

    # --- forecast (battery-connected array physics) ---
    ('FORECAST_LATITUDE', 'forecast.latitude', 'float', 'forecast', 38.91, 0,
     'Site latitude for solar forecasting'),
    ('FORECAST_LONGITUDE', 'forecast.longitude', 'float', 'forecast', -120.84, 0,
     'Site longitude for solar forecasting'),
    ('FORECAST_HOUSE_TILT', 'forecast.house_tilt', 'int', 'forecast', 22, 0,
     'Battery-connected array tilt in degrees (0=flat)'),
    ('FORECAST_HOUSE_AZIMUTH', 'forecast.house_azimuth', 'int', 'forecast', -65, 0,
     'Battery-connected array azimuth (0=south, -90=east, 90=west)'),
    ('FORECAST_HOUSE_KWP', 'forecast.house_kwp', 'float', 'forecast', 6.96, 0,
     'Battery-connected array DC nameplate in kWp'),
    ('FORECAST_SOLAR_API_KEY', 'forecast.solar_api_key', 'str', 'forecast', '', 1,
     'Forecast.Solar API key (optional)'),

    # --- pvoutput ---
    ('PVOUTPUT_API_KEY', 'pvoutput.api_key', 'str', 'pvoutput', '', 1,
     'PVOutput.org API key'),
    ('PVOUTPUT_SYSTEM_IDS', 'pvoutput.system_ids', 'json', 'pvoutput', [], 0,
     'PVOutput system IDs, one per array'),

    # --- solaredge portal scraper ---
    ('SOLAREDGE_SITE_ID', 'solaredge.site_id', 'str', 'solaredge', '', 0,
     'SolarEdge portal site ID'),
    ('SOLAREDGE_USERNAME', 'solaredge.username', 'str', 'solaredge', '', 0,
     'SolarEdge portal login username'),
    ('SOLAREDGE_PASSWORD', 'solaredge.password', 'str', 'solaredge', '', 1,
     'SolarEdge portal login password'),

    # --- engine tuning ---
    ('TARGET_SOC', 'engine.target_soc', 'float', 'engine', 95.0, 0,
     'Configured SOC target (solar-fed ceiling); grid charging stops at the '
     'taper ceiling on non-export systems'),
    ('SAFETY_MARGIN_HOURS', 'engine.safety_margin_hours', 'float', 'engine', 0.75, 0,
     'Hours of safety margin in charge-timing math'),
    ('CHARGING_STRATEGY', 'engine.charging_strategy', 'str', 'engine', 'balanced', 0,
     'Charging strategy profile'),
    ('TAPER_CEILING_PCT', 'engine.taper_ceiling_pct', 'float', 'engine', 85.0, 0,
     'Maximum SOC the engine grid-charges to before letting solar finish; '
     'above this, charge rate tapers and surplus solar is wasted on non-export'),
    ('P45_SAFETY_MARGIN_KWH', 'engine.p45_safety_margin_kwh', 'float', 'engine', 2.0, 0,
     'P4.5 partial-peak protection safety margin in kWh'),
    ('DECISION_INTERVAL_MINUTES', 'engine.decision_interval_minutes', 'int', 'engine', 15, 0,
     'Adaptive engine decision interval in minutes'),

    # --- notifications ---
    ('EMAIL_ENABLED', 'notify.email_enabled', 'bool', 'notify', False, 0,
     'Email notifications enabled'),
    ('SMTP_SERVER', 'notify.smtp_server', 'str', 'notify', 'smtp.gmail.com', 0,
     'SMTP server'),
    ('SMTP_PORT', 'notify.smtp_port', 'int', 'notify', 587, 0,
     'SMTP port'),
    ('SENDER_EMAIL', 'notify.sender_email', 'str', 'notify', '', 0,
     'Notification sender address'),
    ('SENDER_PASSWORD', 'notify.sender_password', 'str', 'notify', '', 1,
     'SMTP app password'),
    ('RECIPIENT_EMAIL', 'notify.recipient_email', 'str', 'notify', '', 0,
     'Notification recipient address'),

    # --- system ---
    ('DEBUG_MODE', 'system.debug_mode', 'bool', 'system', False, 0,
     'Verbose debug logging'),
    ('API_MAX_RETRIES', 'system.api_max_retries', 'int', 'system', 5, 0,
     'Cloud API max retries'),
    ('API_RETRY_DELAY', 'system.api_retry_delay', 'int', 'system', 10, 0,
     'Cloud API retry delay in seconds'),
    ('TZ', 'system.tz', 'str', 'system', 'America/Los_Angeles', 0,
     'System timezone'),
    ('DASHBOARD_PORT', 'system.dashboard_port', 'int', 'system', 8100, 0,
     'Dashboard HTTP port'),
]

# Per-array env keys lifted into dedicated columns (rest -> config_json)
ARRAY_COLUMN_KEYS = {'NAME': 'name', 'TYPE': 'array_type', 'CAPACITY_KW': 'capacity_kw'}
ARRAY_SECRET_KEYS = {'password', 'api_key'}


# =============================================================================
# Env loading
# =============================================================================

def find_env_file(explicit: str = None) -> Path:
    """Resolve the .env path relative to the app layout, never hardcoded
    (the /app/.env hardcode is what blinded the diag tool on LXC)."""
    if explicit:
        return Path(explicit)
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / '.env', here / '.env', Path.cwd() / '.env'):
        if candidate.exists():
            return candidate
    return None


def parse_env_file(path: Path) -> dict:
    """Minimal KEY=VALUE parser; ignores comments/blank lines, strips quotes."""
    env = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        if line.startswith('export '):
            line = line[len('export '):]
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        env[key] = value
    return env


def load_env(explicit_path: str = None):
    """Returns (env_dict, source_path). File values win; os.environ fills gaps
    so LXC/env-only deployments still migrate."""
    path = find_env_file(explicit_path)
    env = parse_env_file(path) if path else {}
    file_keys = set(env.keys())
    for k, v in os.environ.items():
        if k not in env:
            env[k] = v
    return env, path, file_keys


# =============================================================================
# Plan builders (pure — no writes; apply step writes)
# =============================================================================

def build_config_plan(env: dict) -> list:
    """[(key, value, type, category, secret, source, description)] for app_config."""
    plan = []
    for env_var, key, vtype, category, default, secret, desc in MAPPINGS:
        if env_var in env and env[env_var] != '':
            value = env[env_var]
            if vtype == 'json':
                try:
                    json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    value = [item.strip() for item in value.split(',') if item.strip()]
            plan.append((key, value, vtype, category, secret, 'env', desc))
        else:
            if default is None:
                continue  # optional and unset — don't manufacture a row
            plan.append((key, default, vtype, category, secret, 'default', desc))
    return plan


def build_array_plan(env: dict, battery_arrays: set) -> list:
    """One dict per solar array from SOLAR_ARRAYS / SOLAR_ARRAY_<NAME>_* vars."""
    ids = [a.strip().lower() for a in env.get('SOLAR_ARRAYS', '').split(',') if a.strip()]
    solar_export = env.get('SOLAR_EXPORT', 'false').strip().lower() in ('true', '1', 'yes', 'on')
    arrays = []
    for aid in ids:
        prefix = f'SOLAR_ARRAY_{aid.upper()}_'
        fields = {ek[len(prefix):]: ev for ek, ev in env.items() if ek.startswith(prefix)}
        row = {'array_id': aid, 'name': None, 'array_type': None, 'capacity_kw': None,
               'capacity_kwp': None, 'charges_battery': None, 'exports': None,
               'gateway_id': None, 'config_json': None}
        cfg = {}
        for fkey, fval in fields.items():
            if fkey in ARRAY_COLUMN_KEYS:
                col = ARRAY_COLUMN_KEYS[fkey]
                row[col] = float(fval) if col == 'capacity_kw' else fval
            else:
                lk = fkey.lower()
                if lk in ARRAY_SECRET_KEYS:
                    cfg[lk] = 'b64:' + base64.b64encode(fval.encode('utf-8')).decode('ascii')
                else:
                    cfg[lk] = fval
        row['config_json'] = json.dumps(cfg) if cfg else None
        if battery_arrays:
            if aid in battery_arrays:
                row['charges_battery'] = 1
                row['exports'] = 1 if solar_export else 0
                row['gateway_id'] = 'agate_main'
                kwp = env.get('FORECAST_HOUSE_KWP')  # forecast block is defined as the battery-connected array
                if kwp:
                    try:
                        row['capacity_kwp'] = float(kwp)
                    except ValueError:
                        pass
            else:
                row['charges_battery'] = 0
        elif len(ids) == 1:
            row['charges_battery'] = 1
            row['exports'] = 1 if solar_export else 0
            row['gateway_id'] = 'agate_main'
        arrays.append(row)
    return arrays


def load_rate_schedule(explicit_path: str = None):
    """Returns (parsed_json, path) or (None, attempted_path)."""
    if explicit_path:
        path = Path(explicit_path)
    else:
        here = Path(__file__).resolve().parent
        candidates = [here.parent / 'data' / 'rate_schedule.json',
                      Path('/app/data/rate_schedule.json'),
                      Path.cwd() / 'data' / 'rate_schedule.json']
        path = next((c for c in candidates if c.exists()), candidates[0])
    if not path.exists():
        return None, path
    with open(path) as f:
        data = json.load(f)
    return data.get('rate_schedule', data), path


def validate_rate_schedule(rs: dict, allow_legacy_months: bool) -> tuple:
    """Returns (errors, warnings). Errors block import unless overridden."""
    errors, warnings = [], []

    def check_windows(windows, where):
        for i, w in enumerate(windows or []):
            if 'months' in w:
                msg = (f"window {i} in {where} carries a 'months' key — this key is "
                       f"SILENTLY IGNORED by the legacy parser (#18) and is not "
                       f"importable. Move seasonal window layouts into per-season "
                       f"'windows' blocks (see rate_schedule.example.json).")
                if allow_legacy_months:
                    warnings.append(msg + " [--allow-legacy-months: key dropped, "
                                          "window imported as year-round]")
                else:
                    errors.append(msg)

    check_windows(rs.get('windows'), 'top-level windows')
    covered = {}
    for s in rs.get('seasons', []):
        check_windows(s.get('windows'), f"season '{s.get('name')}'")
        for m in s.get('months', []):
            covered.setdefault(m, []).append(s.get('name'))
    if rs.get('seasons'):
        missing = [m for m in range(1, 13) if m not in covered]
        if missing:
            warnings.append(f"months not covered by any season: {missing} "
                            f"(base tiers/windows apply there)")
        overlaps = {m: names for m, names in covered.items() if len(names) > 1}
        if overlaps:
            warnings.append(f"months claimed by multiple seasons: {overlaps} "
                            f"(first match wins — fix the season definitions)")
    return errors, warnings


# =============================================================================
# Apply steps
# =============================================================================

def apply_config_plan(plan: list) -> dict:
    stats = {'env': 0, 'default': 0, 'skipped_user': 0, 'secrets': 0}
    for key, value, vtype, category, secret, source, desc in plan:
        existing = config_store.app_config.get_row(key)
        if existing and existing['source'] == 'user':
            stats['skipped_user'] += 1
            continue
        config_store.app_config.set(
            key, value, value_type=vtype, category=category,
            description=desc, is_secret=bool(secret), source=source)
        stats[source] += 1
        if secret:
            stats['secrets'] += 1
    return stats


def apply_array_plan(arrays: list) -> int:
    count = 0
    for row in arrays:
        existing = config_store.solar_arrays.get(row['array_id'])
        fields = dict(row)
        fields.pop('array_id')
        if existing:
            # Preserve manually-set classification over a None from this run
            for col in ('charges_battery', 'exports', 'gateway_id', 'capacity_kwp',
                        'panel_count'):
                if fields.get(col) is None and existing.get(col) is not None:
                    fields[col] = existing[col]
        config_store.solar_arrays.upsert(row['array_id'], **fields)
        count += 1
    return count


def import_rate_plan(rs: dict, allow_legacy_months: bool) -> dict:
    """Atomic wipe-and-rewrite of the named plan and its child rows."""
    name = rs.get('name', 'imported')
    export = rs.get('export', {}) or {}
    conn = db._get_connection()
    try:
        conn.execute('BEGIN')
        old = conn.execute('SELECT id FROM rate_plans WHERE name = ?', (name,)).fetchone()
        if old:
            pid_old = old[0]
            for table in ('rate_tiers', 'rate_windows', 'rate_seasons', 'rate_holidays'):
                conn.execute(f'DELETE FROM {table} WHERE plan_id = ?', (pid_old,))
            conn.execute('DELETE FROM rate_plans WHERE id = ?', (pid_old,))
        cur = conn.execute("""
            INSERT INTO rate_plans
            (name, export_capable, net_metering, export_rates_json,
             default_tier, holiday_tier, active, source, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, 'rate_schedule.json', ?)
        """, (name,
              1 if export.get('capable') else 0,
              export.get('net_metering'),
              json.dumps(export.get('export_rates')) if export.get('export_rates') is not None else None,
              rs.get('default_tier'),
              rs.get('holiday_tier'),
              datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        plan_id = cur.lastrowid

        for tier, spec in (rs.get('tiers') or {}).items():
            conn.execute(
                'INSERT INTO rate_tiers (plan_id, season_id, tier, rate_cents) '
                'VALUES (?, NULL, ?, ?)', (plan_id, tier, spec.get('rate_cents')))

        def insert_windows(windows, season_id):
            n = 0
            for w in windows or []:
                w = dict(w)
                w.pop('months', None)  # only reachable under --allow-legacy-months
                conn.execute("""
                    INSERT OR IGNORE INTO rate_windows
                    (plan_id, season_id, tier, days_json, start_time, end_time)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (plan_id, season_id, w.get('tier'),
                      json.dumps(w.get('days', [])), w.get('start'), w.get('end')))
                n += 1
            return n

        n_windows = insert_windows(rs.get('windows'), None)
        n_seasons = 0
        n_tier_overrides = 0
        for s in rs.get('seasons', []):
            cur = conn.execute(
                'INSERT INTO rate_seasons (plan_id, name, months_json, comment) '
                'VALUES (?, ?, ?, ?)',
                (plan_id, s.get('name'), json.dumps(s.get('months', [])),
                 s.get('_comment')))
            season_id = cur.lastrowid
            n_seasons += 1
            for tier, rate in (s.get('tier_rates') or {}).items():
                conn.execute(
                    'INSERT INTO rate_tiers (plan_id, season_id, tier, rate_cents) '
                    'VALUES (?, ?, ?, ?)', (plan_id, season_id, tier, rate))
                n_tier_overrides += 1
            n_windows += insert_windows(s.get('windows'), season_id)

        n_holidays = 0
        for h in rs.get('holidays', []) or []:
            if isinstance(h, dict):
                hdate, hname = h.get('date'), h.get('name')
            else:
                hdate, hname = h, None
            conn.execute(
                'INSERT OR IGNORE INTO rate_holidays (plan_id, holiday_date, name) '
                'VALUES (?, ?, ?)', (plan_id, hdate, hname))
            n_holidays += 1

        conn.commit()
        return {'plan_id': plan_id, 'name': name,
                'tiers': len(rs.get('tiers') or {}),
                'tier_overrides': n_tier_overrides,
                'windows': n_windows, 'seasons': n_seasons,
                'holidays': n_holidays}
    except Exception:
        conn.rollback()
        raise


def apply_pr25_alters() -> list:
    """ADD battery_kw_direct + active_reserve_pct to system_readings (guarded,
    idempotent). Old reserve columns retained until the post-soak cleanup."""
    conn = db._get_connection()
    cols = [r[1] for r in conn.execute('PRAGMA table_info(system_readings)').fetchall()]
    applied = []
    if 'battery_kw_direct' not in cols:
        conn.execute('ALTER TABLE system_readings ADD COLUMN battery_kw_direct REAL')
        applied.append('battery_kw_direct REAL (register 1048 parallel logging)')
    if 'active_reserve_pct' not in cols:
        conn.execute('ALTER TABLE system_readings ADD COLUMN active_reserve_pct INTEGER')
        applied.append('active_reserve_pct INTEGER (single-reserve replacement; '
                       'self_reserve_pct/tou_reserve_pct retained until cleanup)')
    conn.commit()
    return applied


# =============================================================================
# Main
# =============================================================================

def main():
    ap = argparse.ArgumentParser(description='v4.6 Phase 1 configuration migration')
    ap.add_argument('--battery-array', action='append', default=[],
                    help='array_id that charges the Franklin battery '
                         '(repeatable; required when multiple arrays are defined)')
    ap.add_argument('--dry-run', action='store_true',
                    help='report everything that would happen; write nothing')
    ap.add_argument('--allow-legacy-months', action='store_true',
                    help="downgrade the per-window 'months' rejection (#18) to a "
                         "warning; the key is dropped and the window imported as "
                         "year-round")
    ap.add_argument('--env-path', help='explicit .env path (default: auto-resolve)')
    ap.add_argument('--rate-schedule-path',
                    help='explicit rate_schedule.json path (default: auto-resolve)')
    ap.add_argument('--db-path', help='explicit DB path (testing)')
    args = ap.parse_args()

    if args.db_path:
        db.set_db_path(Path(args.db_path))

    print('=' * 64)
    print(f'v4.6 Phase 1 Configuration Migration ({SCHEMA_VERSION})')
    print('=' * 64)

    env, env_path, file_keys = load_env(args.env_path)
    if env_path:
        print(f'.env source: {env_path} ({len(file_keys)} keys in file)')
    else:
        print('.env source: NOT FOUND — falling back to process environment only')

    unknown = [k for k in file_keys
               if k not in EXCLUDED_ENV
               and not k.startswith('SOLAR_ARRAY')
               and k != 'SOLAR_ARRAYS'
               and k not in {m[0] for m in MAPPINGS}]
    if unknown:
        print(f'NOTE unmapped env keys (left in .env, not migrated): {sorted(unknown)}')

    config_plan = build_config_plan(env)
    battery_arrays = {a.strip().lower() for a in args.battery_array}
    array_plan = build_array_plan(env, battery_arrays)
    multi_unclassified = [a['array_id'] for a in array_plan if a['charges_battery'] is None]
    if multi_unclassified:
        print(f"WARNING arrays with unknown battery relationship: {multi_unclassified} "
              f"— re-run with --battery-array <id> or UPDATE solar_arrays manually; "
              f"settings page will flag these as unreviewed")

    rs, rs_path = load_rate_schedule(args.rate_schedule_path)
    rate_errors, rate_warnings = ([], [])
    if rs:
        rate_errors, rate_warnings = validate_rate_schedule(rs, args.allow_legacy_months)
        print(f'rate schedule source: {rs_path}')
        for w in rate_warnings:
            print(f'WARNING {w}')
        for e in rate_errors:
            print(f'ERROR   {e}')
    else:
        print(f'rate schedule: NOT FOUND at {rs_path} — rate tables will not be populated')

    env_count = sum(1 for p in config_plan if p[5] == 'env')
    default_count = sum(1 for p in config_plan if p[5] == 'default')
    print()
    print(f'app_config plan: {len(config_plan)} keys '
          f'({env_count} from env, {default_count} code defaults, '
          f'{sum(1 for p in config_plan if p[4])} secrets to obscure)')
    print(f'solar_arrays plan: {len(array_plan)} arrays '
          f'({[a["array_id"] for a in array_plan]})')

    if args.dry_run:
        print()
        print('-- dry run: planned app_config rows --')
        for key, value, vtype, category, secret, source, _ in config_plan:
            shown = '********' if secret else value
            print(f'  [{category}] {key} = {shown!r} ({vtype}, {source})')
        print('-- dry run: planned solar_arrays rows --')
        for a in array_plan:
            print(f"  {a['array_id']}: type={a['array_type']} kw={a['capacity_kw']} "
                  f"kwp={a['capacity_kwp']} charges_battery={a['charges_battery']} "
                  f"exports={a['exports']} gateway={a['gateway_id']}")
        if rs and not rate_errors:
            print(f"-- dry run: rate plan '{rs.get('name')}' would import "
                  f"({len(rs.get('tiers') or {})} tiers, "
                  f"{len(rs.get('windows') or [])} year-round windows, "
                  f"{len(rs.get('seasons') or [])} seasons) --")
        print()
        print('DRY RUN — nothing written.')
        return 0

    if rate_errors:
        print()
        print('ABORTING before any writes: rate schedule failed validation (#18). '
              'Fix the schedule (recommended) or re-run with --allow-legacy-months.')
        return 1

    db.init_db()
    config_store.init_config_schema()

    stats = apply_config_plan(config_plan)
    arrays_written = apply_array_plan(array_plan)
    rate_result = import_rate_plan(rs, args.allow_legacy_months) if rs else None
    alters = apply_pr25_alters()

    config_store.app_state.set('schema_version', SCHEMA_VERSION)
    config_store.app_state.set('migration.phase1_completed_at',
                               datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    config_store.app_state.set('migration.env_source', str(env_path or 'process-environment'))
    config_store.app_state.set('migration.rate_source', str(rs_path) if rs else 'none')

    print()
    print('-- results --')
    print(f"app_config: {stats['env']} env values, {stats['default']} defaults, "
          f"{stats['secrets']} secrets obscured, "
          f"{stats['skipped_user']} user-edited rows preserved")
    print(f'solar_arrays: {arrays_written} rows written')
    if rate_result:
        print(f"rate plan '{rate_result['name']}' imported: "
              f"{rate_result['tiers']} base tiers, "
              f"{rate_result['tier_overrides']} seasonal overrides, "
              f"{rate_result['seasons']} seasons, {rate_result['windows']} windows, "
              f"{rate_result['holidays']} holidays")
    if alters:
        for a in alters:
            print(f'system_readings: added {a}')
    else:
        print('system_readings: PR #25 columns already present')
    print(f"app_state: schema_version={SCHEMA_VERSION}")
    print()
    print('Phase 1 migration complete. .env and rate_schedule.json are UNCHANGED '
          'and remain authoritative until the Phase 2 consumer cutover.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
