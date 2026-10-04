#!/usr/bin/env python3
"""
Local connection to the aGate (v4.7).

Talks to the aGate directly on the home network for live readings and
operating-mode changes, so the engine keeps working when the Franklin cloud
login is unavailable. Uses the franklinwh_local package (pinned in
requirements.txt).

    gw = LocalGateway()
    if gw.enabled:
        flow = gw.read()                 # dict, see read()
        res = gw.set_mode('self_consumption')   # {'ok', 'before', 'after', ...}

Every call opens a short session (connect, handshake, request, close) so
nothing is held open between engine cycles. All calls are synchronous; the
async DataSourceManager runs them in a worker thread.

Each call is recorded in channel_stats (channel 'local').
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import settings
import channel_stats

logger = logging.getLogger(__name__)

try:
    from franklinwh_local import LocalClient, __version__ as LIB_VERSION
    LIB_AVAILABLE = True
except Exception:  # pragma: no cover - missing optional package
    LocalClient = None
    LIB_VERSION = None
    LIB_AVAILABLE = False

DEFAULT_PORT = 9000

# Operating mode <-> programme scheduling_type (same as the cloud workMode).
SCHED_TYPE = {'time_of_use': 1, 'self_consumption': 2, 'emergency_backup': 3}
SCHED_TO_MODE = {v: k for k, v in SCHED_TYPE.items()}
MODE_LABEL = {'time_of_use': 'Time of Use', 'self_consumption': 'Self Consumption',
              'emergency_backup': 'Emergency Backup'}

_CANON = {
    'tou': 'time_of_use', 'time_of_use': 'time_of_use',
    'self': 'self_consumption', 'sc': 'self_consumption', 'self_consumption': 'self_consumption',
    'backup': 'emergency_backup', 'emergency_backup': 'emergency_backup',
}


def canonical_mode(mode: str) -> Optional[str]:
    return _CANON.get(str(mode or '').strip().lower())


def _setting(key: str, env: str, default=None):
    try:
        v = settings.get(key)
        if v not in (None, ''):
            return v
    except Exception:
        pass
    raw = settings.raw(env, '')
    return raw if raw not in (None, '') else default


def _bool(v, default=True) -> bool:
    if isinstance(v, bool):
        return v
    if v in (None, ''):
        return default
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def _modes_cache_path() -> Path:
    return channel_stats._data_dir() / 'local_modes.json'


def _found_host_path() -> Path:
    return channel_stats._data_dir() / 'local_host.json'


def found_host() -> Optional[str]:
    """aGate address found by discovery (used when no host is configured)."""
    try:
        with open(_found_host_path()) as f:
            return json.load(f).get('host') or None
    except Exception:
        return None


def save_found_host(host: str):
    try:
        p = _found_host_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, 'w') as f:
            json.dump({'host': host, 'found_at': datetime.now().isoformat(timespec='seconds')}, f)
    except Exception:
        pass


class LocalGateway:
    """Short-session client for the aGate's local connection."""

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None,
                 timeout: Optional[float] = None, enabled: Optional[bool] = None):
        self.host = host or _setting('local.host', 'LOCAL_HOST') \
            or _setting('modbus.host', 'MODBUS_HOST') or found_host()
        self.port = int(port or _setting('local.port', 'LOCAL_PORT', DEFAULT_PORT))
        self.timeout = float(timeout or _setting('local.timeout', 'LOCAL_TIMEOUT', 8.0))
        want = _bool(_setting('local.enabled', 'LOCAL_ENABLED', True)) if enabled is None else enabled
        self.enabled = bool(want and LIB_AVAILABLE and self.host)
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------------ util
    def _client(self, retries: int = 1):
        return LocalClient(self.host, self.port, timeout=self.timeout, retries=retries)

    def _run(self, op: str, fn, record: bool = True):
        """Open a session, log in, run fn(client), close. Records stats."""
        if not self.enabled:
            raise RuntimeError('local connection not enabled')
        t0 = time.time()
        try:
            with self._client() as c:
                c.login()
                out = fn(c)
            ms = (time.time() - t0) * 1000
            if record:
                channel_stats.record('local', op, True, ms)
            self.last_error = None
            return out, ms
        except Exception as e:
            ms = (time.time() - t0) * 1000
            self.last_error = f'{type(e).__name__}: {e}'
            if record:
                channel_stats.record('local', op, False, ms, self.last_error)
            raise

    # ------------------------------------------------------------ programmes
    def _save_modes(self, modes: List[dict]):
        try:
            p = _modes_cache_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, 'w') as f:
                json.dump({'saved_at': datetime.now().isoformat(timespec='seconds'),
                           'host': self.host, 'list': modes}, f, indent=1)
        except Exception:
            pass

    def cached_modes(self) -> List[dict]:
        try:
            with open(_modes_cache_path()) as f:
                return json.load(f).get('list', [])
        except Exception:
            return []

    def mode_from_id(self, prog_id, modes: Optional[List[dict]] = None) -> Optional[str]:
        for m in (modes if modes is not None else self.cached_modes()):
            try:
                if int(m.get('id')) == int(prog_id):
                    return SCHED_TO_MODE.get(m.get('scheduling_type'))
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _id_for(mode: str, modes: List[dict]) -> Optional[int]:
        st = SCHED_TYPE.get(mode)
        for m in modes:
            if m.get('scheduling_type') == st:
                return int(m['id'])
        return None

    # ----------------------------------------------------------------- reads
    def read(self) -> Dict[str, Any]:
        """Live power flow. Returns a dict with normalised fields:

        soc_pct, grid_w (+import), solar_w, battery_w (+discharge), load_w,
        mode (canonical or None), mode_name, prog_id, run_status,
        per_battery_soc, per_battery_kw, ambient_temp_c, kwh_* totals, read_ms.
        """
        def fn(c):
            pf = c.power_flow()
            modes = None
            prog = pf.get('mode')
            if prog is not None and self.mode_from_id(prog) is None:
                ml = c.mode_list()
                modes = ml.get('list', []) or []
                if modes:
                    self._save_modes(modes)
            return pf, modes

        (pf, modes), ms = self._run('read', fn)
        return self._normalise(pf, modes, ms)

    def _normalise(self, pf: dict, modes, ms: float) -> Dict[str, Any]:
        def f(k):
            v = pf.get(k)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        prog = pf.get('mode')
        mode = self.mode_from_id(prog, modes) if prog is not None else None
        p_fhp = f('p_fhp')
        t_amb = f('t_amb')
        return {
            'soc_pct': f('soc'),
            'grid_w': f('p_uti'),
            'solar_w': f('p_sun'),
            # aGate reports battery power negative while charging, which is
            # already the engine's convention (positive = discharge).
            'battery_w': p_fhp,
            'load_w': f('p_load'),
            'generator_w': f('p_gen'),
            'mode': mode,
            'mode_name': pf.get('name') or (MODE_LABEL.get(mode) if mode else None),
            'prog_id': prog,
            'run_status': pf.get('run_status'),
            'per_battery_soc': pf.get('fhpSoc') or [],
            'per_battery_kw': pf.get('fhpPower') or [],
            # t_amb arrives in tenths of a degree C (206 -> 20.6)
            'ambient_temp_c': (t_amb / 10.0) if t_amb is not None and abs(t_amb) > 60 else t_amb,
            'kwh_grid_import': f('kwh_uti_in'),
            'kwh_grid_export': f('kwh_uti_out'),
            'kwh_solar': f('kwh_sun'),
            'kwh_load': f('kwh_load'),
            'kwh_battery_charge': f('kwh_fhp_chg'),
            'kwh_battery_discharge': f('kwh_fhp_di'),
            'offgrid_reason': pf.get('offgridreason'),
            'read_ms': round(ms),
        }

    def current_mode(self) -> Optional[str]:
        return self.read().get('mode')

    def mode_soc(self) -> Dict[str, Any]:
        out, _ = self._run('read', lambda c: c.mode_soc())
        return out

    # ---------------------------------------------------------------- switch
    def set_mode(self, mode: str, confirm_timeout: float = 15.0) -> Dict[str, Any]:
        """Change operating mode and confirm it through the local reading.

        The per-mode reserves are untouched (the aGate keeps each programme's
        own reserve), so this never resets a reserve set in the Franklin app.

        Returns {'ok', 'target', 'before', 'after', 'send_ms', 'confirm_ms',
                 'error'}.
        """
        target = canonical_mode(mode)
        res = {'ok': False, 'target': target, 'before': None, 'after': None,
               'send_ms': None, 'confirm_ms': None, 'error': None}
        if target is None:
            res['error'] = f'unknown mode {mode!r}'
            return res

        def fn(c):
            ml = c.mode_list()
            modes = ml.get('list', []) or []
            if modes:
                self._save_modes(modes)
            prog = self._id_for(target, modes)
            if prog is None:
                raise RuntimeError(f'no {target} programme on this aGate')
            before = ml.get('current_id')
            reply = c.call(1727, {'opt': 3, 'current_id': prog})
            if reply.get('result') not in (0, None):
                raise RuntimeError(f'aGate refused mode change (result={reply.get("result")})')
            return before, prog, modes

        try:
            (before, prog, modes), ms = self._run('switch', fn)
        except Exception as e:
            res['error'] = self.last_error or str(e)
            return res
        res['send_ms'] = round(ms)
        res['before'] = self.mode_from_id(before, modes) if before is not None else None

        # Confirm through a fresh local reading.
        t0 = time.time()
        deadline = t0 + confirm_timeout
        delay = 1.5
        while time.time() < deadline:
            time.sleep(delay)
            try:
                r = self.read()
                res['after'] = r.get('mode')
                if r.get('prog_id') is not None and int(r['prog_id']) == int(prog):
                    res['ok'] = True
                    res['confirm_ms'] = round((time.time() - t0) * 1000)
                    channel_stats.record('local', 'confirm', True, res['confirm_ms'])
                    return res
            except Exception:
                pass
            delay = min(delay * 1.5, 4.0)
        res['error'] = f'not confirmed: aGate shows {res["after"]}'
        channel_stats.record('local', 'confirm', False, None, res['error'])
        return res

    # --------------------------------------------------------------- testing
    def probe(self) -> Dict[str, Any]:
        """Handshake plus one reading, for the setup Test button."""
        def fn(c):
            pf = c.power_flow()
            ml = c.mode_list()
            return pf, ml
        (pf, ml), ms = self._run('read', fn)
        modes = ml.get('list', []) or []
        if modes:
            self._save_modes(modes)
        flow = self._normalise(pf, modes, ms)
        flow['programmes'] = [
            {'mode': SCHED_TO_MODE.get(m.get('scheduling_type')), 'name': m.get('name'),
             'reserve': m.get('reserved_soc')} for m in modes]
        return flow


