#!/usr/bin/env python3
"""
config_review.py — payload for the v4.7 configuration review (read-only)

Builds everything the review screens need from the settings registry:
for every row, the value in .env, the value in the store, what is in effect
now, and the value the review proposes (conflicts pre-select .env).

Nothing here writes. Committing is config_commit.py (POST /api/v1/review/commit)
and the Test buttons are review_tests.py (POST /api/v1/review/test). The page
opens in preview mode on demand (config_review.html?preview=1), where nothing
is saved whatever the authority flag is.

Served by scheduler.py at GET /api/v1/review.
"""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import settings

LABELS = {
    'modbus.enabled': 'Local Modbus connection',
    'modbus.host': 'aGate IP address',
    'modbus.port': 'Modbus port',
    'modbus.timeout': 'Modbus timeout (s)',
    'system.api_max_retries': 'Cloud API retries',
    'system.api_retry_delay': 'Cloud API retry delay (s)',
    'battery.count': 'Battery count',
    'battery.capacity_kwh': 'Total capacity (kWh)',
    'battery.backup_reserve_pct': 'Planning floor (%)',
    'battery.reserve_soc_home': 'Always enforce reserve (%)',
    'battery.reserve_soc_backup': 'Always enforce backup reserve (%)',
    'engine.target_soc': 'Solar fill ceiling (%)',
    'engine.taper_ceiling_pct': 'Grid charge ceiling (%)',
    'engine.p45_safety_margin_kwh': 'Partial-peak safety margin (kWh)',
    'engine.safety_margin_hours': 'EB lead time (h)',
    'solar.export': 'Strategy',
    'engine.decision_interval_minutes': 'Decision interval (min)',
    'scheduling.check_interval_minutes': 'Polling interval (min, 0 = auto)',
    'scheduling.peak_transition_buffer_minutes': 'Pre-peak check lead (min)',
    'features.adaptive_engine': 'v4 adaptive engine',
    'scheduling.home_mode': 'Home mode (v3)',
    'engine.charging_strategy': 'Charging strategy (v3)',
    'solar.min_for_wait': 'Minimum solar to wait (kW, v3)',
    'battery.charge_rate_per_hour': 'Charge rate label (%/h)',
    'billing.discount_program': 'Discount program',
    'billing.export_compensation': 'Export compensation',
    'billing.multi_meter': 'Aggregate-billed second meter',
    'forecast.house_tilt': 'Tilt (°)',
    'forecast.house_azimuth': 'Azimuth (°, 0 = south)',
    'forecast.house_kwp': 'DC size (kWp)',
    'forecast.latitude': 'Latitude',
    'forecast.longitude': 'Longitude',
    'features.forecast': 'Solar forecasting',
    'features.weather': 'Weather Underground station (PWS)',
    'weather.station_id': 'PWS station ID',
    'features.pvoutput': 'PVOutput uploads',
    'pvoutput.system_ids': 'PVOutput system IDs',
    'features.solaredge_panel_monitoring': 'SolarEdge panel health (portal)',
    'solaredge.modbus_enabled': 'SolarEdge local Modbus',
    'solaredge.modbus_inverters': 'SolarEdge inverter list',
    'features.dynamic_pricing': 'Dynamic pricing',
    'pricing.provider': 'Provider',
    'pricing.threshold_cents': 'Charge threshold (¢/kWh)',
    'pricing.ceiling_cents': 'Price ceiling (¢/kWh)',
    'pricing.solar_override_cents': 'Solar override price (¢/kWh)',
    'telemetry.enabled': 'Anonymous telemetry',
    'telemetry.region': 'Country',
    'system.debug_mode': 'Debug logging',
}

CHOICE_LABELS = {
    'solar.export': {'false': 'Self-consumption (store surplus solar)',
                     'true': 'Export (sell surplus solar)'},
    'billing.discount_program': {'none': 'None', 'CARE': 'CARE', 'FERA': 'FERA',
                                 'other': 'Other'},
    'billing.export_compensation': {'net_metering': 'Net metering (1:1 retail)',
                                    'net_billing': 'Net billing (NEM3-style)',
                                    'no_export': 'No export'},
    'scheduling.home_mode': {'tou': 'Time of use', 'self_consumption': 'Self-consumption'},
}

