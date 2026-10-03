#!/usr/bin/env python3
"""
dump_effective_config.py — effective value of every configuration key under
both authorities (env and store), and the diff between them.

    docker exec franklin-automation python3 /app/scripts/dump_effective_config.py
    ... --all        show matching rows too
    ... --json       machine-readable
    ... --config     also compare every config.Config field

Read-only. Exit code 0 when the store reproduces .env (no DIFF rows), 1 otherwise.
Secrets are never printed: they show as set/unset and always come from .env.

Row status:
  same        identical under both authorities
  equiv       different spelling, same meaning (e.g. PEAK_DAYS everyday vs all)
  env-only    secret / infra / dev-tool: read from the environment either way
  retired     read under env, ignored under store (logged at startup)
  alias       legacy single-array name unset in .env; under store it resolves
              to the matching array field (callers already fall back to it)
  DIFF        the store would change behavior — sync or review before flipping
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings  # noqa: E402

_DAYS_EQUIV = {'everyday': 'all', 'daily': 'all', 'all': 'all', 'every day': 'all'}


def _canon(name, v):
    if v is None:
        return None
    if name == 'PEAK_DAYS':
        return _DAYS_EQUIV.get(str(v).strip().lower(), str(v).strip().lower())
    if name == 'NEM_VERSION':
        lo = str(v).strip().lower()
        return 'nem2' if lo in ('nem1', 'nem2') else lo
    return settings._norm(v)


def _mask(name, v):
    return None if v is None else ('set' if v != '' else '(empty)')


def collect_names():
    names = []
    for ent in settings.REGISTRY:
        for n in ent.env:
            if ent.cls != settings.DEVTOOL and n not in names:
                names.append(n)
    ids = []
    for a in (os.getenv('SOLAR_ARRAYS', '') or '').split(','):
        if a.strip() and a.strip().lower() not in ids:
            ids.append(a.strip().lower())
    settings.force_authority('store')
    for a in settings._snap()['arrays']:
        if a['array_id'].lower() not in ids:
            ids.append(a['array_id'].lower())
    for aid in ids:
        for fld in settings.ARRAY_FIELDS:
            names.append(f'SOLAR_ARRAY_{aid.upper()}_{fld}')
    names.extend(n for n in settings.LEGACY_ARRAY_ALIASES if n not in names)
    return names


def classify(name):
    m = settings._ARRAY_RE.match(name)
    if m:
        return settings.ARRAY_FIELDS[m.group(2)][0], None
    if name in settings.LEGACY_ARRAY_ALIASES:
        return settings.ARRAY_FIELDS[settings.LEGACY_ARRAY_ALIASES[name][1]][0], None
    ent = settings.BY_ENV.get(name)
    return (ent.cls, ent.key) if ent else ('unregistered', None)


def compare_raw():
    rows = []
    names = collect_names()
    settings.force_authority('store')
    for name in names:
        cls, key = classify(name)
        # Unset (no env var / no store row) means the code default applies on
        # that side, so compare effective values. Optional settings have no
        # default: unset is itself the value.
        ent = settings.BY_ENV.get(name)
        dflt = settings.default_text(ent) if (ent and cls in settings.STORE_CLASSES
                                              and not ent.optional) else None
        env_v = os.getenv(name)
        if env_v is None:
            env_v = dflt
        store_v = settings._store_raw(name, dflt)
        secret = cls == settings.SECRET
        if cls in settings.ENV_CLASSES:
            status = 'env-only'
        elif cls == settings.RETIRE:
            status = 'retired' if env_v is not None else 'same'
        elif name in settings.LEGACY_ARRAY_ALIASES and env_v is None:
            status = 'alias'
        elif env_v == store_v:
            status = 'same'
        elif _canon(name, env_v) == _canon(name, store_v):
            status = 'equiv'
        else:
            status = 'DIFF'
        rows.append({'name': name, 'key': key, 'class': cls, 'status': status,
                     'env': _mask(name, env_v) if secret else env_v,
                     'store': _mask(name, store_v) if secret else store_v})
    return rows


def compare_billing():
    rows = []
    for key in settings.IMPORT_CONVERTERS:
        settings.force_authority('env')
        env_v = settings.get(key)
        settings.force_authority('store')
        store_v = settings.get(key)
        rows.append({'name': key, 'key': key, 'class': 'setting',
                     'status': 'same' if env_v == store_v else 'DIFF',
                     'env': env_v, 'store': store_v})
    return rows


def compare_config_fields():
    import config as cfgmod
    out = {}
    for auth in ('env', 'store'):
        settings.force_authority(auth)
        c = cfgmod.Config()
        out[auth] = {k: v for k, v in vars(c).items()}
    secret_fields = {'FRANKLIN_USERNAME', 'FRANKLIN_PASSWORD', 'FRANKLIN_GATEWAY_ID',
                     'SOLAREDGE_USERNAME', 'SOLAREDGE_PASSWORD', 'WEATHER_API_KEY',
                     'PVOUTPUT_API_KEY', 'FORECAST_SOLAR_API_KEY', 'SENDER_PASSWORD'}
    retired = {n for e in settings.REGISTRY if e.cls == settings.RETIRE for n in e.env}
    rows = []
    for k in sorted(out['env']):
        a, b = out['env'][k], out['store'].get(k)
        if a == b:
            status = 'same'
        elif k in retired:
            status = 'retired'
        elif _canon(k, str(a)) == _canon(k, str(b)):
            status = 'equiv'
        else:
            status = 'DIFF'
        if k in secret_fields:
            a, b = _mask(k, a), _mask(k, b)
        rows.append({'name': f'config.{k}', 'key': None, 'class': 'config', 'status': status,
                     'env': str(a), 'store': str(b)})
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--all', action='store_true', help='show matching rows too')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--config', action='store_true', help='also compare config.Config fields')
    args = ap.parse_args()

    flag = settings.read_authority_flag()
    rows = compare_raw() + compare_billing()
    if args.config:
        rows += compare_config_fields()
    settings.force_authority(None)

    diffs = [r for r in rows if r['status'] == 'DIFF']
    if args.json:
        print(json.dumps({'authority_flag': flag, 'rows': rows, 'diff_count': len(diffs)},
                         indent=2, default=str))
        return 1 if diffs else 0

    counts = {}
    for r in rows:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    print(f'authority flag: {flag}')
    print('rows: ' + ', '.join(f'{k}={v}' for k, v in sorted(counts.items())))
    print()
    show = rows if args.all else [r for r in rows if r['status'] not in ('same', 'env-only', 'alias')]
    w = max([len(r['name']) for r in show] + [10])
    print(f"{'status':8} {'name':{w}}  {'env':28} store")
    for r in show:
        print(f"{r['status']:8} {r['name']:{w}}  {str(r['env'])[:28]:28} {r['store']}")
    print()
    print(f'{len(diffs)} DIFF row(s)' + (' — the store reproduces .env' if not diffs else ''))
    return 1 if diffs else 0


if __name__ == '__main__':
    sys.exit(main())
