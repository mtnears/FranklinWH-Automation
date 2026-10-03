#!/usr/bin/env python3
"""
review_tests.py — live connection tests for the configuration review (v4.7)

    python3 review_tests.py '{"test": "modbus", "host": "192.168.5.149"}'

Prints one JSON line: {"ok": bool, "message": str, "ms": int}.
Run by scheduler.py (POST /api/v1/review/test) in a subprocess with a hard
timeout, so a hung connection can never block the API.

Secrets come from the environment (.env) and never leave this process: every
message is scrubbed of secret values before it is printed. Non-secret values
the user may have just edited in the review (an IP, a station ID, system IDs)
are passed in, so a test checks what the user is about to commit.

Tests: modbus, envoy, franklin, solaredge, pvoutput, weather.
"""

import json
import os
import re
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings  # noqa: E402

TIMEOUT = 10
_HOST_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.\-]*$')


def _secret_values():
    vals = set()
    names = [n for e in settings.REGISTRY if e.cls == settings.SECRET for n in e.env]
    names += [n for n, (_, fld) in settings.LEGACY_ARRAY_ALIASES.items()
              if settings.ARRAY_FIELDS[fld][0] == settings.SECRET]
    for n, v in os.environ.items():
        m = settings._ARRAY_RE.match(n)
        if m and settings.ARRAY_FIELDS[m.group(2)][0] == settings.SECRET:
            names.append(n)
    for n in names:
        v = os.environ.get(n)
        if v and len(v) >= 4:
            vals.add(v)
    return vals


def scrub(text: str) -> str:
    text = str(text)
    for v in sorted(_secret_values(), key=len, reverse=True):
        text = text.replace(v, '••••')
    return re.sub(r'(api_?key=)[^&\s]+', r'\1••••', text, flags=re.I)[:300]


def _need_host(v, what):
    v = (v or '').strip()
    if not v:
        raise ValueError(f'No {what} set')
    if not _HOST_RE.match(v):
        raise ValueError(f'{v!r} is not an IP address or host name')
    return v


# =============================================================================
# Tests — each returns a message on success or raises
# =============================================================================

def t_modbus(a):
    host = _need_host(a.get('host') or settings.raw('MODBUS_HOST', ''), 'aGate IP')
    port = int(settings.raw('MODBUS_PORT', '502') or 502)
    from pymodbus.client import ModbusTcpClient
    c = ModbusTcpClient(host=host, port=port, timeout=5)
    if not c.connect():
        raise RuntimeError(f'No answer from {host}:{port}. Check the IP and that Modbus TCP '
                           'is reachable from the NAS.')
    try:
        r = c.read_holding_registers(1037, count=1)
        if r.isError() or not r.registers:
            raise RuntimeError(f'Connected to {host}:{port}, but the aGate refused the read '
                               f'({r}). Is this the aGate?')
        soc = r.registers[0] / 10.0
    finally:
        c.close()
    return f'aGate at {host}:{port} answered. Battery {soc:.1f}%.'


def t_envoy(a):
    import requests
    import urllib3
    urllib3.disable_warnings()
    ip = _need_host(a.get('ip'), 'gateway IP')
    aid = (a.get('array_id') or '').strip().lower()
    r = requests.get(f'https://{ip}/info', verify=False, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f'{ip} answered HTTP {r.status_code} on /info. Is this an Envoy?')
    sn = re.search(r'<sn>([^<]+)</sn>', r.text)
    sw = re.search(r'<software>([^<]+)</software>', r.text)
    msg = f"Envoy at {ip}: serial {sn.group(1) if sn else '?'}" + \
          (f", firmware {sw.group(1)}" if sw else '') + '.'
    default_tok = f'/app/data/enphase_token_{aid}.txt' if aid else '/app/data/enphase_token.txt'
    tok_path = Path(settings.raw(f'SOLAR_ARRAY_{aid.upper()}_TOKEN_FILE', default_tok) if aid
                    else default_tok)
    if tok_path.exists() and len(tok_path.read_text().strip()) > 50:
        t = requests.get(f'https://{ip}/api/v1/production', verify=False, timeout=TIMEOUT,
                         headers={'Authorization': f'Bearer {tok_path.read_text().strip()}'})
        if t.status_code == 200:
            w = t.json().get('wattsNow')
            msg += ' Token accepted' + (f', producing {w} W now.' if w is not None else '.')
        elif t.status_code == 401:
            msg += (' Token rejected (401). The collector fetches a new one with the '
                    'Enlighten login in .env.')
        else:
            msg += f' Token check returned HTTP {t.status_code}.'
    else:
        msg += ' No token cached yet; the collector creates one on its first run.'
    return msg


def t_franklin(a):
    import asyncio
    user = os.environ.get('FRANKLIN_USERNAME', '')
    pw = os.environ.get('FRANKLIN_PASSWORD', '')
    gw = os.environ.get('FRANKLIN_GATEWAY_ID', '')
    missing = [n for n, v in (('FRANKLIN_USERNAME', user), ('FRANKLIN_PASSWORD', pw),
                              ('FRANKLIN_GATEWAY_ID', gw)) if not v]
    if missing:
        raise ValueError('Not set in .env: ' + ', '.join(missing))
    from franklinwh import Client, TokenFetcher

    async def go():
        client = Client(TokenFetcher(user, pw), gw)
        return await client.get_stats()
    stats = asyncio.run(asyncio.wait_for(go(), timeout=25))
    if not stats:
        raise RuntimeError('Logged in, but the gateway returned no data')
    soc = getattr(getattr(stats, 'current', None), 'battery_soc', None)
    return (f'Franklin cloud login OK; aGate •••• {gw[-4:]} answered' +
            (f', battery {soc:.0f}%.' if isinstance(soc, (int, float)) else '.'))


