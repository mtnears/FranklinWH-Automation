#!/usr/bin/env python3
"""
config_commit.py — v4.7 review commit, rollback and restart (Phase 3)

The review page (web/config_review.html) sends only what the user did:
    {"edits":   {key: text, ...},          # typed values, flag answers, rate/window edits
     "choices": {key: "env" | "store"}}    # conflict picks (absent = .env)
Everything else is rebuilt here from the same payload the page was drawn
from (config_review.review_payload), so the server decides the final value
of every setting; the browser cannot write keys the review does not own.

commit():
  1. Guards before anything is written:
       - not within the peak guard (-20/+5 min, config_admin._peak_guard)
       - every submitted value validates against settings.REGISTRY
         (type, choices, vmin/vmax) and every array is classified
       - rate/window edits are numbers/times on the active plan
  2. Snapshot of the config tables to data/backups/config-snapshot-*.json
     (restore: config_admin.py restore <file>)
  3. One transaction: app_config rows (source='user'; values at the code
     default or blank are removed, "unset = no row"), solar_arrays, rate
     edits (plan marked source='review' so sync leaves it alone).
  4. Verify: the store now equals exactly what was submitted, and the rate
     plan resolves today's prices and peak window. Any failure restores
     the snapshot and nothing changes.
  5. config.authority = store, review.completed_* stamps, restart.

There is no env-vs-store "zero DIFF" check here on purpose: a user who edits
a value in the review makes the store differ from .env by design. The guard
is "the store holds what was reviewed", checked after the write.

Restart: the scheduler re-execs itself in place (os.execv; it is PID 1 in
the container) from its main loop, between jobs. The container never stops,
so this does not depend on the compose restart policy, and the new process
reads the authority flag fresh.
"""

import json
import os
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import settings

SNAPSHOT_TABLES = ('app_config', 'solar_arrays', 'rate_plans', 'rate_seasons',
                   'rate_tiers', 'rate_windows', 'rate_holidays')
STATE_PREFIXES = ('config.', 'review.')

REVIEW_COMPLETED_AT = 'review.completed_at'
REVIEW_COMPLETED_VERSION = 'review.completed_version'
REVIEW_LAST_SNAPSHOT = 'review.last_snapshot'

PROCESS_STARTED_AT = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

_restart = {'reason': None, 'requested_at': None}
_lock = threading.Lock()

_TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
_HOST_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.\-]*$')


class CommitRefused(Exception):
    def __init__(self, code: int, reason: str, errors: List[str] = None):
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.errors = errors or []


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _version() -> str:
    try:
        from version import get_version
        return get_version()
    except Exception:
        return 'unknown'


def _backup_dir() -> Path:
    d = settings.db_path().parent / 'backups'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rw_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(settings.db_path()), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=30000')
    return conn


# =============================================================================
# Restart (consumed by scheduler.main)
# =============================================================================

def request_restart(reason: str):
    with _lock:
        _restart['reason'] = reason
        _restart['requested_at'] = _now()


def restart_pending() -> Optional[str]:
    return _restart['reason']


# =============================================================================
# State
# =============================================================================

def _state_get(key: str) -> Optional[str]:
    try:
        conn = sqlite3.connect(f'file:{settings.db_path()}?mode=ro', uri=True, timeout=5)
        try:
            row = conn.execute('SELECT value FROM app_state WHERE key = ?', (key,)).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except Exception:
        return None


def _state_set(conn: sqlite3.Connection, key: str, value: str):
    conn.execute('INSERT OR REPLACE INTO app_state (key, value, updated_at) VALUES (?, ?, ?)',
                 (key, value, _now()))


def setup_complete() -> bool:
    """False on a fresh install: no rate plan in the store and no
    rate_schedule.json, so the engine has nothing to run on and observes
    only until the review commits (T037)."""
    try:
        import rate_schedule
        return rate_schedule.schedule_available(str(settings.db_path().parent / 'rate_schedule.json'))
    except Exception:
        return True


def status() -> Dict[str, Any]:
    return {
        'setup_complete': setup_complete(),            # False: fresh install, engine observes only
        'authority': settings.authority(),              # what this process runs on
        'authority_flag': settings.read_authority_flag(),  # what the next start uses
        'review_completed_at': _state_get(REVIEW_COMPLETED_AT),
        'review_completed_version': _state_get(REVIEW_COMPLETED_VERSION),
        'last_snapshot': _state_get(REVIEW_LAST_SNAPSHOT),
        'process_started_at': PROCESS_STARTED_AT,
        'restart_pending': restart_pending(),
        'version': _version(),
    }


# =============================================================================
# Snapshot / restore
# =============================================================================

def take_snapshot(label: str = 'review') -> Path:
    conn = sqlite3.connect(f'file:{settings.db_path()}?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        snap = {'taken_at': _now(), 'label': label, 'version': _version(), 'tables': {},
                'app_state': []}
        for t in SNAPSHOT_TABLES:
            try:
                snap['tables'][t] = [dict(r) for r in conn.execute(f'SELECT * FROM {t}')]
            except sqlite3.OperationalError:
                snap['tables'][t] = None
        snap['app_state'] = [dict(r) for r in conn.execute('SELECT * FROM app_state')
                             if r['key'].startswith(STATE_PREFIXES)]
    finally:
        conn.close()
    path = _backup_dir() / f"config-snapshot-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{label}.json"
    path.write_text(json.dumps(snap, indent=1, default=str))
    return path


