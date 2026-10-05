#!/usr/bin/env python3
"""
Persisted reliability stats for the gateway channels (v4.7).

The engine runs as a short-lived subprocess, so the in-memory ConnectionStats
in data_sources reset every cycle. This module keeps cumulative per-channel,
per-operation counters in data/channel_stats.json so the dashboard and the
daily telemetry can report how each channel is behaving over days and weeks.

Channels: local (local connection), modbus, cloud.
Operations: read, switch, confirm, login.

Also records:
  - switch events (which path carried the switch, fallbacks, confirm time)
  - cross-check results between local and Modbus readings (SOC, grid W, mode)
  - cloud token use (saved token reused vs. fresh login)

All writes are best-effort and file-locked (several processes write).
"""

import fcntl
import json
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

KEEP_DAYS = 35
MAX_LAT_SAMPLES = 400      # per channel/op/day, for percentiles
MAX_EVENTS = 60            # recent switch events kept verbatim

# Cross-check tolerances: a reading pair outside these counts as a disagreement.
# Grid power swings fast and the two reads are a few hundred ms apart, so the
# grid tolerance scales with the reading: the larger of 400 W or 10%.
SOC_TOL_PCT = 3.0
GRID_TOL_W = 400.0
GRID_TOL_FRAC = 0.10

# Local connection counted as degraded after this long without a success
# while it keeps failing (the 5-minute collector gives ~6 attempts per 30 min).
DEGRADED_AFTER_S = 30 * 60
DEGRADED_MIN_FAILS = 4


def _data_dir() -> Path:
    d = os.getenv('DATA_DIR')
    if d:
        return Path(d)
    here = Path(__file__).resolve().parent
    return here.parent / 'data'


def stats_path() -> Path:
    return _data_dir() / 'channel_stats.json'


@contextmanager
def _locked():
    p = stats_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(p) + '.lock', 'a+')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = {}
        try:
            if p.exists():
                with open(p) as f:
                    data = json.load(f)
        except Exception:
            data = {}
        holder = {'data': data}
        yield holder
        tmp = str(p) + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(holder['data'], f, separators=(',', ':'))
        os.replace(tmp, p)
    finally:
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            lock.close()


def _today() -> str:
    return datetime.now().strftime('%Y-%m-%d')


def _prune(data: dict):
    cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime('%Y-%m-%d')
    days = data.get('days', {})
    for d in [d for d in days if d < cutoff]:
        days.pop(d, None)


def _bucket(data: dict, channel: str, op: str) -> dict:
    day = data.setdefault('days', {}).setdefault(_today(), {})
    return day.setdefault(f'{channel}.{op}', {'ok': 0, 'fail': 0, 'lat': [], 'errors': {}})


def record(channel: str, op: str, ok: bool, ms: Optional[float] = None,
           error: Optional[str] = None):
    """Record one operation on a channel. Never raises."""
    try:
        with _locked() as h:
            data = h['data']
            b = _bucket(data, channel, op)
            if ok:
                b['ok'] += 1
                if ms is not None:
                    b['lat'].append(round(float(ms), 1))
                    if len(b['lat']) > MAX_LAT_SAMPLES:
                        b['lat'] = b['lat'][-MAX_LAT_SAMPLES:]
            else:
                b['fail'] += 1
                if error:
                    key = _err_class(error)
                    b['errors'][key] = b['errors'].get(key, 0) + 1
            last = data.setdefault('last', {}).setdefault(channel, {})
            now = datetime.now().isoformat(timespec='seconds')
            if ok:
                last['ok_at'] = now
                last['consecutive_failures'] = 0
            else:
                last['fail_at'] = now
                last['consecutive_failures'] = last.get('consecutive_failures', 0) + 1
                if error:
                    last['last_error'] = str(error)[:200]
            _prune(data)
    except Exception:
        pass


def _err_class(error: str) -> str:
    import re as _re
    m = _re.match(r'^\[([a-z_]+)\]', str(error))
    if m:
        return m.group(1)
    e = str(error).lower()
    for needle, cls in (('timed out', 'timeout'), ('timeout', 'timeout'),
                        ('refused', 'refused'), ('unreachable', 'unreachable'),
                        ('502', 'http_502'), ('401', 'auth'), ('login', 'auth'),
                        ('not confirmed', 'not_confirmed'), ('mismatch', 'not_confirmed'),
                        ('reset', 'reset'), ('could not connect', 'connect')):
        if needle in e:
            return cls
    return 'other'


