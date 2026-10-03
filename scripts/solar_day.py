#!/usr/bin/env python3
"""
solar_day.py — this install's solar day: astronomy + learned production offsets

Replaces the fixed month-by-month SUN_SCHEDULE table.

    sunrise / sunset   computed for the actual date from the site location in
                       the store (NOAA solar position approximation), on the
                       local clock. Clock changes are handled by the time zone,
                       not by any special-case code.

    start / end / ramp learned from this install's own battery-connected
                       solar (system_readings.solar_kw, which every install
                       has): the typical minutes AFTER sunrise that production
                       starts (>=100 W) and reaches the ramp level, and the
                       minutes BEFORE sunset it ends. Offsets are measured
                       against the sun, not the clock, so they carry straight
                       across a clock change (verified on the Mar 8, 2026
                       spring-forward: +65/+81 min before, +80/+76 after).
                       Terrain, trees and panel orientation are captured
                       automatically; a rolling window follows the seasons.

    solar cap          learned 99.9th percentile of battery-connected solar
                       over the last 60 days, never above the store's AC
                       capacity of the battery-charging arrays.

Everything is recomputed at most once per day and cached in
data/solar_day_cache.json (the engine runs as a short subprocess).

    import solar_day
    sd = solar_day.solar_day()           # today
    sd.end, sd.ramp, sd.start            # local decimal hours
    solar_day.solar_cap_kw()             # float or None
"""

import json
import logging
import math
import os
import sqlite3
import statistics
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import settings

logger = logging.getLogger('solar_day')

LEARN_DAYS = 30            # rolling window for offsets
CAP_DAYS = 60              # rolling window for the solar cap
MIN_LEARN_DAYS = 7         # fewer productive days than this -> default offsets
START_W = 100              # production "present"
RAMP_KW_DEFAULT = 2.0      # "meaningful" production (about average home load)
PRODUCTIVE_FRACTION = 0.5  # learn only from days >= 50% of the window's best day

# Used only until MIN_LEARN_DAYS of data exist. Averages observed on the
# original install (Mar-Sep 2026); a new install replaces them within a week.
DEFAULT_START_OFFSET_MIN = 75.0
DEFAULT_END_OFFSET_MIN = -55.0
DEFAULT_RAMP_OFFSET_MIN = 180.0

CACHE_VERSION = 2


@dataclass
class SolarDay:
    date: str
    sunrise: float           # local decimal hours
    sunset: float
    start: float             # production starts (>= 100 W)
    ramp: float              # production reaches the ramp level
    end: float               # production ends (< 100 W)
    start_offset_min: float  # vs sunrise
    ramp_offset_min: float   # vs sunrise
    end_offset_min: float    # vs sunset (negative = before sunset)
    learned_days: int
    source: str              # 'learned' | 'default'
    location_source: str     # 'settings' | 'default'


# =============================================================================
# Location / time zone
# =============================================================================

def _tz():
    name = settings.raw('TZ', '') or ''
    try:
        from zoneinfo import ZoneInfo
        if name:
            return ZoneInfo(name)
    except Exception:
        pass
    return datetime.now().astimezone().tzinfo


# Last resort only, when no site location is configured (the review requires
# one). Logged as a fallback by solar_forecast.
FALLBACK_LAT, FALLBACK_LON = 38.91, -120.84


def location() -> Tuple[float, float, str]:
    lat, lon = settings.raw('FORECAST_LATITUDE'), settings.raw('FORECAST_LONGITUDE')
    try:
        if lat not in (None, '') and lon not in (None, ''):
            return float(lat), float(lon), 'settings'
    except ValueError:
        pass
    # No site location configured: last-resort fallback (callers log it).
    return FALLBACK_LAT, FALLBACK_LON, 'fallback'


# =============================================================================
# Astronomy (NOAA general solar position approximation, ~1-2 min accuracy)
# =============================================================================

def sun_times(d: date, lat: float = None, lon: float = None) -> Tuple[float, float]:
    """(sunrise, sunset) for date d in local-clock decimal hours."""
    if lat is None or lon is None:
        lat, lon, _ = location()
    n = d.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    phi = math.radians(lat)
    cos_ha = (math.cos(math.radians(90.833)) / (math.cos(phi) * math.cos(decl))
              - math.tan(phi) * math.tan(decl))
    cos_ha = max(-1.0, min(1.0, cos_ha))       # polar day/night guard
    ha = math.degrees(math.acos(cos_ha))
    rise_utc_min = 720 - 4 * (lon + ha) - eqt
    set_utc_min = 720 - 4 * (lon - ha) - eqt
    tz = _tz()
    off_min = datetime(d.year, d.month, d.day, 12, tzinfo=tz).utcoffset().total_seconds() / 60
    return (rise_utc_min + off_min) / 60.0, (set_utc_min + off_min) / 60.0


