#!/usr/bin/env python3
"""
settings.py — v4.7 configuration registry and accessor

The registry below is the single source for:
  - the accessor every production script uses to read configuration
  - dump_effective_config.py (effective value of every key, both authorities)
  - check_settings_access.py (no store-managed key read outside the accessor)
  - the config review / setup wizard screens (sections, help, validation)

Authority
---------
app_state['config.authority'] is 'env' (default) or 'store'. It is read ONCE
per process, on first use. Changing it takes a restart.

  env    Every read is os.getenv(), exactly as in v4.6.x. Zero behavior change.
  store  setting/detected keys come from the config store (app_config,
         solar_arrays); derived keys are computed (peak window and TOU from
         the rate plan, solar flags/capacity from solar_arrays); secret, infra
         and dev-tool names always come from the environment; retired names
         read as unset. .env values for store keys are ignored (reported once
         in the startup summary when they differ).

If the flag or the store cannot be read, the accessor falls back to env and
says so in the startup summary. env is also the rollback.

API
---
    import settings
    settings.raw('MODBUS_HOST', '192.168.5.149')   # drop-in for os.getenv
    settings.get('modbus.host')                     # typed, by canonical key
    settings.is_set('RESERVE_SOC_HOME')             # explicit override present?
    settings.authority()                            # 'env' | 'store'
"""

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger('settings')

AUTHORITY_KEY = 'config.authority'

# Classes
SETTING = 'setting'
DETECTED = 'detected'
DERIVED = 'derived'
SECRET = 'secret'
INFRA = 'infra'
RETIRE = 'retire'
DEVTOOL = 'dev-tool'

STORE_CLASSES = (SETTING, DETECTED)
ENV_CLASSES = (SECRET, INFRA, DEVTOOL)

# Review screen order (draft section letters)
SECTIONS = {
    'A': 'Connection',
    'B': 'Battery',
    'F': 'Solar arrays',
    'D': 'Rates',
    'E': 'Utility & billing',
    'C': 'Engine behavior',
    'G': 'Forecast, weather & site',
    'H': 'Integrations',
    'I': 'Telemetry & system',
    'J': 'Retired',
    'X': 'Dev tools',
}


@dataclass(frozen=True)
class Entry:
    key: Optional[str]            # canonical dotted key (None: env-only names)
    env: Tuple[str, ...]          # env name(s); first is primary, rest are aliases
    cls: str
    type: str = 'str'             # str|int|float|bool|list|enum|float?
    default: Any = None           # code default (must match config.py)
    section: str = 'I'
    help: str = ''
    category: str = ''            # app_config.category
    advanced: bool = False
    legacy: bool = False          # Advanced -> Legacy (v3 path only)
    optional: bool = False        # unset is meaningful (no row = not enforced)
    choices: Tuple[str, ...] = ()
    vmin: Optional[float] = None
    vmax: Optional[float] = None
    recommend: str = ''           # inline Configuration Health recommendation
    dev_aliases: Tuple[str, ...] = ()   # env names only dev tools read
    note: str = ''

    @property
    def store_type(self) -> str:
        return {'list': 'json', 'enum': 'str', 'float?': 'float'}.get(self.type, self.type)


def _e(*a, **k) -> Entry:
    return Entry(*a, **k)


# =============================================================================
# The registry (V47_CONFIG_REGISTRY_DRAFT.md, Draft 2)
# =============================================================================

