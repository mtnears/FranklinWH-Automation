#!/usr/bin/env python3
"""
rate_schedule.py — Rate Schedule Engine for FranklinWH Battery Automation v4.0

Replaces simple peak_start/peak_end with full multi-tier rate schedule:
  - Multiple rate tiers (peak, off-peak, super-off-peak, etc.)
  - Multiple windows per day
  - Weekday/weekend/holiday rules
  - Export rates for NEM 3.0 users
  - Rate lookups: current_rate(), next_rate_change(), is_peak(), cheapest_before_peak()
  - Seasonal switching: per-season tier_rates and/or windows overrides

Reads from data/rate_schedule.json (user-configured).

Part of the v4.0 Adaptive Decision Engine.
"""

import json
import os
import logging
from datetime import datetime, timedelta, time as dtime
from dataclasses import dataclass, field
from typing import Optional, List, Tuple

logger = logging.getLogger('rate_schedule')

# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class RateWindow:
    """A time window with a specific rate tier."""
    tier: str
    days: List[str]       # ["mon","tue","wed","thu","fri"] or ["sat","sun"] etc.
    start: dtime          # Start time
    end: dtime            # End time
    rate_cents: float     # Rate in cents/kWh

    def matches(self, dt: datetime) -> bool:
        """Check if this window is active at the given datetime."""
        day_abbr = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'][dt.weekday()]
        if day_abbr not in self.days:
            return False

        t = dt.time()
        if self.start <= self.end:
            # Normal window: e.g., 17:00 - 20:00
            return self.start <= t < self.end
        else:
            # Midnight-crossing window: e.g., 23:00 - 06:00
            return t >= self.start or t < self.end


@dataclass
class RateTier:
    """A named rate tier with cost."""
    name: str
    rate_cents: float


@dataclass
class ExportConfig:
    """Export/net metering configuration."""
    capable: bool = False
    net_metering: str = ""  # "NEM 2.0", "NEM 3.0", etc.
    # Export rates by tier (for NEM 3.0 / non-NEM users)
    export_rates: Optional[dict] = None  # {tier_name: cents_per_kwh}