_PRIVATE_IP = None


# Home-router defaults, tried last when nothing in the configuration points
# at the home network (cloud-only installs with no device IPs set).
COMMON_SUBNETS = ('192.168.1.1', '192.168.0.1', '10.0.0.1', '192.168.68.1',
                  '192.168.86.1', '10.0.1.1', '192.168.50.1', '192.168.2.1')
MAX_SUBNETS = 10
NEGATIVE_CACHE_S = 3600


def _lan_hint_path() -> Path:
    return channel_stats._data_dir() / 'lan_hint.json'


def save_lan_hint(ip: str):
    """Remember the LAN address the dashboard was opened on (the NAS / host),
    which is on the aGate's subnet in a typical home network."""
    try:
        import ipaddress
        a = ipaddress.ip_address(ip)
        if not a.is_private or a.is_loopback or str(a).startswith('172.'):
            return
        p = _lan_hint_path()
        cur = None
        try:
            cur = json.loads(p.read_text()).get('ip')
        except Exception:
            pass
        if cur != ip:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({'ip': ip, 'seen_at': datetime.now().isoformat(timespec='seconds')}))
    except Exception:
        pass


def _default_gateway() -> Optional[str]:
    try:
        from franklinwh_local.discover import default_gateway
        gw = default_gateway()
        # Under Docker's bridge network this is the bridge (172.16/12), not the LAN.
        if gw and not gw.startswith('172.'):
            return gw
    except Exception:
        pass
    return None