REGISTRY: List[Entry] = [
    # ---- A. Gateway card ----------------------------------------------------
    _e(None, ('FRANKLIN_USERNAME',), SECRET, section='A',
       help='FranklinWH account email. Stays in .env; tested on the post-restart check page.'),
    _e(None, ('FRANKLIN_PASSWORD',), SECRET, section='A',
       help='FranklinWH account password. Stays in .env.'),
    _e(None, ('FRANKLIN_GATEWAY_ID',), SECRET, section='A', dev_aliases=('GATEWAY_ID',),
       help='aGate serial. Stays in .env; shown masked except the last 4 characters.'),
    _e('modbus.enabled', ('MODBUS_ENABLED',), SETTING, 'bool', False, 'A', category='modbus',
       help='Read the aGate directly over the local network (Modbus TCP) instead of '
            'the cloud API. Faster, and keeps working when the cloud is down.'),
    _e('modbus.host', ('MODBUS_HOST',), SETTING, 'str', '192.168.5.149', 'A', category='modbus',
       dev_aliases=('AGATE_IP',),
       help='IP address of the aGate on your network. Give it a DHCP reservation so it '
            'does not change. Needs a restart to apply.'),
    _e('modbus.port', ('MODBUS_PORT',), SETTING, 'int', 502, 'A', category='modbus',
       advanced=True, vmin=1, vmax=65535, dev_aliases=('AGATE_PORT',),
       help='Modbus TCP port on the aGate. Almost always 502.'),
    _e('modbus.timeout', ('MODBUS_TIMEOUT',), SETTING, 'float', 5.0, 'A', category='modbus',
       advanced=True, vmin=0.5, vmax=60,
       help='Seconds to wait for the aGate to answer a Modbus request.'),
    _e('system.api_max_retries', ('API_MAX_RETRIES',), SETTING, 'int', 5, 'A', category='system',
       advanced=True, vmin=0, vmax=20,
       help='How many times a failed Franklin cloud API call is retried.'),
    _e('system.api_retry_delay', ('API_RETRY_DELAY',), SETTING, 'int', 10, 'A', category='system',
       advanced=True, vmin=1, vmax=300,
       help='Seconds between Franklin cloud API retries.'),

    # ---- B. Battery & reserve -----------------------------------------------
    _e('battery.count', ('BATTERY_COUNT',), DETECTED, 'int', 2, 'B', category='battery',
       vmin=1, vmax=15,
       help='Number of aPower units. Detected from Modbus and the cloud serials; set an '
            'override only if detection is wrong.'),
    _e('battery.capacity_kwh', ('BATTERY_CAPACITY_KWH',), DETECTED, 'float', 30.0, 'B',
       category='battery', vmin=5, vmax=200,
       help='Total usable capacity of all aPower units combined. Detected; aPower 2 is '
            '13.6 kWh each.',
       recommend='Should be close to battery count x 13.6 kWh for aPower 2.',
       note='Code default 30.0 is wrong for aPower 2'),
    _e('battery.backup_reserve_pct', ('BACKUP_RESERVE_PCT',), SETTING, 'float', 20.0, 'B',
       category='battery', vmin=0, vmax=100,
       help="The engine's planning floor: it never plans to discharge below this. It "
            'prefers the live aGate reserve when that is available.'),
    _e('battery.reserve_soc_home', ('RESERVE_SOC_HOME',), SETTING, 'int', 20, 'B',
       category='battery', optional=True, vmin=0, vmax=100,
       help='Leave this to the Franklin app unless you want the automation to hold a fixed '
            'reserve no matter what the app says. Examples: several people change settings '
            'in the app, or a utility/VPP program resets it. When set here, every mode '
            'switch sends this value and overrides the app.'),
    _e('battery.reserve_soc_backup', ('RESERVE_SOC_BACKUP',), SETTING, 'int', 100, 'B',
       category='battery', optional=True, vmin=0, vmax=100,
       help='Optional "always enforce" reserve for Emergency Backup mode. Leave unset to '
            'use the Franklin app value.'),

    # ---- C. Engine behavior -------------------------------------------------
    _e('engine.target_soc', ('TARGET_SOC',), SETTING, 'float', 95.0, 'C', category='engine',
       vmin=50, vmax=100,
       help='Highest SOC solar is allowed to fill the battery to.'),
    _e('engine.taper_ceiling_pct', ('TAPER_CEILING_PCT',), SETTING, 'float', 85.0, 'C',
       category='engine', vmin=50, vmax=100,
       help='Highest SOC the engine will charge to from the grid. Above this the charge '
            'rate tapers, so solar is left to finish the fill.'),
    _e('engine.p45_safety_margin_kwh', ('P45_SAFETY_MARGIN_KWH',), SETTING, 'float', 2.0, 'C',
       category='engine', advanced=True, vmin=0, vmax=20,
       help='Extra kWh added to forecast peak demand before accepting partial-peak '
            'imports. Higher is more conservative.'),
    _e('engine.safety_margin_hours', ('SAFETY_MARGIN_HOURS',), SETTING, 'float', 0.5, 'C',
       category='engine', advanced=True, vmin=0, vmax=4,
       help='Extra lead time for Emergency Backup grid charging before peak. Higher starts '
            'EB earlier.'),
    _e('solar.export', ('SOLAR_EXPORT',), SETTING, 'bool', False, 'C', category='solar',
       help='Strategy: self-consumption (off) keeps surplus solar in the battery and caps '
            'grid charging at the taper ceiling; export (on) skips headroom management and '
            'lets surplus export for credit. This is a strategy choice, not a capability.'),
    _e('engine.decision_interval_minutes', ('DECISION_INTERVAL_MINUTES',), SETTING, 'int', 15,
       'C', category='engine', advanced=True, vmin=1, vmax=60,
       help='Decision interval passed to the adaptive engine config.'),
    _e('scheduling.check_interval_minutes', ('CHECK_INTERVAL_MINUTES',), SETTING, 'int', 0, 'C',
       category='scheduling', advanced=True, vmin=0, vmax=120,
       help='Polling interval override in minutes. 0 = automatic from data source and features.'),
    _e('scheduling.peak_transition_buffer_minutes', ('PEAK_TRANSITION_BUFFER_MINUTES',), SETTING,
       'int', 10, 'C', category='scheduling', advanced=True, vmin=1, vmax=59,
       help='How many minutes before peak the pre-peak check runs.'),
    _e('features.adaptive_engine', ('ADAPTIVE_ENGINE_ENABLED',), SETTING, 'bool', False, 'C',
       category='features', legacy=True,
       help='Use the v4 adaptive engine. Off falls back to the v3.5 logic.'),
    _e('scheduling.home_mode', ('HOME_MODE',), SETTING, 'enum', 'tou', 'C',
       category='scheduling', legacy=True, choices=('tou', 'self_consumption'),
       help='v3 path only: default home mode.'),
    _e('engine.charging_strategy', ('CHARGING_STRATEGY',), SETTING, 'str', 'balanced', 'C',
       category='engine', legacy=True,
       help='v3 path only: charging strategy profile.'),
    _e('solar.min_for_wait', ('MIN_SOLAR_FOR_WAIT',), SETTING, 'float', 0.5, 'C',
       category='solar', legacy=True, vmin=0, vmax=20,
       help='v3 path only: minimum solar kW to treat production as active.'),
    _e('battery.charge_rate_per_hour', ('CHARGE_RATE_PER_HOUR',), SETTING, 'float', 32.0, 'C',
       category='battery', legacy=True, vmin=5, vmax=100,
       help='Dashboard label only; the engine uses the observed EB rate.'),

    # ---- D. Rates & peak windows (derived from the rate plan) ----------------
    _e('features.tou', ('TOU_ENABLED',), DERIVED, 'bool', True, 'D',
       help='True when the rate plan on the battery meter has a peak tier.'),
    _e('tou.peak_start_hour', ('PEAK_START_HOUR',), DERIVED, 'int', 17, 'D',
       help="Derived from today's peak window in the battery meter's rate plan."),
    _e('tou.peak_end_hour', ('PEAK_END_HOUR',), DERIVED, 'int', 20, 'D',
       help="Derived from today's peak window in the battery meter's rate plan."),
    _e('tou.peak_days', ('PEAK_DAYS',), DERIVED, 'str', 'weekdays', 'D',
       help='Derived from the peak window days (all / weekdays / weekends / list).'),
    _e(None, ('PEAK2_START_HOUR',), RETIRE, 'int', 0, 'J', note='Decision 1'),
    _e(None, ('PEAK2_END_HOUR',), RETIRE, 'int', 0, 'J', note='Decision 1'),
    _e(None, ('PEAK2_DAYS',), RETIRE, 'str', 'weekdays', 'J', note='Decision 1'),

    # ---- E. Utility & billing -----------------------------------------------
    _e('billing.discount_program', (), SETTING, 'enum', 'none', 'E', category='billing',
       choices=('none', 'CARE', 'FERA', 'other'),
       help='Income-qualified discount on the battery meter, if any. The discounted prices '
            'themselves live in the rate plan.',
       recommend='Should agree with the latest rate history row.',
       note='Imported from CARE_RATE'),
    _e('billing.export_compensation', (), SETTING, 'enum', 'net_metering', 'E',
       category='billing', choices=('net_metering', 'net_billing', 'no_export'),
       help='How exported energy is credited: net metering (1:1 retail), net billing '
            '(NEM3-style avoided-cost credits), or no export.',
       note='Imported from NEM_VERSION'),
    _e('billing.multi_meter', ('MULTI_METER',), SETTING, 'bool', False, 'E', category='billing',
       help='A separately metered array or service is aggregate-billed with this one.'),
    _e(None, ('CARE_RATE',), DERIVED, 'bool', False, 'E',
       help='Legacy name; derived from billing.discount_program == CARE.'),
    _e(None, ('NEM_VERSION',), DERIVED, 'str', None, 'E',
       help='Legacy name; derived from billing.export_compensation.'),
    _e(None, ('METER2_ACCOUNT',), RETIRE, 'str', '', 'J', note='Alias of MULTI_METER'),

    # ---- F. Solar arrays (per-array fields handled by ARRAY_FIELDS) ----------
    _e(None, ('SOLAR_ARRAYS',), DERIVED, 'str', '', 'F',
       help='Array list, from the solar_arrays table.'),
    _e('solar.capacity_kw', ('SOLAR_CAPACITY_KW',), DERIVED, 'float', 0.0, 'F',
       help='Sum of the battery-charging arrays (DC kWp where known, else AC kW).'),
    _e('features.solar', ('SOLAR_ENABLED',), DERIVED, 'bool', True, 'F',
       help='True if any array charges the battery.'),
    _e('forecast.house_tilt', ('FORECAST_HOUSE_TILT',), SETTING, 'int', 22, 'F',
       category='forecast', vmin=0, vmax=90,
       help='Tilt of the battery-charging array in degrees (0 = flat).'),
    _e('forecast.house_azimuth', ('FORECAST_HOUSE_AZIMUTH',), SETTING, 'int', -65, 'F',
       category='forecast', vmin=-180, vmax=180,
       help='Azimuth of the battery-charging array (0 = south, -90 = east, 90 = west).'),
    _e('forecast.house_kwp', ('FORECAST_HOUSE_KWP',), SETTING, 'float', 6.96, 'F',
       category='forecast', vmin=0, vmax=100,
       help='DC nameplate (kWp) of the battery-charging array; the forecast input.'),
    _e(None, ('ENPHASE_ENABLED',), RETIRE, 'bool', False, 'J',
       note='Superseded by solar_arrays'),

    # ---- G. Forecast, weather & site ----------------------------------------
    _e('forecast.latitude', ('FORECAST_LATITUDE',), SETTING, 'float', 38.91, 'G',
       category='forecast', vmin=-90, vmax=90, help='Site latitude.'),
    _e('forecast.longitude', ('FORECAST_LONGITUDE',), SETTING, 'float', -120.84, 'G',
       category='forecast', vmin=-180, vmax=180, help='Site longitude.'),
    _e('features.forecast', ('FORECAST_ENABLED',), SETTING, 'bool', False, 'G',
       category='features', help='Weather-aware solar forecasting (Open-Meteo).'),
    _e('features.weather', ('WEATHER_ENABLED',), SETTING, 'bool', False, 'G',
       category='features', help='Collect observations from a local weather station.'),
    _e('weather.station_id', ('WEATHER_STATION_ID',), SETTING, 'str', '', 'G',
       category='weather', help='Weather Underground station ID.'),
    _e(None, ('WEATHER_API_KEY',), SECRET, section='G', help='Weather Underground API key.'),

    # ---- H. Integrations ----------------------------------------------------
    _e('features.pvoutput', ('PVOUTPUT_ENABLED',), SETTING, 'bool', False, 'H',
       category='features', help='Upload daily production to PVOutput.org.'),
    _e('pvoutput.system_ids', ('PVOUTPUT_SYSTEM_IDS',), SETTING, 'list', '', 'H',
       category='pvoutput', help='PVOutput system IDs, one per array.'),
    _e(None, ('PVOUTPUT_API_KEY',), SECRET, section='H', help='PVOutput API key.'),
    _e('features.solaredge_panel_monitoring', ('SOLAREDGE_PANEL_MONITORING',), SETTING, 'bool',
       False, 'H', category='features',
       help='Per-optimizer health from the SolarEdge portal.'),
    _e('solaredge.modbus_enabled', ('SOLAREDGE_MODBUS_ENABLED',), SETTING, 'bool', False, 'H',
       category='solaredge',
       help='Read the SolarEdge inverters locally over SunSpec Modbus TCP.'),
    _e('solaredge.modbus_inverters', ('SOLAREDGE_MODBUS_INVERTERS',), SETTING, 'str', '', 'H',
       category='solaredge', optional=True, advanced=True,
       help='Inverter list override: name:ip[:port[:unit[:model]]], comma-separated. '
            'Blank uses the built-in list.'),
    _e(None, ('SOLAREDGE_USERNAME',), SECRET, section='H', help='SolarEdge portal login.'),
    _e(None, ('SOLAREDGE_PASSWORD',), SECRET, section='H', help='SolarEdge portal password.'),
    _e('features.dynamic_pricing', ('DYNAMIC_PRICING_ENABLED',), SETTING, 'bool', False, 'H',
       category='features', help='Real-time hourly pricing (e.g. ComEd).'),
    _e('pricing.provider', ('PRICING_PROVIDER',), SETTING, 'str', 'comed', 'H',
       category='pricing', help='Dynamic pricing provider.'),
    _e('pricing.threshold_cents', ('PRICE_THRESHOLD_CENTS',), SETTING, 'float', 4.0, 'H',
       category='pricing', help='Grid-charge when the price is at or below this (cents/kWh).'),
    _e('pricing.ceiling_cents', ('PRICE_CEILING_CENTS',), SETTING, 'float', 10.0, 'H',
       category='pricing', help='Price ceiling (cents/kWh).'),
    _e('pricing.solar_override_cents', ('SOLAR_OVERRIDE_PRICE_CENTS',), SETTING, 'float?', None,
       'H', category='pricing', optional=True,
       help='Charge from grid even with solar producing when the price is at or below '
            'this. Blank = disabled.'),

    # ---- I. Telemetry & system ----------------------------------------------
    _e('telemetry.enabled', ('TELEMETRY_ENABLED',), SETTING, 'bool', False, 'I',
       category='telemetry', help='Anonymous daily usage telemetry.'),
    _e('telemetry.region', ('TELEMETRY_REGION',), SETTING, 'str', None, 'I',
       category='telemetry', optional=True,
       help='Coarse region code sent with telemetry. Blank = auto.'),
    _e('system.debug_mode', ('DEBUG_MODE',), SETTING, 'bool', False, 'I', category='system',
       advanced=True, help='Verbose debug logging.'),
    _e(None, ('TELEMETRY_ENDPOINT',), INFRA, section='I'),
    _e(None, ('TELEMETRY_API_KEY',), INFRA, section='I'),
    _e(None, ('TZ',), INFRA, section='I', help='Container timezone.'),
    _e(None, ('DASHBOARD_PORT',), INFRA, section='I'),
    _e(None, ('API_PORT',), INFRA, section='I'),
    _e(None, ('BASE_DIR',), INFRA, section='I'),
    _e(None, ('LOG_DIR',), INFRA, section='I'),
    _e(None, ('DATA_DIR',), INFRA, section='I'),
    _e(None, ('WEB_DIR',), INFRA, section='I'),

    # ---- J. Retired ---------------------------------------------------------
    _e(None, ('ENGINE_VERSION',), RETIRE, section='J', note='From the VERSION file'),
    _e(None, ('EMAIL_ENABLED',), RETIRE, 'bool', False, 'J'),
    _e(None, ('SMTP_SERVER',), RETIRE, section='J'),
    _e(None, ('SMTP_PORT',), RETIRE, 'int', 587, 'J'),
    _e(None, ('SENDER_EMAIL',), RETIRE, section='J'),
    _e(None, ('SENDER_PASSWORD',), RETIRE, section='J'),
    _e(None, ('RECIPIENT_EMAIL',), RETIRE, section='J'),
    _e(None, ('EMERGENCY_PREP_MODE',), RETIRE, 'bool', False, 'J',
       note='Replaced by override.json'),
    _e(None, ('FORECAST_SOLAR_API_KEY',), RETIRE, section='J'),
    _e(None, ('WEATHER_PROVIDER',), RETIRE, section='J'),
    _e(None, ('CLOUDY_THRESHOLD_PERCENT',), RETIRE, 'int', 50, 'J'),
    _e(None, ('MODBUS_RETRY_ATTEMPTS',), RETIRE, 'int', 3, 'J'),
    _e(None, ('TELEMETRY_INTERVAL_HOURS',), RETIRE, 'int', 24, 'J'),

    # ---- Dev tools (outside the accessor check) ------------------------------
    _e(None, ('AGATE_IP',), DEVTOOL, section='X'),
    _e(None, ('AGATE_PORT',), DEVTOOL, section='X'),
    _e(None, ('GATEWAY_ID',), DEVTOOL, section='X'),
]