@dataclass
class RateSchedule:
    """Complete rate schedule for a utility plan."""
    name: str
    tiers: dict                     # {tier_name: rate_cents}
    windows: List[RateWindow]
    default_tier: str               # Tier when no window matches
    export: ExportConfig
    holidays: List[str] = field(default_factory=list)   # ["2026-01-01", ...]
    holiday_tier: str = "off_peak"

    def current_tier(self, dt: Optional[datetime] = None) -> Tuple[str, float]:
        """Get current rate tier name and rate in cents/kWh.
        
        Returns (tier_name, rate_cents).
        """
        if dt is None:
            dt = datetime.now()

        # Holiday check
        date_str = dt.strftime('%Y-%m-%d')
        if date_str in self.holidays:
            rate = self.tiers.get(self.holiday_tier, 0)
            return self.holiday_tier, rate

        # Check windows (first match wins — peak windows should be listed first)
        for window in self.windows:
            if window.matches(dt):
                return window.tier, window.rate_cents

        # Default
        rate = self.tiers.get(self.default_tier, 0)
        return self.default_tier, rate

    def current_rate(self, dt: Optional[datetime] = None) -> float:
        """Current rate in cents/kWh."""
        _, rate = self.current_tier(dt)
        return rate

    def is_peak(self, dt: Optional[datetime] = None) -> bool:
        """Is the current time in a peak rate window?"""
        tier, _ = self.current_tier(dt)
        return tier == 'peak'

    def is_partial_peak(self, dt: Optional[datetime] = None) -> bool:
        """Is the current time in a partial-peak rate window?

        Partial-peak tiers exist on three-tier rate plans (e.g., PG&E EV2-A
        with 4-9pm peak and 3-4pm / 9pm-midnight partial-peak). These windows
        are mid-priced — the engine should avoid imports when possible but
        accept them gracefully when battery can't cover the full expensive
        window. See Priority 4.5 in adaptive_engine.py for the decision logic.
        """
        tier, _ = self.current_tier(dt)
        return tier == 'partial_peak'

    def is_expensive(self, dt: Optional[datetime] = None) -> bool:
        """Is the current time in peak OR partial-peak — 'avoid imports if possible'."""
        tier, _ = self.current_tier(dt)
        return tier in ('peak', 'partial_peak')

    def expensive_window_remaining_hours(
            self, dt: Optional[datetime] = None) -> Tuple[float, float]:
        """Hours of peak and partial-peak remaining in the current contiguous
        expensive period (until the next off-peak transition).

        Returns (peak_hours_remaining, partial_peak_hours_remaining).
        Returns (0.0, 0.0) if not currently in an expensive window.

        Used by Priority 4.5 to differentiate pre-peak partial-peak (peak
        still ahead, preserve battery) from post-peak partial-peak (peak
        already done, free to discharge).

        Walks forward in 5-minute increments from dt until the first off-peak
        tier transition (or 12-hour safety limit).
        """
        if dt is None:
            dt = datetime.now()

        if not self.is_expensive(dt):
            return (0.0, 0.0)

        peak_hours = 0.0
        partial_hours = 0.0
        step = timedelta(minutes=5)
        step_hours = step.total_seconds() / 3600.0
        check = dt
        limit = dt + timedelta(hours=12)

        while check < limit:
            tier, _ = self.current_tier(check)
            if tier == 'peak':
                peak_hours += step_hours
            elif tier == 'partial_peak':
                partial_hours += step_hours
            else:
                break  # exited expensive window
            check += step

        return (peak_hours, partial_hours)

    def export_rate(self, dt: Optional[datetime] = None) -> float:
        """Current export rate in cents/kWh. Returns 0 if non-export."""
        if not self.export.capable or not self.export.export_rates:
            return 0.0
        tier, _ = self.current_tier(dt)
        return self.export.export_rates.get(tier, 0.0)

    def next_rate_change(self, dt: Optional[datetime] = None) -> Tuple[datetime, str, float]:
        """Find the next time the rate changes.
        
        Returns (change_datetime, new_tier_name, new_rate_cents).
        Scans forward in 15-minute increments up to 48 hours.
        """
        if dt is None:
            dt = datetime.now()

        current_tier, current_rate = self.current_tier(dt)
        check = dt + timedelta(minutes=5)
        limit = dt + timedelta(hours=96)

        while check <= limit:
            new_tier, new_rate = self.current_tier(check)
            if new_tier != current_tier:
                # Refine to the minute
                refine = check - timedelta(minutes=5)
                while refine < check:
                    rt, rr = self.current_tier(refine)
                    if rt != current_tier:
                        return refine, rt, rr
                    refine += timedelta(minutes=1)
                return check, new_tier, new_rate
            check += timedelta(minutes=5)

        # No change found in 48 hours
        return limit, current_tier, current_rate

    def next_peak_start(self, dt: Optional[datetime] = None) -> Optional[datetime]:
        """Find the next peak period start time. Returns None if no peak in 48h."""
        if dt is None:
            dt = datetime.now()

        if self.is_peak(dt):
            # Already in peak — find when current peak ends, then next peak starts
            check = dt
            while self.is_peak(check) and check < dt + timedelta(hours=96):
                check += timedelta(minutes=5)
            dt = check

        # Scan forward in 5-minute increments for precise detection
        check = dt + timedelta(minutes=5)
        limit = dt + timedelta(hours=96)
        was_peak = self.is_peak(dt)
        while check <= limit:
            now_peak = self.is_peak(check)
            if now_peak and not was_peak:
                # Transition found — refine to the exact minute
                refine = check - timedelta(minutes=5)
                while refine < check:
                    if self.is_peak(refine):
                        return refine
                    refine += timedelta(minutes=1)
                return check
            was_peak = now_peak
            check += timedelta(minutes=5)
        return None

    def next_peak_end(self, dt: Optional[datetime] = None) -> Optional[datetime]:
        """Find when the current or next peak period ends."""
        if dt is None:
            dt = datetime.now()

        # If not in peak, find next peak first
        if not self.is_peak(dt):
            peak_start = self.next_peak_start(dt)
            if peak_start is None:
                return None
            dt = peak_start

        # Find when peak ends using 5-min increments
        check = dt + timedelta(minutes=5)
        limit = dt + timedelta(hours=96)
        while check <= limit:
            if not self.is_peak(check):
                return check
            check += timedelta(minutes=5)
        return None

    def hours_to_peak(self, dt: Optional[datetime] = None) -> Optional[float]:
        """Hours until next peak period. None if no peak in 48h."""
        if dt is None:
            dt = datetime.now()
        if self.is_peak(dt):
            return 0.0
        peak = self.next_peak_start(dt)
        if peak is None:
            return None
        return (peak - dt).total_seconds() / 3600.0

    def cheapest_rate_before_peak(self, dt: Optional[datetime] = None) -> Tuple[str, float]:
        """Find the cheapest rate tier between now and the next peak.
        
        Returns (tier_name, rate_cents). Used for deciding whether to charge now.
        """
        if dt is None:
            dt = datetime.now()

        peak_start = self.next_peak_start(dt)
        if peak_start is None:
            # No peak coming — return current rate
            return self.current_tier(dt)

        cheapest_tier = None
        cheapest_rate = float('inf')

        check = dt
        while check < peak_start:
            tier, rate = self.current_tier(check)
            if rate < cheapest_rate:
                cheapest_rate = rate
                cheapest_tier = tier
            check += timedelta(minutes=15)

        return cheapest_tier or self.default_tier, cheapest_rate

    def peak_duration_hours(self, dt: Optional[datetime] = None) -> float:
        """Duration of the next (or current) peak period in hours."""
        if dt is None:
            dt = datetime.now()

        if self.is_peak(dt):
            start = dt
        else:
            start = self.next_peak_start(dt)
            if start is None:
                return 0.0

        end = self.next_peak_end(start)
        if end is None:
            return 0.0
        return (end - start).total_seconds() / 3600.0

    def hours_since_peak_end(self, dt: Optional[datetime] = None) -> Optional[float]:
        """Hours since the most recent peak period ended.

        Returns None if:
          - No peak has occurred in the last 24 hours
          - Currently in peak (use is_peak() for that)
          - Today is not a peak day and yesterday wasn't either

        Used by post-peak logic to determine if we just came out of peak
        and should consider burning solar-charged battery energy.
        """
        if dt is None:
            dt = datetime.now()

        if self.is_peak(dt):
            return None

        search = dt - timedelta(minutes=5)
        limit = dt - timedelta(hours=24)
        while search >= limit:
            if self.is_peak(search):
                peak_end = self.next_peak_end(search)
                if peak_end and peak_end <= dt:
                    return (dt - peak_end).total_seconds() / 3600.0
                return (dt - search).total_seconds() / 3600.0
            search -= timedelta(minutes=15)

        return None

    def rate_spread(self, dt: Optional[datetime] = None) -> float:
        """Difference between peak and current rate in cents/kWh.
        Indicates value of peak avoidance."""
        peak_rate = self.tiers.get('peak', 0)
        _, current_rate = self.current_tier(dt)
        return peak_rate - current_rate


