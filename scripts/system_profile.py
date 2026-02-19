#!/usr/bin/env python3
"""
system_profile.py — Learned System Behavior for FranklinWH Battery Automation v4.0

Scans continuous_monitoring.csv history to build:
  - Battery charge curve (grid charge rate by SOC bucket)
  - Home consumption patterns (hourly, weekday/weekend)
  - Solar production profile (hourly by month, daily totals)
  - System capacity parameters

Writes learned profiles to data/system_profile.json.
Profiles are rebuilt on first run from all available history, then refined
continuously as new data arrives.

Part of the v4.0 Adaptive Decision Engine.
"""

import csv
import json
import os
import logging
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger('system_profile')

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# SOC bucket width for charge curve (percentage points)
CHARGE_CURVE_BUCKET_WIDTH = 5

# Minimum charge rate (kW) to count as meaningful charging
MIN_CHARGE_RATE_KW = 0.5

# Grid charging detection: grid import threshold + charge rate threshold
GRID_CHARGE_MIN_GRID_KW = 2.0
GRID_CHARGE_MIN_RATE_KW = 3.0

# Minimum solar to count as producing
MIN_SOLAR_KW = 0.01

# Minimum home load to include (filters Modbus-era zero readings)
MIN_HOME_LOAD_KW = 0.05

# Default consumption when no history (kW flat)
DEFAULT_CONSUMPTION_KW = 1.0

# Default battery parameters (discovered or configured)
DEFAULT_BATTERY_COUNT = 1
DEFAULT_CAPACITY_KWH = 13.6  # Single FranklinWH aPower
DEFAULT_MAX_CHARGE_KW = 5.0
DEFAULT_MAX_DISCHARGE_KW = 5.0
DEFAULT_BACKUP_RESERVE_PCT = 20