def subnet_hints(extra: Optional[List[str]] = None, include_common: bool = True) -> List[str]:
    """Addresses whose /24 the aGate is likely on, best first:
    explicit hints, private IPs in the configuration (Modbus host, Envoy and
    inverter IPs, ...), the LAN address the dashboard was opened on, the
    container's default gateway (host networking), then common home subnets."""
    import os
    import re
    global _PRIVATE_IP
    if _PRIVATE_IP is None:
        _PRIVATE_IP = re.compile(
            r'\b(10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b')
    cands = list(extra or [])
    for k, v in sorted(os.environ.items()):
        if not isinstance(v, str) or k.startswith(('HOSTNAME', 'DOCKER')):
            continue
        cands += _PRIVATE_IP.findall(v)
    for key in ('modbus.host', 'local.host'):
        try:
            v = settings.get(key)
            if v:
                cands.append(str(v))
        except Exception:
            pass
    try:
        cands.append(json.loads(_lan_hint_path().read_text()).get('ip') or '')
    except Exception:
        pass
    cands.append(_default_gateway() or '')
    if include_common:
        cands += list(COMMON_SUBNETS)
    seen, out = set(), []
    for ip in cands:
        parts = (ip or '').split('.')
        if len(parts) != 4 or (ip.startswith('172.') and ip not in (extra or [])):
            continue
        net = '.'.join(parts[:3])
        if net not in seen:
            seen.add(net)
            out.append(ip)
    return out[:MAX_SUBNETS]


