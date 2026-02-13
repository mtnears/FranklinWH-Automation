#!/usr/bin/env python3
"""
Calculate daily battery automation savings using mode-aware tracking.

Enhanced version that uses the 'mode' column from continuous_monitoring.csv
to accurately track grid vs solar charging during dynamic switching.

This script reads the continuous_monitoring.csv log and calculates:
1. Solar vs grid contribution to battery charging based on actual mode
2. Battery discharge during peak and post-peak periods
3. Actual savings based on rate structure

Usage:
    python calculate_daily_savings.py --date 2026-01-15
    python calculate_daily_savings.py --date-range 2026-01-15 2026-01-20
    python calculate_daily_savings.py --yesterday  # Calculate yesterday's savings
    python calculate_daily_savings.py --all  # Calculate all available dates

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

# Try to import config for Docker compatibility
try:
    from config import config
    BASE_DIR = config.BASE_DIR
    DATA_DIR = config.DATA_DIR
    LOG_DIR = config.LOG_DIR
except ImportError:
    # Fallback for standalone use
    BASE_DIR = Path(os.getenv('BASE_DIR', '/volume1/docker/franklin'))
    DATA_DIR = BASE_DIR / 'data'
    LOG_DIR = BASE_DIR / 'logs'

# Constants
BATTERY_CAPACITY_KWH = 30
PEAK_START_HOUR = 17  # 5 PM
PEAK_END_HOUR = 20    # 8 PM
OFF_GRID_START_HOUR = 16.5  # 4:30 PM - when we want to be off grid
OFF_GRID_END_HOUR = 22  # 10 PM

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


def load_monitoring_data(log_path=None):
    """Load and parse the continuous monitoring CSV.
    
    Handles format evolution where newer rows may have extra trailing columns
    (e.g. per-battery SOC, temperature) beyond the original 13-column header.
    Strategy: read header to get column names, then read all rows allowing
    extra fields, keeping only the columns defined in the header.
    """
    if log_path is None:
        log_path = LOG_DIR / 'continuous_monitoring.csv'
    
    try:
        # Read header to get expected column names
        with open(log_path, 'r') as f:
            header_line = f.readline().strip()
        header_cols = [c.strip() for c in header_line.split(',')]
        n_cols = len(header_cols)
        
        # Read all data lines, splitting manually to handle variable column counts
        rows = []
        with open(log_path, 'r') as f:
            next(f)  # skip header
            for line in f:
                fields = line.strip().split(',')
                # Take only the first n_cols fields (ignore extra trailing columns)
                rows.append(fields[:n_cols])
        
        df = pd.DataFrame(rows, columns=header_cols)
        
        df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
        df = df.dropna(subset=['timestamp'])
        
        # Convert numeric columns
        for col in ['soc_percent', 'solar_kw', 'grid_kw', 'battery_kw', 'home_load_kw',
                     'battery_charge_total', 'battery_discharge_total', 'grid_import_total', 'solar_total']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        df['date'] = df['timestamp'].dt.date
        df['hour'] = df['timestamp'].dt.hour + df['timestamp'].dt.minute / 60
        return df
    except Exception as e:
        print(f"Error loading monitoring data: {e}")
        return None


def calculate_charging_by_mode(df_day):
    """
    Calculate battery charging using the mode column for accurate tracking.
    
    When mode == 'BACKUP': Grid is charging the battery
    When mode == 'TOU': Solar is charging (or battery is idle/discharging)
    
    Returns: (total_charged_kwh, solar_kwh, grid_kwh, solar_ratio)
    """
    if len(df_day) < 2:
        return 0, 0, 0, 0
    
    # Check if mode column exists
    if 'mode' not in df_day.columns:
        # Fallback to old proportional method
        return calculate_charging_proportional(df_day)
    
    # Calculate time intervals between readings (in hours)
    df_day = df_day.sort_values('timestamp').copy()
    df_day['time_delta_hours'] = df_day['timestamp'].diff().dt.total_seconds() / 3600
    df_day['time_delta_hours'] = df_day['time_delta_hours'].fillna(0.25)  # Assume 15 min for first
    
    # Cap time deltas at 1 hour to handle gaps
    df_day['time_delta_hours'] = df_day['time_delta_hours'].clip(upper=1.0)
    
    # Battery charging occurs when battery_kw is negative (energy flowing INTO battery)
    # We want to track this by mode
    df_day['is_charging'] = df_day['battery_kw'] < -0.05  # Small threshold to ignore noise
    df_day['charge_kw'] = df_day['battery_kw'].abs().where(df_day['is_charging'], 0)
    
    # Calculate energy charged in each interval
    df_day['charge_kwh'] = df_day['charge_kw'] * df_day['time_delta_hours']
    
    # Split by mode
    # BACKUP mode = grid charging
    grid_charging_mask = (df_day['mode'] == 'BACKUP') & df_day['is_charging']
    grid_charged_kwh = df_day.loc[grid_charging_mask, 'charge_kwh'].sum()
    
    # TOU mode with solar = solar charging
    # Also check that solar is actually producing
    solar_charging_mask = (df_day['mode'] == 'TOU') & df_day['is_charging'] & (df_day['solar_kw'] > 0.05)
    solar_charged_kwh = df_day.loc[solar_charging_mask, 'charge_kwh'].sum()
    
    # TOU mode without solar (rare, but possible) - count as grid
    tou_no_solar_mask = (df_day['mode'] == 'TOU') & df_day['is_charging'] & (df_day['solar_kw'] <= 0.05)
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
    
    # Find morning low SOC
    morning_data = df_day[df_day['hour'] < 12]
    if len(morning_data) == 0:
        return 0, 0, 0, 0
    
    morning_soc = morning_data['soc_percent'].min()
    
    # Find pre-peak SOC
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
    
    # Use totals for proportional allocation
    if 'solar_total' in df_day.columns and 'grid_import_total' in df_day.columns:
        solar_generated = df_day['solar_total'].iloc[-1] - df_day['solar_total'].iloc[0]
        grid_imported = df_day['grid_import_total'].iloc[-1] - df_day['grid_import_total'].iloc[0]
        
        total_available = solar_generated + grid_imported
        if total_available > 0:
            solar_fraction = solar_generated / total_available
            solar_charged = total_charged_kwh * solar_fraction
            grid_charged = total_charged_kwh - solar_charged
            return total_charged_kwh, solar_charged, grid_charged, solar_fraction
    
    # Very rough fallback
    return total_charged_kwh, total_charged_kwh * 0.5, total_charged_kwh * 0.5, 0.5


def calculate_discharge_phase(df_day):
    """
    Calculate battery discharge during off-grid period (4:30 PM - 10 PM).
    
    Returns: (peak_discharge_kwh, post_peak_discharge_kwh, total_discharge_kwh)
    """
    # Get SOC at key times
    pre_peak_data = df_day[(df_day['hour'] >= OFF_GRID_START_HOUR) & 
                           (df_day['hour'] < OFF_GRID_START_HOUR + 0.5)]
    
    peak_end_data = df_day[(df_day['hour'] >= PEAK_END_HOUR) & 
                           (df_day['hour'] < PEAK_END_HOUR + 0.5)]
    
    end_data = df_day[(df_day['hour'] >= OFF_GRID_END_HOUR - 0.5) & 
                      (df_day['hour'] <= OFF_GRID_END_HOUR + 0.5)]
    
    # Get SOC values with fallbacks
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
        # Estimate based on available data
        peak_end_soc = None
    
    if len(end_data) > 0:
        end_soc = end_data['soc_percent'].iloc[-1]
    else:
        # Use last reading of day
        end_soc = df_day['soc_percent'].iloc[-1]
    
    # Calculate discharges
    total_discharge_kwh = max(0, (pre_peak_soc - end_soc) / 100 * BATTERY_CAPACITY_KWH)
    
    if total_discharge_kwh <= 0:
        return 0, 0, 0
    
    # If we have peak end SOC, use actual split
    if peak_end_soc is not None:
        peak_discharge_kwh = max(0, (pre_peak_soc - peak_end_soc) / 100 * BATTERY_CAPACITY_KWH)
        post_peak_discharge_kwh = max(0, (peak_end_soc - end_soc) / 100 * BATTERY_CAPACITY_KWH)
    else:
        # Proportional split based on time
        peak_hours = PEAK_END_HOUR - PEAK_START_HOUR  # 3 hours
        post_peak_hours = OFF_GRID_END_HOUR - PEAK_END_HOUR  # 2 hours
        total_hours = peak_hours + post_peak_hours
        
        peak_discharge_kwh = total_discharge_kwh * (peak_hours / total_hours)
        post_peak_discharge_kwh = total_discharge_kwh * (post_peak_hours / total_hours)
    
    return peak_discharge_kwh, post_peak_discharge_kwh, total_discharge_kwh


def calculate_daily_savings(date, df_monitoring, sys_config, quiet=False):
    """
    Calculate savings for a specific date using mode-aware tracking.
    
    Returns dict with savings breakdown or None if no data.
    """
    # Filter data for this date
    df_day = df_monitoring[df_monitoring['date'] == date].copy()
    
    if len(df_day) < 10:  # Need reasonable amount of data
        if not quiet:
            print(f"  {date}: Insufficient data ({len(df_day)} rows)")
        return None
    
    # Get applicable rates
    rates, rate_type = get_rates_for_date(date, sys_config)
    peak_rate = rates.get('peak_rate', DEFAULT_RATES['care']['peak_rate'])
    off_peak_rate = rates.get('off_peak_rate', DEFAULT_RATES['care']['off_peak_rate'])
    
    # Calculate charging phase using mode-aware method
    total_charged, solar_charged, grid_charged, solar_ratio = calculate_charging_by_mode(df_day)
    
    # Calculate discharge phase
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
            'off_peak_rate': off_peak_rate
        }
    
    # Calculate savings using the solar ratio from charging
    # This ratio represents what portion of stored energy came from solar vs grid
    
    # === Peak period savings (5-8 PM) ===
    # Solar portion: Full peak rate savings (free energy displacing peak purchases)
    peak_solar_kwh = peak_discharge * solar_ratio
    peak_solar_savings = peak_solar_kwh * peak_rate
    
    # Grid portion: Arbitrage spread only (bought at off-peak, used at peak)
    peak_grid_kwh = peak_discharge * (1 - solar_ratio)
    peak_grid_savings = peak_grid_kwh * (peak_rate - off_peak_rate)
    
    peak_savings = peak_solar_savings + peak_grid_savings
    
    # === Post-peak savings (8-10 PM, off-peak rate) ===
    # Solar portion: Full off-peak rate savings
    post_peak_solar_kwh = post_peak_discharge * solar_ratio
    post_peak_solar_savings = post_peak_solar_kwh * off_peak_rate
    
    # Grid portion: Break-even (bought at off-peak, used at off-peak)
    # Small loss due to round-trip efficiency, but we ignore that for simplicity
    post_peak_grid_savings = 0
    
    post_peak_savings = post_peak_solar_savings + post_peak_grid_savings
    
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
        'off_peak_rate': off_peak_rate
    }


def main():
    parser = argparse.ArgumentParser(description='Calculate daily battery automation savings')
    parser.add_argument('--date', type=str, help='Single date to calculate (YYYY-MM-DD)')
    parser.add_argument('--date-range', nargs=2, metavar=('START', 'END'),
                       help='Date range (YYYY-MM-DD YYYY-MM-DD)')
    parser.add_argument('--yesterday', action='store_true', 
                       help='Calculate yesterday\'s savings (for daily automation)')
    parser.add_argument('--all', action='store_true', help='Calculate all available dates')
    parser.add_argument('--output', type=str, help='Output file path (default: DATA_DIR/daily_savings.csv)')
    parser.add_argument('--config', type=str, help='System configuration file path')
    parser.add_argument('--quiet', '-q', action='store_true', help='Minimal output (for automation)')
    
    args = parser.parse_args()
    
    # Set output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = DATA_DIR / 'daily_savings.csv'
    
    # Load configuration
    sys_config = load_system_config(args.config)
    
    # Load monitoring data
    if not args.quiet:
        print("Loading monitoring data...")
    df_monitoring = load_monitoring_data()
    
    if df_monitoring is None:
        print("Error: Could not load monitoring data")
        return 1
    
    # Determine date range
    if args.date:
        dates = [datetime.strptime(args.date, '%Y-%m-%d').date()]
    elif args.date_range:
        start = datetime.strptime(args.date_range[0], '%Y-%m-%d').date()
        end = datetime.strptime(args.date_range[1], '%Y-%m-%d').date()
        dates = [start + timedelta(days=x) for x in range((end - start).days + 1)]
    elif args.yesterday:
        dates = [(datetime.now() - timedelta(days=1)).date()]
    elif args.all:
        dates = sorted(df_monitoring['date'].unique())
        # Exclude today (incomplete data)
        today = datetime.now().date()
        dates = [d for d in dates if d < today]
    else:
        print("Error: Must specify --date, --date-range, --yesterday, or --all")
        return 1
    
    # Calculate savings for each date
    results = []
    if not args.quiet:
        print(f"\nCalculating savings for {len(dates)} day(s)...")
    
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
    
    # Save results
    df_results = pd.DataFrame(results)
    
    # Create output directory if needed
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Merge with existing file (update existing dates, add new ones)
    if output_path.exists():
        df_existing = pd.read_csv(output_path)
        # Remove dates we're updating
        dates_updating = set(df_results['date'].astype(str))
        df_existing = df_existing[~df_existing['date'].astype(str).isin(dates_updating)]
        # Combine and sort
        df_combined = pd.concat([df_existing, df_results], ignore_index=True)
        df_combined = df_combined.sort_values('date')
        df_combined.to_csv(output_path, index=False)
    else:
        df_results.to_csv(output_path, index=False)
    
    if not args.quiet:
        print(f"\nSaved to {output_path}")
        
        # Print summary
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
        # Quiet mode - just print the total for today/yesterday
        print(f"${df_results['total_savings'].sum():.2f}")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
