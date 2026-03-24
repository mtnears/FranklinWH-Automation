#!/usr/bin/env python3
"""
Calculate daily battery automation savings using mode-aware tracking.

Reads system_readings from SQLite to track grid vs solar charging
during dynamic mode switching, then calculates savings based on
TOU rate structure.

Usage:
    python calculate_daily_savings.py --date 2026-01-15
    python calculate_daily_savings.py --date-range 2026-01-15 2026-01-20
    python calculate_daily_savings.py --yesterday
    python calculate_daily_savings.py --all

For Docker/automated use:
    python calculate_daily_savings.py --yesterday --quiet
"""

import argparse
import json
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
import sys
import os

sys.path.insert(0, str(Path(__file__).parent))

try:
    from db import store as db_store, query as db_query, init_db
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False

try:
    from config import config
    DATA_DIR = config.DATA_DIR
except ImportError:
    DATA_DIR = Path(os.getenv('DATA_DIR', '/app/data'))

# Constants
BATTERY_CAPACITY_KWH = 30
PEAK_START_HOUR = 17  # 5 PM
PEAK_END_HOUR = 20    # 8 PM
OFF_GRID_START_HOUR = 16.5  # 4:30 PM - when we want to be off grid
OFF_GRID_END_HOUR = 23.5  # 11:30 PM — captures most solar discharge overnight

# Rate structure (default, will be loaded from config if available)
DEFAULT_RATES = {
    'care': {
        'peak_rate': 0.39,
        'off_peak_rate': 0.27
    },
    'pre_care': {
        'peak_rate': 0.60,
        'off_peak_rate': 0.41
    }
}


def load_system_config(config_path=None):
    """Load system configuration including rates and dates."""
    if config_path is None:
        config_path = DATA_DIR / 'system_milestones.json'

    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def get_rates_for_date(date, config):
    """Determine which rate structure applies for a given date."""
    if config and 'critical_dates' in config:
        care_start_str = config['critical_dates'].get('care_activation')
        if care_start_str:
            care_start = datetime.strptime(care_start_str, '%Y-%m-%d').date()
            if date >= care_start:
                if 'rate_history' in config and 'care_rates' in config['rate_history']:
                    return config['rate_history']['care_rates'], 'care'
                return DEFAULT_RATES['care'], 'care'
    return DEFAULT_RATES['pre_care'], 'pre_care'


def load_monitoring_data_db(date_str=None, start_date=None, end_date=None):
    """Load monitoring data from SQLite system_readings table.

    Supports single date, date range, or all available data.
    Returns a pandas DataFrame with columns matching the calculation functions.
    """
    if date_str:
        rows = db_query(
            "SELECT timestamp, soc_pct, solar_kw, grid_kw, battery_kw, home_load_kw, "
            "kwh_solar, kwh_grid_import, kwh_battery_charge, kwh_battery_discharge, mode "
            "FROM system_readings WHERE date(timestamp) = ? ORDER BY timestamp",
            (date_str,)
        )
    elif start_date and end_date:
        rows = db_query(
            "SELECT timestamp, soc_pct, solar_kw, grid_kw, battery_kw, home_load_kw, "
            "kwh_solar, kwh_grid_import, kwh_battery_charge, kwh_battery_discharge, mode "
            "FROM system_readings WHERE date(timestamp) >= ? AND date(timestamp) <= ? ORDER BY timestamp",
            (start_date, end_date)
        )
    else:
        rows = db_query(
            "SELECT timestamp, soc_pct, solar_kw, grid_kw, battery_kw, home_load_kw, "
            "kwh_solar, kwh_grid_import, kwh_battery_charge, kwh_battery_discharge, mode "
            "FROM system_readings ORDER BY timestamp"
        )

    if not rows:
        return None

    df = pd.DataFrame(rows)

    df.rename(columns={
        'soc_pct': 'soc_percent',
        'kwh_solar': 'solar_total',
        'kwh_grid_import': 'grid_import_total',
        'kwh_battery_charge': 'battery_charge_total',
        'kwh_battery_discharge': 'battery_discharge_total',
    }, inplace=True)

    df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
    df = df.dropna(subset=['timestamp'])

    for col in ['soc_percent', 'solar_kw', 'grid_kw', 'battery_kw', 'home_load_kw',
                 'battery_charge_total', 'battery_discharge_total', 'grid_import_total', 'solar_total']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    df['date'] = df['timestamp'].dt.date
    df['hour'] = df['timestamp'].dt.hour + df['timestamp'].dt.minute / 60
    return df


