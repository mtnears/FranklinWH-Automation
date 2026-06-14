#!/usr/bin/env python3
"""
rate_config.py — v4.6 Canonical Rate Resolver

THE single answer to "what rates and what peak window apply on date D".
Fixes two classes of divergence:

  - calculate_daily_savings.py computed savings from hardcoded fictional
    rates (0.60/0.41) and its own hardcoded peak hours (17-20) — the "sixth
    divergent rate path".
  - smart_decision.py read peak hours from legacy env vars (PEAK_START_HOUR/
    PEAK_END_HOUR), which drifted from the rate schedule (issue #26).

Resolution sources, in precedence order (mirrors the engine's proven
load_rate_schedule() merge so engine and resolver always agree):

  1. v4.6 rate tables (rate_plans/rate_seasons/rate_tiers/rate_windows,
     populated by migrate_v46.py) — per-date season selection, full tier set.
  2. rate_history overlay for peak/off_peak — most recent row with
     effective_date <= D, CARE-first (mirrors rate_schedule._load_rates_from_db
     exactly, including the dollars->cents x100).
  3. JSON fallback (data/rate_schedule.json) with per-date season selection,
     for installs that haven't run the v4.6 migration yet.

If nothing resolves, returns None — callers skip/warn rather than fabricate.

Units: RateResolution carries CENTS canonically (engine convention);
.dollars(tier) converts for dollar-domain consumers (savings math).

Usage:
    import rate_config
    r = rate_config.resolve_rates('2026-06-12')
    r.rate_cents('peak')      # 34.976
    r.dollars('peak')         # 0.34976
    r.label                   # 'CARE'
    w = rate_config.peak_window_for_date('2026-06-12')
    w.start_hour, w.end_hour  # 16, 21
    w.applies_on(date)        # day-of-week check
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, date as date_type
from pathlib import Path
from typing import Dict, List, Optional

import db

logger = logging.getLogger(__name__)

_DAY_NAMES = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']

# Per-date caches — cleared naturally by process lifetime; date-keyed so a
# long-running process crosses season/rate boundaries correctly.
_rates_cache: Dict[str, Optional['RateResolution']] = {}
_window_cache: Dict[str, Optional['PeakWindow']] = {}


@dataclass
class RateResolution:
    """Resolved tier rates for one date. Rates are in CENTS per kWh."""
    date: str
    tier_rates_cents: Dict[str, float]
    label: str                 # 'CARE' | 'standard' | 'plan' | 'json'
    source: str                # human-readable provenance chain
    plan_name: str = ''
    season: Optional[str] = None

    def rate_cents(self, tier: str, default: float = None) -> Optional[float]:
        return self.tier_rates_cents.get(tier, default)

    def dollars(self, tier: str, default: float = None) -> Optional[float]:
        c = self.tier_rates_cents.get(tier)
        return round(c / 100.0, 6) if c is not None else default


@dataclass
class PeakWindow:
    """Resolved primary peak window for one date."""
    date: str
    start_hour: int
    end_hour: int
    start: str                 # 'HH:MM'
    end: str                   # 'HH:MM'
    days: List[str] = field(default_factory=lambda: list(_DAY_NAMES))
    source: str = 'plan'       # 'plan' | 'json'
    season: Optional[str] = None

    @property
    def crosses_midnight(self) -> bool:
        return self.start_hour > self.end_hour

    def applies_on(self, d) -> bool:
        """Does this peak window apply on the given date's weekday?"""
        if isinstance(d, str):
            d = datetime.strptime(d, '%Y-%m-%d').date()
        if isinstance(d, datetime):
            d = d.date()
        return _DAY_NAMES[d.weekday()] in self.days

    def days_mode(self) -> str:
        """Collapse the days list to the legacy PEAK_DAYS vocabulary."""
        s = set(self.days)
        if s == set(_DAY_NAMES):
            return 'all'
        if s == {'mon', 'tue', 'wed', 'thu', 'fri'}:
            return 'weekdays'
        if s == {'sat', 'sun'}:
            return 'weekends'
        return ','.join(self.days)


def _date_str(d) -> str:
    if isinstance(d, str):
        return d
    if isinstance(d, datetime):
        return d.strftime('%Y-%m-%d')
    if isinstance(d, date_type):
        return d.strftime('%Y-%m-%d')
    return str(d)


def _hour_of(hhmm: str) -> int:
    try:
        return int(str(hhmm).split(':')[0])
    except (ValueError, IndexError, AttributeError):
        return 0


# =============================================================================
# Source 1 — v4.6 rate tables
# =============================================================================