# Per-array fields: SOLAR_ARRAY_<ID>_<FIELD>
# field -> (class, where, name)   where: 'col' = solar_arrays column, 'cfg' = config_json
ARRAY_FIELDS: Dict[str, Tuple[str, str, str]] = {
    'NAME': (SETTING, 'col', 'name'),
    'TYPE': (SETTING, 'col', 'array_type'),
    'MODEL': (SETTING, 'cfg', 'model'),
    'IP': (SETTING, 'cfg', 'ip'),
    'SERIAL': (SETTING, 'cfg', 'serial'),
    'SITE_ID': (SETTING, 'cfg', 'site_id'),
    'CAPACITY_KW': (SETTING, 'col', 'capacity_kw'),
    'EMAIL': (SECRET, 'env', 'email'),
    'PASSWORD': (SECRET, 'env', 'password'),
    'API_KEY': (SECRET, 'env', 'api_key'),
    'TOKEN_FILE': (INFRA, 'env', 'token_file'),
    'LAYOUT_FILE': (INFRA, 'env', 'layout_file'),
}
ARRAY_SECRET_CFG_KEYS = ('email', 'password', 'api_key')
_ARRAY_RE = re.compile(r'^SOLAR_ARRAY_(.+?)_(' + '|'.join(
    sorted(ARRAY_FIELDS, key=len, reverse=True)) + r')$')