# =============================================================================
# Learning from this install's battery-connected solar
# =============================================================================

def _connect():
    c = sqlite3.connect(f'file:{settings.db_path()}?mode=ro', uri=True, timeout=10)
    return c


def _daily_windows(end_day: date, days: int, ramp_kw: float) -> Dict[str, dict]:
    """Per day: first/last >= START_W, first >= ramp_kw, energy proxy."""
    start_day = end_day - timedelta(days=days)
    out: Dict[str, dict] = {}
    try:
        c = _connect()
        try:
            rows = c.execute(
                "SELECT timestamp, solar_kw FROM system_readings "
                "WHERE timestamp >= ? AND timestamp < ? AND solar_kw IS NOT NULL "
                "ORDER BY timestamp",
                (start_day.isoformat(), end_day.isoformat())).fetchall()
        finally:
            c.close()
    except Exception as e:
        logger.warning('solar_day: readings unavailable (%s)', e)
        return out
    for ts, kw in rows:
        day = ts[:10]
        try:
            h = int(ts[11:13]) + int(ts[14:16]) / 60.0
        except ValueError:
            continue
        e = out.setdefault(day, {'first': None, 'last': None, 'ramp': None, 'sum_kw': 0.0,
                                 'n': 0})
        e['sum_kw'] += max(0.0, kw)
        e['n'] += 1
        if kw * 1000 >= START_W:
            if e['first'] is None:
                e['first'] = h
            e['last'] = h
        if e['ramp'] is None and kw >= ramp_kw:
            e['ramp'] = h
    return out


def learn_offsets(end_day: date = None, days: int = LEARN_DAYS,
                  ramp_kw: float = RAMP_KW_DEFAULT) -> dict:
    """Median start/ramp/end offsets (minutes vs sunrise/sunset) over productive days."""
    end_day = end_day or date.today()
    lat, lon, _ = location()
    wins = _daily_windows(end_day, days, ramp_kw)
    usable = {d: w for d, w in wins.items() if w['first'] is not None and w['n'] > 0}
    if not usable:
        return {'days': 0}
    # Productive-day filter: average kW as an energy proxy (reading cadence is
    # the same within a day, so this ranks days correctly)
    best = max(w['sum_kw'] / w['n'] for w in usable.values())
    starts, ends, ramps = [], [], []
    for d, w in usable.items():
        if best <= 0 or (w['sum_kw'] / w['n']) < PRODUCTIVE_FRACTION * best:
            continue
        sr, ss = sun_times(date.fromisoformat(d), lat, lon)
        starts.append((w['first'] - sr) * 60)
        ends.append((w['last'] - ss) * 60)
        if w['ramp'] is not None:
            ramps.append((w['ramp'] - sr) * 60)
    if not starts:
        return {'days': 0}
    return {
        'days': len(starts),
        'start': statistics.median(starts),
        'end': statistics.median(ends),
        'ramp': statistics.median(ramps) if len(ramps) >= max(3, len(starts) // 3) else None,
        'ramp_kw': ramp_kw,
    }


def learn_cap_kw(end_day: date = None, days: int = CAP_DAYS) -> Optional[float]:
    end_day = end_day or date.today()
    try:
        c = _connect()
        try:
            vals = [r[0] for r in c.execute(
                "SELECT solar_kw FROM system_readings WHERE timestamp >= ? AND timestamp < ? "
                "AND solar_kw > 0.05", ((end_day - timedelta(days=days)).isoformat(),
                                        end_day.isoformat()))]
        finally:
            c.close()
    except Exception as e:
        logger.warning('solar_day: cap readings unavailable (%s)', e)
        return None
    if len(vals) < 500:
        return None
    vals.sort()
    return round(vals[min(len(vals) - 1, int(len(vals) * 0.999))], 2)


def store_ac_capacity_kw() -> Optional[float]:
    """Sum of AC capacity of the battery-charging arrays (store), if known."""
    try:
        if settings.authority() == 'store':
            arrays = [a for a in settings._snap()['arrays'] if a.get('charges_battery') == 1]
        else:
            import config_store
            arrays = config_store.solar_arrays.battery_connected()
        total = sum(float(a.get('capacity_kw') or 0) for a in arrays)
        return total or None
    except Exception:
        return None


# =============================================================================
# Cache
# =============================================================================

def _cache_path() -> Path:
    d = os.getenv('DATA_DIR', '')
    base = Path(d) if d.startswith('/') else Path(__file__).resolve().parent.parent / (d or 'data')
    if not base.exists() and Path('/app/data').exists():
        base = Path('/app/data')
    return base / 'solar_day_cache.json'


_mem: Dict[str, dict] = {}


def _learned(today: date) -> dict:
    key = today.isoformat()
    if key in _mem:
        return _mem[key]
    p = _cache_path()
    try:
        data = json.loads(p.read_text())
        if data.get('computed_for') == key and data.get('version') == CACHE_VERSION:
            _mem[key] = data
            return data
    except Exception:
        pass
    offs = learn_offsets(today)
    cap_obs = learn_cap_kw(today)
    cap_store = store_ac_capacity_kw()
    if cap_obs is not None and cap_store is not None:
        cap = min(cap_obs, cap_store)
    else:
        cap = cap_obs if cap_obs is not None else cap_store
    data = {'version': CACHE_VERSION, 'computed_for': key,
            'computed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'offsets': offs, 'cap_observed_kw': cap_obs, 'cap_store_kw': cap_store,
            'cap_kw': cap}
    try:
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, p)
    except Exception:
        pass            # read-only contexts (tools) just skip the cache
    _mem[key] = data
    return data