# ---------------------------------------------------------------------------
# JSON Loading
# ---------------------------------------------------------------------------

def _parse_time(s: str) -> dtime:
    """Parse "HH:MM" string to time object."""
    parts = s.split(':')
    return dtime(int(parts[0]), int(parts[1]))


def _validate_seasons(seasons: list, tier_rates: dict) -> None:
    """Sanity-check the seasons config and log warnings for likely misconfig.

    Warnings only — config still loads with whatever was provided:
      - Months that overlap between seasons (first match wins)
      - Months not covered by any season (will fall back to JSON base)
      - Invalid month values (not int 1-12)
      - Season tier_rates references tier name not defined in JSON tiers
      - Empty 'months' list (season will never match)
    """
    if not isinstance(seasons, list):
        logger.warning("'seasons' must be a list — got %s, ignoring", type(seasons).__name__)
        return

    matched_months = {}
    for season in seasons:
        name = season.get('name', 'unnamed')
        months = season.get('months', [])
        if not months:
            logger.warning("Season '%s' has empty 'months' list — will never match", name)
            continue
        for m in months:
            if not isinstance(m, int) or not 1 <= m <= 12:
                logger.warning(
                    "Season '%s' has invalid month %r — must be int 1-12",
                    name, m
                )
                continue
            if m in matched_months:
                logger.warning(
                    "Month %d is in both season '%s' and season '%s' — first match wins",
                    m, matched_months[m], name
                )
            else:
                matched_months[m] = name
        for tier_name in (season.get('tier_rates') or {}).keys():
            if tier_name not in tier_rates:
                logger.warning(
                    "Season '%s' tier_rates references unknown tier '%s' — ignored",
                    name, tier_name
                )

    missing = sorted(set(range(1, 13)) - matched_months.keys())
    if missing:
        logger.warning(
            "Seasons config does not cover months %s — these months will use JSON base config",
            missing
        )