def get_available_dates():
    """Get all dates that have system_readings data, excluding today."""
    rows = db_query(
        "SELECT DISTINCT date(timestamp) as d FROM system_readings ORDER BY d"
    )
    today = datetime.now().strftime('%Y-%m-%d')
    return [r['d'] for r in rows if r['d'] and r['d'] != today]


def calculate_charging_by_mode(df_day):
    """
    Calculate battery charging using the mode column for accurate tracking.

    Mode normalization handles both Modbus and cloud API conventions:
      time_of_use / TOU -> TOU (solar or grid charging)
      emergency_backup / TOU-B / BACKUP -> BACKUP (grid charging)
      self_consumption / Self-Consumption -> SELF (solar charging if producing)

    Returns: (total_charged_kwh, solar_kwh, grid_kwh, solar_ratio)
    """
    if len(df_day) < 2:
        return 0, 0, 0, 0

    if 'mode' not in df_day.columns:
        return calculate_charging_proportional(df_day)

    df_day = df_day.sort_values('timestamp').copy()

    MODE_MAP = {
        'time_of_use': 'TOU',
        'self_consumption': 'SELF',
        'Self-Consumption': 'SELF',
        'emergency_backup': 'BACKUP',
        'TOU-B': 'BACKUP',
        'TOU': 'TOU',
        'BACKUP': 'BACKUP',
    }
    df_day['mode'] = df_day['mode'].map(MODE_MAP).fillna('UNKNOWN')

    df_day['time_delta_hours'] = df_day['timestamp'].diff().dt.total_seconds() / 3600
    df_day['time_delta_hours'] = df_day['time_delta_hours'].fillna(0.25)
    df_day['time_delta_hours'] = df_day['time_delta_hours'].clip(upper=1.0)

    df_day['is_charging'] = df_day['battery_kw'] < -0.05
    df_day['charge_kw'] = df_day['battery_kw'].abs().where(df_day['is_charging'], 0)
    df_day['charge_kwh'] = df_day['charge_kw'] * df_day['time_delta_hours']

    grid_charging_mask = (df_day['mode'] == 'BACKUP') & df_day['is_charging']
    grid_charged_kwh = df_day.loc[grid_charging_mask, 'charge_kwh'].sum()

    solar_charging_mask = (df_day['mode'].isin(['TOU', 'SELF'])) & df_day['is_charging'] & (df_day['solar_kw'] > 0.05)
    solar_charged_kwh = df_day.loc[solar_charging_mask, 'charge_kwh'].sum()

    tou_no_solar_mask = (df_day['mode'].isin(['TOU', 'SELF'])) & df_day['is_charging'] & (df_day['solar_kw'] <= 0.05)
    grid_charged_kwh += df_day.loc[tou_no_solar_mask, 'charge_kwh'].sum()

    total_charged_kwh = solar_charged_kwh + grid_charged_kwh
    solar_ratio = solar_charged_kwh / total_charged_kwh if total_charged_kwh > 0 else 0

    return total_charged_kwh, solar_charged_kwh, grid_charged_kwh, solar_ratio