# CSV interval (minutes) - used for kWh estimation
CSV_INTERVAL_MINUTES = 15
CSV_INTERVAL_HOURS = CSV_INTERVAL_MINUTES / 60.0


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ChargeCurve:
    """Battery charge rate by SOC bucket, separated by source."""
    # Grid charging: {soc_bucket: {"avg_kw": x, "max_kw": y, "samples": n}}
    grid: dict = field(default_factory=dict)
    # Solar charging: {soc_bucket: {"avg_kw": x, "max_kw": y, "samples": n}}
    solar: dict = field(default_factory=dict)

    def grid_charge_rate_kw(self, soc_pct: float) -> float:
        """Estimated grid charge rate at given SOC. Uses learned curve or defaults."""
        bucket = str(int(soc_pct // CHARGE_CURVE_BUCKET_WIDTH) * CHARGE_CURVE_BUCKET_WIDTH)
        if bucket in self.grid:
            return self.grid[bucket]['avg_kw']
        # Interpolate from nearest buckets
        return self._interpolate(self.grid, soc_pct, default=DEFAULT_MAX_CHARGE_KW)

    def _interpolate(self, curve: dict, soc_pct: float, default: float) -> float:
        """Interpolate from nearest known buckets."""
        if not curve:
            return default
        target = soc_pct
        buckets = sorted([(int(k), v['avg_kw']) for k, v in curve.items()])
        if not buckets:
            return default
        # Find bracketing buckets
        below = [(b, r) for b, r in buckets if b <= target]
        above = [(b, r) for b, r in buckets if b > target]
        if below and above:
            b_low, r_low = below[-1]
            b_high, r_high = above[0]
            if b_high == b_low:
                return r_low
            frac = (target - b_low) / (b_high - b_low)
            return r_low + frac * (r_high - r_low)
        elif below:
            return below[-1][1]
        elif above:
            return above[0][1]
        return default


@dataclass
class ConsumptionProfile:
    """Hourly home consumption averages by day type."""
    # {hour: avg_kw} for weekdays
    weekday: dict = field(default_factory=dict)
    # {hour: avg_kw} for weekends
    weekend: dict = field(default_factory=dict)
    # Overall average
    overall_avg_kw: float = DEFAULT_CONSUMPTION_KW
    # Peak observed
    peak_kw: float = 0.0
    # Total samples
    sample_count: int = 0

    def expected_kw(self, dt: Optional[datetime] = None) -> float:
        """Expected consumption at given datetime (or now)."""
        if dt is None:
            dt = datetime.now()
        hour = str(dt.hour)
        if dt.weekday() < 5:
            return self.weekday.get(hour, self.overall_avg_kw)
        else:
            return self.weekend.get(hour, self.overall_avg_kw)

    def expected_kwh(self, start: datetime, end: datetime) -> float:
        """Estimated total consumption between two datetimes."""
        total = 0.0
        current = start
        while current < end:
            total += self.expected_kw(current) * (1.0 / 4.0)  # 15-min steps
            current += timedelta(minutes=15)
        return total


@dataclass
class SolarProfile:
    """Learned solar production by hour and month."""
    # {month: {hour: avg_kw}}
    by_month_hour: dict = field(default_factory=dict)
    # {month: {"avg_daily_kwh": x, "clear_day_kwh": y, "days": n}}
    monthly_summary: dict = field(default_factory=dict)
    # Latest daily totals for trend
    recent_daily_kwh: list = field(default_factory=list)

    def expected_kw(self, dt: Optional[datetime] = None) -> float:
        """Expected solar production at given datetime (or now)."""
        if dt is None:
            dt = datetime.now()
        month = str(dt.month)
        hour = str(dt.hour)
        if month in self.by_month_hour:
            return self.by_month_hour[month].get(hour, 0.0)
        # Fallback: check all months for this hour
        all_for_hour = [
            m_data.get(hour, 0.0)
            for m_data in self.by_month_hour.values()
        ]
        return sum(all_for_hour) / len(all_for_hour) if all_for_hour else 0.0

    def forecast_remaining_kwh(self, from_dt: Optional[datetime] = None,
                                until_hour: int = 17) -> float:
        """Estimate remaining solar kWh from now until given hour (e.g., peak start)."""
        if from_dt is None:
            from_dt = datetime.now()
        total = 0.0
        current = from_dt
        end = from_dt.replace(hour=until_hour, minute=0, second=0)
        if current >= end:
            return 0.0
        while current < end:
            total += self.expected_kw(current) * CSV_INTERVAL_HOURS
            current += timedelta(minutes=CSV_INTERVAL_MINUTES)
        return total


@dataclass
class SystemCapacity:
    """Physical battery system parameters."""
    battery_count: int = DEFAULT_BATTERY_COUNT
    capacity_per_battery_kwh: float = DEFAULT_CAPACITY_KWH
    total_capacity_kwh: float = DEFAULT_BATTERY_COUNT * DEFAULT_CAPACITY_KWH
    backup_reserve_pct: float = DEFAULT_BACKUP_RESERVE_PCT
    usable_capacity_kwh: float = 0.0
    max_charge_kw: float = DEFAULT_MAX_CHARGE_KW
    max_discharge_kw: float = DEFAULT_MAX_DISCHARGE_KW
    soh_percent: float = 0.0          # State of Health from Modbus
    discovery_source: str = 'defaults' # 'modbus', 'config', or 'defaults'

    def __post_init__(self):
        self.total_capacity_kwh = self.battery_count * self.capacity_per_battery_kwh
        self.usable_capacity_kwh = self.total_capacity_kwh * (100.0 - self.backup_reserve_pct) / 100.0

    def kwh_at_soc(self, soc_pct: float) -> float:
        """Stored energy at given SOC percentage."""
        return self.total_capacity_kwh * soc_pct / 100.0

    def kwh_above_reserve(self, soc_pct: float) -> float:
        """Usable energy above backup reserve."""
        return max(0.0, self.kwh_at_soc(soc_pct) - self.kwh_at_soc(self.backup_reserve_pct))

    def soc_for_kwh(self, kwh: float) -> float:
        """SOC percentage for given stored kWh."""
        if self.total_capacity_kwh <= 0:
            return 0.0
        return min(100.0, max(0.0, kwh / self.total_capacity_kwh * 100.0))


@dataclass
class SystemProfile:
    """Complete learned system profile."""
    charge_curve: ChargeCurve = field(default_factory=ChargeCurve)
    consumption: ConsumptionProfile = field(default_factory=ConsumptionProfile)
    solar: SolarProfile = field(default_factory=SolarProfile)
    capacity: SystemCapacity = field(default_factory=SystemCapacity)
    last_rebuilt: str = ""
    data_start: str = ""
    data_end: str = ""
    total_rows_processed: int = 0
    version: str = "4.0.0"

    def time_to_charge_kwh(self, current_soc: float, target_soc: float) -> float:
        """Estimate hours to charge from current_soc to target_soc using grid.
        Uses learned charge curve for SOC-dependent rate estimation."""
        if current_soc >= target_soc:
            return 0.0

        total_hours = 0.0
        soc = current_soc
        step = float(CHARGE_CURVE_BUCKET_WIDTH)

        while soc < target_soc:
            next_soc = min(soc + step, target_soc)
            kwh_needed = self.capacity.total_capacity_kwh * (next_soc - soc) / 100.0
            rate = self.charge_curve.grid_charge_rate_kw(soc)
            if rate <= 0:
                rate = DEFAULT_MAX_CHARGE_KW
            total_hours += kwh_needed / rate
            soc = next_soc

        return total_hours


# ---------------------------------------------------------------------------
# CSV Parsing
# ---------------------------------------------------------------------------

def _parse_csv_row(row: list) -> Optional[dict]:
    """Parse a CSV row into a normalized dict. Handles all format eras.
    
    Returns dict with keys:
        timestamp, soc_percent, solar_kw, grid_kw, battery_kw, home_load_kw,
        grid_status, mode
    Or None if row can't be parsed.
    """
    if len(row) < 6:
        return None
    try:
        ts = datetime.strptime(row[0].strip(), '%Y-%m-%d %H:%M:%S')
        soc = float(row[1])
        solar = max(0.0, float(row[2]))  # Clip negative noise
        grid = float(row[3])
        battery = float(row[4])
        home_load = float(row[5])
        grid_status = row[6].strip() if len(row) > 6 else 'Unknown'

        # Mode detection across format eras
        mode = None
        if len(row) == 13:
            # Standard format: mode is col 12
            mode = row[12].strip()
        elif len(row) == 11:
            # Early format: no mode column
            mode = None
        elif len(row) >= 16:
            # Extended Modbus formats: mode is col 8
            candidate = row[8].strip() if len(row) > 8 else None
            if candidate in ('TOU', 'BACKUP', 'Self-Consumption', 'Emergency Backup'):
                mode = candidate

        return {
            'timestamp': ts,
            'soc_percent': soc,
            'solar_kw': solar,
            'grid_kw': grid,
            'battery_kw': battery,
            'home_load_kw': home_load,
            'grid_status': grid_status,
            'mode': mode,
        }
    except (ValueError, IndexError) as e:
        return None


def scan_csv(csv_path: str) -> list:
    """Read and parse all rows from continuous_monitoring.csv."""
    rows = []
    skipped = 0
    with open(csv_path, 'r', newline='') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for raw_row in reader:
            parsed = _parse_csv_row(raw_row)
            if parsed:
                rows.append(parsed)
            else:
                skipped += 1

    logger.info(f"Scanned {len(rows)} rows from {csv_path} ({skipped} skipped)")
    return rows


# ---------------------------------------------------------------------------
# Profile Builders
# ---------------------------------------------------------------------------

def build_charge_curve(rows: list) -> ChargeCurve:
    """Build battery charge curve from CSV history.
    
    Separates grid charging (high-power Backup mode) from solar charging.
    Grid charge curve is what matters for gap calculations — it tells us
    how fast we can charge from grid at each SOC level.
    """
    grid_buckets = defaultdict(list)
    solar_buckets = defaultdict(list)

    for row in rows:
        battery = row['battery_kw']
        if battery >= -MIN_CHARGE_RATE_KW:
            continue  # Not charging (battery_kw negative = charging in)

        charge_rate = abs(battery)
        soc = row['soc_percent']
        bucket = int(soc // CHARGE_CURVE_BUCKET_WIDTH) * CHARGE_CURVE_BUCKET_WIDTH
        grid = row['grid_kw']
        solar = row['solar_kw']

        # Grid charging: high grid import + high charge rate
        if grid > GRID_CHARGE_MIN_GRID_KW and charge_rate > GRID_CHARGE_MIN_RATE_KW:
            grid_buckets[bucket].append(charge_rate)
        elif solar > 0.3:
            solar_buckets[bucket].append(charge_rate)

    curve = ChargeCurve()
    for bucket, rates in grid_buckets.items():
        curve.grid[str(bucket)] = {
            'avg_kw': round(sum(rates) / len(rates), 2),
            'max_kw': round(max(rates), 2),
            'min_kw': round(min(rates), 2),
            'samples': len(rates),
        }
    for bucket, rates in solar_buckets.items():
        curve.solar[str(bucket)] = {
            'avg_kw': round(sum(rates) / len(rates), 2),
            'max_kw': round(max(rates), 2),
            'min_kw': round(min(rates), 2),
            'samples': len(rates),
        }

    total_grid = sum(v['samples'] for v in curve.grid.values())
    total_solar = sum(v['samples'] for v in curve.solar.values())
    logger.info(f"Charge curve built: {len(curve.grid)} grid buckets ({total_grid} samples), "
                f"{len(curve.solar)} solar buckets ({total_solar} samples)")
    return curve


def build_consumption_profile(rows: list) -> ConsumptionProfile:
    """Build hourly consumption averages from CSV history.
    
    Separates weekday/weekend patterns. Filters out Modbus-era rows
    where home_load reads as zero.
    """
    weekday_by_hour = defaultdict(list)
    weekend_by_hour = defaultdict(list)
    all_loads = []

    for row in rows:
        load = row['home_load_kw']
        if load < MIN_HOME_LOAD_KW:
            continue  # Skip zero/missing readings

        ts = row['timestamp']
        hour = ts.hour
        all_loads.append(load)

        if ts.weekday() < 5:
            weekday_by_hour[hour].append(load)
        else:
            weekend_by_hour[hour].append(load)

    profile = ConsumptionProfile()

    if all_loads:
        profile.overall_avg_kw = round(sum(all_loads) / len(all_loads), 3)
        profile.peak_kw = round(max(all_loads), 3)
        profile.sample_count = len(all_loads)

    for hour, vals in weekday_by_hour.items():
        profile.weekday[str(hour)] = round(sum(vals) / len(vals), 3)
    for hour, vals in weekend_by_hour.items():
        profile.weekend[str(hour)] = round(sum(vals) / len(vals), 3)

    logger.info(f"Consumption profile built: avg={profile.overall_avg_kw} kW, "
                f"peak={profile.peak_kw} kW, {profile.sample_count} samples")
    return profile


def build_solar_profile(rows: list) -> SolarProfile:
    """Build solar production profile from CSV history.
    
    Groups by month and hour. Also computes daily totals for monthly summaries.
    This represents the house array (battery-contributing) only.
    """
    by_month_hour = defaultdict(lambda: defaultdict(list))
    daily_totals = defaultdict(float)

    for row in rows:
        ts = row['timestamp']
        solar = row['solar_kw']
        month = ts.month
        hour = ts.hour

        if solar > MIN_SOLAR_KW:
            by_month_hour[month][hour].append(solar)

        # Accumulate daily estimate (kW * interval hours)
        daily_totals[ts.strftime('%Y-%m-%d')] += solar * CSV_INTERVAL_HOURS

    profile = SolarProfile()

    for month, hours in by_month_hour.items():
        profile.by_month_hour[str(month)] = {}
        for hour, vals in hours.items():
            profile.by_month_hour[str(month)][str(hour)] = round(sum(vals) / len(vals), 3)

    for month, hours in by_month_hour.items():
        month_dates = [d for d, kwh in daily_totals.items()
                       if datetime.strptime(d, '%Y-%m-%d').month == month and kwh > 0.5]
        month_kwh = [daily_totals[d] for d in month_dates]
        if month_kwh:
            profile.monthly_summary[str(month)] = {
                'avg_daily_kwh': round(sum(month_kwh) / len(month_kwh), 1),
                'clear_day_kwh': round(max(month_kwh), 1),
                'min_daily_kwh': round(min(month_kwh), 1),
                'days': len(month_kwh),
            }

    # Recent daily totals (last 14 days)
    sorted_dates = sorted(daily_totals.keys())
    profile.recent_daily_kwh = [
        {'date': d, 'kwh': round(daily_totals[d], 2)}
        for d in sorted_dates[-14:]
    ]

    logger.info(f"Solar profile built: {len(profile.by_month_hour)} months, "
                f"{len(profile.recent_daily_kwh)} recent days")
    return profile


def discover_capacity(rows: list, config: Optional[dict] = None) -> SystemCapacity:
    """Determine system capacity from Modbus, config, and/or historical data.
    
    Priority order:
      1. Modbus auto-discovery (if MODBUS_ENABLED and host available)
      2. Config overrides from .env (BATTERY_COUNT, BACKUP_RESERVE_PCT)
      3. Defaults + historical data analysis

    Modbus provides: battery count, capacity, max charge/discharge, SoH, reserve %.
    Config provides: user overrides (always honored if set).
    Historical data: validates max observed charge/discharge rates.
    """
    config = config or {}

    # --- Attempt Modbus auto-discovery ---
    modbus_info = None
    modbus_host = config.get('modbus_host') or os.environ.get('MODBUS_HOST')
    modbus_enabled = config.get('modbus_enabled', os.environ.get('MODBUS_ENABLED', 'false').lower() == 'true')

    if modbus_enabled and modbus_host:
        try:
            from modbus_discovery import discover_from_modbus
            modbus_port = int(config.get('modbus_port') or os.environ.get('MODBUS_PORT', '502'))
            modbus_info = discover_from_modbus(modbus_host, modbus_port)
            if modbus_info:
                logger.info(f"Modbus auto-discovery successful: {modbus_info.battery_count} batteries, "
                            f"{modbus_info.total_capacity_kwh} kWh, SoH={modbus_info.soh_percent:.1f}%")
        except ImportError:
            logger.debug("modbus_discovery module not available — using config/defaults")
        except Exception as e:
            logger.warning(f"Modbus discovery failed: {e} — using config/defaults")

    # --- Find max observed rates from historical data ---
    max_charge = 0.0
    max_discharge = 0.0
    for row in rows:
        battery = row['battery_kw']
        if battery < 0:
            max_charge = max(max_charge, abs(battery))
        elif battery > 0:
            max_discharge = max(max_discharge, battery)

    # --- Resolve values: Modbus → Config → Defaults ---
    # Config explicitly set by user always wins
    if 'battery_count' in config:
        battery_count = config['battery_count']
    elif modbus_info:
        battery_count = modbus_info.battery_count
    else:
        battery_count = DEFAULT_BATTERY_COUNT

    if 'capacity_per_battery_kwh' in config:
        capacity_per = config['capacity_per_battery_kwh']
    elif modbus_info:
        capacity_per = modbus_info.capacity_per_battery_kwh
    else:
        capacity_per = DEFAULT_CAPACITY_KWH

    if 'backup_reserve_pct' in config:
        backup_reserve = config['backup_reserve_pct']
    elif modbus_info:
        # Read live reserve from Modbus
        try:
            from modbus_discovery import read_live_from_modbus
            modbus_port = int(config.get('modbus_port') or os.environ.get('MODBUS_PORT', '502'))
            live = read_live_from_modbus(modbus_host, modbus_port)
            if live:
                backup_reserve = live.reserve_pct
                logger.info(f"Modbus reserve: {backup_reserve}% (mode={live.mode})")
            else:
                backup_reserve = DEFAULT_BACKUP_RESERVE_PCT
        except Exception:
            backup_reserve = DEFAULT_BACKUP_RESERVE_PCT
    else:
        backup_reserve = DEFAULT_BACKUP_RESERVE_PCT

    # Max charge/discharge: use Modbus ratings if available, validate against history
    if modbus_info:
        best_charge = modbus_info.max_charge_kw
        best_discharge = modbus_info.max_discharge_kw
    else:
        best_charge = DEFAULT_MAX_CHARGE_KW * battery_count
        best_discharge = DEFAULT_MAX_DISCHARGE_KW * battery_count

    # Use the higher of Modbus rating or observed historical max
    best_charge = max(best_charge, max_charge)
    best_discharge = max(best_discharge, max_discharge)

    cap = SystemCapacity(
        battery_count=battery_count,
        capacity_per_battery_kwh=capacity_per,
        backup_reserve_pct=backup_reserve,
        max_charge_kw=round(best_charge, 2),
        max_discharge_kw=round(best_discharge, 2),
    )

    # Store SoH if discovered
    if modbus_info and modbus_info.soh_percent > 0:
        cap.soh_percent = modbus_info.soh_percent

    # Set discovery source
    if modbus_info and modbus_info.discovery_source == 'modbus':
        cap.discovery_source = 'modbus'
    elif any(k in config for k in ('battery_count', 'capacity_per_battery_kwh', 'backup_reserve_pct')):
        cap.discovery_source = 'config'
    else:
        cap.discovery_source = 'defaults'

    source = modbus_info.discovery_source if modbus_info else 'config/defaults'
    logger.info(f"System capacity ({source}): {cap.battery_count}x {cap.capacity_per_battery_kwh} kWh = "
                f"{cap.total_capacity_kwh} kWh, usable={cap.usable_capacity_kwh:.1f} kWh, "
                f"max charge={cap.max_charge_kw} kW, max discharge={cap.max_discharge_kw} kW")
    return cap


# ---------------------------------------------------------------------------
# Profile Build & Save
# ---------------------------------------------------------------------------

def build_profile(csv_path: str, config: Optional[dict] = None) -> SystemProfile:
    """Build complete system profile from CSV history.
    
    This is the main entry point. Call on first run (scans all history)
    or periodically to rebuild with latest data.
    """
    logger.info(f"Building system profile from {csv_path}")
    rows = scan_csv(csv_path)

    if not rows:
        logger.warning("No data found in CSV — using defaults")
        return SystemProfile(
            capacity=SystemCapacity(),
            last_rebuilt=datetime.now().isoformat(),
        )

    profile = SystemProfile(
        charge_curve=build_charge_curve(rows),
        consumption=build_consumption_profile(rows),
        solar=build_solar_profile(rows),
        capacity=discover_capacity(rows, config),
        last_rebuilt=datetime.now().isoformat(),
        data_start=rows[0]['timestamp'].isoformat(),
        data_end=rows[-1]['timestamp'].isoformat(),
        total_rows_processed=len(rows),
    )

    return profile


def save_profile(profile: SystemProfile, output_path: str):
    """Save profile to JSON file."""
    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)

    # Convert dataclass to dict, handling nested dataclasses
    data = {
        'version': profile.version,
        'last_rebuilt': profile.last_rebuilt,
        'data_start': profile.data_start,
        'data_end': profile.data_end,
        'total_rows_processed': profile.total_rows_processed,
        'charge_curve': {
            'grid': profile.charge_curve.grid,
            'solar': profile.charge_curve.solar,
        },
        'consumption': {
            'weekday': profile.consumption.weekday,
            'weekend': profile.consumption.weekend,
            'overall_avg_kw': profile.consumption.overall_avg_kw,
            'peak_kw': profile.consumption.peak_kw,
            'sample_count': profile.consumption.sample_count,
        },
        'solar': {
            'by_month_hour': profile.solar.by_month_hour,
            'monthly_summary': profile.solar.monthly_summary,
            'recent_daily_kwh': profile.solar.recent_daily_kwh,
        },
        'capacity': {
            'battery_count': profile.capacity.battery_count,
            'capacity_per_battery_kwh': profile.capacity.capacity_per_battery_kwh,
            'total_capacity_kwh': profile.capacity.total_capacity_kwh,
            'backup_reserve_pct': profile.capacity.backup_reserve_pct,
            'usable_capacity_kwh': profile.capacity.usable_capacity_kwh,
            'max_charge_kw': profile.capacity.max_charge_kw,
            'max_discharge_kw': profile.capacity.max_discharge_kw,
            'soh_percent': profile.capacity.soh_percent,
            'discovery_source': profile.capacity.discovery_source,
        },
    }

    with open(output_path, 'w') as f:
        json.dump(data, f, indent=2)
    logger.info(f"Profile saved to {output_path}")


def load_profile(input_path: str) -> Optional[SystemProfile]:
    """Load profile from JSON file. Returns None if file doesn't exist."""
    if not os.path.exists(input_path):
        return None

    with open(input_path, 'r') as f:
        data = json.load(f)

    charge_curve = ChargeCurve(
        grid=data.get('charge_curve', {}).get('grid', {}),
        solar=data.get('charge_curve', {}).get('solar', {}),
    )

    consumption = ConsumptionProfile(
        weekday=data.get('consumption', {}).get('weekday', {}),
        weekend=data.get('consumption', {}).get('weekend', {}),
        overall_avg_kw=data.get('consumption', {}).get('overall_avg_kw', DEFAULT_CONSUMPTION_KW),
        peak_kw=data.get('consumption', {}).get('peak_kw', 0.0),
        sample_count=data.get('consumption', {}).get('sample_count', 0),
    )

    solar = SolarProfile(
        by_month_hour=data.get('solar', {}).get('by_month_hour', {}),
        monthly_summary=data.get('solar', {}).get('monthly_summary', {}),
        recent_daily_kwh=data.get('solar', {}).get('recent_daily_kwh', []),
    )

    cap_data = data.get('capacity', {})
    capacity = SystemCapacity(
        battery_count=cap_data.get('battery_count', DEFAULT_BATTERY_COUNT),
        capacity_per_battery_kwh=cap_data.get('capacity_per_battery_kwh', DEFAULT_CAPACITY_KWH),
        backup_reserve_pct=cap_data.get('backup_reserve_pct', DEFAULT_BACKUP_RESERVE_PCT),
        max_charge_kw=cap_data.get('max_charge_kw', DEFAULT_MAX_CHARGE_KW),
        max_discharge_kw=cap_data.get('max_discharge_kw', DEFAULT_MAX_DISCHARGE_KW),
        soh_percent=cap_data.get('soh_percent', 0.0),
        discovery_source=cap_data.get('discovery_source', 'loaded'),
    )

    profile = SystemProfile(
        charge_curve=charge_curve,
        consumption=consumption,
        solar=solar,
        capacity=capacity,
        last_rebuilt=data.get('last_rebuilt', ''),
        data_start=data.get('data_start', ''),
        data_end=data.get('data_end', ''),
        total_rows_processed=data.get('total_rows_processed', 0),
        version=data.get('version', '4.0.0'),
    )

    logger.info(f"Profile loaded from {input_path} (rebuilt {profile.last_rebuilt})")
    return profile


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

    csv_path = sys.argv[1] if len(sys.argv) > 1 else 'data/continuous_monitoring.csv'
    output_path = sys.argv[2] if len(sys.argv) > 2 else 'data/system_profile.json'

    # Optional config overrides via environment
    config = {}
    if os.environ.get('BATTERY_COUNT'):
        config['battery_count'] = int(os.environ['BATTERY_COUNT'])
    if os.environ.get('BATTERY_CAPACITY_KWH'):
        config['capacity_per_battery_kwh'] = float(os.environ['BATTERY_CAPACITY_KWH'])
    if os.environ.get('BACKUP_RESERVE_PCT'):
        config['backup_reserve_pct'] = float(os.environ['BACKUP_RESERVE_PCT'])

    profile = build_profile(csv_path, config)
    save_profile(profile, output_path)

    # Print summary
    print("\n" + "=" * 60)
    print("SYSTEM PROFILE SUMMARY")
    print("=" * 60)
    print(f"Data range: {profile.data_start} to {profile.data_end}")
    print(f"Rows processed: {profile.total_rows_processed}")

    print(f"\n--- Battery Capacity ---")
    c = profile.capacity
    print(f"  {c.battery_count}x {c.capacity_per_battery_kwh} kWh = {c.total_capacity_kwh} kWh total")
    print(f"  Usable: {c.usable_capacity_kwh:.1f} kWh (reserve={c.backup_reserve_pct}%)")
    print(f"  Max charge: {c.max_charge_kw} kW | Max discharge: {c.max_discharge_kw} kW")
    print(f"  Discovery: {c.discovery_source}")
    if c.soh_percent > 0:
        print(f"  SoH: {c.soh_percent:.1f}%")

    print(f"\n--- Grid Charge Curve ---")
    print(f"  {'SOC':>6}  {'Rate':>8}  {'Max':>8}  {'Samples':>8}")
    for bucket in sorted(profile.charge_curve.grid.keys(), key=lambda x: int(x)):
        d = profile.charge_curve.grid[bucket]
        b = int(bucket)
        print(f"  {b:>3}-{b+5:<3}% {d['avg_kw']:>7.1f}  {d['max_kw']:>7.1f}  {d['samples']:>8}")

    print(f"\n--- Consumption Profile ---")
    print(f"  Overall avg: {profile.consumption.overall_avg_kw:.2f} kW")
    print(f"  Peak observed: {profile.consumption.peak_kw:.2f} kW")
    print(f"  Samples: {profile.consumption.sample_count}")

    # Show peak hours consumption
    for label, data in [("Weekday", profile.consumption.weekday),
                         ("Weekend", profile.consumption.weekend)]:
        peak_hours = {h: v for h, v in data.items() if 17 <= int(h) <= 19}
        if peak_hours:
            avg_peak = sum(peak_hours.values()) / len(peak_hours)
            print(f"  {label} peak hours (5-8 PM): avg {avg_peak:.2f} kW")

    print(f"\n--- Solar Profile ---")
    for month, summary in sorted(profile.solar.monthly_summary.items(), key=lambda x: int(x[0])):
        print(f"  Month {month}: avg {summary['avg_daily_kwh']} kWh/day, "
              f"clear day {summary['clear_day_kwh']} kWh ({summary['days']} days)")

    # Charge time estimate example
    t = profile.time_to_charge_kwh(30, 95)
    print(f"\n--- Charge Time Estimates ---")
    print(f"  30% → 95% from grid: {t:.1f} hours ({t*60:.0f} minutes)")
    t2 = profile.time_to_charge_kwh(50, 95)
    print(f"  50% → 95% from grid: {t2:.1f} hours ({t2*60:.0f} minutes)")
    t3 = profile.time_to_charge_kwh(80, 95)
    print(f"  80% → 95% from grid: {t3:.1f} hours ({t3*60:.0f} minutes)")

    print(f"\nProfile saved to {output_path}")