# Legacy single-array aliases. Under store they resolve to the array of the
# given type (battery-charging first); the SOLAR_ARRAY_* form wins on import.
# name -> (array_type, FIELD)
LEGACY_ARRAY_ALIASES: Dict[str, Tuple[str, str]] = {
    'ENPHASE_ARRAY_NAME': ('enphase', 'NAME'),
    'ENPHASE_ENVOY_IP': ('enphase', 'IP'),
    'ENPHASE_IP': ('enphase', 'IP'),
    'ENPHASE_ENVOY_SERIAL': ('enphase', 'SERIAL'),
    'ENPHASE_MODEL': ('enphase', 'MODEL'),
    'ENPHASE_EMAIL': ('enphase', 'EMAIL'),
    'ENPHASE_PASSWORD': ('enphase', 'PASSWORD'),
    'ENPHASE_TOKEN_FILE': ('enphase', 'TOKEN_FILE'),
    'ENPHASE_ARRAY_LAYOUT': ('enphase', 'LAYOUT_FILE'),
    'SOLAREDGE_SITE_ID': ('solaredge', 'SITE_ID'),
    'SOLAREDGE_API_KEY': ('solaredge', 'API_KEY'),
}

# Import converters for store keys with no env name of their own
def _conv_care(v: str) -> Optional[str]:
    return 'CARE' if str(v).strip().lower() in ('true', '1', 'yes', 'on') else 'none'