SECRET_LABELS = {
    'FRANKLIN_USERNAME': 'Franklin account',
    'FRANKLIN_PASSWORD': 'Franklin password',
    'FRANKLIN_GATEWAY_ID': 'Gateway ID',
    'WEATHER_API_KEY': 'Weather Underground API key',
    'PVOUTPUT_API_KEY': 'PVOutput API key',
    'SOLAREDGE_USERNAME': 'SolarEdge portal login',
    'SOLAREDGE_PASSWORD': 'SolarEdge portal password',
}

ARRAY_FIELD_LABELS = {
    'NAME': 'Name', 'TYPE': 'Type', 'MODEL': 'Model', 'IP': 'Gateway IP',
    'SERIAL': 'Gateway serial', 'SITE_ID': 'Site ID', 'CAPACITY_KW': 'AC capacity (kW)',
    'EMAIL': 'Login email', 'PASSWORD': 'Login password', 'API_KEY': 'API key',
}

SCREENS = [
    ('connection', 'Connection',
     'Your FranklinWH account and how the automation talks to the aGate.'),
    ('battery', 'Battery',
     'Detected hardware and the reserve the automation plans around.'),
    ('arrays', 'Solar arrays',
     'One card per array. Only arrays that charge the battery feed the forecast and the engine.'),
    ('rates', 'Rates',
     'Your rate plan. Peak windows for the engine come from the plan on the battery meter.'),
    ('billing', 'Utility & billing', 'Discounts and how exports are credited.'),
    ('engine', 'Engine behavior', 'How hard the engine charges, and whether it stores or exports surplus solar.'),
    ('forecast', 'Forecast, weather & site', 'Where you are and where weather comes from.'),
    ('integrations', 'Integrations', 'PVOutput, SolarEdge and dynamic pricing.'),
    ('telemetry', 'Telemetry', 'Anonymous usage reporting.'),
    ('advanced', 'Advanced', 'Intervals, retries, debug, the v3 legacy knobs, and deployment values.'),
    ('summary', 'Summary', 'Everything that differs, what you changed, and retired keys still in .env.'),
]

SECTION_SCREEN = {'A': 'connection', 'B': 'battery', 'F': 'arrays', 'D': 'rates',
                  'E': 'billing', 'C': 'engine', 'G': 'forecast', 'H': 'integrations',
                  'I': 'telemetry'}


# =============================================================================
# Data access (fresh read, read-only)
# =============================================================================