def _resolve_from_tables(date_str: str):
    """Returns (tier_rates_cents, plan_name, season_name, peak_windows) or None
    if the v4.6 tables are absent/empty (pre-migration install)."""
    try:
        plans = db.query("SELECT * FROM rate_plans WHERE active = 1 ORDER BY id LIMIT 1")
    except Exception:
        return None
    if not plans:
        return None
    plan = plans[0]
    plan_id = plan['id']
    month = int(date_str[5:7])

    season_id = None
    season_name = None
    for s in db.query("SELECT * FROM rate_seasons WHERE plan_id = ? ORDER BY id", (plan_id,)):
        try:
            months = json.loads(s['months_json'])
        except (json.JSONDecodeError, TypeError):
            months = []
        if month in months:
            season_id, season_name = s['id'], s['name']
            break

    rates = {}
    for r in db.query("SELECT tier, rate_cents FROM rate_tiers WHERE plan_id = ? AND season_id IS NULL",
                      (plan_id,)):
        rates[r['tier']] = r['rate_cents']
    if season_id is not None:
        for r in db.query("SELECT tier, rate_cents FROM rate_tiers WHERE plan_id = ? AND season_id = ?",
                          (plan_id, season_id)):
            rates[r['tier']] = r['rate_cents']
    if not rates:
        return None

    windows = []
    if season_id is not None:
        windows = db.query(
            "SELECT * FROM rate_windows WHERE plan_id = ? AND season_id = ? ORDER BY start_time",
            (plan_id, season_id))
    if not windows:
        windows = db.query(
            "SELECT * FROM rate_windows WHERE plan_id = ? AND season_id IS NULL ORDER BY start_time",
            (plan_id,))
    peak_windows = []
    for w in windows:
        if w['tier'] != 'peak':
            continue
        try:
            days = json.loads(w['days_json'])
        except (json.JSONDecodeError, TypeError):
            days = list(_DAY_NAMES)
        peak_windows.append({'start': w['start_time'], 'end': w['end_time'], 'days': days})

    return rates, plan.get('name', ''), season_name, peak_windows


# =============================================================================
# Source 2 — rate_history overlay (mirrors rate_schedule._load_rates_from_db)
# =============================================================================

def _history_overlay(date_str: str):
    """Most recent rate_history row effective on/before date, CARE-first.
    Returns (peak_cents, off_peak_cents, label) or None."""
    try:
        rows = db.query(
            "SELECT peak_rate, off_peak_rate, care_peak_rate, care_off_peak_rate, "
            "rate_name, effective_date FROM rate_history "
            "WHERE effective_date <= ? ORDER BY effective_date DESC LIMIT 1",
            (date_str,))
    except Exception as e:
        logger.warning("rate_history lookup failed for %s (%s)", date_str, e)
        return None
    if not rows:
        return None
    row = rows[0]
    if row['care_peak_rate'] is not None and row['care_off_peak_rate'] is not None:
        return (row['care_peak_rate'] * 100, row['care_off_peak_rate'] * 100,
                'CARE', row['rate_name'], row['effective_date'])
    if row['peak_rate'] is not None and row['off_peak_rate'] is not None:
        return (row['peak_rate'] * 100, row['off_peak_rate'] * 100,
                'standard', row['rate_name'], row['effective_date'])
    return None


# =============================================================================
# Source 3 — JSON fallback (pre-migration installs), per-date season
# =============================================================================

def _find_rate_schedule_json() -> Optional[Path]:
    here = Path(__file__).resolve().parent
    for c in (here.parent / 'data' / 'rate_schedule.json',
              Path('/app/data/rate_schedule.json'),
              Path.cwd() / 'data' / 'rate_schedule.json'):
        if c.exists():
            return c
    return None


def _resolve_from_json(date_str: str):
    path = _find_rate_schedule_json()
    if not path:
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("rate_schedule.json unreadable (%s)", e)
        return None
    rs = data.get('rate_schedule', data)
    rates = {}
    for tier, spec in (rs.get('tiers') or {}).items():
        rates[tier] = spec.get('rate_cents', 0) if isinstance(spec, dict) else float(spec)
    month = int(date_str[5:7])
    season_name = None
    raw_windows = rs.get('windows', [])
    for s in rs.get('seasons') or []:
        if month in s.get('months', []):
            season_name = s.get('name')
            for tier, rate in (s.get('tier_rates') or {}).items():
                rates[tier] = float(rate)
            if s.get('windows'):
                raw_windows = s['windows']
            break
    if not rates:
        return None
    peak_windows = [{'start': w.get('start'), 'end': w.get('end'),
                     'days': w.get('days', list(_DAY_NAMES))}
                    for w in raw_windows if w.get('tier') == 'peak']
    return rates, rs.get('name', ''), season_name, peak_windows