def _conv_nem(v: str) -> Optional[str]:
    v = str(v).strip().lower().replace(' ', '')
    if v in ('nem1', 'nem2', 'nem1.0', 'nem2.0', '1:1', 'net_metering'):
        return 'net_metering'
    if v in ('nem3', 'nem3.0', 'nbt', 'net_billing'):
        return 'net_billing'
    if v in ('none', 'no_export', 'nonexport', 'non-export'):
        return 'no_export'
    return None


IMPORT_CONVERTERS: Dict[str, Tuple[str, Callable[[str], Optional[str]]]] = {
    'billing.discount_program': ('CARE_RATE', _conv_care),
    'billing.export_compensation': ('NEM_VERSION', _conv_nem),
}

# Indexes
BY_KEY: Dict[str, Entry] = {e.key: e for e in REGISTRY if e.key}
BY_ENV: Dict[str, Entry] = {}
for _ent in REGISTRY:
    for _n in _ent.env:
        BY_ENV[_n] = _ent


def known_env_names() -> set:
    names = set(BY_ENV) | set(LEGACY_ARRAY_ALIASES)
    for ent in REGISTRY:
        names.update(ent.dev_aliases)
    for src, _ in IMPORT_CONVERTERS.values():
        names.add(src)
    return names


def is_store_managed_env(name: str) -> bool:
    """True if reading this env name directly would bypass the store."""
    m = _ARRAY_RE.match(name)
    if m:
        return ARRAY_FIELDS[m.group(2)][0] in STORE_CLASSES
    if name in LEGACY_ARRAY_ALIASES:
        return ARRAY_FIELDS[LEGACY_ARRAY_ALIASES[name][1]][0] in STORE_CLASSES
    ent = BY_ENV.get(name)
    return bool(ent and ent.cls in (SETTING, DETECTED, DERIVED, RETIRE))


