#!/usr/bin/env python3
"""
collect_solar_enphase.py — Consolidated Enphase Solar Collector

Single collector that does everything:
  1. Queries local Enphase IQ Gateway (one set of API calls)
  2. Stores readings to SQLite (enphase_readings table)
  3. Generates solar_{array_id}.json for dashboard + data_sources.py
  4. Tracks per-panel daily history (rolling 30 days)
  5. Runs panel health analysis (peer comparison)
  6. Detects array-level issues (offline, stale envoy)

Replaces both the old collect_enphase.py (dashboard JSON) and the
original collect_solar_enphase.py (SQLite only) with a single script
that hits the gateway once per cycle.

Supports multiple Enphase gateways via --array-id (reads SOLAR_ARRAY_{ID}_* env).
Falls back to legacy ENPHASE_* vars when no array-id is given.

Usage:
    python3 collect_solar_enphase.py --array-id house
    python3 collect_solar_enphase.py --loop --interval 300
    python3 collect_solar_enphase.py

Output:
    SQLite: /app/data/franklin.db (enphase_readings table)
    JSON:   data/solar_{array_id}.json + web/solar_{array_id}.json
    JSON:   data/enphase_daily_history_{array_id}.json (rolling 30-day)
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
    format='%(asctime)s [enphase] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('collect_solar_enphase')

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    log.error("requests module required: pip install requests")

try:
    from db import store, init_db
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
# Panel Specifications & Health Thresholds
# =============================================================================

PANEL_SPECS = {
    'house': {
        'model': 'Hyundai HiN-T435NF(BK)',
        'nameplate_w': 435,
        'ptc_w': 407.2,
        'install_date': '2025-09-05',
        'tilt_deg': 22,
        'azimuth_deg': 295,
        'microinverter': 'Enphase IQ8M-72-M-US',
        'degradation_year1_pct': 2.0,
        'degradation_annual_pct': 0.5,
        'circuits': 2,
        'panels_per_circuit': 8,
    },
}

HEALTH_THRESHOLDS = {
    'excellent': 1.05,
    'good': 0.95,
    'fair': 0.85,
    'watch': 0.75,
}


# =============================================================================
# Configuration
# =============================================================================

def load_config(array_id: str = None) -> dict:
    """Load gateway config from environment variables."""
    if array_id:
        prefix = f"SOLAR_ARRAY_{array_id.upper()}_"
        return {
            'array_id': array_id.lower(),
            'name': os.getenv(f'{prefix}NAME', array_id.title()),
            'type': os.getenv(f'{prefix}TYPE', 'enphase'),
            'ip': os.getenv(f'{prefix}IP', ''),
            'serial': os.getenv(f'{prefix}SERIAL', ''),
            'email': os.getenv(f'{prefix}EMAIL', ''),
            'password': os.getenv(f'{prefix}PASSWORD', ''),
            'model': os.getenv(f'{prefix}MODEL', 'IQ8'),
            'token_file': os.getenv(f'{prefix}TOKEN_FILE',
                                    f'/app/data/enphase_token_{array_id.lower()}.txt'),
            'layout_file': os.getenv(f'{prefix}LAYOUT_FILE',
                                     str(DATA_DIR / f'enphase_array_layout_{array_id.lower()}.json')),
        }
    return {
        'array_id': 'house',
        'name': os.getenv('ENPHASE_ARRAY_NAME', 'Solar Array'),
        'type': 'enphase',
        'ip': os.getenv('ENPHASE_ENVOY_IP', os.getenv('ENPHASE_IP', '')),
        'serial': os.getenv('ENPHASE_ENVOY_SERIAL', ''),
        'email': os.getenv('ENPHASE_EMAIL', ''),
        'password': os.getenv('ENPHASE_PASSWORD', ''),
        'model': os.getenv('ENPHASE_MODEL', 'IQ8'),
        'token_file': os.getenv('ENPHASE_TOKEN_FILE', '/app/data/enphase_token.txt'),
        'layout_file': os.getenv('ENPHASE_ARRAY_LAYOUT',
                                 str(DATA_DIR / 'enphase_array_layout.json')),
    }


# =============================================================================
# Token Management
# =============================================================================

def get_token(cfg: dict) -> str:
    token_path = Path(cfg['token_file'])
    if token_path.exists():
        token = token_path.read_text().strip()
        if token and len(token) > 50:
            return token
    return None


def refresh_token(cfg: dict) -> str:
    if not cfg['email'] or not cfg['password'] or not cfg['serial']:
        log.error("Cannot refresh token: email, password, and serial required")
        return None

    log.info(f"Fetching new Enphase token for {cfg['array_id']}...")

    try:
        login_resp = requests.post(
            'https://enlighten.enphaseenergy.com/login/login.json?',
            data={
                'user[email]': cfg['email'],
                'user[password]': cfg['password'],
            },
            timeout=30,
        )
        if login_resp.status_code != 200:
            log.error(f"Enphase login failed (HTTP {login_resp.status_code})")
            return None

        session_id = login_resp.json().get('session_id')
        if not session_id:
            log.error("No session_id in login response")
            return None

        token_resp = requests.post(
            'https://entrez.enphaseenergy.com/tokens',
            json={
                'session_id': session_id,
                'serial_num': cfg['serial'],
                'username': cfg['email'],
            },
            timeout=30,
        )
        if token_resp.status_code != 200:
            log.error(f"Token request failed (HTTP {token_resp.status_code})")
            return None

        token = token_resp.text.strip()
        if not token or len(token) < 50:
            log.error(f"Invalid token received (length={len(token)})")
            return None

        token_path = Path(cfg['token_file'])
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(token)
        log.info(f"New token saved to {token_path}")
        return token

    except requests.RequestException as e:
        log.error(f"Token fetch error: {e}")
        return None


def get_or_refresh_token(cfg: dict) -> str:
    token = get_token(cfg)
    if token:
        return token
    return refresh_token(cfg)


def invalidate_and_refresh(cfg: dict) -> str:
    token_path = Path(cfg['token_file'])
    if token_path.exists():
        token_path.unlink()
    return refresh_token(cfg)


# =============================================================================
# Gateway API Queries
# =============================================================================

def query_inverters(ip: str, token: str) -> list:
    try:
        resp = requests.get(
            f'https://{ip}/api/v1/production/inverters',
            headers={'Authorization': f'Bearer {token}'},
            verify=False, timeout=10,
        )
        if resp.status_code == 401:
            return None
        if resp.status_code != 200:
            log.error(f"Inverter query failed (HTTP {resp.status_code})")
            return []
        return resp.json()
    except requests.RequestException as e:
        log.error(f"Inverter query error: {e}")
        return []


def query_production(ip: str, token: str) -> dict:
    try:
        resp = requests.get(
            f'https://{ip}/production.json',
            headers={'Authorization': f'Bearer {token}'},
            verify=False, timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException as e:
        log.debug(f"Production query failed: {e}")
    return {}


def query_inventory(ip: str, token: str) -> list:
    try:
        resp = requests.get(
            f'https://{ip}/inventory.json',
            headers={'Authorization': f'Bearer {token}'},
            verify=False, timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException as e:
        log.debug(f"Inventory query failed: {e}")
    return []


def query_meter_readings(ip: str, token: str) -> list:
    try:
        resp = requests.get(
            f'https://{ip}/ivp/meters/readings',
            headers={'Authorization': f'Bearer {token}'},
            verify=False, timeout=10,
        )
        if resp.status_code == 200:
            return resp.json()
    except requests.RequestException as e:
        log.debug(f"Meter readings query failed: {e}")
    return []


# =============================================================================
# Response Parsers
# =============================================================================

def parse_inventory(inv_data: list, inverter_serials: set) -> dict:
    result = {}
    for device_group in inv_data:
        for device in device_group.get('devices', []):
            sn = device.get('serial_num', '')
            if sn in inverter_serials:
                result[sn] = {
                    'producing': device.get('producing', False),
                    'communicating': device.get('communicating', False),
                    'operating': device.get('operating', False),
                    'phase': device.get('phase', None),
                    'firmware': device.get('img_pnum_running', ''),
                }
    return result


def parse_meter_readings(meter_data: list) -> dict:
    result = {
        'meter_voltage_v': None,
        'meter_current_a': None,
        'meter_frequency_hz': None,
        'meter_power_factor': None,
        'meter_apparent_power_va': None,
        'meter_reactive_power_var': None,
        'meter_voltage_l1': None,
        'meter_voltage_l2': None,
    }
    for meter in meter_data:
        eid = meter.get('eid', 0)
        if eid == 0:
            continue
        if meter.get('actEnergyDlvd', 0) > 0 or eid % 256 < 128:
            result['meter_voltage_v'] = meter.get('voltage')
            result['meter_current_a'] = meter.get('current')
            result['meter_frequency_hz'] = meter.get('freq')
            result['meter_power_factor'] = meter.get('pwrFactor')
            result['meter_apparent_power_va'] = meter.get('apparentPower')
            result['meter_reactive_power_var'] = meter.get('reactivePower')
            channels = meter.get('channels', [])
            if len(channels) >= 2:
                result['meter_voltage_l1'] = channels[0].get('voltage')
                result['meter_voltage_l2'] = channels[1].get('voltage')
            break
    return result


def parse_production(data: dict) -> dict:
    result = {
        'meter_w': None,
        'meter_wh_today': None,
        'meter_wh_lifetime': None,
        'inverter_active_count': None,
        'inverter_wh_today': None,
        'inverter_wh_lifetime': None,
        'consumption_w': None,
        'consumption_wh_today': None,
        'net_consumption_w': None,
    }
    for p in data.get('production', []):
        if p.get('type') == 'eim' and p.get('measurementType', '') == 'production':
            result['meter_w'] = p.get('wNow')
            result['meter_wh_today'] = p.get('whToday')
            result['meter_wh_lifetime'] = p.get('whLifetime')
        elif p.get('type') == 'inverters':
            result['inverter_active_count'] = p.get('activeCount')
            result['inverter_wh_today'] = p.get('whToday')
            result['inverter_wh_lifetime'] = p.get('whLifetime')
    for c in data.get('consumption', []):
        if c.get('measurementType') == 'total-consumption':
            result['consumption_w'] = c.get('wNow')
            result['consumption_wh_today'] = c.get('whToday')
        elif c.get('measurementType') == 'net-consumption':
            result['net_consumption_w'] = c.get('wNow')
    return result


# =============================================================================
# Daily History Tracking (rolling 30-day per-panel stats)
# =============================================================================

def update_daily_history(cfg: dict, inverters: list) -> dict:
    today = datetime.now().strftime('%Y-%m-%d')
    history_file = DATA_DIR / f"enphase_daily_history_{cfg['array_id']}.json"
    history = {}

    if history_file.exists():
        try:
            with open(history_file, 'r') as f:
                history = json.load(f)
        except (json.JSONDecodeError, IOError):
            history = {}

    if today not in history:
        history[today] = {}

    for inv in inverters:
        serial = inv.get('serialNumber', '')
        watts = inv.get('lastReportWatts', 0)
        max_watts = inv.get('maxReportWatts', 0)

        if serial not in history[today]:
            history[today][serial] = {
                'first_seen_watts': watts,
                'max_watts_today': watts,
                'peak_max': max_watts,
                'samples': 0,
                'cumulative_watts': 0,
            }

        entry = history[today][serial]
        entry['max_watts_today'] = max(entry['max_watts_today'], watts)
        entry['samples'] += 1
        entry['cumulative_watts'] += watts
        entry['last_watts'] = watts

    dates = sorted(history.keys())
    if len(dates) > 30:
        for old_date in dates[:-30]:
            del history[old_date]

    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(history_file, 'w') as f:
            json.dump(history, f, indent=2)
    except IOError as e:
        log.warning(f"Failed to save daily history: {e}")

    return history


def load_array_layout(cfg: dict) -> dict:
    layout_path = Path(cfg['layout_file'])
    if layout_path.exists():
        try:
            with open(layout_path, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            log.warning(f"Failed to load array layout: {e}")
    return {}


# =============================================================================
# Panel Health Analysis
# =============================================================================

def calc_panel_age_years(install_date_str):
    install = datetime.strptime(install_date_str, '%Y-%m-%d')
    delta = datetime.now() - install
    return round(delta.days / 365.25, 2)


def calc_expected_degradation(spec):
    age = calc_panel_age_years(spec['install_date'])
    if age <= 0:
        return {'age_years': 0, 'expected_pct_remaining': 100.0,
                'effective_ptc_w': spec['ptc_w']}
    year1_factor = 1.0 - (spec['degradation_year1_pct'] / 100)
    if age <= 1:
        factor = 1.0 - (spec['degradation_year1_pct'] / 100) * age
    else:
        additional_years = age - 1
        factor = year1_factor * (1 - spec['degradation_annual_pct'] / 100 * additional_years)
    return {
        'age_years': age,
        'expected_pct_remaining': round(factor * 100, 1),
        'effective_ptc_w': round(spec['ptc_w'] * factor, 1),
    }


def classify_health(ratio):
    if ratio >= HEALTH_THRESHOLDS['excellent']:
        return 'excellent'
    elif ratio >= HEALTH_THRESHOLDS['good']:
        return 'good'
    elif ratio >= HEALTH_THRESHOLDS['fair']:
        return 'fair'
    elif ratio >= HEALTH_THRESHOLDS['watch']:
        return 'watch'
    return 'alert'


def analyze_health(inverter_data, array_id='house'):
    spec = PANEL_SPECS.get(array_id)
    if not spec:
        return {}

    panels = list(inverter_data.values())
    if not panels:
        return {}

    degradation = calc_expected_degradation(spec)

    current_watts = [p.get('current_watts', 0) for p in panels]
    producing = [w for w in current_watts if w > 0]
    realtime_analysis = {}
    if len(producing) >= 4:
        rt_avg = sum(producing) / len(producing)
        for serial, p in inverter_data.items():
            w = p.get('current_watts', 0)
            if w > 0:
                ratio = w / rt_avg if rt_avg > 0 else 1.0
                realtime_analysis[serial] = {
                    'ratio_vs_array': round(ratio, 3),
                    'status': classify_health(ratio),
                }

    daily_avgs = {s: p.get('avg_today_watts', 0) for s, p in inverter_data.items()}
    active_avgs = {s: v for s, v in daily_avgs.items() if v > 0}
    daily_analysis = {}
    if len(active_avgs) >= 4:
        day_mean = sum(active_avgs.values()) / len(active_avgs)
        for serial, avg in active_avgs.items():
            ratio = avg / day_mean if day_mean > 0 else 1.0
            daily_analysis[serial] = {
                'ratio_vs_array_today': round(ratio, 3),
                'status_today': classify_health(ratio),
            }

    max_evers = {s: p.get('max_ever_watts', 0) for s, p in inverter_data.items()}
    active_maxes = {s: v for s, v in max_evers.items() if v > 0}
    lifetime_analysis = {}
    if active_maxes:
        max_avg = sum(active_maxes.values()) / len(active_maxes)
        for serial, mx in active_maxes.items():
            ratio = mx / max_avg if max_avg > 0 else 1.0
            lifetime_analysis[serial] = {
                'ratio_vs_lifetime': round(ratio, 3),
                'status_lifetime': classify_health(ratio),
            }

    per_panel = {}
    for serial in inverter_data:
        per_panel[serial] = {}
        if serial in realtime_analysis:
            per_panel[serial].update(realtime_analysis[serial])
        if serial in daily_analysis:
            per_panel[serial].update(daily_analysis[serial])
        if serial in lifetime_analysis:
            per_panel[serial].update(lifetime_analysis[serial])

    underperformers = []
    for serial, analysis in per_panel.items():
        status = analysis.get('status_today', analysis.get('status', 'unknown'))
        if status in ('watch', 'alert'):
            underperformers.append({
                'serial': serial,
                'status': status,
                'ratio': analysis.get('ratio_vs_array_today',
                                      analysis.get('ratio_vs_array', 0)),
            })

    return {
        'panel_specs': {
            'model': spec['model'],
            'nameplate_w': spec['nameplate_w'],
            'ptc_w': spec['ptc_w'],
        },
        'degradation': degradation,
        'per_panel': per_panel,
        'underperformers': underperformers,
        'timestamp': datetime.now().isoformat(),
    }


# =============================================================================
# Array-Level Health (offline/stale detection)
# =============================================================================

def check_array_health(cfg: dict, total_watts: int, wh_today: float) -> dict:
    aid = cfg['array_id']
    state_file = DATA_DIR / f"array_health_state_{aid}.json"
    now = datetime.now()
    hour = now.hour
    is_solar_hours = 10 <= hour <= 15

    prev_state = {}
    if state_file.exists():
        try:
            with open(state_file, 'r') as f:
                prev_state = json.load(f)
        except (json.JSONDecodeError, IOError):
            prev_state = {}

    zero_streak = prev_state.get('zero_streak', 0)
    stale_streak = prev_state.get('stale_streak', 0)

    if is_solar_hours:
        if total_watts == 0:
            zero_streak += 1
        else:
            zero_streak = 0

        prev_wh = prev_state.get('wh_today', None)
        if prev_wh is not None and wh_today == prev_wh and total_watts > 0:
            stale_streak += 1
        elif wh_today != prev_wh:
            stale_streak = 0
    else:
        zero_streak = 0
        stale_streak = 0

    status = 'ok'
    issues = []

    if zero_streak >= 2:
        status = 'offline'
        issues.append(f"Zero production for {zero_streak} consecutive daytime checks")
        log.warning(f"ARRAY HEALTH [{aid}]: OFFLINE — zero production for "
                    f"{zero_streak} consecutive collections")

    if stale_streak >= 2:
        status = 'stale' if status == 'ok' else status
        issues.append(f"Envoy wh_today unchanged ({wh_today} Wh) for "
                      f"{stale_streak} consecutive checks")
        log.warning(f"ARRAY HEALTH [{aid}]: STALE ENVOY — wh_today stuck at "
                    f"{wh_today} Wh for {stale_streak} checks")

    health = {
        'status': status,
        'issues': issues,
        'zero_streak': zero_streak,
        'stale_streak': stale_streak,
        'checked_at': now.isoformat(),
        'is_solar_hours': is_solar_hours,
    }

    new_state = {
        'wh_today': wh_today,
        'zero_streak': zero_streak,
        'stale_streak': stale_streak,
        'total_watts': total_watts,
        'checked_at': now.isoformat(),
    }
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, 'w') as f:
            json.dump(new_state, f, indent=2)
    except IOError:
        pass

    return health


# =============================================================================
# Dashboard JSON Builder
# =============================================================================

def build_dashboard_json(cfg: dict, inverters: list, production_raw: dict,
                         all_daily_history: dict, today_daily: dict,
                         layout: dict, array_health: dict) -> dict:
    """Build the solar_{array_id}.json consumed by dashboard and data_sources.py."""
    now = datetime.now()

    inverter_data = {}
    total_watts = 0
    total_max_ever = 0
    active_count = 0

    for inv in inverters:
        serial = inv.get('serialNumber', '')
        current_watts = inv.get('lastReportWatts', 0)
        firmware_max = inv.get('maxReportWatts', 0)
        last_report = inv.get('lastReportDate', 0)
        dev_type = inv.get('devType', 0)

        best_watts = firmware_max
        for d_date, d_panels in all_daily_history.items():
            if serial in d_panels:
                day_max = d_panels[serial].get('max_watts_today', 0)
                day_peak = d_panels[serial].get('peak_max', 0)
                best_watts = max(best_watts, day_max, day_peak)
        max_ever = best_watts or current_watts

        total_watts += current_watts
        total_max_ever += max_ever
        if current_watts > 0:
            active_count += 1

        daily = today_daily.get(serial, {})

        inverter_data[serial] = {
            'serial': serial,
            'current_watts': current_watts,
            'max_ever_watts': max_ever,
            'last_report_time': last_report,
            'last_report_human': (
                datetime.fromtimestamp(last_report).strftime('%H:%M:%S')
                if last_report else 'N/A'
            ),
            'dev_type': dev_type,
            'max_today_watts': daily.get('max_watts_today', 0),
            'samples_today': daily.get('samples', 0),
            'avg_today_watts': (
                round(daily['cumulative_watts'] / daily['samples'], 1)
                if daily.get('samples', 0) > 0 else 0
            ),
        }

    watt_values = [inv.get('lastReportWatts', 0) for inv in inverters]
    avg_watts = sum(watt_values) / len(watt_values) if watt_values else 0

    underperformers = []
    if avg_watts > 5:
        for inv in inverters:
            if inv.get('lastReportWatts', 0) < avg_watts * 0.7:
                underperformers.append({
                    'serial': inv['serialNumber'],
                    'watts': inv['lastReportWatts'],
                    'pct_of_avg': round(
                        inv['lastReportWatts'] / avg_watts * 100, 1
                    ),
                })

    prod_summary = {}
    for p in production_raw.get('production', []):
        if p.get('type') == 'inverters':
            prod_summary['inverters_wh_lifetime'] = p.get('whLifetime', 0)
            prod_summary['inverters_wh_today'] = p.get('whToday', 0)
            prod_summary['inverters_active'] = p.get('activeCount', 0)
        elif (p.get('type') == 'eim'
              and p.get('measurementType') == 'production'):
            prod_summary['meter_w_now'] = p.get('wNow', 0)
            prod_summary['meter_wh_today'] = p.get('whToday', 0)
            prod_summary['meter_wh_lifetime'] = p.get('whLifetime', 0)

    return {
        'timestamp': now.isoformat(),
        'timestamp_epoch': int(now.timestamp()),
        'array_id': cfg['array_id'],
        'array_name': cfg['name'],
        'array_type': cfg.get('type', 'enphase'),
        'gateway': {
            'ip': cfg['ip'],
            'serial': cfg['serial'],
            'model': cfg.get('model', ''),
        },
        'summary': {
            'total_watts': total_watts,
            'total_kw': round(total_watts / 1000, 2),
            'panel_count': len(inverters),
            'active_count': active_count,
            'average_watts': round(avg_watts, 1),
            'min_watts': min(watt_values) if watt_values else 0,
            'max_watts': max(watt_values) if watt_values else 0,
            'total_max_ever': total_max_ever,
            'spread': (max(watt_values) - min(watt_values))
                      if watt_values else 0,
        },
        'production': prod_summary,
        'inverters': inverter_data,
        'underperformers': underperformers,
        'layout': layout,
        'collection_status': 'ok',
        'health': analyze_health(inverter_data, cfg['array_id']),
        'array_health': array_health,
    }


# =============================================================================
# JSON Output
# =============================================================================

def write_dashboard_json(cfg: dict, output: dict):
    aid = cfg['array_id']
    fname = f"solar_{aid}.json"

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WEB_DIR.mkdir(parents=True, exist_ok=True)

    for path in [DATA_DIR / fname, WEB_DIR / fname]:
        try:
            with open(path, 'w') as f:
                json.dump(output, f, indent=2)
        except IOError as e:
            log.error(f"Failed to write {path}: {e}")

    log.info(
        f"Dashboard JSON: {output['summary']['panel_count']} panels, "
        f"{output['summary']['total_watts']}W total, "
        f"{output['summary']['active_count']} active → {fname}"
    )


def write_error_output(cfg: dict, error_msg: str):
    aid = cfg['array_id']
    fname = f"solar_{aid}.json"
    output = {
        'timestamp': datetime.now().isoformat(),
        'array_id': aid,
        'array_name': cfg.get('name', aid),
        'array_type': cfg.get('type', 'enphase'),
        'collection_status': 'error',
        'error': error_msg,
        'summary': {},
        'inverters': {},
        'layout': {},
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    for path in [DATA_DIR / fname, WEB_DIR / fname]:
        try:
            with open(path, 'w') as f:
                json.dump(output, f, indent=2)
        except IOError:
            pass


# =============================================================================
# Collection Cycle
# =============================================================================

def collect_once(cfg: dict) -> dict:
    """Run one full collection cycle for an Enphase array.

    Single gateway query set → SQLite DB + dashboard JSON.
    """
    if not cfg['ip']:
        log.error(f"No IP configured for array '{cfg['array_id']}'")
        write_error_output(cfg, "No IP configured")
        return None

    token = get_or_refresh_token(cfg)
    if not token:
        log.error(f"No token for {cfg['array_id']}")
        write_error_output(cfg, "Failed to obtain token")
        return None

    inverters = query_inverters(cfg['ip'], token)

    if inverters is None:
        log.info("Token expired, refreshing...")
        token = invalidate_and_refresh(cfg)
        if token:
            inverters = query_inverters(cfg['ip'], token)

    if not inverters:
        log.error(f"Failed to query inverters for {cfg['array_id']}")
        write_error_output(cfg, "Failed to query inverters")
        return None

    production_raw = query_production(cfg['ip'], token)
    production = parse_production(production_raw)

    inventory_raw = query_inventory(cfg['ip'], token)
    meter_readings_raw = query_meter_readings(cfg['ip'], token)

    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    inverter_sum_w = sum(i.get('lastReportWatts', 0) for i in inverters)
    panels_reporting = sum(1 for i in inverters if i.get('lastReportWatts', 0) > 0)
    panel_count = len(inverters)

    meter_w = production.get('meter_w') or 0
    curtailed_w = max(0, inverter_sum_w - meter_w) if production.get('meter_w') is not None else None

    inv_serials = {i.get('serialNumber', '') for i in inverters}
    inv_status = parse_inventory(inventory_raw, inv_serials) if inventory_raw else {}
    meter_detail = parse_meter_readings(meter_readings_raw) if meter_readings_raw else {}

    # --- SQLite storage ---
    panels_snapshot = []
    for i in inverters:
        sn = i.get('serialNumber', '')
        panel = {
            's': sn,
            'w': i.get('lastReportWatts', 0),
            'mx': i.get('maxReportWatts', 0),
            't': i.get('lastReportDate', 0),
        }
        st = inv_status.get(sn)
        if st:
            panel['p'] = st['producing']
            panel['c'] = st['communicating']
            panel['o'] = st['operating']
            panel['ph'] = st['phase']
            if st.get('firmware'):
                panel['fw'] = st['firmware']
        panels_snapshot.append(panel)

    if DB_AVAILABLE:
        store.enphase_reading(
            array_id=cfg['array_id'],
            inverter_sum_w=inverter_sum_w,
            meter_w=meter_w if production.get('meter_w') is not None else None,
            curtailed_w=curtailed_w,
            panel_count=panel_count,
            panels_reporting=panels_reporting,
            panels_json=json.dumps(panels_snapshot),
            meter_wh_today=production.get('meter_wh_today'),
            meter_wh_lifetime=production.get('meter_wh_lifetime'),
            inverter_wh_today=production.get('inverter_wh_today'),
            inverter_wh_lifetime=production.get('inverter_wh_lifetime'),
            consumption_w=production.get('consumption_w'),
            net_consumption_w=production.get('net_consumption_w'),
            meter_voltage_v=meter_detail.get('meter_voltage_v'),
            meter_frequency_hz=meter_detail.get('meter_frequency_hz'),
            meter_power_factor=meter_detail.get('meter_power_factor'),
            meter_voltage_l1=meter_detail.get('meter_voltage_l1'),
            meter_voltage_l2=meter_detail.get('meter_voltage_l2'),
            timestamp=ts,
        )

    # --- Dashboard JSON generation ---
    all_daily_history = update_daily_history(cfg, inverters)
    today_str = datetime.now().strftime('%Y-%m-%d')
    today_daily = all_daily_history.get(today_str, {})
    layout = load_array_layout(cfg)

    wh_today = production.get('meter_wh_today') or production.get('inverter_wh_today') or 0
    array_health = check_array_health(cfg, inverter_sum_w, wh_today)

    dashboard = build_dashboard_json(
        cfg, inverters, production_raw, all_daily_history,
        today_daily, layout, array_health
    )
    write_dashboard_json(cfg, dashboard)

    # --- Summary log ---
    meter_str = f"Meter={meter_w:.0f}W " if production.get('meter_w') is not None else ""
    curt_str = f"Curtailed={curtailed_w:.0f}W " if curtailed_w is not None else ""

    log.info(
        f"Array={cfg['array_id']} "
        f"Inverters={inverter_sum_w:.0f}W "
        f"{meter_str}"
        f"{curt_str}"
        f"Panels={panels_reporting}/{panel_count}"
    )

    return {
        'timestamp': ts,
        'array_id': cfg['array_id'],
        'inverter_sum_w': inverter_sum_w,
        'meter_w': meter_w,
        'curtailed_w': curtailed_w,
        'panel_count': panel_count,
        'panels_reporting': panels_reporting,
        'production': production,
    }


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Consolidated Enphase Solar Collector (SQLite + Dashboard JSON)'
    )
    parser.add_argument('--array-id', default=None,
                        help='Array identifier (e.g. "house")')
    parser.add_argument('--loop', action='store_true',
                        help='Run continuously')
    parser.add_argument('--interval', type=int, default=300,
                        help='Seconds between reads in loop mode (default 300)')
    args = parser.parse_args()

    if not REQUESTS_AVAILABLE:
        sys.exit(1)

    if DB_AVAILABLE:
        init_db()

    cfg = load_config(args.array_id)

    running = True
    def handle_signal(sig, frame):
        nonlocal running
        running = False
        log.info("Shutdown signal received")
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    if args.loop:
        log.info(f"Starting continuous collection: array={cfg['array_id']} "
                 f"ip={cfg['ip']} every {args.interval}s")
        while running:
            collect_once(cfg)
            if running:
                time.sleep(args.interval)
    else:
        result = collect_once(cfg)
        if result:
            print(json.dumps(result, indent=2, default=str))
        else:
            sys.exit(1)

    log.info("Done.")


if __name__ == '__main__':
    main()