def calculate_charging_proportional(df_day):
    """
    Fallback: Calculate charging using proportional allocation method.
    Used when mode column is not available.
    """
    if len(df_day) < 2:
        return 0, 0, 0, 0

    morning_data = df_day[df_day['hour'] < 12]
    if len(morning_data) == 0:
        return 0, 0, 0, 0

    morning_soc = morning_data['soc_percent'].min()

    pre_peak_data = df_day[(df_day['hour'] >= OFF_GRID_START_HOUR) &
                           (df_day['hour'] < OFF_GRID_START_HOUR + 0.5)]
    if len(pre_peak_data) == 0:
        pre_peak_data = df_day[df_day['hour'] < OFF_GRID_START_HOUR]
        if len(pre_peak_data) == 0:
            return 0, 0, 0, 0

    pre_peak_soc = pre_peak_data['soc_percent'].iloc[-1]
    total_charged_kwh = max(0, (pre_peak_soc - morning_soc) / 100 * BATTERY_CAPACITY_KWH)

    if total_charged_kwh <= 0:
        return 0, 0, 0, 0

    if 'solar_total' in df_day.columns and 'grid_import_total' in df_day.columns:
        solar_generated = df_day['solar_total'].iloc[-1] - df_day['solar_total'].iloc[0]
        grid_imported = df_day['grid_import_total'].iloc[-1] - df_day['grid_import_total'].iloc[0]

        total_available = solar_generated + grid_imported
        if total_available > 0:
            solar_fraction = solar_generated / total_available
            solar_charged = total_charged_kwh * solar_fraction
            grid_charged = total_charged_kwh - solar_charged
            return total_charged_kwh, solar_charged, grid_charged, solar_fraction

    return total_charged_kwh, total_charged_kwh * 0.5, total_charged_kwh * 0.5, 0.5


def calculate_discharge_phase(df_day):
    """
    Calculate battery discharge during off-grid period (4:30 PM - 10 PM).

    Returns: (peak_discharge_kwh, post_peak_discharge_kwh, total_discharge_kwh)
    """
    pre_peak_data = df_day[(df_day['hour'] >= OFF_GRID_START_HOUR) &
                           (df_day['hour'] < OFF_GRID_START_HOUR + 0.5)]

    peak_end_data = df_day[(df_day['hour'] >= PEAK_END_HOUR) &
                           (df_day['hour'] < PEAK_END_HOUR + 0.5)]

    end_data = df_day[(df_day['hour'] >= OFF_GRID_END_HOUR - 0.5) &
                      (df_day['hour'] <= OFF_GRID_END_HOUR + 0.5)]

    if len(pre_peak_data) > 0:
        pre_peak_soc = pre_peak_data['soc_percent'].iloc[0]
    else:
        afternoon_data = df_day[df_day['hour'] < PEAK_START_HOUR]
        if len(afternoon_data) == 0:
            return 0, 0, 0
        pre_peak_soc = afternoon_data['soc_percent'].iloc[-1]

    if len(peak_end_data) > 0:
        peak_end_soc = peak_end_data['soc_percent'].iloc[0]
    else:
        peak_end_soc = None

    if len(end_data) > 0:
        end_soc = end_data['soc_percent'].iloc[-1]
    else:
        end_soc = df_day['soc_percent'].iloc[-1]

    total_discharge_kwh = max(0, (pre_peak_soc - end_soc) / 100 * BATTERY_CAPACITY_KWH)

    if total_discharge_kwh <= 0:
        return 0, 0, 0

    if peak_end_soc is not None:
        peak_discharge_kwh = max(0, (pre_peak_soc - peak_end_soc) / 100 * BATTERY_CAPACITY_KWH)
        post_peak_discharge_kwh = max(0, (peak_end_soc - end_soc) / 100 * BATTERY_CAPACITY_KWH)
    else:
        peak_hours = PEAK_END_HOUR - PEAK_START_HOUR
        post_peak_hours = OFF_GRID_END_HOUR - PEAK_END_HOUR
        total_hours = peak_hours + post_peak_hours

        peak_discharge_kwh = total_discharge_kwh * (peak_hours / total_hours)
        post_peak_discharge_kwh = total_discharge_kwh * (post_peak_hours / total_hours)

    return peak_discharge_kwh, post_peak_discharge_kwh, total_discharge_kwh