# =============================================================================
# Authority and store snapshot (read once per process)
# =============================================================================

_state: Dict[str, Any] = {'authority': None, 'forced': None, 'snap': None,
                          'fallback_reason': None}
_env_diverged: Dict[str, Tuple[str, str]] = {}
_retired_present: set = set()


def db_path() -> Path:
    d = os.getenv('DATA_DIR', '')
    if d.startswith('/'):
        p = Path(d)
    else:
        p = Path(__file__).resolve().parent.parent / (d or 'data')
    if not p.exists() and Path('/app/data').exists():
        p = Path('/app/data')
    return p / 'franklin.db'


def _ro_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f'file:{db_path()}?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def read_authority_flag() -> str:
    """The persisted flag, straight from the DB ('env' when absent/unreadable)."""
    try:
        conn = _ro_connect()
        try:
            row = conn.execute("SELECT value FROM app_state WHERE key = ?",
                               (AUTHORITY_KEY,)).fetchone()
        finally:
            conn.close()
        v = (row['value'] if row else 'env') or 'env'
        return v.strip().lower() if v.strip().lower() in ('env', 'store') else 'env'
    except Exception:
        return 'env'


def _load_snapshot() -> Optional[Dict[str, Any]]:
    try:
        conn = _ro_connect()
        try:
            cfg = {}
            for r in conn.execute("SELECT key, value, value_type, is_secret, source "
                                  "FROM app_config WHERE scope = 'global'"):
                cfg[r['key']] = dict(r)
            arrays = []
            for r in conn.execute("SELECT * FROM solar_arrays WHERE enabled = 1 ORDER BY id"):
                a = dict(r)
                try:
                    a['config'] = json.loads(a.get('config_json') or '{}') or {}
                except (json.JSONDecodeError, TypeError):
                    a['config'] = {}
                arrays.append(a)
        finally:
            conn.close()
        return {'cfg': cfg, 'arrays': arrays}
    except Exception as e:
        _state['fallback_reason'] = f'config store unreadable ({e})'
        return None


def authority() -> str:
    if _state['forced']:
        return _state['forced']
    if _state['authority'] is None:
        flag = read_authority_flag()
        if flag == 'store':
            snap = _load_snapshot()
            if snap is None or not snap['cfg']:
                if _state['fallback_reason'] is None:
                    _state['fallback_reason'] = 'config store is empty'
                flag = 'env'
            else:
                _state['snap'] = snap
        _state['authority'] = flag
    return _state['authority']


def force_authority(value: Optional[str]):
    """Tools only (dump/sync/tests): evaluate under a given authority in-process."""
    _state['forced'] = value
    if value == 'store':
        _state['snap'] = _load_snapshot()
    from_rate_cache.clear()


def reload():
    """Tools only: drop cached authority and snapshot."""
    _state.update({'authority': None, 'snap': None, 'fallback_reason': None})
    from_rate_cache.clear()


def _snap() -> Dict[str, Any]:
    if _state['snap'] is None:
        _state['snap'] = _load_snapshot() or {'cfg': {}, 'arrays': []}
    return _state['snap']


# =============================================================================
# Resolution
# =============================================================================

def _bool_text(v: Any) -> str:
    return 'true' if str(v).strip().lower() in ('true', '1', 'yes', 'on') else 'false'


def _row_text(row: Optional[dict]) -> Optional[str]:
    """Stored app_config row -> env-style text (what os.getenv would return)."""
    if not row or row.get('value') is None:
        return None
    text = row['value']
    vt = row.get('value_type')
    if vt == 'bool':
        return _bool_text(text)
    if vt == 'json':
        try:
            val = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text
        if isinstance(val, list):
            return ','.join(str(x) for x in val)
        return text
    return str(text)


def _note_divergence(name: str, store_text: Optional[str]):
    env_text = os.environ.get(name)
    if env_text is None:
        return
    if store_text is None or _norm(env_text) != _norm(store_text):
        _env_diverged[name] = (env_text, store_text)


def _norm(v: Optional[str]) -> str:
    s = '' if v is None else str(v).strip()
    lo = s.lower()
    if lo in ('true', 'yes', 'on'):
        return 'true'
    if lo in ('false', 'no', 'off'):
        return 'false'
    try:
        f = float(s)
        return repr(f)
    except ValueError:
        return ','.join(p.strip() for p in s.split(',')) if ',' in s else s


