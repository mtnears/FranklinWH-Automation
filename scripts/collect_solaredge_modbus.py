#!/usr/bin/env python3
"""
SolarEdge Inverter-Level Collector (Modbus TCP)
================================================
Reads both barn SolarEdge inverters directly over local SunSpec Modbus TCP
and writes:

  1. SQLite: solaredge_inverter_readings  (per-inverter, every cycle)
  2. JSON:   data/solar_barn.json + web/solar_barn.json  (dashboard Solar Arrays tab)

This replaces the cloud-portal path (collect_solaredge_panels.py) that broke
when SolarEdge changed portal auth. It restores live barn production for the
dashboard and the inverter-level health/status the app's barn section needs.

SCOPE NOTE — what Modbus does and does NOT provide:
  - Inverter-level AC/DC power, voltage, current, frequency, temperature,
    operating status, and lifetime energy: YES (this collector).
  - Per-optimizer / per-panel telemetry (health_ratio_vs_string, etc.): NO.
    SolarEdge does not serve module-level data over SunSpec Modbus. That stays
    a cloud-portal capability (collect_solaredge_panels.py, currently broken).

ENERGY NOTE:
  The SunSpec AC_Energy_WH register is the inverter's LOCAL lifetime accumulator.
  It is reset by firmware updates / inverter service, so it does NOT match the
  portal's true cumulative lifetime and must not be treated as such. "Today"
  energy is derived from the delta vs the first reading of the day in the DB,
  with a reset guard (negative delta -> treated as 0 for that day).

Inverter identity (confirmed against the SolarEdge portal Inverter Power chart):
  192.168.5.121  SE11400H  "Inverter 1"  (higher producer)
  192.168.5.122  SE7600H   "Inverter 2"  (lower producer)

Usage:
    python3 collect_solaredge_modbus.py            # single read
    python3 collect_solaredge_modbus.py --json     # single read, print JSON
    python3 collect_solaredge_modbus.py --loop --interval 300

Environment Variables:
    SOLAREDGE_SITE_ID            Portal site id (default 1241660)
    SOLAREDGE_MODBUS_INVERTERS   Override inverter list, comma-separated:
                                 "Name:ip:port:unit,Name:ip:port:unit"
                                 (default: the two barn inverters below)
    WEB_DIR                      Dashboard web dir (default <project>/web)
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [se-modbus] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('collect_solaredge_modbus')

try:
    from pymodbus.client import ModbusTcpClient
    MODBUS_AVAILABLE = True
except ImportError:
    MODBUS_AVAILABLE = False
    log.error("pymodbus required: pip install pymodbus")

try:
    from db import store, init_db, query, get_latest_device_firmware
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    log.warning("db.py not available — SQLite storage disabled")


# =============================================================================
# Path Setup
# =============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == 'scripts' else SCRIPT_DIR
DATA_DIR = PROJECT_ROOT / 'data'
_web_env = os.getenv('WEB_DIR', '')
if _web_env and os.path.isabs(_web_env):
    WEB_DIR = Path(_web_env)
else:
    WEB_DIR = PROJECT_ROOT / (_web_env or 'web')


# =============================================================================
# Configuration
# =============================================================================

SITE_ID = os.getenv('SOLAREDGE_SITE_ID', '1241660')

# Default barn inverters. Each: (name, ip, port, unit, model)
_DEFAULT_INVERTERS = [
    {'name': 'Inverter 1', 'ip': '192.168.5.121', 'port': 1502, 'unit': 1, 'model': 'SE11400H'},
    {'name': 'Inverter 2', 'ip': '192.168.5.122', 'port': 1502, 'unit': 1, 'model': 'SE7600H'},
]


def load_inverters() -> list:
    """Inverter list from SOLAREDGE_MODBUS_INVERTERS env override, else defaults."""
    raw = os.getenv('SOLAREDGE_MODBUS_INVERTERS', '').strip()
    if not raw:
        return list(_DEFAULT_INVERTERS)
    out = []
    for i, part in enumerate(raw.split(',')):
        bits = [b.strip() for b in part.split(':')]
        if len(bits) < 2:
            continue
        name = bits[0] or f'Inverter {i + 1}'
        ip = bits[1]
        port = int(bits[2]) if len(bits) > 2 and bits[2] else 1502
        unit = int(bits[3]) if len(bits) > 3 and bits[3] else 1
        out.append({'name': name, 'ip': ip, 'port': port, 'unit': unit, 'model': ''})
    return out or list(_DEFAULT_INVERTERS)


# SunSpec inverter operating-state enum (model 101/102/103 I_Status)
SUNSPEC_STATE = {
    1: 'Off', 2: 'Sleeping', 3: 'Starting', 4: 'Producing',
    5: 'Throttled', 6: 'ShuttingDown', 7: 'Fault', 8: 'Standby',
}


# =============================================================================
# SunSpec decode helpers
# =============================================================================

def _u16(v):
    """uint16 with NOT_IMPLEMENTED sentinel -> None."""
    return None if v == 0xFFFF else v


def _i16(v):
    """int16 with NOT_IMPLEMENTED sentinel (0x8000) -> None."""
    if v == 0x8000:
        return None
    return v - 0x10000 if v >= 0x8000 else v


def _sf(v):
    """Scale factor register -> multiplier (10**sf), or None if unusable.

    SunSpec scale factors are small signed exponents (roughly -10..10). If a
    register is misread (NA sentinel, wrong offset), the exponent can be wild;
    clamp to a sane range and treat anything outside it as 'no usable value'
    rather than overflowing.
    """
    if v == 0x8000:
        return None
    s = v - 0x10000 if v >= 0x8000 else v
    if s < -10 or s > 10:
        return None
    return 10.0 ** s


def _acc32(hi, lo):
    """uint32 accumulator; 0 or all-FF -> None."""
    val = (hi << 16) | lo
    if val in (0x00000000, 0xFFFFFFFF):
        return None
    return val


def _scaled(raw, sf_raw, signed=True):
    """Apply scale factor to a raw register, honoring NA sentinels."""
    v = _i16(raw) if signed else _u16(raw)
    if v is None:
        return None
    mult = _sf(sf_raw)
    if mult is None:
        return None
    return v * mult


def _regs_to_str(regs):
    """Decode a SunSpec ASCII string block (2 chars per register)."""
    b = bytearray()
    for r in regs:
        b.append((r >> 8) & 0xFF)
        b.append(r & 0xFF)
    return b.decode('ascii', errors='ignore').replace('\x00', '').strip()


def normalize_firmware(v):
    """Canonical firmware form: strip per-segment zero padding.

    SunSpec C_Version is zero-padded ('0004.0024.0025'); store the human form
    mySolarEdge shows ('4.24.25') so the DB has one representation regardless of
    collection method. Non-numeric versions are left untouched.
    """
    if not v:
        return v
    parts = v.split('.')
    if parts and all(p.isdigit() for p in parts):
        return '.'.join(str(int(p)) for p in parts)
    return v


# =============================================================================
# Modbus read
# =============================================================================

def read_inverter(inv: dict) -> dict:
    """Read one inverter's SunSpec common + model-1xx block. Returns dict or None.

    Two targeted reads (common @40000, inverter @40069) — the inverter block
    offsets are validated against the live portal numbers.
    """
    ip, port, unit = inv['ip'], inv['port'], inv['unit']
    client = ModbusTcpClient(ip, port=port, timeout=5)
    try:
        if not client.connect():
            log.warning(f"{inv['name']} ({ip}): connect failed")
            return None

        common = client.read_holding_registers(40000, count=70, device_id=unit)
        if common.isError():
            log.warning(f"{inv['name']} ({ip}): common block error {common}")
            return None
        c = common.registers

        block = client.read_holding_registers(40069, count=40, device_id=unit)
        if block.isError():
            log.warning(f"{inv['name']} ({ip}): inverter block error {block}")
            return None
        r = block.registers
    except Exception as e:
        log.warning(f"{inv['name']} ({ip}): exception {e!r}")
        return None
    finally:
        client.close()

    # Common block: serial @40052-40067 (idx 52-67), model @40020-40035 (idx 20-35),
    # firmware/C_Version @40044-40051 (idx 44-51)
    serial = _regs_to_str(c[52:68]) or f"{inv['name'].replace(' ', '')}-{ip}"
    model = _regs_to_str(c[20:36]) or inv.get('model', '')
    firmware = normalize_firmware(_regs_to_str(c[44:52])) or None

    # Inverter block decode (indices relative to 40069)
    ac_w = _scaled(r[14], r[15], signed=True)
    ac_a = _scaled(r[2], r[6], signed=False)
    ac_v = _scaled(r[10], r[13], signed=False)
    hz = _scaled(r[16], r[17], signed=False)
    ac_va = None
    ac_var = None
    ac_pf = None
    wh_acc = _acc32(r[24], r[25])
    wh_sf = _sf(r[26])
    wh = wh_acc * wh_sf if (wh_acc is not None and wh_sf is not None) else None
    dc_a = _scaled(r[27], r[28], signed=False)
    dc_v = _scaled(r[29], r[30], signed=False)
    dc_w = _scaled(r[31], r[32], signed=True)
    temp = _scaled(r[34], r[37], signed=True)
    state_raw = _u16(r[38])
    status = SUNSPEC_STATE.get(state_raw, str(state_raw) if state_raw is not None else None)
    status_vendor = _u16(r[39]) if len(r) > 39 else None

    return {
        'name': inv['name'],
        'ip': ip,
        'inverter_id': serial,
        'model': model,
        'firmware': firmware,
        'ac_power_w': round(ac_w, 1) if ac_w is not None else None,
        'ac_current_a': round(ac_a, 2) if ac_a is not None else None,
        'ac_voltage_v': round(ac_v, 1) if ac_v is not None else None,
        'ac_frequency_hz': round(hz, 2) if hz is not None else None,
        'ac_va': round(ac_va, 1) if ac_va is not None else None,
        'ac_var': round(ac_var, 1) if ac_var is not None else None,
        'ac_pf': round(ac_pf, 2) if ac_pf is not None else None,
        'ac_energy_wh': round(wh, 1) if wh is not None else None,
        'dc_power_w': round(dc_w, 1) if dc_w is not None else None,
        'dc_voltage_v': round(dc_v, 1) if dc_v is not None else None,
        'dc_current_a': round(dc_a, 2) if dc_a is not None else None,
        'temperature_c': round(temp, 1) if temp is not None else None,
        'status': status,
        'status_vendor': str(status_vendor) if status_vendor is not None else None,
    }


# =============================================================================
# Derived "today" energy from accumulator delta (reset-guarded)
# =============================================================================

def today_energy_wh(inverter_id: str, current_wh):
    """Today's Wh = current lifetime accumulator minus the first reading today.

    Reset guard: if the delta is negative (counter reset mid-day, e.g. firmware
    update), return None rather than a bogus value.
    """
    if not DB_AVAILABLE or current_wh is None:
        return None
    midnight = datetime.now().strftime('%Y-%m-%d 00:00:00')
    try:
        rows = query(
            "SELECT ac_energy_wh FROM solaredge_inverter_readings "
            "WHERE inverter_id = ? AND ac_energy_wh IS NOT NULL AND ac_energy_wh > 0 "
            "AND timestamp >= ? ORDER BY timestamp ASC LIMIT 1",
            (inverter_id, midnight),
        )
    except Exception as e:
        log.debug(f"today_energy_wh query failed for {inverter_id}: {e}")
        return None
    if not rows:
        return 0.0
    baseline = rows[0].get('ac_energy_wh')
    if baseline is None:
        return 0.0
    delta = current_wh - baseline
    return round(delta, 1) if delta >= 0 else None


def _stat(sql, params):
    """Single-row aggregate helper returning the first column or None."""
    if not DB_AVAILABLE:
        return None
    try:
        rows = query(sql, params)
    except Exception:
        return None
    if not rows:
        return None
    first = rows[0]
    return next(iter(first.values()), None)


def track_firmware(d: dict, ts: str):
    """Write a device_inventory row only when firmware/model changes.

    Replicates the change-only behavior of collect_device_inventory.py so a
    SolarEdge firmware push is captured within one cycle. No-ops when nothing
    has changed since the last recorded entry.
    """
    if not DB_AVAILABLE:
        return
    serial = d['inverter_id']
    fw = d.get('firmware')
    model = d.get('model')
    try:
        prev = get_latest_device_firmware('solaredge', serial)
    except Exception as e:
        log.debug(f"firmware lookup failed for {serial}: {e}")
        prev = None

    changed = (
        prev is None
        or prev.get('firmware') != fw
        or prev.get('model') != model
    )
    if not changed:
        return

    try:
        store.device_inventory(
            system='solaredge',
            serial_number=serial,
            device_type='inverter',
            model=model,
            firmware=fw,
            extra_json=json.dumps({'name': d.get('name'), 'ip': d.get('ip')}),
            timestamp=ts,
        )
        if prev is None:
            log.info(f"Device inventory: {d.get('name')} ({serial}) fw={fw} model={model} [new]")
        else:
            log.info(
                f"Device inventory: {d.get('name')} ({serial}) firmware "
                f"{prev.get('firmware')} -> {fw}"
            )
    except Exception as e:
        log.warning(f"device_inventory write failed for {serial}: {e}")


# =============================================================================
# Dashboard JSON (solar_barn.json) — schema matches the Solar Arrays tab
# =============================================================================

def build_dashboard_json(readings: list, ok_count: int, total_count: int) -> dict:
    now = datetime.now()
    epoch = int(now.timestamp())

    inverters = {}
    watt_values = []
    total_today_wh = 0.0
    total_lifetime_wh = 0.0

    for d in readings:
        sn = d['inverter_id']
        cur_w = d['ac_power_w'] or 0
        watt_values.append(cur_w)

        t_wh = today_energy_wh(sn, d['ac_energy_wh'])
        if t_wh:
            total_today_wh += t_wh
        if d['ac_energy_wh']:
            total_lifetime_wh += d['ac_energy_wh']

        max_today = _stat(
            "SELECT MAX(ac_power_w) FROM solaredge_inverter_readings "
            "WHERE inverter_id = ? AND timestamp >= ?",
            (sn, now.strftime('%Y-%m-%d 00:00:00')),
        ) or 0
        max_ever = _stat(
            "SELECT MAX(ac_power_w) FROM solaredge_inverter_readings WHERE inverter_id = ?",
            (sn,),
        ) or 0
        max_today = max(max_today, cur_w)
        max_ever = max(max_ever, cur_w)
        samples_today = _stat(
            "SELECT COUNT(*) FROM solaredge_inverter_readings "
            "WHERE inverter_id = ? AND timestamp >= ?",
            (sn, now.strftime('%Y-%m-%d 00:00:00')),
        ) or 0

        inverters[sn] = {
            'serial': sn,
            'current_watts': round(cur_w),
            'max_ever_watts': round(max_ever),
            'last_report_time': epoch,
            'last_report_human': now.strftime('%H:%M:%S'),
            'dev_type': 0,
            'max_today_watts': round(max_today),
            'samples_today': samples_today,
            'avg_today_watts': 0,
            'manufacturer': 'SolarEdge',
            'model': d['model'] or '',
            'firmware': d.get('firmware') or '',
            'optimizer': False,
            'inverter_level': True,
            'parent_inverter': '',
            'parent_name': d['name'],
            'string': '',
            'health_status': d['status'] or '',
            'health_ratio_vs_string': 1.0,
            'health_ratio_vs_array': 1.0,
            'today_wh': t_wh if t_wh is not None else 0,
            'lifetime_wh': d['ac_energy_wh'] or 0,
            'dc_watts': round(d['dc_power_w']) if d['dc_power_w'] is not None else 0,
            'voltage_v': d['ac_voltage_v'] or 0,
            'frequency_hz': d['ac_frequency_hz'] or 0,
            'temperature_c': d['temperature_c'] if d['temperature_c'] is not None else 0,
        }

    total_watts = sum(watt_values)
    active_now = sum(1 for w in watt_values if w > 0)
    avg_watts = total_watts / len(watt_values) if watt_values else 0
    max_w = max(watt_values) if watt_values else 0
    min_w = min((w for w in watt_values if w > 0), default=0)
    spread = max_w - min_w if active_now > 0 else 0

    status = 'ok' if ok_count == total_count else ('partial' if ok_count > 0 else 'error')

    return {
        'timestamp': now.isoformat(),
        'timestamp_epoch': epoch,
        'array_id': 'barn',
        'array_name': 'Barn',
        'array_type': 'solaredge',
        'gateway': {
            'ip': 'modbus',
            'serial': SITE_ID,
            'model': 'SolarEdge',
        },
        'summary': {
            'total_watts': round(total_watts),
            'total_kw': round(total_watts / 1000, 2),
            'panel_count': len(inverters),
            'active_count': active_now,
            'average_watts': round(avg_watts),
            'min_watts': round(min_w),
            'max_watts': round(max_w),
            'total_max_ever': 0,
            'spread': round(spread),
            'today_kwh': round(total_today_wh / 1000, 2),
            'lifetime_spread_pct': 0,
        },
        'production': {
            'inverters_wh_lifetime': round(total_lifetime_wh),
            'inverters_wh_today': round(total_today_wh),
            'inverters_active': active_now,
            'meter_w_now': round(total_watts, 1),
            'meter_wh_today': float(round(total_today_wh)),
            'meter_wh_lifetime': float(round(total_lifetime_wh)),
        },
        'inverters': inverters,
        'underperformers': [],
        'layout': {},
        'collection_status': status,
        'source': 'modbus_tcp',
    }


def write_dashboard_json(output: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    for path in [DATA_DIR / 'solar_barn.json', WEB_DIR / 'solar_barn.json']:
        try:
            with open(path, 'w') as f:
                json.dump(output, f, indent=2)
        except IOError as e:
            log.error(f"Failed to write {path}: {e}")
    log.info(
        f"Dashboard JSON: {output['summary']['active_count']}/"
        f"{output['summary']['panel_count']} inverters active, "
        f"{output['summary']['total_watts']}W total → solar_barn.json"
    )


# =============================================================================
# Collection cycle
# =============================================================================

def collect_once() -> dict:
    inverters = load_inverters()
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    readings = []
    for inv in inverters:
        d = read_inverter(inv)
        if d is None:
            continue
        readings.append(d)

        if DB_AVAILABLE:
            store.solaredge_inverter_reading(
                inverter_id=d['inverter_id'],
                site_id=SITE_ID,
                ac_power_w=d['ac_power_w'],
                ac_energy_wh=d['ac_energy_wh'],
                ac_voltage_v=d['ac_voltage_v'],
                ac_current_a=d['ac_current_a'],
                ac_frequency_hz=d['ac_frequency_hz'],
                ac_va=d['ac_va'],
                ac_var=d['ac_var'],
                ac_pf=d['ac_pf'],
                dc_power_w=d['dc_power_w'],
                dc_voltage_v=d['dc_voltage_v'],
                dc_current_a=d['dc_current_a'],
                status=d['status'],
                status_vendor=d['status_vendor'],
                temperature_c=d['temperature_c'],
                source='modbus_tcp',
                timestamp=ts,
            )
            track_firmware(d, ts)

    ok_count = len(readings)
    total_count = len(inverters)

    dashboard = build_dashboard_json(readings, ok_count, total_count)
    write_dashboard_json(dashboard)

    parts = [
        f"{d['name']}={d['ac_power_w']:.0f}W "
        f"({d['status']}, {d['temperature_c']}C, {(d['ac_energy_wh'] or 0)/1000:.0f}kWh)"
        for d in readings
    ]
    log.info(
        f"Barn: {dashboard['summary']['total_kw']}kW total | "
        + " | ".join(parts) if parts else "Barn: no inverters reachable"
    )

    return {
        'timestamp': ts,
        'ok': ok_count,
        'total': total_count,
        'total_kw': dashboard['summary']['total_kw'],
        'today_kwh': dashboard['summary']['today_kwh'],
        'inverters': readings,
        'collection_status': dashboard['collection_status'],
    }


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='SolarEdge Inverter-Level Collector (Modbus TCP → SQLite + Dashboard JSON)'
    )
    parser.add_argument('--loop', action='store_true', help='Run continuously')
    parser.add_argument('--interval', type=int, default=300,
                        help='Seconds between reads in loop mode (default 300)')
    parser.add_argument('--json', action='store_true',
                        help='Print result JSON to stdout (single-shot)')
    args = parser.parse_args()

    if not MODBUS_AVAILABLE:
        sys.exit(1)

    if DB_AVAILABLE:
        init_db()

    running = True

    def handle_signal(sig, frame):
        nonlocal running
        running = False
        log.info("Shutdown signal received")

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    if args.loop:
        invs = load_inverters()
        log.info(
            "Starting continuous collection: "
            + ", ".join(f"{i['name']}@{i['ip']}:{i['port']}" for i in invs)
            + f" every {args.interval}s"
        )
        while running:
            collect_once()
            if running:
                time.sleep(args.interval)
    else:
        result = collect_once()
        if args.json:
            print(json.dumps(result, indent=2, default=str))
        if result['ok'] == 0:
            sys.exit(1)

    log.info("Done.")


if __name__ == '__main__':
    main()