def ensure_host() -> Optional[str]:
    """When the local connection is on but no address is known, look for the
    aGate on the likely subnets and remember what was found. A search that
    finds nothing is not repeated for an hour."""
    gw = LocalGateway()
    if gw.host or not LIB_AVAILABLE:
        return gw.host
    want = _bool(_setting('local.enabled', 'LOCAL_ENABLED', True))
    if not want:
        return None
    try:
        last = json.loads(_found_host_path().read_text())
        if not last.get('host') and last.get('searched_at'):
            age = (datetime.now() - datetime.fromisoformat(last['searched_at'])).total_seconds()
            if age < NEGATIVE_CACHE_S:
                return None
    except Exception:
        pass
    hints = subnet_hints()
    for hint in hints:
        hosts = discover(hint)
        if hosts:
            save_found_host(hosts[0])
            logger.info(f'Local connection: found aGate at {hosts[0]}')
            return hosts[0]
    try:
        p = _found_host_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({'host': None, 'searched_at': datetime.now().isoformat(timespec='seconds'),
                                 'subnets': ['.'.join(h.split('.')[:3]) + '.0/24' for h in hints]}))
    except Exception:
        pass
    logger.warning('Local connection: no aGate found on %d subnet(s); enter the aGate IP '
                   'on the Connection step of the configuration review', len(hints))
    return None


def discover(subnet_hint: Optional[str] = None, timeout: float = 0.6) -> List[str]:
    """Find aGates answering the local connection on the /24 of subnet_hint
    (an IP on the home network, e.g. the configured Modbus host)."""
    if not LIB_AVAILABLE:
        return []
    try:
        from franklinwh_local.discover import scan, expand_targets
        hint = subnet_hint or ''
        parts = hint.split('.')
        if len(parts) != 4:
            return []
        targets = expand_targets('.'.join(parts[:3]) + '.0/24')
        found = scan(targets, ports=(DEFAULT_PORT,), timeout=timeout, workers=64, probe=True)
        # One aGate can answer on two addresses (wired + Wi-Fi): keep one per
        # serial, and put the configured gateway's serial first.
        want_sn = (settings.raw('FRANKLIN_GATEWAY_ID', '') or '').strip()
        seen, hosts = set(), []
        for r in found:
            if not getattr(r, 'sendmqtt_confirmed', False):
                continue
            sn = (r.manifest or {}).get('IBG_SN') or r.host
            if sn in seen:
                continue
            seen.add(sn)
            hosts.insert(0, r.host) if (want_sn and sn == want_sn) else hosts.append(r.host)
        return hosts
    except Exception as e:
        logger.debug(f'discover failed: {e}')
        return []


if __name__ == '__main__':
    import sys
    gw = LocalGateway()
    print(f'enabled={gw.enabled} host={gw.host}:{gw.port} lib={LIB_VERSION}')
    if len(sys.argv) > 2 and sys.argv[1] == 'set':
        print(json.dumps(gw.set_mode(sys.argv[2]), indent=2))
    else:
        print(json.dumps(gw.probe(), indent=2, default=str))