# =============================================================================
# Public API
# =============================================================================

def resolve_rates(d, use_cache: bool = True) -> Optional[RateResolution]:
    """Canonical per-date tier rates. None if nothing resolves — callers
    should skip/warn rather than invent rates."""
    date_str = _date_str(d)
    if use_cache and date_str in _rates_cache:
        return _rates_cache[date_str]

    result = None
    base = _resolve_from_tables(date_str)
    src_chain = []
    if base:
        rates, plan_name, season, _ = base
        src_chain.append(f"rate tables ({plan_name}"
                         f"{', season=' + season if season else ''})")
        label = 'plan'
    else:
        jbase = _resolve_from_json(date_str)
        if jbase:
            rates, plan_name, season, _ = jbase
            src_chain.append(f"rate_schedule.json ({plan_name}"
                             f"{', season=' + season if season else ''})")
            label = 'json'
        else:
            rates = plan_name = season = None

    if rates:
        overlay = _history_overlay(date_str)
        if overlay:
            peak_c, off_c, olabel, oname, oeff = overlay
            rates = dict(rates)
            rates['peak'] = peak_c
            rates['off_peak'] = off_c
            label = olabel
            src_chain.append(f"rate_history overlay ({oname}, effective {oeff})")
        result = RateResolution(
            date=date_str, tier_rates_cents=rates, label=label,
            source=' + '.join(src_chain), plan_name=plan_name or '',
            season=season)
    else:
        logger.warning("No rate source resolved for %s — no tables, no JSON", date_str)

    if use_cache:
        _rates_cache[date_str] = result
    return result


def peak_window_for_date(d, use_cache: bool = True) -> Optional[PeakWindow]:
    """Canonical primary peak window for a date. None if no source resolves
    (callers fall back to legacy env values and should log that fact)."""
    date_str = _date_str(d)
    if use_cache and date_str in _window_cache:
        return _window_cache[date_str]

    result = None
    for resolver, source in ((_resolve_from_tables, 'plan'),
                             (_resolve_from_json, 'json')):
        base = resolver(date_str)
        if not base:
            continue
        _, _, season, peak_windows = base
        if not peak_windows:
            continue
        # Primary = the longest peak window (plans with one peak window — the
        # common case — are unaffected; multi-window plans get the dominant one)
        def _duration(w):
            s, e = _hour_of(w['start']), _hour_of(w['end'])
            return (e - s) % 24 or 24
        w = max(peak_windows, key=_duration)
        result = PeakWindow(
            date=date_str,
            start_hour=_hour_of(w['start']), end_hour=_hour_of(w['end']),
            start=w['start'], end=w['end'],
            days=w.get('days', list(_DAY_NAMES)),
            source=source, season=season)
        break

    if result is None:
        logger.warning("No peak window resolved for %s from rate tables or JSON", date_str)
    if use_cache:
        _window_cache[date_str] = result
    return result


def check_env_window_conflict(env_start: int, env_end: int, d=None) -> Optional[str]:
    """Issue #26 surfacing: compare the legacy env peak window against the
    canonical schedule window. Returns a description when they DISAGREE,
    None when they agree or nothing canonical resolves."""
    w = peak_window_for_date(d or datetime.now())
    if w is None:
        return None
    if env_start != w.start_hour or env_end != w.end_hour:
        return (f"legacy env peak window {env_start:02d}:00-{env_end:02d}:00 "
                f"DISAGREES with rate schedule {w.start}-{w.end} "
                f"(source: {w.source}"
                f"{', season=' + w.season if w.season else ''}) — "
                f"engine follows the schedule; update or remove "
                f"PEAK_START_HOUR/PEAK_END_HOUR in .env (#26)")
    return None


def clear_cache():
    _rates_cache.clear()
    _window_cache.clear()


if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(levelname)-7s %(message)s')
    dates = sys.argv[1:] or [datetime.now().strftime('%Y-%m-%d')]
    for ds in dates:
        r = resolve_rates(ds)
        w = peak_window_for_date(ds)
        if r:
            tiers = ', '.join(f"{t}={c:.3f}¢ (${r.dollars(t):.5f})"
                              for t, c in sorted(r.tier_rates_cents.items()))
            print(f"{ds}: [{r.label}] {tiers}")
            print(f"  source: {r.source}")
        else:
            print(f"{ds}: NO RATES RESOLVED")
        if w:
            print(f"  peak window: {w.start}-{w.end} days={w.days_mode()} "
                  f"(source={w.source}{', season=' + w.season if w.season else ''})")
        else:
            print(f"  peak window: NOT RESOLVED")
