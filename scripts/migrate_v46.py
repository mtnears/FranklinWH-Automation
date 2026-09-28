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
  2. Maps every .env SETTING (per the v4.7 settings registry) into app_config.
     Secrets and infra never leave .env; unset keys get no row; retired keys
     are not migrated. CARE_RATE/NEM_VERSION import as billing.discount_program
     / billing.export_compensation.
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

# v4.7: the key set, types, classes and import conversions come from the
# settings registry (settings.py). Only setting/detected keys are written.
# Secrets and infra stay in .env and are never copied into the store;
# retired keys are no longer migrated (existing rows are left alone and
# ignored by the accessor). Unset keys get no row, so an install on the
# store reads the same code defaults it read from .env.
import settings as _settings

# Per-array env keys lifted into dedicated columns (rest -> config_json)
ARRAY_COLUMN_KEYS = {'NAME': 'name', 'TYPE': 'array_type', 'CAPACITY_KW': 'capacity_kw'}
# Per-array fields that stay in .env (secrets + infra) — never copied
ARRAY_ENV_ONLY_KEYS = {k.lower() for k, (cls, _, _) in _settings.ARRAY_FIELDS.items()
                       if cls not in _settings.STORE_CLASSES}


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
    """[(key, value, type, category, secret, source, description)] for app_config.
    Registry-driven (settings.store_plan_from_env); only keys set in env."""
    plan = []
    for key, text, vtype, category, desc in _settings.store_plan_from_env(env)['set']:
        plan.append((key, text, vtype, category, 0, 'env', desc))
    return plan


def build_array_plan(env: dict, battery_arrays: set) -> list:
    """One dict per solar array from SOLAR_ARRAYS / SOLAR_ARRAY_<NAME>_* vars."""
    ids = [a.strip().lower() for a in env.get('SOLAR_ARRAYS', '').split(',') if a.strip()]
    solar_export = env.get('SOLAR_EXPORT', 'false').strip().lower() in ('true', '1', 'yes', 'on')
    legacy = {r['array_id']: r for r in _settings.array_plan_from_env(env)}
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
                if lk in ARRAY_ENV_ONLY_KEYS:
                    continue  # secrets/infra stay in .env
                cfg[lk] = fval
        for ck, cv in (legacy.get(aid, {}).get('cfg') or {}).items():
            cfg.setdefault(ck, cv)  # legacy single-array aliases fill gaps only
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
               if not k.startswith('SOLAR_ARRAY')
               and k != 'SOLAR_ARRAYS'
               and k not in _settings.known_env_names()]
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
