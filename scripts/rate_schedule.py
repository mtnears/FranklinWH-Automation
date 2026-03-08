#!/usr/bin/env python3
"""
rate_schedule.py — Rate Schedule Engine for FranklinWH Battery Automation v4.0

Replaces simple peak_start/peak_end with full multi-tier rate schedule:
  - Multiple rate tiers (peak, off-peak, super-off-peak, etc.)
  - Multiple windows per day
  - Weekday/weekend/holiday rules
  - Export rates for NEM 3.0 users
  - Rate lookups: current_rate(), next_rate_change(), is_peak(), cheapest_before_peak()

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


def load_rate_schedule(json_path: str) -> RateSchedule:
    """Load rate schedule from JSON config file."""
    with open(json_path, 'r') as f:
        data = json.load(f)

    rs = data.get('rate_schedule', data)  # Allow nested or flat

    # Parse tiers
    tiers = rs.get('tiers', {})
    tier_rates = {}
    for tier_name, tier_info in tiers.items():
        if isinstance(tier_info, dict):
            tier_rates[tier_name] = tier_info.get('rate_cents', 0)
        else:
            tier_rates[tier_name] = float(tier_info)

    # Parse windows
    windows = []
    for w in rs.get('windows', []):
        tier = w['tier']
        rate = tier_rates.get(tier, 0)
        windows.append(RateWindow(
            tier=tier,
            days=w.get('days', ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']),
            start=_parse_time(w['start']),
            end=_parse_time(w['end']),
            rate_cents=rate,
        ))

    # Parse export config
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

    logger.info(f"Loaded rate schedule: {schedule.name} "
                f"({len(schedule.tiers)} tiers, {len(schedule.windows)} windows)")
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
