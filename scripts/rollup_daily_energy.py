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


def counter_day_kwh(rows, next_rows, col: str) -> float:
    """One day's energy from a gateway counter that resets daily.

    The counters (kwh_solar, kwh_grid_import, ...) count up from a reset a
    minute or so after midnight. Until Oct 2026 the rollup took max - min over
    the date's rows, so a 00:00 row still holding the previous day's total
    became this day's value too (Oct 5 and Oct 6 both showed 17.18 kWh solar).

    - Uses one source only (the one with the most values), since Modbus/local
      and cloud rows carry slightly different counter values.
    - Drops the rows before the early-morning reset; they belong to yesterday.
    - Counts from zero when the day's data starts right after the reset, and
      from the first value when it starts later (unknown energy before a gap).
    - Adds the next day's pre-reset rows, which close out this day.
    - A drop later in the day counts as another reset.
    """
    from collections import Counter

    def series(rs):
        return [(str(r.get('timestamp', '')), r.get('source') or '', float(r[col]))
                for r in rs if r.get(col) is not None]

    today = series(rows)
    if not today:
        return 0.0
    src = Counter(v[1] for v in today).most_common(1)[0][0]
    vals = [(t, v) for t, s_, v in today if s_ == src]
    vals.sort()

    # Drop the previous day's carry-over before the early-morning reset
    for i in range(1, len(vals)):
        if vals[i][0][11:16] >= '00:30':
            break
        if vals[i][1] < vals[i - 1][1]:
            vals = vals[i:]
            break

    after_reset = vals[0][0][11:16] < '00:30'
    first = vals[0][1]
    span = max(v for _, v in vals) - first
    # A lifetime-style counter (huge first value, small growth) never resets
    total = first if (after_reset and not first > 5 * max(span, 0.001)) else 0.0

    tail = sorted((t, v) for t, s_, v in series(next_rows) if s_ == src)
    for t, v in tail:
        if v >= vals[-1][1]:
            vals.append((t, v))
        else:
            break

    for (_, a), (_, b) in zip(vals, vals[1:]):
        total += (b - a) if b >= a else b
    return total


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

    # Next day's first rows: the gateway's daily counters reset a minute or so
    # after midnight, so the rows just after midnight still carry the end of
    # this day's totals.
    from datetime import datetime as _dt, timedelta as _td
    next_date = (_dt.strptime(date_str, '%Y-%m-%d') + _td(days=1)).strftime('%Y-%m-%d')
    try:
        next_rows = [r for r in db.get_readings_for_date(next_date, device_id)
                     if str(r.get('timestamp', ''))[11:16] < '00:30']
    except Exception:
        next_rows = []

    def daily_kwh(col):
        return counter_day_kwh(rows, next_rows, col)

    def daily_kwh_from_instantaneous(kw_vals, row_list):
        """Estimate daily kWh by integrating instantaneous kW readings over time intervals.
        Used as fallback when the cumulative counter (kwh_*) is sparse or absent,
        which happens for Modbus-sourced rows that don't populate cloud counters."""
        if len(kw_vals) < 2:
            return 0.0
        total_kwh = 0.0
        prev_ts = None
        for r in row_list:
            kw = r.get('home_load_kw')
            if kw is None:
                continue
            ts_str = r.get('timestamp', '')
            try:
                from datetime import datetime as _dt
                ts = _dt.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                continue
            if prev_ts is not None:
                delta_h = (ts - prev_ts).total_seconds() / 3600.0
                if 0 < delta_h <= 1.0:
                    total_kwh += kw * delta_h
            prev_ts = ts
        return total_kwh

    # Home load kWh: the cumulative counter (kwh_load) is only populated by
    # cloud API enrichment rows. Modbus-only rows leave it NULL, so daily_kwh()
    # returns 0.0 on days with mostly Modbus data. Fall back to integrating
    # the instantaneous home_load_kw readings when the counter is too sparse.
    home_load_from_counter = daily_kwh('kwh_load')
    if home_load_from_counter < 0.1 and loads:
        home_load_from_counter = daily_kwh_from_instantaneous(loads, rows)

    summary = {
        'solar_kwh': round(daily_kwh('kwh_solar'), 3),
        'grid_import_kwh': round(daily_kwh('kwh_grid_import'), 3),
        'grid_export_kwh': round(daily_kwh('kwh_grid_export'), 3),
        'battery_charge_kwh': round(daily_kwh('kwh_battery_charge'), 3),
        'battery_discharge_kwh': round(daily_kwh('kwh_battery_discharge'), 3),
        'home_load_kwh': round(home_load_from_counter, 3),
        'generator_kwh': round(daily_kwh('kwh_generator'), 3),
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