def _array_for(array_id: str = None, array_type: str = None) -> Optional[dict]:
    arrays = _snap()['arrays']
    if array_id is not None:
        for a in arrays:
            if a['array_id'].lower() == array_id.lower():
                return a
        return None
    cands = [a for a in arrays if (a.get('array_type') or '').lower() == array_type]
    cands.sort(key=lambda a: (0 if a.get('charges_battery') == 1 else 1, a['id']))
    return cands[0] if cands else None


def _array_field_text(a: Optional[dict], fld: str) -> Optional[str]:
    if a is None:
        return None
    _, where, name = ARRAY_FIELDS[fld]
    v = a.get(name) if where == 'col' else a['config'].get(name)
    if v is None or v == '':
        return None
    if isinstance(v, float):
        return f'{v:g}'
    return str(v)


# ---- derived --------------------------------------------------------------

from_rate_cache: Dict[str, Any] = {}


def _peak_window():
    """Today's primary peak window from the battery meter's plan."""
    from datetime import datetime
    day = datetime.now().strftime('%Y-%m-%d')
    if day not in from_rate_cache:
        try:
            import rate_config
            from_rate_cache[day] = rate_config.peak_window_for_date(day)
        except Exception as e:
            logger.warning('peak window unavailable from rate plan (%s)', e)
            from_rate_cache[day] = None
    return from_rate_cache[day]


def _derived_text(name: str, default: Optional[str]) -> Optional[str]:
    snap = _snap()
    arrays = snap['arrays']
    battery_arrays = [a for a in arrays if a.get('charges_battery') == 1]
    if name == 'SOLAR_ARRAYS':
        return ','.join(a['array_id'] for a in arrays) if arrays else default
    if name == 'SOLAR_ENABLED':
        return 'true' if battery_arrays else 'false'
    if name == 'SOLAR_CAPACITY_KW':
        total = sum((a.get('capacity_kwp') or a.get('capacity_kw') or 0) for a in battery_arrays)
        return f'{total:g}' if total else default
    if name == 'CARE_RATE':
        v = _row_text(snap['cfg'].get('billing.discount_program'))
        return 'true' if v == 'CARE' else 'false'
    if name == 'NEM_VERSION':
        v = _row_text(snap['cfg'].get('billing.export_compensation'))
        return {'net_metering': 'nem2', 'net_billing': 'nem3'}.get(v or '', default)
    if name in ('TOU_ENABLED', 'PEAK_START_HOUR', 'PEAK_END_HOUR', 'PEAK_DAYS'):
        w = _peak_window()
        if name == 'TOU_ENABLED':
            return 'true' if w is not None else 'false'
        if w is None:
            return default
        if name == 'PEAK_START_HOUR':
            return str(w.start_hour)
        if name == 'PEAK_END_HOUR':
            return str(w.end_hour)
        return w.days_mode()
    return default


def _store_raw(name: str, default: Optional[str] = None) -> Optional[str]:
    m = _ARRAY_RE.match(name)
    if m:
        cls = ARRAY_FIELDS[m.group(2)][0]
        if cls not in STORE_CLASSES:
            return os.getenv(name, default)
        text = _array_field_text(_array_for(array_id=m.group(1)), m.group(2))
        _note_divergence(name, text)
        return default if text is None else text

    if name in LEGACY_ARRAY_ALIASES:
        atype, fld = LEGACY_ARRAY_ALIASES[name]
        if ARRAY_FIELDS[fld][0] not in STORE_CLASSES:
            return os.getenv(name, default)
        text = _array_field_text(_array_for(array_type=atype), fld)
        _note_divergence(name, text)
        return default if text is None else text

    ent = BY_ENV.get(name)
    if ent is None or ent.cls in ENV_CLASSES:
        return os.getenv(name, default)
    if ent.cls == RETIRE:
        if os.environ.get(name) is not None:
            _retired_present.add(name)
        return default
    if ent.cls == DERIVED:
        text = _derived_text(name, None)
        _note_divergence(name, text)
        return default if text is None else text
    text = _row_text(_snap()['cfg'].get(ent.key))
    _note_divergence(name, text)
    return default if text is None else text


def raw(name: str, default: Optional[str] = None) -> Optional[str]:
    """Drop-in replacement for os.getenv(name, default)."""
    if authority() == 'env':
        return os.getenv(name, default)
    return _store_raw(name, default)


def is_set(name: str) -> bool:
    """Is an explicit (non-default) value present? For optional overrides such
    as RESERVE_SOC_HOME, where presence itself changes behavior."""
    if authority() == 'env':
        return bool(os.environ.get(name, '').strip())
    ent = BY_ENV.get(name)
    if ent is None or ent.cls in ENV_CLASSES:
        return bool(os.environ.get(name, '').strip())
    if ent.cls != SETTING and ent.cls != DETECTED:
        return bool((_store_raw(name) or '').strip())
    row = _snap()['cfg'].get(ent.key)
    if not row or row.get('value') in (None, ''):
        return False
    return row.get('source') != 'default'


def coerce(text: Optional[str], typ: str, default: Any = None) -> Any:
    if text is None or (text == '' and typ not in ('str', 'enum')):
        return default
    try:
        if typ == 'bool':
            return str(text).strip().lower() in ('true', '1', 'yes', 'on')
        if typ == 'int':
            return int(float(text))
        if typ in ('float', 'float?'):
            return float(text)
        if typ == 'list':
            s = str(text).strip()
            if s.startswith('['):
                return [str(x) for x in json.loads(s)]
            return [p.strip() for p in s.split(',') if p.strip()]
    except (ValueError, TypeError, json.JSONDecodeError):
        return default
    return text