def _load_rates_from_db(db_path: str, today: str) -> Optional[dict]:
    """Query rate_history for the most recent row effective on or before today.

    Returns a dict with 'peak' and 'off_peak' rate_cents, or None if the DB
    is unavailable, empty, or the relevant row has no rate data.

    Rate selection priority:
      1. care_peak_rate / care_off_peak_rate  (CARE discount users)
      2. peak_rate / off_peak_rate            (standard users)

    This allows any user who has populated rate_history to get automatic
    seasonal switching — just insert rows with the correct effective_date
    and the right rates will be picked up automatically on the flip date.
    """
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT peak_rate, off_peak_rate, care_peak_rate, care_off_peak_rate,
                   rate_name, effective_date
            FROM rate_history
            WHERE effective_date <= ?
            ORDER BY effective_date DESC
            LIMIT 1
            """,
            (today,)
        ).fetchone()
        conn.close()

        if row is None:
            logger.debug("rate_history: no rows found for date %s — using JSON rates", today)
            return None

        # Prefer CARE rates if present, fall back to standard rates
        if row['care_peak_rate'] is not None and row['care_off_peak_rate'] is not None:
            peak = row['care_peak_rate'] * 100      # dollars → cents
            off_peak = row['care_off_peak_rate'] * 100
            rate_type = 'CARE'
        elif row['peak_rate'] is not None and row['off_peak_rate'] is not None:
            peak = row['peak_rate'] * 100
            off_peak = row['off_peak_rate'] * 100
            rate_type = 'standard'
        else:
            logger.warning(
                "rate_history row '%s' (effective %s) has no usable rates — using JSON rates",
                row['rate_name'], row['effective_date']
            )
            return None

        logger.info(
            "Rates from DB: %s (%s, effective %s) — peak=%.3f¢  off_peak=%.3f¢",
            row['rate_name'], rate_type, row['effective_date'], peak, off_peak
        )
        return {'peak': peak, 'off_peak': off_peak}

    except Exception as e:
        logger.warning("rate_history DB lookup failed (%s) — using JSON rates", e)
        return None


def load_rate_schedule(json_path: str) -> RateSchedule:
    """Load rate schedule from JSON config, applying seasonal and DB overrides.

    Precedence (later wins on conflict):
      1. JSON base tier_rates and windows
      2. Seasonal overrides from seasons[].tier_rates and seasons[].windows
         (matched by current month against seasons[].months)
      3. DB rate_history override for peak/off_peak (most recent row with
         effective_date <= today)

    The DB override only covers peak/off_peak because rate_history has no
    partial_peak column. For 3-tier rate plans the seasons block handles
    partial_peak switching across summer/winter; DB rate_history continues
    to handle peak/off_peak for backward compat and mid-season rate changes.

    A config without a 'seasons' block behaves exactly as before — fully
    backward compatible with any existing user JSON.
    """
    with open(json_path, 'r') as f:
        data = json.load(f)

    rs = data.get('rate_schedule', data)  # Allow nested or flat

    # --- Step 1: Parse JSON base tiers (fallback baseline) ---
    tiers = rs.get('tiers', {})
    tier_rates = {}
    for tier_name, tier_info in tiers.items():
        if isinstance(tier_info, dict):
            tier_rates[tier_name] = tier_info.get('rate_cents', 0)
        else:
            tier_rates[tier_name] = float(tier_info)

    # --- Step 2: Apply seasonal overrides (tier_rates and/or windows) ---
    raw_windows = rs.get('windows', [])
    active_season_name = None
    seasons = rs.get('seasons')
    if seasons:
        _validate_seasons(seasons, tier_rates)
        current_month = datetime.now().month
        for season in seasons:
            if current_month in season.get('months', []):
                active_season_name = season.get('name', 'unnamed')
                season_tier_rates = season.get('tier_rates') or {}
                applied = []
                for tier_name, rate in season_tier_rates.items():
                    if tier_name in tier_rates:
                        tier_rates[tier_name] = float(rate)
                        applied.append(f"{tier_name}={float(rate):.3f}¢")
                if season.get('windows'):
                    raw_windows = season['windows']
                logger.info(
                    "Active season: %s (month %d) — tier overrides: [%s]%s",
                    active_season_name, current_month,
                    ', '.join(applied) if applied else 'none',
                    ', windows: yes' if season.get('windows') else ''
                )
                break
        if not active_season_name:
            logger.warning(
                "Seasons configured but no season matched month %d — falling back to JSON base config",
                datetime.now().month
            )

    # --- Step 3: DB override for peak/off_peak (preserves existing behaviour) ---
    db_path = os.path.join(os.path.dirname(json_path), 'franklin.db')
    today = datetime.now().strftime('%Y-%m-%d')
    db_rates = _load_rates_from_db(db_path, today)
    if db_rates:
        tier_rates['peak'] = db_rates['peak']
        tier_rates['off_peak'] = db_rates['off_peak']
    elif not active_season_name:
        # Only log JSON-rates banner when neither seasons nor DB applied
        logger.info(
            "Using JSON base rates: peak=%.3f¢  off_peak=%.3f¢  partial_peak=%.3f¢",
            tier_rates.get('peak', 0), tier_rates.get('off_peak', 0),
            tier_rates.get('partial_peak', 0)
        )

    # --- Parse windows with resolved tier_rates ---
    windows = []
    for w in raw_windows:
        tier = w['tier']
        rate = tier_rates.get(tier, 0)
        windows.append(RateWindow(
            tier=tier,
            days=w.get('days', ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']),
            start=_parse_time(w['start']),
            end=_parse_time(w['end']),
            rate_cents=rate,
        ))

    # --- Parse export config ---
    export_data = rs.get('export', {})
    export = ExportConfig(
        capable=export_data.get('capable', False),
        net_metering=export_data.get('net_metering', ''),
        export_rates=export_data.get('export_rates'),
    )

    schedule = RateSchedule(
        name=rs.get('name', 'Unknown'),
        tiers=tier_rates,
        windows=windows,
        default_tier=rs.get('default_tier', 'off_peak'),
        export=export,
        holidays=rs.get('holidays', []),
        holiday_tier=rs.get('holiday_tier', 'off_peak'),
    )

    season_info = f", season={active_season_name}" if active_season_name else ""
    logger.info(f"Loaded rate schedule: {schedule.name} "
                f"({len(schedule.tiers)} tiers, {len(schedule.windows)} windows{season_info})")
    return schedule


# ---------------------------------------------------------------------------
# CLI — Run standalone for testing
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    json_path = sys.argv[1] if len(sys.argv) > 1 else 'data/rate_schedule.json'
    schedule = load_rate_schedule(json_path)

    now = datetime.now()
    tier, rate = schedule.current_tier(now)

    print(f"\n{'=' * 60}")
    print(f"RATE SCHEDULE: {schedule.name}")
    print(f"{'=' * 60}")

    print(f"\nTiers:")
    for name, cents in schedule.tiers.items():
        print(f"  {name}: {cents}¢/kWh")

    print(f"\nWindows:")
    for w in schedule.windows:
        days = ','.join(w.days)
        print(f"  {w.tier}: {w.start.strftime('%H:%M')}-{w.end.strftime('%H:%M')} [{days}]")

    print(f"\nExport: {'yes' if schedule.export.capable else 'no'}"
          f" ({schedule.export.net_metering})" if schedule.export.net_metering else "")

    print(f"\n--- Current State ({now.strftime('%A %Y-%m-%d %H:%M')}) ---")
    print(f"  Current tier: {tier} @ {rate}¢/kWh")
    print(f"  Is peak: {schedule.is_peak(now)}")

    h2p = schedule.hours_to_peak(now)
    print(f"  Hours to peak: {h2p:.1f}" if h2p is not None else "  Hours to peak: no peak in 48h")

    next_change_dt, next_tier, next_rate = schedule.next_rate_change(now)
    print(f"  Next rate change: {next_change_dt.strftime('%H:%M')} → {next_tier} @ {next_rate}¢/kWh")

    cheapest_tier, cheapest_rate = schedule.cheapest_rate_before_peak(now)
    print(f"  Cheapest before peak: {cheapest_tier} @ {cheapest_rate}¢/kWh")

    spread = schedule.rate_spread(now)
    print(f"  Rate spread (peak - current): {spread:.1f}¢/kWh")

    peak_dur = schedule.peak_duration_hours(now)
    print(f"  Peak duration: {peak_dur:.1f} hours")

    # Walk through 24 hours
    print(f"\n--- 24-Hour Rate Walk ---")
    print(f"  {'Time':>8}  {'Tier':>12}  {'Rate':>8}  {'Peak':>5}")
    check = now.replace(hour=0, minute=0, second=0)
    for _ in range(96):  # every 15 min
        t, r = schedule.current_tier(check)
        is_p = "YES" if t == 'peak' else ""
        if check.minute == 0:
            print(f"  {check.strftime('%H:%M'):>8}  {t:>12}  {r:>6.1f}¢  {is_p:>5}")
        check += timedelta(minutes=15)
