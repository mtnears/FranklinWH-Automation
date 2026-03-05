#!/usr/bin/env python3
"""
rollup_daily_energy.py — Nightly aggregation of system_readings into daily_energy_summary.

Runs once per night (00:10) to roll up the previous day's 5-minute readings
into a single summary row. This enables fast queries for dashboards, reports,
and charts without scanning thousands of individual readings.

Can also backfill missing days from existing system_readings data.

Usage:
    python rollup_daily_energy.py                    # Roll up yesterday
    python rollup_daily_energy.py --date 2026-02-15  # Roll up specific date
    python rollup_daily_energy.py --backfill         # Backfill all missing days
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import db
    db.init_db()
    DB_AVAILABLE = True
except Exception as e:
    print(f"Database not available: {e}")
    DB_AVAILABLE = False


def rollup_date(date_str: str, device_id: str = 'agate_main') -> bool:
    """Aggregate system_readings for a single date into daily_energy_summary."""
    rows = db.get_readings_for_date(date_str, device_id)
    if not rows:
        print(f"  {date_str}: no readings found, skipping")
        return False

    socs = [r['soc_pct'] for r in rows if r.get('soc_pct') is not None]
    solars = [r['solar_kw'] for r in rows if r.get('solar_kw') is not None]
    loads = [r['home_load_kw'] for r in rows if r.get('home_load_kw') is not None]
    grids = [r['grid_kw'] for r in rows if r.get('grid_kw') is not None]
    batteries = [r['battery_kw'] for r in rows if r.get('battery_kw') is not None]

    kwh_solar_vals = [r['kwh_solar'] for r in rows if r.get('kwh_solar') is not None]
    kwh_grid_imp_vals = [r['kwh_grid_import'] for r in rows if r.get('kwh_grid_import') is not None]
    kwh_grid_exp_vals = [r['kwh_grid_export'] for r in rows if r.get('kwh_grid_export') is not None]
    kwh_bat_chg_vals = [r['kwh_battery_charge'] for r in rows if r.get('kwh_battery_charge') is not None]
    kwh_bat_dis_vals = [r['kwh_battery_discharge'] for r in rows if r.get('kwh_battery_discharge') is not None]
    kwh_load_vals = [r['kwh_load'] for r in rows if r.get('kwh_load') is not None]
    kwh_gen_vals = [r['kwh_generator'] for r in rows if r.get('kwh_generator') is not None]

    def daily_kwh(vals):
        if not vals:
            return 0.0
        return max(vals) - min(vals)

    summary = {
        'solar_kwh': round(daily_kwh(kwh_solar_vals), 3),
        'grid_import_kwh': round(daily_kwh(kwh_grid_imp_vals), 3),
        'grid_export_kwh': round(daily_kwh(kwh_grid_exp_vals), 3),
        'battery_charge_kwh': round(daily_kwh(kwh_bat_chg_vals), 3),
        'battery_discharge_kwh': round(daily_kwh(kwh_bat_dis_vals), 3),
        'home_load_kwh': round(daily_kwh(kwh_load_vals), 3),
        'generator_kwh': round(daily_kwh(kwh_gen_vals), 3),
        'peak_solar_kw': round(max(solars), 3) if solars else 0,
        'peak_load_kw': round(max(loads), 3) if loads else 0,
        'peak_grid_kw': round(max(grids), 3) if grids else 0,
        'peak_battery_kw': round(max(abs(b) for b in batteries), 3) if batteries else 0,
        'soc_min': round(min(socs), 1) if socs else None,
        'soc_max': round(max(socs), 1) if socs else None,
        'soc_avg': round(sum(socs) / len(socs), 1) if socs else None,
        'soc_end': round(socs[-1], 1) if socs else None,
        'reading_count': len(rows),
        'source': 'rollup',
    }

    ok = db.store_daily_energy_summary(date=date_str, device_id=device_id, **summary)
    if ok:
        print(f"  {date_str}: {len(rows)} readings → "
              f"solar={summary['solar_kwh']}kWh, load={summary['home_load_kwh']}kWh, "
              f"SOC {summary['soc_min']}-{summary['soc_max']}%")
    return ok


def backfill(device_id: str = 'agate_main'):
    """Find all dates with system_readings but no daily_energy_summary, and roll them up."""
    missing = db.query("""
        SELECT DISTINCT date(timestamp) as d FROM system_readings
        WHERE device_id = ? AND date(timestamp) < date('now')
        AND date(timestamp) NOT IN (
            SELECT date FROM daily_energy_summary WHERE device_id = ?
        )
        ORDER BY d
    """, (device_id, device_id))

    if not missing:
        print("No missing days to backfill")
        return 0

    print(f"Backfilling {len(missing)} days...")
    count = 0
    for row in missing:
        if rollup_date(row['d'], device_id):
            count += 1
    print(f"Backfilled {count}/{len(missing)} days")
    return count


def main():
    if not DB_AVAILABLE:
        print("Database not available, exiting")
        return 1

    parser = argparse.ArgumentParser(description='Daily energy summary rollup')
    parser.add_argument('--date', type=str, help='Roll up specific date (YYYY-MM-DD)')
    parser.add_argument('--backfill', action='store_true', help='Backfill all missing days')
    parser.add_argument('--device-id', default='agate_main', help='Device ID')
    args = parser.parse_args()

    if args.backfill:
        backfill(args.device_id)
    elif args.date:
        rollup_date(args.date, args.device_id)
    else:
        yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        print(f"Rolling up {yesterday}...")
        rollup_date(yesterday, args.device_id)

    return 0


if __name__ == '__main__':
    sys.exit(main())