def get(key: str, default: Any = None) -> Any:
    """Typed read by canonical key under the active authority."""
    ent = BY_KEY.get(key)
    if ent is None:
        raise KeyError(f'unknown setting {key!r}')
    fallback = ent.default if default is None else default
    if not ent.env:
        if authority() == 'store':
            return coerce(_row_text(_snap()['cfg'].get(key)), ent.type, fallback)
        src, conv = IMPORT_CONVERTERS.get(key, (None, None))
        v = os.getenv(src) if src else None
        return conv(v) if (conv and v not in (None, '')) else fallback
    return coerce(raw(ent.env[0]), ent.type, fallback)


def array_ids() -> List[str]:
    s = raw('SOLAR_ARRAYS', '') or ''
    return [a.strip() for a in s.split(',') if a.strip()]


# =============================================================================
# Store plan from an env mapping (shared by migrate_v46 and config_admin sync)
# =============================================================================

def store_plan_from_env(env: Dict[str, str]) -> Dict[str, Any]:
    """What the store should hold to reproduce `env` exactly.

    Returns {'set': [(key, text, value_type, category, description)],
             'unset': [key, ...]}   # registry settings with no env value
    """
    plan_set, plan_unset = [], []
    for ent in REGISTRY:
        if ent.cls not in STORE_CLASSES or not ent.key:
            continue
        text = None
        if ent.env:
            for n in ent.env:
                if env.get(n, '') != '':
                    text = env[n]
                    break
        elif ent.key in IMPORT_CONVERTERS:
            src, conv = IMPORT_CONVERTERS[ent.key]
            if env.get(src, '') != '':
                text = conv(env[src])
        if text is None:
            plan_unset.append(ent.key)
            continue
        if ent.store_type == 'bool':
            text = _bool_text(text)
        elif ent.store_type == 'json':
            s = str(text).strip()
            try:
                lst = json.loads(s) if s.startswith('[') else None
            except json.JSONDecodeError:
                lst = None
            if lst is None:
                lst = [p.strip() for p in s.split(',') if p.strip()]
            text = json.dumps(lst)
        plan_set.append((ent.key, text, ent.store_type, ent.category or ent.key.split('.')[0],
                         ent.help))
    return {'set': plan_set, 'unset': plan_unset}


def array_plan_from_env(env: Dict[str, str]) -> List[Dict[str, Any]]:
    """Non-secret per-array fields from SOLAR_ARRAYS / SOLAR_ARRAY_<ID>_* (plus
    legacy aliases where the SOLAR_ARRAY_* form is absent)."""
    ids = [a.strip().lower() for a in env.get('SOLAR_ARRAYS', '').split(',') if a.strip()]
    out = []
    for aid in ids:
        prefix = f'SOLAR_ARRAY_{aid.upper()}_'
        row = {'array_id': aid, 'cols': {}, 'cfg': {}}
        for fld, (cls, where, name) in ARRAY_FIELDS.items():
            if cls not in STORE_CLASSES:
                continue
            v = env.get(prefix + fld, '')
            if v == '':
                continue
            if where == 'col':
                row['cols'][name] = float(v) if name == 'capacity_kw' else v
            else:
                row['cfg'][name] = v
        out.append(row)
    for alias, (atype, fld) in LEGACY_ARRAY_ALIASES.items():
        cls, where, name = ARRAY_FIELDS[fld]
        if cls not in STORE_CLASSES or env.get(alias, '') == '':
            continue
        for row in out:
            t = (row['cols'].get('array_type') or '').lower()
            if t == atype:
                bucket = row['cols'] if where == 'col' else row['cfg']
                bucket.setdefault(name, env[alias])
                break
    return out


# =============================================================================
# Startup summary
# =============================================================================

def startup_summary() -> List[str]:
    lines = [f'Config authority: {authority()}'
             + (f" (flag was store; fell back: {_state['fallback_reason']})"
                if _state['fallback_reason'] else '')]
    if authority() == 'store':
        # Touch every registered env name so divergence/retired sets fill.
        for ent in REGISTRY:
            for n in ent.env:
                _store_raw(n)
        for aid in array_ids():
            for fld in ARRAY_FIELDS:
                _store_raw(f'SOLAR_ARRAY_{aid.upper()}_{fld}')
        if _env_diverged:
            lines.append('  .env values ignored (store is authoritative): '
                         + ', '.join(sorted(_env_diverged)))
        if _retired_present:
            lines.append('  retired .env keys ignored: ' + ', '.join(sorted(_retired_present)))
    return lines


def env_divergence() -> Dict[str, Tuple[str, str]]:
    return dict(_env_diverged)


if __name__ == '__main__':
    for line in startup_summary():
        print(line)
    print(f'registry: {len(REGISTRY)} entries, {len(BY_KEY)} canonical keys, '
          f'{len(ARRAY_FIELDS)} per-array fields')