def _conn():
    c = sqlite3.connect(f'file:{settings.db_path()}?mode=ro', uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _q(sql, params=()):
    try:
        c = _conn()
        try:
            return [dict(r) for r in c.execute(sql, params).fetchall()]
        finally:
            c.close()
    except Exception:
        return []


def _store_rows() -> Dict[str, dict]:
    return {r['key']: r for r in _q("SELECT * FROM app_config WHERE scope = 'global'")}


def _state(key: str) -> Optional[str]:
    return (_q("SELECT value FROM app_state WHERE key = ?", (key,)) or [{'value': None}])[0]['value']


def store_written_at() -> Optional[str]:
    """When the configuration store last changed: the newest write to settings,
    arrays or rate plans (a review commit, a sync from .env, or an admin tool)."""
    stamps = [(_q(sql) or [{'t': None}])[0]['t'] for sql in (
        "SELECT MAX(updated_at) AS t FROM app_config",
        "SELECT MAX(updated_at) AS t FROM solar_arrays",
        "SELECT MAX(imported_at) AS t FROM rate_plans")]
    stamps = [str(s) for s in stamps if s]
    return max(stamps) if stamps else None


def _store_arrays() -> List[dict]:
    out = []
    for a in _q("SELECT * FROM solar_arrays ORDER BY id"):
        try:
            a['config'] = json.loads(a.get('config_json') or '{}') or {}
        except (json.JSONDecodeError, TypeError):
            a['config'] = {}
        out.append(a)
    return out


def _profile() -> dict:
    p = Path(os.getenv('DATA_DIR', '/app/data')) / 'system_profile.json'
    try:
        return json.loads(p.read_text()).get('capacity', {}) or {}
    except Exception:
        return {}


def _latest_reading() -> dict:
    rows = _q("SELECT timestamp, soc_pct, active_reserve_pct, self_reserve_pct, tou_reserve_pct, "
              "mode FROM system_readings ORDER BY id DESC LIMIT 1")
    return rows[0] if rows else {}


# =============================================================================
# Row builders
# =============================================================================

def _env_text(ent: settings.Entry) -> Optional[str]:
    for n in ent.env:
        v = os.environ.get(n)
        if v not in (None, ''):
            return v
    if ent.key in settings.IMPORT_CONVERTERS:
        src, conv = settings.IMPORT_CONVERTERS[ent.key]
        v = os.environ.get(src)
        if v not in (None, ''):
            return conv(v)
    return None


def _canon(ent_type: str, v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    if ent_type == 'list':
        return ','.join(str(x) for x in settings.coerce(v, 'list', []))
    if ent_type == 'bool':
        return settings._bool_text(v)
    return settings._norm(v)


def _default_text(ent: settings.Entry) -> Optional[str]:
    d = ent.default
    if d is None:
        return None
    if isinstance(d, bool):
        return 'true' if d else 'false'
    return str(d)


def _status(env_v, store_v, typ):
    if env_v is None and store_v is None:
        return 'default'
    if env_v is None:
        return 'store_only'
    if store_v is None:
        return 'env_only'
    return 'match' if _canon(typ, env_v) == _canon(typ, store_v) else 'conflict'


# Set per payload: has a review been committed on this install?
_CTX = {'reviewed': False}


def setting_row(ent: settings.Entry, store: Dict[str, dict], extra: dict = None) -> dict:
    env_v = _env_text(ent)
    store_v = settings._row_text(store.get(ent.key))
    prefer = 'env'
    if _CTX['reviewed']:
        # After a committed review the store is the reviewed configuration:
        # 'no row' is a deliberate choice of the code default (or of 'not set'
        # for optional values), and conflicts pre-select the store.
        prefer = 'store'
        if store_v is None:
            store_v = None if ent.optional else _default_text(ent)
        if env_v is None:
            status = 'store_only' if store_v is not None else 'default'
        elif store_v is None:
            status = 'conflict'
        else:
            status = 'match' if _canon(ent.type, env_v) == _canon(ent.type, store_v) else 'conflict'
        proposed = store_v
    else:
        status = _status(env_v, store_v, ent.type)
        if status in ('conflict', 'env_only', 'match'):
            proposed = env_v
        elif status == 'store_only':
            proposed = store_v
        else:
            proposed = None if ent.optional else _default_text(ent)
    effective = settings.raw(ent.env[0]) if ent.env else (
        settings._row_text(settings._snap()['cfg'].get(ent.key))
        if settings.authority() == 'store' else env_v)
    row = {
        'kind': 'setting', 'key': ent.key, 'env_name': ent.env[0] if ent.env else
        settings.IMPORT_CONVERTERS.get(ent.key, (None,))[0],
        'label': LABELS.get(ent.key, ent.key), 'cls': ent.cls, 'type': ent.type,
        'choices': list(ent.choices) or (['false', 'true'] if ent.key in CHOICE_LABELS and ent.type == 'bool' else []),
        'choice_labels': CHOICE_LABELS.get(ent.key, {}),
        'help': ent.help, 'recommend': ent.recommend, 'note': ent.note,
        'advanced': ent.advanced, 'legacy': ent.legacy, 'optional': ent.optional,
        'vmin': ent.vmin, 'vmax': ent.vmax, 'default': _default_text(ent),
        'env': env_v, 'store': store_v, 'effective': effective,
        'proposed': proposed, 'status': status, 'hints': [],
        'required_if': ent.required_if, 'prefer': prefer,
    }
    if ent.required_if and proposed in (None, ''):
        row['hints'].append({'level': 'warn', 'text':
                             'Required for this install: there is no built-in default '
                             'because it is specific to your site.' +
                             ('' if ent.required_if == 'always' else
                              f' (needed while {LABELS.get(ent.required_if, ent.required_if)} is on)')})
    if extra:
        row.update(extra)
    return row


def secret_row(name: str, label: str = None, help_text: str = '') -> dict:
    v = os.environ.get(name)
    shown = None
    if v and name == 'FRANKLIN_GATEWAY_ID':
        shown = '•••• ' + v[-4:]
    ent = settings.BY_ENV.get(name)
    return {'kind': 'secret', 'env_name': name,
            'label': label or SECRET_LABELS.get(name, name),
            'help': help_text or (ent.help if ent else ''),
            'set': bool(v), 'shown': shown, 'status': 'env',
            'testable': name in SECRET_TESTS, 'test': SECRET_TESTS.get(name)}


# Secret row -> live test run with it (review_tests.py)
SECRET_TESTS = {'FRANKLIN_USERNAME': 'franklin', 'WEATHER_API_KEY': 'weather',
                'PVOUTPUT_API_KEY': 'pvoutput', 'SOLAREDGE_USERNAME': 'solaredge'}


def info_row(label: str, value: Any, help_text: str = '') -> dict:
    return {'kind': 'info', 'label': label, 'value': value, 'help': help_text}


# =============================================================================
# Payload
# =============================================================================

def review_payload() -> Dict[str, Any]:
    store = _store_rows()
    _CTX['reviewed'] = bool((_q("SELECT value FROM app_state WHERE key = 'review.completed_at'")
                             or [{'value': None}])[0]['value'])
    by_screen: Dict[str, List[dict]] = {s[0]: [] for s in SCREENS}
    advanced_rows, legacy_rows = [], []

    def place(ent, row):
        if ent.legacy:
            legacy_rows.append(row)
        elif ent.advanced:
            advanced_rows.append(row)
        else:
            by_screen[SECTION_SCREEN[ent.section]].append(row)

    rows_by_key = {}
    for ent in settings.REGISTRY:
        if ent.cls in settings.STORE_CLASSES and ent.key:
            rows_by_key[ent.key] = setting_row(ent, store)

    profile = _profile()
    reading = _latest_reading()

    # ---- Connection: one gateway card -------------------------------------
    gw_rows = [secret_row('FRANKLIN_USERNAME'), secret_row('FRANKLIN_PASSWORD'),
               secret_row('FRANKLIN_GATEWAY_ID')]
    for k in ('modbus.enabled', 'modbus.host'):
        gw_rows.append(rows_by_key[k])
    rows_by_key['modbus.enabled']['testable'] = True
    rows_by_key['modbus.enabled']['test'] = 'modbus'
    if profile:
        gw_rows.append(info_row('Batteries on this aGate',
                                f"{profile.get('battery_count', '?')} × aPower "
                                f"({profile.get('capacity_per_battery_kwh', '?')} kWh each, "
                                f"SoH {profile.get('soh_percent', '?')}%)",
                                'Detected from Modbus. Details on the Battery screen.'))
    cards = {s[0]: [] for s in SCREENS}
    cards['connection'].append({'title': 'aGate', 'subtitle': 'FranklinWH gateway', 'rows': gw_rows})

    # ---- Battery ------------------------------------------------------------
    cnt, cap = rows_by_key['battery.count'], rows_by_key['battery.capacity_kwh']
    det_cnt, det_cap = profile.get('battery_count'), profile.get('total_capacity_kwh')
    for r, det in ((cnt, det_cnt), (cap, det_cap)):
        r['detected'] = None if det is None else str(det)
        r['detected_source'] = profile.get('discovery_source')
        if det is not None and r['proposed'] is not None and \
                _canon(r['type'], r['proposed']) == _canon(r['type'], str(det)):
            r['hints'].append({'level': 'ok', 'text': 'Matches the detected value. '
                               'You can clear the override and let detection decide.'})
        elif det is not None and r['proposed'] is not None:
            r['hints'].append({'level': 'warn', 'text': f'Detected {det}, configured '
                               f"{r['proposed']}. Check which is right."})
    try:
        c, k = int(float(cnt['proposed'])), float(cap['proposed'])
        if abs(k - c * 13.6) > 0.5:
            cap['hints'].append({'level': 'warn', 'text': f'{c} aPower 2 units = {c * 13.6:.1f} kWh.'})
    except (TypeError, ValueError):
        pass
    app_reserve = reading.get('active_reserve_pct') or reading.get('tou_reserve_pct')
    for k in ('battery.reserve_soc_home', 'battery.reserve_soc_backup'):
        r = rows_by_key[k]
        r['app_value'] = app_reserve if k.endswith('home') else None
        # A source=default row is not an override; the review shows "use the app"
        if r['status'] == 'store_only' and (store.get(k) or {}).get('source') == 'default':
            r['status'] = 'default'
            r['proposed'] = None
    cards['battery'].append({'title': 'Detected hardware', 'rows': [cnt, cap]})
    cards['battery'].append({'title': 'Reserve', 'rows': [
        info_row('Franklin app reserve (now)',
                 f'{app_reserve}%' if app_reserve is not None else 'unknown',
                 'Read from the aGate. The app is the source of truth unless you set an '
                 '"always enforce" value below.'),
        rows_by_key['battery.reserve_soc_home'], rows_by_key['battery.backup_reserve_pct']]})
    advanced_rows.append(rows_by_key['battery.reserve_soc_backup'])

    # ---- Arrays ---------------------------------------------------------------
    s_arrays = {a['array_id']: a for a in _store_arrays()}
    env_ids = [a.strip().lower() for a in os.environ.get('SOLAR_ARRAYS', '').split(',') if a.strip()]
    ids = env_ids + [a for a in s_arrays if a not in env_ids]
    for aid in ids:
        sa = s_arrays.get(aid)
        rows = []
        atype_now = ((sa or {}).get('array_type') or
                     os.environ.get(f'SOLAR_ARRAY_{aid.upper()}_TYPE') or '').lower()
        for fld, (cls, where, name) in settings.ARRAY_FIELDS.items():
            env_name = f'SOLAR_ARRAY_{aid.upper()}_{fld}'
            if cls == settings.SECRET:
                if os.environ.get(env_name) is not None:
                    rows.append(secret_row(env_name, ARRAY_FIELD_LABELS[fld]))
                    if fld == 'API_KEY' and atype_now == 'solaredge':
                        # Monitoring API (site ID + key), plus the inverters over
                        # local Modbus when that is on
                        rows[-1].update({'testable': True, 'test': 'solaredge_site',
                                         'array_id': aid})
                continue
            if cls == settings.INFRA:
                continue
            env_v = os.environ.get(env_name) or None
            store_v = settings._array_field_text(
                dict(sa, config=sa['config']) if sa else None, fld) if sa else None
            if env_v is None and store_v is None:
                continue
            st = _status(env_v, store_v, 'float' if fld == 'CAPACITY_KW' else 'str')
            rows.append({'kind': 'setting', 'key': f'array.{aid}.{name}', 'env_name': env_name,
                         'label': ARRAY_FIELD_LABELS[fld], 'cls': 'setting',
                         'type': 'float' if fld == 'CAPACITY_KW' else 'str',
                         'choices': ['enphase', 'solaredge'] if fld == 'TYPE' else [],
                         'choice_labels': {}, 'help': '', 'hints': [],
                         'env': env_v, 'store': store_v, 'effective': store_v if
                         settings.authority() == 'store' else env_v,
                         'proposed': env_v if env_v is not None else store_v, 'status': st,
                         'readonly': fld == 'TYPE'})
            if fld == 'IP':
                if atype_now == 'enphase':
                    rows[-1].update({'testable': True, 'test': 'envoy', 'array_id': aid})
        charges = sa.get('charges_battery') if sa else None
        exports = sa.get('exports') if sa else None
        rows.append({'kind': 'flag', 'key': f'array.{aid}.charges_battery',
                     'label': 'Charges the battery', 'value': charges,
                     'help': 'Required. Only arrays wired into the aGate charge the battery; '
                             'they drive the forecast and the engine.',
                     'status': 'store_only' if charges is not None else 'unreviewed'})
        rows.append({'kind': 'flag', 'key': f'array.{aid}.exports', 'label': 'Exports to the grid',
                     'value': exports, 'help': 'Whether this array sends surplus to the grid.',
                     'status': 'store_only' if exports is not None else 'unreviewed'})
        if charges == 1:
            # The array row's DC size follows forecast.house_kwp on commit; show it
            # only when the two differ (e.g. after an edit to one of them)
            try:
                same_kwp = abs(float(sa.get('capacity_kwp')) -
                               float(rows_by_key['forecast.house_kwp']['proposed'])) < 0.005
            except (TypeError, ValueError):
                same_kwp = False
            if not same_kwp:
                rows.append(info_row('DC size in store (kWp)', sa.get('capacity_kwp'),
                                     'The DC size on the array record. Committing sets it to '
                                     'the DC size below.'))
            for k in ('forecast.house_kwp', 'forecast.house_tilt', 'forecast.house_azimuth'):
                rows.append(rows_by_key[k])
        title = (sa or {}).get('name') or os.environ.get(f'SOLAR_ARRAY_{aid.upper()}_NAME') or aid
        atype = (sa or {}).get('array_type') or os.environ.get(f'SOLAR_ARRAY_{aid.upper()}_TYPE')
        subtitle = f"{atype or 'unknown type'} · " + (
            'charges the battery' if charges == 1 else
            'separately metered' if charges == 0 else 'classification needed')
        cards['arrays'].append({'title': title, 'subtitle': subtitle, 'array_id': aid,
                                'in_env': aid in env_ids, 'in_store': sa is not None,
                                'rows': rows})
    derived_rows = [
        info_row('Battery-charging solar (kWp)', settings._derived_text('SOLAR_CAPACITY_KW', None)
                 if settings.authority() == 'store' else os.environ.get('SOLAR_CAPACITY_KW'),
                 'Sum of the arrays that charge the battery. Replaces SOLAR_CAPACITY_KW.')]
    try:
        import solar_day
        sds = solar_day.summary()
        t = sds['today']
        derived_rows.append(info_row(
            'Solar day (learned)',
            f"production {solar_day.fmt(t['start'])}-{solar_day.fmt(t['end'])} today "
            f"(sunrise {solar_day.fmt(t['sunrise'])}, sunset {solar_day.fmt(t['sunset'])}; "
            f"{t['source']}, {t['learned_days']} days)",
            'When your battery-connected solar actually starts and stops, learned from your own '
            'data relative to the real sunrise and sunset, so terrain, trees and clock changes '
            'are handled automatically.'))
        cap_row = info_row('Solar maximum (learned)',
                           f"{sds['cap_kw']} kW (observed {sds['cap_observed_kw']}, "
                           f"configured AC {sds['cap_store_kw']})",
                           'The most your battery-connected arrays actually produce. Used to cap '
                           'forecasts; never above the configured AC capacity.')
        obs, st = sds.get('cap_observed_kw'), sds.get('cap_store_kw')
        if obs and st and abs(st - obs) / obs > 0.10:
            charging = [c for c in cards['arrays'] if c.get('array_id') and
                        (s_arrays.get(c['array_id']) or {}).get('charges_battery') == 1]
            non_export = bool(charging) and all(
                (s_arrays.get(c['array_id']) or {}).get('exports') == 0 for c in charging)
            text = (f'Configured AC capacity {st} kW differs from the observed maximum {obs} kW '
                    f'by more than 10%. Check the array AC size against its nameplate.')
            if non_export and obs < st:
                text += (' This array does not export, so when the battery is full the Franklin '
                         'curtails it and the observed maximum can stay well below nameplate. If '
                         'the nameplate matches, leave it.')
            hint = {'level': 'warn', 'text': text}
            # On the AC capacity row of the battery-charging array(s), where the fix
            # is made; Derived only points there
            placed_on = []
            for c in charging:
                for r in c['rows']:
                    if r.get('key') == f"array.{c['array_id']}.capacity_kw":
                        r['hints'].append(dict(hint))
                        placed_on.append(c['title'])
            cap_row['hints'] = ([{'level': 'rec', 'text': 'Differs from the configured AC capacity '
                                  'by more than 10%: see ' + ', '.join(placed_on) +
                                  ' → AC capacity above.'}] if placed_on else [hint])
        derived_rows.append(cap_row)
    except Exception:
        pass
    cards['arrays'].append({'title': 'Derived', 'rows': derived_rows})

    # ---- Rates ----------------------------------------------------------------
    cards['rates'] = rates_cards()

    # ---- Telemetry region: ISO 3166-1 country codes ------------------------------
    try:
        import regions
        tr = rows_by_key['telemetry.region']
        tr['choice_labels'] = dict(regions.ordered())
        tr['choice_labels'][''] = 'Not set (use the consent choice or time zone)'
        for side in ('proposed', 'store', 'env'):
            if tr.get(side):
                code, note = regions.normalize_country(tr[side])
                if side == 'proposed':
                    if note:
                        tr['hints'].append({'level': 'warn', 'text': note})
                    tr['proposed'] = code
    except Exception:
        pass

    # ---- Billing --------------------------------------------------------------
    dp = rows_by_key['billing.discount_program']
    hist = _q("SELECT rate_name, effective_date, care_peak_rate FROM rate_history "
              "ORDER BY effective_date DESC LIMIT 1")
    if hist:
        h = hist[0]
        is_care = h['care_peak_rate'] is not None
        if (dp['proposed'] == 'CARE') != is_care:
            dp['hints'].append({'level': 'warn', 'text':
                                f"Your latest rate history row ('{h['rate_name']}', effective "
                                f"{h['effective_date']}) is {'CARE' if is_care else 'not CARE'}. "
                                f"Update this to match."})
    cards['billing'].append({'title': 'Battery meter', 'rows': [
        dp, rows_by_key['billing.export_compensation'], rows_by_key['billing.multi_meter']]})

    # ---- Remaining registry rows by section ------------------------------------
    placed = {id(r) for c in cards.values() for card in c for r in card['rows']}
    placed |= {id(r) for r in advanced_rows}
    for ent in settings.REGISTRY:
        if not ent.key or ent.key not in rows_by_key:
            continue
        r = rows_by_key[ent.key]
        if id(r) in placed:
            continue
        if ent.key.startswith('billing.') or ent.key.startswith('battery.count') or \
                ent.key.startswith('forecast.house_'):
            continue
        place(ent, r)
        placed.add(id(r))

    def card_of(screen, title, rows):
        if rows:
            cards[screen].append({'title': title, 'rows': rows})

    card_of('engine', 'Charging', by_screen['engine'])
    fw = by_screen['forecast'] + [secret_row('WEATHER_API_KEY')]
    card_of('forecast', 'Site & weather', fw)
    integ = by_screen['integrations']
    dyn_keys = ('pricing.provider', 'pricing.threshold_cents', 'pricing.ceiling_cents',
                'pricing.solar_override_cents')
    for r in integ:
        if r.get('key') in dyn_keys:
            r['show_if'] = 'features.dynamic_pricing'
    pv = [r for r in integ if r.get('key', '').startswith(('features.pvoutput', 'pvoutput.'))]
    se = [r for r in integ if 'solaredge' in (r.get('key') or '')]
    dyn = [r for r in integ if r not in pv and r not in se]
    card_of('integrations', 'PVOutput', pv + [secret_row('PVOUTPUT_API_KEY')])
    card_of('integrations', 'SolarEdge', se + [secret_row('SOLAREDGE_USERNAME'),
                                               secret_row('SOLAREDGE_PASSWORD')])
    card_of('integrations', 'Dynamic pricing', dyn)
    card_of('telemetry', 'Telemetry', by_screen['telemetry'])
    for ent_key in ('battery.charge_rate_per_hour',):
        pass
    card_of('advanced', 'Intervals, retries & debug', advanced_rows)
    if legacy_rows:
        cards['advanced'].append({'title': 'Legacy (v3 path only)', 'collapsed': True,
                                  'rows': legacy_rows})
    infra = []
    for n in ('TZ', 'DASHBOARD_PORT', 'API_PORT', 'DATA_DIR', 'LOG_DIR', 'WEB_DIR',
              'TELEMETRY_ENDPOINT'):
        v = os.environ.get(n)
        infra.append(info_row(n, v if v else '(image default)', 'Deployment value; stays in the environment.'))
    cards['advanced'].append({'title': 'Deployment (read-only)', 'collapsed': True, 'rows': infra})

    # ---- Summary data -----------------------------------------------------------
    retired = sorted(n for e in settings.REGISTRY if e.cls == settings.RETIRE
                     for n in e.env if os.environ.get(n) is not None)
    aliases = sorted(n for n in settings.LEGACY_ARRAY_ALIASES if os.environ.get(n) is not None)
    derived = []
    for n, label in (('PEAK_START_HOUR', 'Peak start'), ('PEAK_END_HOUR', 'Peak end'),
                     ('PEAK_DAYS', 'Peak days'), ('TOU_ENABLED', 'TOU'),
                     ('SOLAR_CAPACITY_KW', 'Solar capacity'), ('CARE_RATE', 'CARE_RATE'),
                     ('NEM_VERSION', 'NEM_VERSION')):
        if os.environ.get(n) is not None:
            derived.append({'env_name': n, 'label': label, 'env': os.environ.get(n)})

    return {
        'available': True,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'authority': settings.authority(),
        'synced_at': (_q("SELECT value FROM app_state WHERE key = 'config.synced_at'") or
                      [{'value': None}])[0]['value'],
        'review_completed_at': (_q("SELECT value FROM app_state WHERE key = 'review.completed_at'") or
                                [{'value': None}])[0]['value'],
        'store_written_at': store_written_at(),
        'screens': [{'id': sid, 'title': t, 'intro': intro, 'cards': cards.get(sid, [])}
                    for sid, t, intro in SCREENS],
        'summary': {'retired_in_env': retired, 'legacy_aliases_in_env': aliases,
                    'derived_in_env': derived},
    }


def rates_cards() -> List[dict]:
    try:
        import rate_config
    except Exception as e:
        return [{'title': 'Rate plan', 'rows': [info_row('Unavailable', str(e))]}]
    plans = _q("SELECT * FROM rate_plans WHERE active = 1 ORDER BY id")
    if not plans:
        return [{'title': 'Rate plan', 'rows': [info_row(
            'Not in the store yet',
            'Your data/rate_schedule.json is imported when you commit, so the store runs on the '
            'same prices and windows you have today. Check that file before committing; prices '
            'and times can be edited here after that.')]}]
    out = []
    today = datetime.now().strftime('%Y-%m-%d')
    win = rate_config.peak_window_for_date(today)
    res = rate_config.resolve_rates(today)
    for i, plan in enumerate(plans):
        pid = plan['id']
        base = {r['tier']: r['rate_cents'] for r in
                _q("SELECT tier, rate_cents FROM rate_tiers WHERE plan_id = ? AND season_id IS NULL",
                   (pid,))}
        seasons = []
        for s in _q("SELECT * FROM rate_seasons WHERE plan_id = ? ORDER BY id", (pid,)):
            try:
                months = json.loads(s['months_json'])
            except (json.JSONDecodeError, TypeError):
                months = []
            tiers = dict(base)
            for r in _q("SELECT tier, rate_cents FROM rate_tiers WHERE plan_id = ? AND season_id = ?",
                        (pid, s['id'])):
                tiers[r['tier']] = r['rate_cents']
            wins = _q("SELECT id, tier, days_json, start_time, end_time, season_id FROM rate_windows "
                      "WHERE plan_id = ? AND season_id = ? ORDER BY start_time", (pid, s['id'])) or \
                _q("SELECT id, tier, days_json, start_time, end_time, season_id FROM rate_windows "
                   "WHERE plan_id = ? AND season_id IS NULL ORDER BY start_time", (pid,))
            for w in wins:
                try:
                    w['days'] = json.loads(w.pop('days_json'))
                except (json.JSONDecodeError, TypeError):
                    w['days'] = []
                # A year-round window is shown under every season; editing it changes all of them
                w['shared'] = w.pop('season_id') is None
            seasons.append({'id': s['id'], 'name': s['name'], 'months': months, 'tiers': tiers,
                            'windows': wins})
        hints = []
        if i == 0 and res and win and win.season:
            plan_tiers = next((s['tiers'] for s in seasons if s['name'] == win.season), {})
            for t in ('peak', 'off_peak'):
                pv, rv = plan_tiers.get(t), res.tier_rates_cents.get(t)
                if pv is not None and rv is not None and abs(pv - rv) > 0.0005:
                    hints.append({'level': 'warn', 'text':
                                  f"Today's {t.replace('_', '-')} price in effect is {rv:.3f}¢ from "
                                  f"rate history, not the plan's {pv:.3f}¢. Rate history overrides "
                                  f"peak and off-peak, so editing those two here will not change what "
                                  f"the engine uses until the history row is updated."})
        out.append({'title': plan['name'], 'subtitle':
                    ('Battery meter · drives the engine' if i == 0 else 'Billing only'),
                    'kind': 'rate_plan', 'hints': hints, 'plan': {
                        'id': pid, 'source': plan.get('source'),
                        'name': plan['name'], 'default_tier': plan.get('default_tier'),
                        'export_capable': bool(plan.get('export_capable')),
                        'net_metering': plan.get('net_metering'),
                        'imported_at': plan.get('imported_at'), 'seasons': seasons},
                    'rows': []})
    out.append({'title': 'In effect today', 'rows': [
        info_row('Peak window', f'{win.start}–{win.end}, {win.days_mode()}' if win else 'not resolved',
                 'The engine, the pre/post-peak checks and the dashboard countdown use this. '
                 'Replaces PEAK_START_HOUR / PEAK_END_HOUR / PEAK_DAYS.'),
        info_row('Season', (win.season if win else None) or '—'),
        info_row('Prices today', ', '.join(f'{t} {c:.2f}¢' for t, c in sorted(
            res.tier_rates_cents.items())) if res else 'not resolved',
            res.source if res else ''),
        dict(info_row('Check against your tariff',
                      'Compare these prices and windows with the rate schedule named on your bill. '
                      "The Setup Checklist includes a link to PG&E's rate schedules; links for "
                      'other utilities will be added as we find them.',
                      'Utilities publish their rate schedules (tariffs) as documents on their '
                      'website. The Setup Checklist (Rates section) lists where to find them, '
                      'for example the PG&E tariffs page.'),
             link='/SETUP_CHECKLIST.pdf', link_text='Open the Setup Checklist (PDF)'),
    ]})
    return out


if __name__ == '__main__':
    print(json.dumps(review_payload(), indent=2, default=str)[:6000])