def record_switch(target: str, path: str, ok: bool, attempts: list,
                  confirm_via: Optional[str] = None, confirm_ms: Optional[float] = None,
                  total_ms: Optional[float] = None):
    """Record one engine mode switch end-to-end.

    path: the channel that carried the switch when it succeeded ('local',
    'cloud'), or 'none'. attempts: list of {'channel', 'ok', 'error'?}.
    """
    try:
        with _locked() as h:
            data = h['data']
            day = data.setdefault('days', {}).setdefault(_today(), {})
            sw = day.setdefault('switches', {'total': 0, 'ok': 0, 'failed': 0,
                                             'by_path': {}, 'fallbacks': 0,
                                             'confirm_ms': []})
            sw['total'] += 1
            if ok:
                sw['ok'] += 1
                sw['by_path'][path] = sw['by_path'].get(path, 0) + 1
            else:
                sw['failed'] += 1
            if len([a for a in attempts if a.get('channel') != path]) > 0 and ok:
                sw['fallbacks'] += 1
            if confirm_ms is not None:
                sw['confirm_ms'].append(round(confirm_ms))
                sw['confirm_ms'] = sw['confirm_ms'][-200:]
            ev = data.setdefault('events', [])
            ev.append({
                'at': datetime.now().isoformat(timespec='seconds'),
                'target': target, 'path': path, 'ok': ok,
                'confirm_via': confirm_via,
                'confirm_ms': round(confirm_ms) if confirm_ms is not None else None,
                'total_ms': round(total_ms) if total_ms is not None else None,
                'attempts': attempts[:8],
            })
            data['events'] = ev[-MAX_EVENTS:]
            _prune(data)
    except Exception:
        pass


def record_crosscheck(soc_local: Optional[float], soc_modbus: Optional[float],
                      grid_local_w: Optional[float], grid_modbus_w: Optional[float],
                      mode_local: Optional[str], mode_modbus: Optional[str]):
    """Compare one local reading against the Modbus reading taken alongside it."""
    try:
        with _locked() as h:
            data = h['data']
            day = data.setdefault('days', {}).setdefault(_today(), {})
            cc = day.setdefault('crosscheck', {'n': 0, 'soc_bad': 0, 'grid_bad': 0,
                                               'mode_bad': 0, 'soc_dev': [], 'grid_dev': []})
            cc['n'] += 1
            if soc_local is not None and soc_modbus is not None:
                dev = abs(soc_local - soc_modbus)
                cc['soc_dev'] = (cc['soc_dev'] + [round(dev, 2)])[-MAX_LAT_SAMPLES:]
                if dev > SOC_TOL_PCT:
                    cc['soc_bad'] += 1
            if grid_local_w is not None and grid_modbus_w is not None:
                dev = abs(grid_local_w - grid_modbus_w)
                cc['grid_dev'] = (cc['grid_dev'] + [round(dev)])[-MAX_LAT_SAMPLES:]
                tol = max(GRID_TOL_W, GRID_TOL_FRAC * max(abs(grid_local_w), abs(grid_modbus_w)))
                if dev > tol:
                    cc['grid_bad'] += 1
            if mode_local and mode_modbus and mode_local != mode_modbus:
                cc['mode_bad'] += 1
            _prune(data)
    except Exception:
        pass


def record_token(event: str):
    """event: 'reused' (saved token worked), 'login_ok', 'login_fail'."""
    try:
        with _locked() as h:
            data = h['data']
            day = data.setdefault('days', {}).setdefault(_today(), {})
            tk = day.setdefault('cloud_token', {})
            tk[event] = tk.get(event, 0) + 1
            _prune(data)
    except Exception:
        pass


# =============================================================================
# Summaries
# =============================================================================

def _pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = min(len(s) - 1, max(0, int(round(p / 100.0 * (len(s) - 1)))))
    return s[k]