def restore_snapshot(path: Path, conn: sqlite3.Connection = None) -> Dict[str, int]:
    """Replace the config tables (and config./review. app_state keys) with the
    snapshot's contents. Readings and every other table are untouched."""
    snap = json.loads(Path(path).read_text())
    own = conn is None
    conn = conn or _rw_connect()
    counts = {}
    try:
        conn.execute('BEGIN IMMEDIATE')
        for t, rows in snap['tables'].items():
            if rows is None:
                continue
            conn.execute(f'DELETE FROM {t}')
            for r in rows:
                cols = list(r)
                conn.execute(f"INSERT INTO {t} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                             [r[c] for c in cols])
            counts[t] = len(rows)
        for p in STATE_PREFIXES:
            conn.execute('DELETE FROM app_state WHERE key LIKE ?', (p + '%',))
        for r in snap.get('app_state', []):
            conn.execute('INSERT OR REPLACE INTO app_state (key, value, updated_at) VALUES (?, ?, ?)',
                         (r['key'], r['value'], r.get('updated_at')))
        counts['app_state'] = len(snap.get('app_state', []))
        conn.execute('COMMIT')
    except Exception:
        conn.execute('ROLLBACK')
        raise
    finally:
        if own:
            conn.close()
    return counts


# =============================================================================
# Validation
# =============================================================================

def _blank(v) -> bool:
    return v is None or str(v).strip() == ''


def _store_text(ent: settings.Entry, text: str) -> Tuple[Optional[str], Optional[str]]:
    """Validate one submitted value. Returns (store_text, error)."""
    s = str(text).strip()
    typ = ent.type
    if typ == 'bool':
        lo = s.lower()
        if lo in ('true', '1', 'yes', 'on'):
            return 'true', None
        if lo in ('false', '0', 'no', 'off'):
            return 'false', None
        return None, f'expected on/off, got {s!r}'
    if ent.choices and s not in ent.choices:
        if len(ent.choices) > 12:
            return None, f'{s!r} is not one of the listed options'
        return None, f"{s!r} is not one of {', '.join(ent.choices)}"
    if typ in ('int', 'float', 'float?'):
        try:
            f = float(s)
        except ValueError:
            return None, f'{s!r} is not a number'
        if typ == 'int':
            if f != int(f):
                return None, f'{s!r} must be a whole number'
            f = int(f)
        if ent.vmin is not None and f < ent.vmin:
            return None, f'{s} is below the minimum {ent.vmin:g}'
        if ent.vmax is not None and f > ent.vmax:
            return None, f'{s} is above the maximum {ent.vmax:g}'
        return (str(f) if typ == 'int' else s), None
    if typ == 'list':
        items = settings.coerce(s, 'list', [])
        return json.dumps([str(x) for x in items]), None
    if ent.key == 'modbus.host' and not _HOST_RE.match(s):
        return None, f'{s!r} is not an IP address or host name'
    return s, None


def _same(typ: str, a: Optional[str], b: Optional[str]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if typ == 'list':
        return settings.coerce(a, 'list', []) == settings.coerce(b, 'list', [])
    return settings._norm(a) == settings._norm(b)


def _final(row: dict, edits: dict, choices: dict):
    key = row['key']
    if key in edits:
        v = edits[key]
    elif row.get('status') == 'conflict':
        c = choices.get(key) or row.get('prefer') or 'env'
        v = row.get('store') if c == 'store' else row.get('env')
    else:
        v = row.get('proposed')
    return None if _blank(v) else str(v).strip()


# =============================================================================
# Rate plan editor (T033)
# =============================================================================

_DAYS = ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')
_TIER_RE = re.compile(r'^[a-z][a-z0-9_]{0,30}$')
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def _spans(start: str, end: str):
    """Minute ranges covered by a window; a midnight-crossing window is two."""
    s, e = _mins(start), _mins(end)
    if e == 0:
        e = 1440
    if s < e:
        return [(s, e)]
    return [(s, 1440), (0, e)]


def validate_rate_plan(raw, active_ids: set) -> Tuple[Optional[dict], List[str]]:
    """Check a plan from the review's editor against the rules a plan must meet
    for the engine to run on it (SPEC_2026-10-18_v470_required_settings.md):

      - every month of the year in exactly one season
      - every season prices off_peak, the default tier, the holiday tier and
        every tier one of its windows uses; prices 0 < ¢ < 1000
      - windows are HH:MM, not empty, days a subset of mon..sun, and no two
        windows in a season overlap on the same day
      - no base-tier inheritance: a season's prices are its own

    Returns (normalized plan, errors). The plan is written only if errors is empty.
    """
    errors: List[str] = []
    E = errors.append
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as e:
            return None, [f'Rates: plan is not valid JSON ({e.msg})']
    if not isinstance(raw, dict):
        return None, ['Rates: plan must be an object']

    pid = raw.get('id')
    if pid not in (None, ''):
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None, ['Rates: plan id must be a number']
        if pid not in active_ids:
            return None, [f'Rates: plan {pid} is not an active plan']
    else:
        pid = None

    name = str(raw.get('name') or '').strip()
    if not name:
        E('Rates: the plan needs a name (the rate schedule on your bill, e.g. "PG&E EV2-A")')
    default_tier = str(raw.get('default_tier') or 'off_peak').strip()
    holiday_tier = str(raw.get('holiday_tier') or default_tier).strip()
    for t, what in ((default_tier, 'default tier'), (holiday_tier, 'holiday tier')):
        if not _TIER_RE.match(t):
            E(f'Rates: {what} {t!r} is not a valid tier name (lowercase letters, digits, _)')

    seasons_in = raw.get('seasons')
    if not isinstance(seasons_in, list) or not seasons_in:
        E('Rates: the plan needs at least one season (a single season covering all 12 months is fine)')
        seasons_in = []

    month_owner: Dict[int, str] = {}
    names_seen = set()
    seasons: List[dict] = []
    for i, s in enumerate(seasons_in, 1):
        if not isinstance(s, dict):
            E(f'Rates: season {i} is malformed')
            continue
        sname = str(s.get('name') or '').strip()
        label = sname or f'season {i}'
        if not sname:
            E(f'Rates: season {i} needs a name')
        elif sname.lower() in names_seen:
            E(f'Rates: two seasons are named {sname!r}')
        names_seen.add(sname.lower())

        months = []
        for m in (s.get('months') or []):
            try:
                mi = int(m)
            except (TypeError, ValueError):
                E(f'Rates: {label}: month {m!r} is not a number')
                continue
            if not 1 <= mi <= 12:
                E(f'Rates: {label}: month {mi} is outside 1–12')
            elif mi in month_owner:
                E(f'Rates: month {mi} is in both {month_owner[mi]!r} and {label!r}')
            else:
                month_owner[mi] = label
                months.append(mi)
        if not months:
            E(f'Rates: {label} has no months')

        tiers: Dict[str, float] = {}
        for t, c in (s.get('tiers') or {}).items():
            t = str(t).strip()
            if not _TIER_RE.match(t):
                E(f'Rates: {label}: tier name {t!r} is not valid (lowercase letters, digits, _)')
                continue
            if c in (None, ''):
                E(f'Rates: {label}: {t.replace("_", " ")} has no price')
                continue
            try:
                cents = float(c)
            except (TypeError, ValueError):
                E(f'Rates: {label}: {t.replace("_", " ")} price {c!r} is not a number')
                continue
            if not 0 < cents < 1000:
                E(f'Rates: {label}: {t.replace("_", " ")} price {cents:g}¢ is outside 0–1000¢/kWh')
                continue
            tiers[t] = round(cents, 4)
        if 'off_peak' not in tiers:
            E(f'Rates: {label} has no off-peak price')
        if default_tier not in tiers and _TIER_RE.match(default_tier):
            E(f'Rates: {label} has no price for the default tier {default_tier!r}')
        if holiday_tier not in tiers and _TIER_RE.match(holiday_tier) and holiday_tier != default_tier:
            E(f'Rates: {label} has no price for the holiday tier {holiday_tier!r}')

        windows: List[dict] = []
        for j, w in enumerate(s.get('windows') or [], 1):
            if not isinstance(w, dict):
                E(f'Rates: {label}: window {j} is malformed')
                continue
            wt = str(w.get('tier') or '').strip()
            start, end = str(w.get('start') or '').strip(), str(w.get('end') or '').strip()
            days = [str(d).strip().lower() for d in (w.get('days') or [])]
            wl = f"{label}: {wt.replace('_', ' ') or 'window'} {start or '?'}–{end or '?'}"
            if wt not in tiers:
                E(f'Rates: {wl} uses tier {wt!r}, which has no price in {label}')
            if not _TIME_RE.match(start) or not _TIME_RE.match(end):
                E(f'Rates: {wl}: times must be HH:MM')
                continue
            if start == end:
                E(f'Rates: {wl} is empty (start equals end)')
                continue
            if not days or any(d not in _DAYS for d in days):
                E(f'Rates: {wl}: days must be some of mon, tue, wed, thu, fri, sat, sun')
                continue
            days = [d for d in _DAYS if d in days]
            windows.append({'tier': wt, 'start': start, 'end': end, 'days': days})
        # Overlap: two windows active at the same minute on the same day
        for a in range(len(windows)):
            for b in range(a + 1, len(windows)):
                wa, wb = windows[a], windows[b]
                if not set(wa['days']) & set(wb['days']):
                    continue
                for s1, e1 in _spans(wa['start'], wa['end']):
                    for s2, e2 in _spans(wb['start'], wb['end']):
                        if s1 < e2 and s2 < e1:
                            E(f"Rates: {label}: {wa['tier'].replace('_', ' ')} {wa['start']}–{wa['end']} "
                              f"overlaps {wb['tier'].replace('_', ' ')} {wb['start']}–{wb['end']}")
                            break
                    else:
                        continue
                    break
        seasons.append({'name': sname, 'months': sorted(months), 'tiers': tiers, 'windows': windows})

    if seasons_in:
        missing = sorted(set(range(1, 13)) - set(month_owner))
        if missing:
            mn = ['', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
            E('Rates: no season covers ' + ', '.join(mn[m] for m in missing))

    holidays = None
    if raw.get('holidays') is not None:
        holidays = []
        for h in raw.get('holidays') or []:
            hd = str(h).strip()
            if not _DATE_RE.match(hd):
                E(f'Rates: holiday {h!r} must be YYYY-MM-DD')
            else:
                holidays.append(hd)

    net_metering = str(raw.get('net_metering') or '').strip()
    export_capable = str(raw.get('export_capable', False)).strip().lower() in ('1', 'true', 'yes', 'on')
    has_peak_window = any(w['tier'] == 'peak' for s in seasons for w in s['windows'])
    return ({'id': pid, 'name': name, 'default_tier': default_tier, 'holiday_tier': holiday_tier,
             'export_capable': 1 if export_capable else 0, 'net_metering': net_metering,
             'seasons': seasons, 'holidays': holidays, 'has_peak_window': has_peak_window},
            errors)


# =============================================================================
# Plan
# =============================================================================

def build_plan(edits: Dict[str, Any], choices: Dict[str, str]) -> Dict[str, Any]:
    import config_review

    edits = {str(k): (None if v is None else str(v)) for k, v in (edits or {}).items()}
    choices = {str(k): str(v) for k, v in (choices or {}).items() if str(v) in ('env', 'store')}
    payload = config_review.review_payload()
    rows: Dict[str, dict] = {}
    array_cards = []
    rate_cards = []
    for sc in payload['screens']:
        for card in sc['cards']:
            if card.get('array_id'):
                array_cards.append(card)
            if card.get('kind') == 'rate_plan':
                rate_cards.append(card)
            for r in card['rows']:
                if r.get('key'):
                    rows[r['key']] = r
    store = config_review._store_rows()
    errors: List[str] = []
    known = set(rows)

    # ---- settings -----------------------------------------------------------
    settings_plan = []          # (key, store_text|None, value_type, category, description)
    for ent in settings.REGISTRY:
        if ent.cls not in settings.STORE_CLASSES or not ent.key:
            continue
        row = rows.get(ent.key) or config_review.setting_row(ent, store)
        known.add(ent.key)
        v = _final(row, edits, choices)
        text = None
        if v is not None:
            text, err = _store_text(ent, v)
            if err:
                errors.append(f"{row.get('label', ent.key)} — {err}")
                continue
        default = config_review._default_text(ent)
        if text is not None and not ent.optional and default is not None and \
                _same(ent.type, settings._row_text({'value': text, 'value_type': ent.store_type}),
                      default):
            text = None         # left at the code default: no row
        settings_plan.append((ent.key, text, ent.store_type,
                              ent.category or ent.key.split('.')[0], ent.help))

    # Site-specific values (no built-in default) must be set when needed.
    final_text = {k: t for k, t, *_ in settings_plan}
    for ent in settings.REGISTRY:
        if not ent.required_if or ent.key not in final_text or final_text[ent.key] is not None:
            continue
        if ent.required_if != 'always':
            ctl = settings.BY_KEY.get(ent.required_if)
            ctl_text = final_text.get(ent.required_if)
            if ctl_text is None and ctl is not None:
                ctl_text = config_review._default_text(ctl)
            if settings._bool_text(ctl_text or 'false') != 'true':
                continue
        label = (rows.get(ent.key) or {}).get('label', ent.key)
        cond = '' if ent.required_if == 'always' else \
            f" while {config_review.LABELS.get(ent.required_if, ent.required_if)} is on"
        errors.append(f'{label} — required{cond}; it is specific to your site and has no '
                      f'built-in default')

    # Fresh install: values whose code default is a guess must be chosen (T037).
    # The review proposes nothing for them until the first commit; a detected
    # battery value counts as chosen.
    for ent in settings.REGISTRY:
        if not ent.confirm or not ent.key or ent.key not in final_text:
            continue
        row = rows.get(ent.key) or {}
        if not row.get('confirm') or row.get('detected') not in (None, ''):
            continue
        if _final(row, edits, choices) is not None:
            continue   # chosen (even if it equals the default and so stores no row)
        default = config_review._default_text(ent)
        errors.append(f"{row.get('label', ent.key)} — choose a value; the built-in default "
                      f"({default}) is a guess that is wrong for many sites")

    # Secrets live in .env, but a feature that needs one cannot be committed
    # without it (T037).
    def _feature_on(key: str) -> bool:
        if key == 'always':
            return True
        t = final_text.get(key)
        if t is None:
            ctl = settings.BY_KEY.get(key)
            t = config_review._default_text(ctl) if ctl else 'false'
        return settings._bool_text(t or 'false') == 'true'
    for ent in settings.REGISTRY:
        if ent.cls != settings.SECRET or not ent.required_if or not ent.env:
            continue
        if _feature_on(ent.required_if) and not os.environ.get(ent.env[0]):
            cond = '' if ent.required_if == 'always' else \
                f" while {config_review.LABELS.get(ent.required_if, ent.required_if)} is on"
            errors.append(f"{config_review.SECRET_LABELS.get(ent.env[0], ent.env[0])} — required{cond}: "
                          f"add {ent.env[0]}=... to .env, run docker compose up -d, then commit")

    # ---- arrays ---------------------------------------------------------------
    arrays_plan = []
    for card in array_cards:
        aid = card['array_id']
        cols, cfg = {}, {}
        flags = {}
        for r in card['rows']:
            key = r.get('key') or ''
            if r.get('kind') == 'flag':
                v = edits.get(key, r.get('value'))
                if v in (None, ''):
                    errors.append(f"{card['title']}: answer “{r['label']}” on the Solar arrays step")
                    continue
                flags[key.rsplit('.', 1)[1]] = 1 if str(v).strip().lower() in ('1', 'true', 'yes') else 0
                continue
            if r.get('kind') != 'setting' or not key.startswith(f'array.{aid}.'):
                continue
            name = key.split('.', 2)[2]
            v = r.get('proposed') if r.get('readonly') else _final(r, edits, choices)
            if name == 'capacity_kw' and v is not None:
                try:
                    f = float(v)
                    if not 0 < f < 1000:
                        raise ValueError
                    v = f
                except ValueError:
                    errors.append(f"{card['title']} — AC capacity {v!r} must be a number of kW")
                    continue
            if name == 'ip' and v is not None and not _HOST_RE.match(str(v)):
                errors.append(f"{card['title']} — gateway IP {v!r} is not an IP address or host name")
                continue
            where = next(w for _, (_, w, n) in settings.ARRAY_FIELDS.items() if n == name)
            (cols if where == 'col' else cfg)[name] = v
        # An array the engine can use has a name, a type, an AC size and the
        # connection field its type needs (T037)
        atype = str(cols.get('array_type') or '').lower()
        if not atype or not cols.get('name') or cols.get('capacity_kw') is None:
            errors.append(f"{card['title']} — needs a name, a type and an AC capacity (kW)")
        elif atype not in ('enphase', 'solaredge'):
            errors.append(f"{card['title']} — type must be enphase or solaredge")
        elif atype == 'enphase' and (not cfg.get('ip') or not cfg.get('serial')):
            errors.append(f"{card['title']} — an Enphase array needs the Envoy IP and serial")
        elif atype == 'solaredge' and not cfg.get('site_id'):
            errors.append(f"{card['title']} — a SolarEdge array needs the monitoring site ID")
        arrays_plan.append({'array_id': aid, 'cols': cols, 'cfg': cfg, 'flags': flags,
                            'title': card['title']})
        known.update(r.get('key') for r in card['rows'] if r.get('key'))

    # The forecast DC size is defined as the battery-connected array's; keep the
    # array row's capacity_kwp (used for SOLAR_CAPACITY_KW) in step with it,
    # as migrate_v46 does. Only unambiguous with exactly one charging array.
    charging = [a for a in arrays_plan if a['flags'].get('charges_battery') == 1]
    if len(charging) == 1:
        kwp = next((t for k, t, *_ in settings_plan if k == 'forecast.house_kwp'), None)
        kwp = kwp if kwp is not None else config_review._default_text(settings.BY_KEY['forecast.house_kwp'])
        try:
            charging[0]['cols']['capacity_kwp'] = float(kwp) if kwp is not None else None
        except ValueError:
            pass
        charging[0]['gateway_id'] = 'agate_main'

    # ---- rates ----------------------------------------------------------------
    rates_plan = {'tiers': [], 'windows': [], 'plans': set(), 'replace': None}
    if 'rate_plan' in edits:
        # Full plan from the review's plan editor (T033): seasons, tiers and
        # windows are replaced wholesale; in-place rate./win. edits are ignored.
        known.add('rate_plan')
        rp, rp_errors = validate_rate_plan(edits.get('rate_plan'),
                                           {c['plan']['id'] for c in rate_cards})
        errors.extend(rp_errors)
        if not rp_errors:
            rates_plan['replace'] = rp
        edits = {k: v for k, v in edits.items() if not k.startswith(('rate.', 'win.'))}
    if not rate_cards and not rates_plan['replace']:
        # No plan in the store: an un-migrated install still has rate_schedule.json
        # (imported at commit); a fresh install must build the plan on the Rates step
        try:
            import migrate_v46
            has_json = bool(migrate_v46.load_rate_schedule()[0])
        except Exception:
            has_json = False
        if not has_json:
            errors.append('Rates — no rate plan yet. Build it on the Rates step from the rate '
                          'schedule on your bill (seasons, prices, time windows).')
    plan_ids = {c['plan']['id'] for c in rate_cards}
    season_ids = {s['id']: c['plan']['id'] for c in rate_cards for s in c['plan']['seasons']}
    window_ids = {w['id']: (c['plan']['id'], w) for c in rate_cards
                  for s in c['plan']['seasons'] for w in s['windows']}
    for k, v in edits.items():
        if k.startswith(('rate.', 'win.')):
            known.add(k)
        if k.startswith('rate.'):
            parts = k.split('.')
            if len(parts) != 4:
                errors.append(f'Rates: malformed edit {k!r}')
                continue
            _, pid, sid, tier = parts
            try:
                pid, sid = int(pid), int(sid)
                cents = float(v)
            except (TypeError, ValueError):
                errors.append(f'Rates: {tier} price {v!r} is not a number')
                continue
            if pid not in plan_ids or season_ids.get(sid) != pid:
                errors.append(f'Rates: {k} is not on an active plan')
                continue
            if not 0 < cents < 1000:
                errors.append(f'Rates: {tier} price {cents:g}¢ is outside 0–1000¢/kWh')
                continue
            rates_plan['tiers'].append((pid, sid, tier, round(cents, 4)))
            rates_plan['plans'].add(pid)
        elif k.startswith('win.'):
            parts = k.split('.')
            if len(parts) != 3 or parts[2] not in ('start', 'end'):
                errors.append(f'Rates: malformed edit {k!r}')
                continue
            try:
                wid = int(parts[1])
            except ValueError:
                errors.append(f'Rates: malformed edit {k!r}')
                continue
            if wid not in window_ids:
                errors.append(f'Rates: window {wid} is not on an active plan')
                continue
            if not _TIME_RE.match(str(v or '')):
                errors.append(f"Rates: {window_ids[wid][1]['tier']} {parts[2]} time {v!r} must be HH:MM")
                continue
            rates_plan['windows'].append((wid, parts[2], str(v)))
            rates_plan['plans'].add(window_ids[wid][0])
    # A window whose start equals its end would be empty
    win_after = {wid: dict(start=w['start_time'], end=w['end_time']) for wid, (_, w) in window_ids.items()}
    for wid, which, v in rates_plan['windows']:
        win_after[wid][which] = v
    for wid, se in win_after.items():
        if se['start'] == se['end']:
            errors.append(f"Rates: {window_ids[wid][1]['tier']} window {se['start']}–{se['end']} is empty")

    unknown = sorted(k for k in list(edits) + list(choices) if k not in known)
    if unknown:
        errors.append('Not part of the review: ' + ', '.join(unknown[:10]))

    env_diffs = []
    for key, text, vtype, _, _ in settings_plan:
        ent = settings.BY_KEY[key]
        env_v = config_review._env_text(ent)
        if env_v is not None and not _same(ent.type, settings._row_text(
                {'value': text, 'value_type': vtype}) if text is not None else
                config_review._default_text(ent), env_v):
            env_diffs.append(key)

    return {'settings': settings_plan, 'arrays': arrays_plan, 'rates': rates_plan,
            'errors': errors, 'env_diffs': env_diffs}


# =============================================================================
# Apply / verify
# =============================================================================

_ENV_ONLY_CFG = {n for _, (cls, _, n) in settings.ARRAY_FIELDS.items()
                 if cls not in settings.STORE_CLASSES}


def _apply(conn: sqlite3.Connection, plan: Dict[str, Any]) -> Dict[str, int]:
    now = _now()
    n = {'rows_set': 0, 'rows_removed': 0, 'arrays_written': 0, 'rate_rows_written': 0}
    for key, text, vtype, cat, desc in plan['settings']:
        if text is None:
            cur = conn.execute("DELETE FROM app_config WHERE scope = 'global' AND key = ?", (key,))
            n['rows_removed'] += cur.rowcount
        else:
            conn.execute("""INSERT OR REPLACE INTO app_config
                            (scope, key, value, value_type, category, description,
                             is_secret, source, updated_at)
                            VALUES ('global', ?, ?, ?, ?, ?, 0, 'user', ?)""",
                         (key, text, vtype, cat, desc, now))
            n['rows_set'] += 1
    for a in plan['arrays']:
        cur = conn.execute('SELECT * FROM solar_arrays WHERE array_id = ?', (a['array_id'],)).fetchone()
        cur = dict(cur) if cur else {}
        try:
            cfg = json.loads(cur.get('config_json') or '{}') or {}
        except (json.JSONDecodeError, TypeError):
            cfg = {}
        cfg = {k: v for k, v in cfg.items() if k not in _ENV_ONLY_CFG}
        for k, v in a['cfg'].items():
            if v is None:
                cfg.pop(k, None)
            else:
                cfg[k] = v
        merged = {c: cur.get(c) for c in ('name', 'array_type', 'charges_battery', 'exports',
                                           'gateway_id', 'capacity_kw', 'capacity_kwp',
                                           'panel_count', 'enabled')}
        merged.update(a['cols'])
        merged.update(a['flags'])
        if a.get('gateway_id') and not merged.get('gateway_id'):
            merged['gateway_id'] = a['gateway_id']
        if merged.get('enabled') is None:
            merged['enabled'] = 1
        if cur:
            conn.execute("""UPDATE solar_arrays SET name=?, array_type=?, charges_battery=?, exports=?,
                            gateway_id=?, capacity_kw=?, capacity_kwp=?, config_json=?, enabled=?,
                            updated_at=? WHERE id=?""",
                         (merged['name'], merged['array_type'], merged['charges_battery'],
                          merged['exports'], merged['gateway_id'], merged['capacity_kw'],
                          merged['capacity_kwp'], json.dumps(cfg) if cfg else None,
                          merged['enabled'], now, cur['id']))
        else:
            conn.execute("""INSERT INTO solar_arrays (array_id, name, array_type, charges_battery,
                            exports, gateway_id, capacity_kw, capacity_kwp, config_json, enabled,
                            updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                         (a['array_id'], merged['name'], merged['array_type'],
                          merged['charges_battery'], merged['exports'], merged['gateway_id'],
                          merged['capacity_kw'], merged['capacity_kwp'],
                          json.dumps(cfg) if cfg else None, now))
        n['arrays_written'] += 1
    rp = plan['rates'].get('replace')
    if rp:
        # Plan editor: replace the plan's structure. No base-tier rows are
        # written, so every season's prices are its own (T033).
        if rp['id'] is None:
            conn.execute("UPDATE rate_plans SET active = 0 WHERE active = 1")
            cur = conn.execute("""INSERT INTO rate_plans (name, export_capable, net_metering,
                                  export_rates_json, default_tier, holiday_tier, active, source,
                                  imported_at) VALUES (?, ?, ?, NULL, ?, ?, 1, 'review', ?)""",
                               (rp['name'], rp['export_capable'], rp['net_metering'],
                                rp['default_tier'], rp['holiday_tier'], now))
            pid = cur.lastrowid
        else:
            pid = rp['id']
            conn.execute("""UPDATE rate_plans SET name = ?, export_capable = ?, net_metering = ?,
                            default_tier = ?, holiday_tier = ?, source = 'review', imported_at = ?
                            WHERE id = ?""",
                         (rp['name'], rp['export_capable'], rp['net_metering'], rp['default_tier'],
                          rp['holiday_tier'], now, pid))
            for t in ('rate_tiers', 'rate_windows', 'rate_seasons'):
                conn.execute(f'DELETE FROM {t} WHERE plan_id = ?', (pid,))
        for s in rp['seasons']:
            cur = conn.execute('INSERT INTO rate_seasons (plan_id, name, months_json, comment) '
                               'VALUES (?, ?, ?, ?)',
                               (pid, s['name'], json.dumps(s['months']), 'review'))
            sid = cur.lastrowid
            for tier, cents in s['tiers'].items():
                conn.execute('INSERT INTO rate_tiers (plan_id, season_id, tier, rate_cents) '
                             'VALUES (?, ?, ?, ?)', (pid, sid, tier, cents))
                n['rate_rows_written'] += 1
            for w in s['windows']:
                conn.execute('INSERT INTO rate_windows (plan_id, season_id, tier, days_json, '
                             'start_time, end_time) VALUES (?, ?, ?, ?, ?, ?)',
                             (pid, sid, w['tier'], json.dumps(w['days']), w['start'], w['end']))
                n['rate_rows_written'] += 1
        if rp['holidays'] is not None:
            conn.execute('DELETE FROM rate_holidays WHERE plan_id = ?', (pid,))
            for hd in rp['holidays']:
                conn.execute('INSERT OR IGNORE INTO rate_holidays (plan_id, holiday_date, name) '
                             'VALUES (?, ?, ?)', (pid, hd, 'review'))
        rp['_written_id'] = pid
    for pid, sid, tier, cents in plan['rates']['tiers']:
        cur = conn.execute('UPDATE rate_tiers SET rate_cents = ? WHERE plan_id = ? AND season_id = ? '
                           'AND tier = ?', (cents, pid, sid, tier))
        if cur.rowcount == 0:
            conn.execute('INSERT INTO rate_tiers (plan_id, season_id, tier, rate_cents) '
                         'VALUES (?, ?, ?, ?)', (pid, sid, tier, cents))
        n['rate_rows_written'] += 1
    for wid, which, v in plan['rates']['windows']:
        col = 'start_time' if which == 'start' else 'end_time'
        conn.execute(f'UPDATE rate_windows SET {col} = ? WHERE id = ?', (v, wid))
        n['rate_rows_written'] += 1
    for pid in plan['rates']['plans']:
        conn.execute("UPDATE rate_plans SET source = 'review', imported_at = ? WHERE id = ?", (now, pid))
    return n


def verify(plan: Dict[str, Any]) -> List[str]:
    """The store must now hold exactly what was reviewed, and rates must resolve."""
    problems = []
    conn = sqlite3.connect(f'file:{settings.db_path()}?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        cfg = {r['key']: dict(r) for r in
               conn.execute("SELECT * FROM app_config WHERE scope = 'global'")}
        for key, text, vtype, _, _ in plan['settings']:
            ent = settings.BY_KEY[key]
            got = cfg.get(key)
            if text is None:
                if got is not None:
                    problems.append(f'{key}: expected no row, store has {got["value"]!r}')
            elif got is None:
                problems.append(f'{key}: expected {text!r}, store has no row')
            elif not _same(ent.type, settings._row_text(got),
                           settings._row_text({'value': text, 'value_type': vtype})) \
                    or got['source'] != 'user':
                problems.append(f'{key}: expected {text!r} (user), store has '
                                f'{got["value"]!r} ({got["source"]})')
        for a in plan['arrays']:
            r = conn.execute('SELECT * FROM solar_arrays WHERE array_id = ?', (a['array_id'],)).fetchone()
            if r is None:
                problems.append(f"array {a['array_id']}: missing after write")
                continue
            r = dict(r)
            stored_cfg = json.loads(r.get('config_json') or '{}') or {}
            for k, v in a['cols'].items():
                if (r.get(k) is None) != (v is None) or (v is not None and str(r.get(k)) != str(v)):
                    problems.append(f"array {a['array_id']}.{k}: expected {v!r}, store has {r.get(k)!r}")
            for k, v in a['cfg'].items():
                if stored_cfg.get(k) != v:
                    problems.append(f"array {a['array_id']}.{k}: expected {v!r}, store has "
                                    f"{stored_cfg.get(k)!r}")
            for k, v in a['flags'].items():
                if r.get(k) != v:
                    problems.append(f"array {a['array_id']}.{k}: expected {v}, store has {r.get(k)}")
        for pid, sid, tier, cents in plan['rates']['tiers']:
            r = conn.execute('SELECT rate_cents FROM rate_tiers WHERE plan_id = ? AND season_id = ? '
                             'AND tier = ?', (pid, sid, tier)).fetchone()
            if r is None or abs(r[0] - cents) > 1e-9:
                problems.append(f'rate {tier} (season {sid}): expected {cents}, store has '
                                f'{r[0] if r else None}')
        rp = plan['rates'].get('replace')
        if rp:
            pid = rp.get('_written_id')
            want = (len(rp['seasons']), sum(len(s['tiers']) for s in rp['seasons']),
                    sum(len(s['windows']) for s in rp['seasons']), 0)
            got = tuple(conn.execute(f'SELECT COUNT(*) FROM {t} WHERE plan_id = ?'
                                     + (' AND season_id IS NULL' if t == 'rate_tiers' and i == 3 else ''),
                                     (pid,)).fetchone()[0]
                        for i, t in enumerate(('rate_seasons', 'rate_tiers', 'rate_windows', 'rate_tiers')))
            if want != got:
                problems.append(f'rate plan: expected {want[0]} seasons, {want[1]} prices, {want[2]} '
                                f'windows, 0 base prices; store has {got}')
            active = [r[0] for r in conn.execute('SELECT id FROM rate_plans WHERE active = 1 ORDER BY id')]
            if pid not in active:
                problems.append(f'rate plan: {pid} is not active after the write (active: {active})')
    finally:
        conn.close()
    try:
        import rate_config
        today = datetime.now().strftime('%Y-%m-%d')
        rate_config.clear_cache()
        if rate_config.resolve_rates(today, use_cache=False) is None:
            problems.append('rate plan: no prices resolve for today')
        rp = plan['rates'].get('replace')
        expect_peak = rp['has_peak_window'] if rp else True
        if expect_peak and rate_config.peak_window_for_date(today, use_cache=False) is None:
            # A flat-rate plan (no peak window anywhere) is allowed: TOU derives off
            problems.append('rate plan: no peak window resolves for today')
        rate_config.clear_cache()
    except Exception as e:
        problems.append(f'rate plan check failed: {e}')
    return problems


# =============================================================================
# Public entry points
# =============================================================================

def _peak_guard() -> Optional[str]:
    import config_admin
    return config_admin._peak_guard()


def _ensure_schema():
    try:
        import config_store
        config_store.init_config_schema()
    except Exception:
        pass


def _ensure_rate_plan() -> Optional[str]:
    """An install that never ran the v4.6 migration has no rate plan in the
    store. Import rate_schedule.json (what such an install runs on today)
    so the store resolves the same prices and windows. Refuses on an invalid
    file rather than committing a store without rates."""
    conn = sqlite3.connect(f'file:{settings.db_path()}?mode=ro', uri=True, timeout=10)
    try:
        n = conn.execute('SELECT COUNT(*) FROM rate_plans WHERE active = 1').fetchone()[0]
    finally:
        conn.close()
    if n:
        return None
    import migrate_v46
    rs, path = migrate_v46.load_rate_schedule()
    if not rs:
        raise CommitRefused(400, 'No rate plan yet. Build your rate plan on the Rates step '
                                 '(seasons, prices and time windows from the rate schedule on your '
                                 f'bill), or add data/rate_schedule.json ({path}) and reload.')
    errs, _ = migrate_v46.validate_rate_schedule(rs, False)
    if errs:
        raise CommitRefused(400, f'rate_schedule.json ({path}) failed validation; nothing was saved.',
                            errs)
    migrate_v46.import_rate_plan(rs, False)
    return rs.get('name')


def commit(edits: Dict[str, Any], choices: Dict[str, str], dry_run: bool = False,
           force_peak: bool = False) -> Dict[str, Any]:
    _ensure_schema()
    if not force_peak:
        g = _peak_guard()
        if g:
            raise CommitRefused(409, f'Not now: {g}. Peak transitions must not be disturbed; '
                                     'try again after the peak window.')
    plan = build_plan(edits, choices)
    if plan['errors']:
        raise CommitRefused(400, 'Some values need fixing before committing.', plan['errors'])
    summary = {
        'settings_written': sum(1 for p in plan['settings'] if p[1] is not None),
        'settings_at_default': sum(1 for p in plan['settings'] if p[1] is None),
        'arrays': [a['array_id'] for a in plan['arrays']],
        'rate_edits': len(plan['rates']['tiers']) + len(plan['rates']['windows']),
        'differ_from_env': plan['env_diffs'],
    }
    if dry_run:
        return {'ok': True, 'dry_run': True, 'summary': summary,
                'plan': {'settings': [(k, t) for k, t, *_ in plan['settings']],
                         'arrays': plan['arrays'],
                         'rates': {'tiers': plan['rates']['tiers'],
                                   'windows': plan['rates']['windows']}}}

    snap = take_snapshot('pre-review')
    imported = None if plan['rates'].get('replace') else _ensure_rate_plan()
    if imported:
        summary['rate_plan_imported'] = imported
    if plan['rates'].get('replace'):
        summary['rate_plan_replaced'] = plan['rates']['replace']['name']
    conn = _rw_connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            counts = _apply(conn, plan)
            conn.execute('COMMIT')
        except Exception as e:
            conn.execute('ROLLBACK')
            raise CommitRefused(500, f'Write failed, nothing changed: {e}')
        problems = verify(plan)
        if problems:
            restore_snapshot(snap, conn)
            raise CommitRefused(500, 'The store did not match the review after writing; '
                                     'the previous configuration was restored.', problems)
        conn.execute('BEGIN IMMEDIATE')
        prev = settings.read_authority_flag()
        _state_set(conn, settings.AUTHORITY_KEY, 'store')
        _state_set(conn, 'config.authority_changed_at', _now())
        _state_set(conn, REVIEW_COMPLETED_AT, _now())
        _state_set(conn, REVIEW_COMPLETED_VERSION, _version())
        _state_set(conn, REVIEW_LAST_SNAPSHOT, snap.name)
        conn.execute('COMMIT')
    finally:
        conn.close()
    summary.update(counts)
    summary['previous_authority'] = prev
    summary['snapshot'] = snap.name
    request_restart('review committed')
    return {'ok': True, 'summary': summary, 'restarting': True}


def rollback_to_env() -> Dict[str, Any]:
    """One-click rollback: run on .env again. Store data is kept, so the
    review can be committed again later."""
    prev = settings.read_authority_flag()
    conn = _rw_connect()
    try:
        conn.execute('BEGIN IMMEDIATE')
        _state_set(conn, settings.AUTHORITY_KEY, 'env')
        _state_set(conn, 'config.authority_changed_at', _now())
        conn.execute('COMMIT')
    finally:
        conn.close()
    request_restart('rolled back to .env')
    return {'ok': True, 'previous_authority': prev, 'authority': 'env', 'restarting': True}