def calculate_daily_savings(date, df_monitoring, sys_config, quiet=False):
    """
    Calculate savings for a specific date using mode-aware tracking.

    Returns dict with savings breakdown or None if no data.
    """
    df_day = df_monitoring[df_monitoring['date'] == date].copy()

    if len(df_day) < 10:
        if not quiet:
            print(f"  {date}: Insufficient data ({len(df_day)} rows)")
        return None

    rates, rate_type = get_rates_for_date(date, sys_config)
    peak_rate = rates.get('peak_rate', DEFAULT_RATES['care']['peak_rate'])
    off_peak_rate = rates.get('off_peak_rate', DEFAULT_RATES['care']['off_peak_rate'])

    total_charged, solar_charged, grid_charged, solar_ratio = calculate_charging_by_mode(df_day)

    peak_discharge, post_peak_discharge, total_discharge = calculate_discharge_phase(df_day)

    if total_discharge <= 0:
        return {
            'date': str(date),
            'solar_ratio': round(solar_ratio, 3),
            'total_charged_kwh': round(total_charged, 2),
            'solar_charged_kwh': round(solar_charged, 2),
            'grid_charged_kwh': round(grid_charged, 2),
            'peak_discharge_kwh': 0,
            'post_peak_discharge_kwh': 0,
            'peak_savings': 0,
            'post_peak_savings': 0,
            'total_savings': 0,
            'rate_type': rate_type,
            'peak_rate': peak_rate,
            'off_peak_rate': off_peak_rate,
            'solar_discharge_kwh': 0,
            'solar_discharge_savings': 0,
        }

    peak_solar_kwh = peak_discharge * solar_ratio
    peak_solar_savings = peak_solar_kwh * peak_rate

    peak_grid_kwh = peak_discharge * (1 - solar_ratio)
    peak_grid_savings = peak_grid_kwh * (peak_rate - off_peak_rate)

    peak_savings = peak_solar_savings + peak_grid_savings

    post_peak_solar_kwh = post_peak_discharge * solar_ratio
    post_peak_solar_savings = post_peak_solar_kwh * off_peak_rate

    post_peak_savings = post_peak_solar_savings

    # Solar discharge: the portion of post-peak discharge attributable to
    # free solar energy (solar_ratio of the post-peak discharge).
    # This tracks how much of the "burn free solar" feature is working.
    solar_discharge_kwh = post_peak_solar_kwh
    solar_discharge_savings = post_peak_solar_savings

    total_savings = peak_savings + post_peak_savings

    return {
        'date': str(date),
        'solar_ratio': round(solar_ratio, 3),
        'total_charged_kwh': round(total_charged, 2),
        'solar_charged_kwh': round(solar_charged, 2),
        'grid_charged_kwh': round(grid_charged, 2),
        'peak_discharge_kwh': round(peak_discharge, 2),
        'post_peak_discharge_kwh': round(post_peak_discharge, 2),
        'peak_savings': round(peak_savings, 2),
        'post_peak_savings': round(post_peak_savings, 2),
        'total_savings': round(total_savings, 2),
        'rate_type': rate_type,
        'peak_rate': peak_rate,
        'off_peak_rate': off_peak_rate,
        'solar_discharge_kwh': round(solar_discharge_kwh, 2),
        'solar_discharge_savings': round(solar_discharge_savings, 2),
    }