def load() -> Dict[str, Any]:
    try:
        with open(stats_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def summary(days: int = 7, data: Optional[dict] = None) -> Dict[str, Any]:
    """Aggregate the last `days` days into a compact, telemetry-safe summary."""
    data = data if data is not None else load()
    cutoff = (datetime.now() - timedelta(days=days - 1)).strftime('%Y-%m-%d')
    ops: Dict[str, dict] = {}
    sw = {'total': 0, 'ok': 0, 'failed': 0, 'by_path': {}, 'fallbacks': 0, 'confirm_ms': []}
    cc = {'n': 0, 'soc_bad': 0, 'grid_bad': 0, 'mode_bad': 0, 'soc_dev': [], 'grid_dev': []}
    tk: Dict[str, int] = {}
    for d, day in (data.get('days') or {}).items():
        if d < cutoff:
            continue
        for k, v in day.items():
            if k == 'switches':
                for f in ('total', 'ok', 'failed', 'fallbacks'):
                    sw[f] += v.get(f, 0)
                for p, n in v.get('by_path', {}).items():
                    sw['by_path'][p] = sw['by_path'].get(p, 0) + n
                sw['confirm_ms'] += v.get('confirm_ms', [])
            elif k == 'crosscheck':
                for f in ('n', 'soc_bad', 'grid_bad', 'mode_bad'):
                    cc[f] += v.get(f, 0)
                cc['soc_dev'] += v.get('soc_dev', [])
                cc['grid_dev'] += v.get('grid_dev', [])
            elif k == 'cloud_token':
                for e, n in v.items():
                    tk[e] = tk.get(e, 0) + n
            elif '.' in k:
                o = ops.setdefault(k, {'ok': 0, 'fail': 0, 'lat': [], 'errors': {}})
                o['ok'] += v.get('ok', 0)
                o['fail'] += v.get('fail', 0)
                o['lat'] += v.get('lat', [])
                for e, n in v.get('errors', {}).items():
                    o['errors'][e] = o['errors'].get(e, 0) + n
    out_ops = {}
    for k, o in sorted(ops.items()):
        n = o['ok'] + o['fail']
        out_ops[k] = {
            'attempts': n,
            'success_pct': round(100.0 * o['ok'] / n, 1) if n else None,
            'avg_ms': round(sum(o['lat']) / len(o['lat'])) if o['lat'] else None,
            'p95_ms': _pct(o['lat'], 95),
            'errors': o['errors'],
        }
    return {
        'days': days,
        'ops': out_ops,
        'switches': {
            'total': sw['total'], 'ok': sw['ok'], 'failed': sw['failed'],
            'by_path': sw['by_path'], 'fallbacks': sw['fallbacks'],
            'confirm_p50_ms': _pct(sw['confirm_ms'], 50),
            'confirm_p95_ms': _pct(sw['confirm_ms'], 95),
        },
        'crosscheck': {
            'n': cc['n'],
            'soc_disagree_pct': round(100.0 * cc['soc_bad'] / cc['n'], 2) if cc['n'] else None,
            'grid_disagree_pct': round(100.0 * cc['grid_bad'] / cc['n'], 2) if cc['n'] else None,
            'mode_disagree': cc['mode_bad'],
            'soc_dev_p95': _pct(cc['soc_dev'], 95),
            'grid_dev_p95_w': _pct(cc['grid_dev'], 95),
        },
        'cloud_token': tk,
    }


def degraded(data: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    """Local connection state for the dashboard notice: None when healthy,
    else {'since', 'failures', 'last_error', 'error_class', 'modbus_ok'}."""
    data = data if data is not None else load()
    last = (data.get('last') or {})
    loc = last.get('local') or {}
    fails = int(loc.get('consecutive_failures') or 0)
    if fails < DEGRADED_MIN_FAILS:
        return None
    try:
        ok_at = datetime.fromisoformat(loc['ok_at']) if loc.get('ok_at') else None
    except Exception:
        ok_at = None
    if ok_at and (datetime.now() - ok_at).total_seconds() < DEGRADED_AFTER_S:
        return None
    mb = last.get('modbus') or {}
    modbus_ok = bool(mb.get('ok_at')) and int(mb.get('consecutive_failures') or 0) == 0
    err = loc.get('last_error') or ''
    return {'since': loc.get('ok_at'), 'failures': fails, 'last_error': err[:160],
            'error_class': _err_class(err) if err else None, 'modbus_ok': modbus_ok}


def channel_status() -> Dict[str, Any]:
    """Last-success / failure state per channel for the dashboard."""
    data = load()
    return {
        'last': data.get('last', {}),
        'recent_switches': (data.get('events') or [])[-5:],
        'today': summary(1, data),
        'degraded': degraded(data),
    }


if __name__ == '__main__':
    import sys
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    print(json.dumps({'summary': summary(days), 'status': channel_status()['last']}, indent=2))