# =============================================================================
# Public API
# =============================================================================

def solar_day(d: date = None) -> SolarDay:
    d = d or date.today()
    if isinstance(d, datetime):
        d = d.date()
    lat, lon, loc_src = location()
    sr, ss = sun_times(d, lat, lon)
    offs = _learned(date.today())['offsets']
    if offs.get('days', 0) >= MIN_LEARN_DAYS:
        so, eo = offs['start'], offs['end']
        ro = offs['ramp'] if offs.get('ramp') is not None else DEFAULT_RAMP_OFFSET_MIN
        src, n = 'learned', offs['days']
    else:
        so, eo, ro = DEFAULT_START_OFFSET_MIN, DEFAULT_END_OFFSET_MIN, DEFAULT_RAMP_OFFSET_MIN
        src, n = 'default', offs.get('days', 0)
    start = sr + so / 60.0
    end = ss + eo / 60.0
    ramp = min(sr + ro / 60.0, (start + end) / 2.0)     # never past solar noon-ish
    return SolarDay(date=d.isoformat(), sunrise=round(sr, 3), sunset=round(ss, 3),
                    start=round(start, 3), ramp=round(ramp, 3), end=round(end, 3),
                    start_offset_min=round(so, 1), ramp_offset_min=round(ro, 1),
                    end_offset_min=round(eo, 1), learned_days=n, source=src,
                    location_source=loc_src)


def solar_cap_kw() -> Optional[float]:
    return _learned(date.today()).get('cap_kw')


def summary() -> dict:
    sd = solar_day()
    data = _learned(date.today())
    return {'today': asdict(sd), 'cap_kw': data.get('cap_kw'),
            'cap_observed_kw': data.get('cap_observed_kw'),
            'cap_store_kw': data.get('cap_store_kw'),
            'computed_at': data.get('computed_at')}


def fmt(h: float) -> str:
    # Round to whole minutes first, so 6.9953 h is 07:00, not 06:00
    m = int(round(h * 60))
    return f'{(m // 60) % 24:02d}:{m % 60:02d}'


if __name__ == '__main__':
    s = summary()
    t = s['today']
    print(f"{t['date']}: sunrise {fmt(t['sunrise'])}  sunset {fmt(t['sunset'])}  "
          f"(location: {t['location_source']})")
    print(f"  production {fmt(t['start'])}-{fmt(t['end'])}, ramp {fmt(t['ramp'])}  "
          f"offsets {t['start_offset_min']:+.0f}/{t['ramp_offset_min']:+.0f}/{t['end_offset_min']:+.0f} min "
          f"({t['source']}, {t['learned_days']} days)")
    print(f"  solar cap {s['cap_kw']} kW (observed {s['cap_observed_kw']}, store AC {s['cap_store_kw']})")