def main():
    parser = argparse.ArgumentParser(description='Calculate daily battery automation savings')
    parser.add_argument('--date', type=str, help='Single date to calculate (YYYY-MM-DD)')
    parser.add_argument('--date-range', nargs=2, metavar=('START', 'END'),
                       help='Date range (YYYY-MM-DD YYYY-MM-DD)')
    parser.add_argument('--yesterday', action='store_true',
                       help='Calculate yesterday\'s savings (for daily automation)')
    parser.add_argument('--all', action='store_true', help='Calculate all available dates')
    parser.add_argument('--config', type=str, help='System configuration file path')
    parser.add_argument('--quiet', '-q', action='store_true', help='Minimal output (for automation)')

    args = parser.parse_args()

    if not DB_AVAILABLE:
        print("Error: Database not available")
        return 1

    init_db()

    sys_config = load_system_config(args.config)

    if args.date:
        dates_str = [args.date]
        df_monitoring = load_monitoring_data_db(date_str=args.date)
    elif args.date_range:
        start_str, end_str = args.date_range
        start = datetime.strptime(start_str, '%Y-%m-%d').date()
        end = datetime.strptime(end_str, '%Y-%m-%d').date()
        dates_str = [(start + timedelta(days=x)).strftime('%Y-%m-%d') for x in range((end - start).days + 1)]
        df_monitoring = load_monitoring_data_db(start_date=start_str, end_date=end_str)
    elif args.yesterday:
        yest = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        dates_str = [yest]
        df_monitoring = load_monitoring_data_db(date_str=yest)
    elif args.all:
        dates_str = get_available_dates()
        if not dates_str:
            print("No data available in database")
            return 1
        df_monitoring = load_monitoring_data_db(start_date=dates_str[0], end_date=dates_str[-1])
    else:
        print("Error: Must specify --date, --date-range, --yesterday, or --all")
        return 1

    if df_monitoring is None or len(df_monitoring) == 0:
        if not args.quiet:
            print("Error: No monitoring data found")
        return 1

    dates = [datetime.strptime(d, '%Y-%m-%d').date() if isinstance(d, str) else d for d in dates_str]

    if not args.quiet:
        print(f"Loading monitoring data...")
        print(f"\nCalculating savings for {len(dates)} day(s)...")

    results = []
    for date in dates:
        result = calculate_daily_savings(date, df_monitoring, sys_config, args.quiet)
        if result:
            results.append(result)
            if not args.quiet:
                print(f"  {result['date']}: ${result['total_savings']:.2f} "
                      f"(solar: {result['solar_ratio']*100:.0f}%, "
                      f"charged: {result['total_charged_kwh']:.1f}kWh, "
                      f"discharged: {result['peak_discharge_kwh'] + result['post_peak_discharge_kwh']:.1f}kWh)")

    if not results:
        if not args.quiet:
            print("No results calculated")
        return 1

    db_count = 0
    for result in results:
        try:
            db_store.daily_savings(
                date=result['date'],
                solar_ratio=result.get('solar_ratio'),
                total_charged_kwh=result.get('total_charged_kwh'),
                solar_charged_kwh=result.get('solar_charged_kwh'),
                grid_charged_kwh=result.get('grid_charged_kwh'),
                peak_discharge_kwh=result.get('peak_discharge_kwh'),
                post_peak_discharge_kwh=result.get('post_peak_discharge_kwh'),
                peak_savings=result.get('peak_savings'),
                post_peak_savings=result.get('post_peak_savings'),
                total_savings=result.get('total_savings'),
                rate_type=result.get('rate_type'),
                peak_rate=result.get('peak_rate'),
                off_peak_rate=result.get('off_peak_rate'),
                solar_discharge_kwh=result.get('solar_discharge_kwh'),
                solar_discharge_savings=result.get('solar_discharge_savings'),
            )
            db_count += 1
        except Exception as e:
            print(f"DB write warning for {result['date']}: {e}")

    if not args.quiet:
        df_results = pd.DataFrame(results)
        print(f"\nSQLite: {db_count} rows written to daily_savings")
        print(f"\n{'='*50}")
        print(f"SUMMARY")
        print(f"{'='*50}")
        print(f"Days calculated: {len(results)}")
        print(f"Total savings: ${df_results['total_savings'].sum():.2f}")
        print(f"Average daily: ${df_results['total_savings'].mean():.2f}")
        print(f"Average solar ratio: {df_results['solar_ratio'].mean()*100:.1f}%")
        if df_results['total_savings'].sum() > 0:
            peak_pct = df_results['peak_savings'].sum() / df_results['total_savings'].sum() * 100
            print(f"Peak vs post-peak: {peak_pct:.0f}% / {100-peak_pct:.0f}%")
    else:
        total = sum(r['total_savings'] for r in results)
        print(f"${total:.2f}")

    return 0


if __name__ == '__main__':
    sys.exit(main())