def _solaredge_site():
    for aid in settings.array_ids():
        if (settings.raw(f'SOLAR_ARRAY_{aid.upper()}_TYPE', '') or '').lower() == 'solaredge':
            s = settings.raw(f'SOLAR_ARRAY_{aid.upper()}_SITE_ID', '')
            if s:
                return s
    return settings.raw('SOLAREDGE_SITE_ID', '')


def t_solaredge(a):
    user = os.environ.get('SOLAREDGE_USERNAME', '')
    pw = os.environ.get('SOLAREDGE_PASSWORD', '')
    if not user or not pw:
        raise ValueError('SOLAREDGE_USERNAME / SOLAREDGE_PASSWORD not set in .env')
    import collect_solaredge_panels as se
    cached = se.TOKEN_CACHE.exists()
    token, _uuid = se.get_se_token(user, pw)
    if not token:
        raise RuntimeError('Portal login returned no token')
    exp = se._jwt_claims(token).get('exp')
    hrs = f', valid {max(0, int((exp - time.time()) / 3600))} h' if exp else ''
    msg = f"SolarEdge portal login OK ({'cached/refreshed token' if cached else 'new token'}{hrs})."
    key = os.environ.get('SOLAREDGE_API_KEY') or ''
    for n, v in os.environ.items():
        m = settings._ARRAY_RE.match(n)
        if not key and m and m.group(2) == 'API_KEY' and v:
            key = v
    site = (a.get('site_id') or _solaredge_site() or '').strip()
    if key and site:
        import requests
        r = requests.get(f'https://monitoringapi.solaredge.com/site/{site}/details',
                         params={'api_key': key}, timeout=TIMEOUT)
        if r.status_code == 200:
            name = (r.json().get('details') or {}).get('name', '?')
            msg += f' Monitoring API key OK for site {site} ({name}).'
        else:
            msg += f' Monitoring API key check: HTTP {r.status_code} for site {site}.'
    return msg


def t_pvoutput(a):
    import requests
    key = os.environ.get('PVOUTPUT_API_KEY', '')
    if not key:
        raise ValueError('PVOUTPUT_API_KEY not set in .env')
    ids = settings.coerce(a.get('system_ids') or settings.raw('PVOUTPUT_SYSTEM_IDS', ''), 'list', [])
    if not ids:
        raise ValueError('No PVOutput system IDs set')
    out, bad = [], []
    for sid in ids:
        r = requests.get('https://pvoutput.org/service/r2/getsystem.jsp', timeout=TIMEOUT,
                         headers={'X-Pvoutput-Apikey': key, 'X-Pvoutput-SystemId': str(sid)})
        if r.status_code == 200:
            out.append(f"{sid} “{r.text.split(',')[0]}”")
        else:
            bad.append(f'{sid}: HTTP {r.status_code} {r.text.strip()[:80]}')
    if bad:
        raise RuntimeError('; '.join(bad + [f'OK: {x}' for x in out]))
    return 'PVOutput key OK for ' + ', '.join(out) + '.'


def t_weather(a):
    import requests
    key = os.environ.get('WEATHER_API_KEY', '')
    if not key:
        raise ValueError('WEATHER_API_KEY not set in .env')
    station = (a.get('station_id') or settings.raw('WEATHER_STATION_ID', '') or '').strip()
    if not station:
        raise ValueError('No station ID set')
    r = requests.get('https://api.weather.com/v2/pws/observations/current', timeout=TIMEOUT,
                     params={'stationId': station, 'format': 'json', 'units': 'e', 'apiKey': key})
    if r.status_code == 204:
        raise RuntimeError(f'Key accepted, but station {station} has no current observation '
                           '(offline, or the ID is wrong)')
    if r.status_code != 200:
        raise RuntimeError(f'HTTP {r.status_code} for station {station}'
                           + (' (key rejected)' if r.status_code == 401 else ''))
    obs = (r.json().get('observations') or [{}])[0]
    temp = (obs.get('imperial') or {}).get('temp')
    return (f"Station {station} OK: {temp}°F at {obs.get('obsTimeLocal', '?')}."
            if temp is not None else f'Station {station} OK.')


TESTS = {'modbus': t_modbus, 'envoy': t_envoy, 'franklin': t_franklin,
         'solaredge': t_solaredge, 'pvoutput': t_pvoutput, 'weather': t_weather}


def run(args: dict) -> dict:
    name = args.get('test')
    fn = TESTS.get(name)
    if not fn:
        return {'ok': False, 'message': f'Unknown test {name!r}', 'ms': 0}
    t0 = time.time()
    try:
        socket.setdefaulttimeout(TIMEOUT + 5)
        msg = fn(args)
        return {'ok': True, 'message': scrub(msg), 'ms': int((time.time() - t0) * 1000)}
    except Exception as e:
        return {'ok': False, 'message': scrub(f'{type(e).__name__}: {e}' if not isinstance(
            e, (ValueError, RuntimeError)) else str(e)), 'ms': int((time.time() - t0) * 1000)}


if __name__ == '__main__':
    try:
        a = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    except json.JSONDecodeError:
        a = {}
    print(json.dumps(run(a)), flush=True)
