#!/usr/bin/env python3
"""
config_admin.py — headless config store administration (v4.7)

    python3 /app/scripts/config_admin.py status
    python3 /app/scripts/config_admin.py sync [--dry-run] [--overwrite-user] [--skip-rates]
    python3 /app/scripts/config_admin.py authority store [--force]
    python3 /app/scripts/config_admin.py authority env

sync        Make the store reproduce the current environment (.env via compose):
            the headless equivalent of the review's "accept all".
              - every registry setting set in env is written (source='env');
                rows the user edited (source='user') are kept unless
                --overwrite-user
              - settings unset in env lose their env/default rows, so the
                store falls back to the same code defaults env does
              - CARE_RATE / NEM_VERSION import as billing.discount_program /
                billing.export_compensation
              - solar_arrays fields refreshed from SOLAR_ARRAY_<ID>_*;
                battery/export classification and DC kWp are kept
              - secrets are removed from the store (they live in .env only)
              - rate_schedule.json re-imported into the rate tables
            Retired keys already in the store are left alone (ignored).

authority   Set app_state['config.authority']. Takes effect on the next
            restart (docker compose restart franklin-automation; no rebuild).
            'store' refuses unless the rate plan resolves, the effective-config
            dump shows no DIFF rows, and we are not near peak (--force skips
            the peak guard only). 'env' is the rollback and always allowed.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings  # noqa: E402
import db  # noqa: E402
import config_store  # noqa: E402

# Keys that were migrated into the store as settings before v4.7 but are
# secrets under the registry.
LEGACY_SECRET_KEYS = {'solaredge.username'}


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _delete_config_rows(keys):
    if not keys:
        return
    with db._safe_write() as conn:
        conn.executemany("DELETE FROM app_config WHERE scope = 'global' AND key = ?",
                         [(k,) for k in keys])


# =============================================================================
# sync
# =============================================================================

def cmd_sync(args):
    env = dict(os.environ)
    plan = settings.store_plan_from_env(env)
    existing = {r['key']: r for r in db.query("SELECT * FROM app_config WHERE scope = 'global'")}

    writes, kept_user, removals = [], [], []
    for key, text, vtype, category, desc in plan['set']:
        cur = existing.get(key)
        if cur and cur['source'] == 'user' and not args.overwrite_user:
            if cur['value'] != text:
                kept_user.append((key, cur['value'], text))
            continue
        if cur and cur['value'] == text and cur['value_type'] == vtype and cur['source'] == 'env':
            continue
        writes.append((key, text, vtype, category, desc, cur['value'] if cur else None))
    for key in plan['unset']:
        cur = existing.get(key)
        if cur and cur['source'] in ('env', 'default'):
            removals.append((key, cur['value'], cur['source']))

    secret_rows = [k for k, r in existing.items()
                   if r['is_secret'] or k in LEGACY_SECRET_KEYS]

    # arrays
    array_changes = []
    store_arrays = {a['array_id']: a for a in config_store.solar_arrays.all(enabled_only=False)}
    env_only = {name for _, (cls, where, name) in settings.ARRAY_FIELDS.items()
                if cls not in settings.STORE_CLASSES}
    for row in settings.array_plan_from_env(env):
        aid = row['array_id']
        cur = store_arrays.get(aid)
        cur_cfg = dict((cur or {}).get('config') or {})
        new_cfg = {k: v for k, v in cur_cfg.items() if k not in env_only}
        new_cfg.update(row['cfg'])
        fields = dict(row['cols'])
        changed = {}
        for col, v in fields.items():
            if cur is None or cur.get(col) != v:
                changed[col] = ((cur or {}).get(col), v)
        if new_cfg != cur_cfg:
            changed['config'] = (sorted(cur_cfg), sorted(new_cfg))
        if changed or cur is None:
            fields['config_json'] = json.dumps(new_cfg) if new_cfg else None
            array_changes.append((aid, fields, changed, cur is None))
    stray = [a for a in store_arrays if a not in {r['array_id'] for r in
                                                   settings.array_plan_from_env(env)}]

    print(f"sync plan ({'dry run' if args.dry_run else 'applying'}):")
    for key, text, vtype, _, _, old in writes:
        print(f'  set    {key} = {text!r}' + (f'   (was {old!r})' if old is not None else '   (new)'))
    for key, old, src in removals:
        print(f'  unset  {key}   (was {old!r}, source={src}; code default applies)')
    for key, cur, want in kept_user:
        print(f'  KEEP   {key} = {cur!r}   (user-edited; env has {want!r}; --overwrite-user to replace)')
    for k in sorted(secret_rows):
        print(f'  purge  {k}   (secret; stays in .env)')
    for aid, fields, changed, new in array_changes:
        what = ', '.join(f'{c}: {o!r} -> {n!r}' for c, (o, n) in changed.items())
        print(f"  array  {aid}{' (NEW — classify charges_battery/exports)' if new else ''}: {what}")
    for aid in stray:
        print(f'  note   array {aid} is in the store but not in SOLAR_ARRAYS (left as is)')

    rs = rs_path = None
    if not args.skip_rates:
        import migrate_v46
        rs, rs_path = migrate_v46.load_rate_schedule()
        if rs:
            errs, warns = migrate_v46.validate_rate_schedule(rs, False)
            for w in warns:
                print(f'  WARN   rate schedule: {w}')
            if errs:
                for e in errs:
                    print(f'  ERROR  rate schedule: {e}')
                print('ABORT: rate schedule failed validation; nothing written.')
                return 1
            print(f"  rates  re-import '{rs.get('name')}' from {rs_path}")
        else:
            print(f'  rates  rate_schedule.json not found ({rs_path}); rate tables untouched')

    if args.dry_run:
        print('\nDRY RUN — nothing written.')
        return 0

    for key, text, vtype, category, desc, _ in writes:
        config_store.app_config.set(key, text, value_type=vtype, category=category,
                                    description=desc, is_secret=False, source='env')
    _delete_config_rows([k for k, _, _ in removals])
    _delete_config_rows(secret_rows)
    for aid, fields, _, _ in array_changes:
        config_store.solar_arrays.upsert(aid, **fields)
    if rs:
        import migrate_v46
        r = migrate_v46.import_rate_plan(rs, False)
        print(f"  rates  imported: {r['tiers']} tiers, {r['seasons']} seasons, {r['windows']} windows")
    config_store.app_state.set('config.synced_at', _now())
    print(f'\nsync complete: {len(writes)} set, {len(removals)} unset, '
          f'{len(secret_rows)} secrets purged, {len(array_changes)} array rows updated.')
    print('Next: dump_effective_config.py should report 0 DIFF rows.')
    return 0


# =============================================================================
# authority
# =============================================================================

def _peak_guard():
    """Refuse a flip from 20 min before peak to 5 min after it ends."""
    import rate_config
    now = datetime.now()
    w = rate_config.peak_window_for_date(now.strftime('%Y-%m-%d'))
    if not w or not w.applies_on(now):
        return None
    start = now.replace(hour=w.start_hour, minute=0, second=0, microsecond=0)
    end = now.replace(hour=w.end_hour, minute=0, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
    if start - timedelta(minutes=20) <= now <= end + timedelta(minutes=5):
        return f'within the peak guard ({w.start}-{w.end}, -20/+5 min)'
    return None


def cmd_authority(args):
    target = args.value
    current = settings.read_authority_flag()
    if target == 'store':
        import rate_config
        if rate_config.peak_window_for_date(datetime.now().strftime('%Y-%m-%d')) is None:
            print('REFUSED: no peak window resolves from the rate plan; run sync first.')
            return 1
        import dump_effective_config as dump
        rows = dump.compare_raw() + dump.compare_billing()
        settings.force_authority(None)
        diffs = [r for r in rows if r['status'] == 'DIFF']
        if diffs:
            print(f'REFUSED: {len(diffs)} DIFF row(s) between env and store:')
            for r in diffs:
                print(f"  {r['name']}: env={r['env']!r} store={r['store']!r}")
            print('Run config_admin.py sync (or review) first.')
            return 1
        guard = _peak_guard()
        if guard and not args.force:
            print(f'REFUSED: {guard}. Try again later or pass --force.')
            return 1
    if not config_store.app_state.set(settings.AUTHORITY_KEY, target):
        print('FAILED to write app_state.')
        return 1
    config_store.app_state.set('config.authority_changed_at', _now())
    print(f'config.authority: {current} -> {target}')
    print('Takes effect on restart: docker compose restart franklin-automation')
    return 0


def cmd_status(args):
    flag = settings.read_authority_flag()
    print(f"config.authority flag : {flag}")
    print(f"changed at            : {config_store.app_state.get('config.authority_changed_at', '-')}")
    print(f"last sync             : {config_store.app_state.get('config.synced_at', '-')}")
    print(f"schema_version        : {config_store.app_state.get('schema_version', '-')}")
    import dump_effective_config as dump
    rows = dump.compare_raw() + dump.compare_billing()
    settings.force_authority(None)
    diffs = [r for r in rows if r['status'] == 'DIFF']
    print(f"env vs store          : {len(diffs)} DIFF row(s)"
          + (' (run dump_effective_config.py for detail)' if diffs else ''))
    return 0


def main():
    ap = argparse.ArgumentParser(description='v4.7 config store administration')
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status')
    s = sub.add_parser('sync')
    s.add_argument('--dry-run', action='store_true')
    s.add_argument('--overwrite-user', action='store_true')
    s.add_argument('--skip-rates', action='store_true')
    a = sub.add_parser('authority')
    a.add_argument('value', choices=('env', 'store'))
    a.add_argument('--force', action='store_true', help='skip the peak guard')
    args = ap.parse_args()
    writing = (args.cmd == 'authority') or (args.cmd == 'sync' and not args.dry_run)
    if writing:
        db.init_db()
        config_store.init_config_schema()
    return {'status': cmd_status, 'sync': cmd_sync, 'authority': cmd_authority}[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
