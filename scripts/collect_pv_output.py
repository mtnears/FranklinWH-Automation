#!/usr/bin/env python3
"""
collect_pv_output.py — PVOutput Daily Data Collector (SQLite)

Collects completed daily solar production from PVOutput.org into the local
pvoutput_daily table. Supports multiple systems via PVOUTPUT_SYSTEM_IDS.

Each run asks for every completed day since the newest row already stored
for that system (looking back at most CATCHUP_DAYS), so days missed while
PVOutput or its upstream source was down fill in on their own once the data
appears. That is still one API call per system per run. With nothing missing,
the request is just yesterday.

Usage:
    python3 collect_pv_output.py
    python3 collect_pv_output.py --test
    python3 collect_pv_output.py --from 2026-06-10 --to 2026-10-06   # one-off backfill
"""
import argparse
import logging
import os
import settings
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [pvoutput] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('collect_pv_output')

try:
    from config import config
    API_KEY = config.PVOUTPUT_API_KEY
    SYSTEM_IDS = config.PVOUTPUT_SYSTEM_IDS
except ImportError:
    API_KEY = os.getenv('PVOUTPUT_API_KEY', '')
    SYSTEM_IDS = [s.strip() for s in settings.raw('PVOUTPUT_SYSTEM_IDS', '').split(',') if s.strip()]

try:
    from db import store, init_db
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    log.warning("db.py not available — cannot write data")

# getoutput.jsp returns at most 50 rows per call without a PVOutput donation.
MAX_ROWS = 50
# Automatic catch-up window. Longer gaps need an explicit --from/--to run.
CATCHUP_DAYS = 30

KNOWN_SYSTEMS = {
    '104523': ('barn', 'Barn'),
    '110645': ('house', 'House'),
}


def get_system_info(system_id, index):
    if system_id in KNOWN_SYSTEMS:
        return KNOWN_SYSTEMS[system_id]
    return (f'system_{index + 1}', f'System {index + 1} ({system_id})')


def last_stored_date(array_id):
    """Newest date already in pvoutput_daily for this array, or None."""
    try:
        import db
        row = db._get_connection().execute(
            "SELECT MAX(date) FROM pvoutput_daily WHERE array_id = ?",
            (array_id,)).fetchone()
        if row and row[0]:
            return datetime.strptime(row[0], '%Y-%m-%d')
    except Exception as e:
        log.debug(f"last_stored_date({array_id}): {e}")
    return None


def catchup_start(array_id, yesterday):
    """First day to request: the day after the newest stored row, bounded
    to CATCHUP_DAYS back, and never later than yesterday."""
    floor = yesterday - timedelta(days=CATCHUP_DAYS - 1)
    last = last_stored_date(array_id)
    start = (last + timedelta(days=1)) if last else floor
    if start < floor:
        start = floor
    if start > yesterday:
        start = yesterday
    return start


def collect_system(system_id, file_slug, display_name, date_from, date_to=None,
                   test_mode=False):
    """Collect daily output for one system over [date_from, date_to].

    Returns True on success. A day PVOutput has no data for simply returns
    no row; it is asked for again on the next run while inside the window.
    """
    date_to = date_to or date_from
    url = "https://pvoutput.org/service/r2/getoutput.jsp"
    headers = {
        "X-Pvoutput-Apikey": API_KEY,
        "X-Pvoutput-SystemId": system_id
    }
    if date_from == date_to:
        params = {"d": date_from.strftime("%Y%m%d")}
    else:
        params = {"df": date_from.strftime("%Y%m%d"),
                  "dt": date_to.strftime("%Y%m%d"),
                  "limit": MAX_ROWS}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code == 400 and ('no status found' in response.text.lower()
                                           or 'no system or data found' in response.text.lower()):
            # PVOutput's answer when no day in the range has data yet.
            log.info(f"{display_name}: no data on PVOutput for the requested day(s)")
            return True
        if response.status_code != 200:
            log.error(f"{display_name}: HTTP {response.status_code} {response.text.strip()[:120]}")
            return False

        records = response.text.strip().split(';')
        db_writes = 0

        for record in records:
            parts = record.split(',')
            if len(parts) < 6:
                continue

            date_str = parts[0]
            if len(date_str) == 8 and date_str.isdigit():
                date_str = '{}-{}-{}'.format(date_str[:4], date_str[4:6], date_str[6:8])
            try:
                energy = float(parts[1]) if parts[1] and parts[1] != 'NaN' else 0.0
                efficiency = float(parts[2]) if parts[2] and parts[2] != 'NaN' else 0.0
                exported = float(parts[3]) if parts[3] and parts[3] != 'NaN' else 0.0
                used = float(parts[4]) if parts[4] and parts[4] != 'NaN' else 0.0
                peak_power = float(parts[5]) if parts[5] and parts[5] != 'NaN' else 0.0
            except ValueError:
                continue

            peak_time = parts[6] if len(parts) > 6 else ''
            condition = parts[7] if len(parts) > 7 else ''

            if test_mode:
                log.info(f"  {date_str}: {energy} Wh, peak {peak_power} W @ {peak_time}")
                continue

            if DB_AVAILABLE:
                store.pvoutput_daily(
                    date=date_str,
                    array_id=file_slug,
                    energy_wh=energy,
                    peak_power_w=peak_power,
                    peak_time=peak_time,
                    efficiency=efficiency,
                    exported_wh=exported,
                    used_wh=used,
                    condition=condition,
                )
                db_writes += 1

        span = date_from.strftime('%Y-%m-%d')
        if date_to != date_from:
            span += f"..{date_to.strftime('%Y-%m-%d')}"
        log.info(f"{display_name}: {db_writes} DB writes ({span})")
        return True

    except Exception as e:
        log.error(f"{display_name}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description='Collect PVOutput daily data')
    parser.add_argument('--test', action='store_true', help='Print data without writing')
    parser.add_argument('--from', dest='date_from', help='Backfill start date YYYY-MM-DD')
    parser.add_argument('--to', dest='date_to', help='Backfill end date YYYY-MM-DD (default: yesterday)')
    args = parser.parse_args()

    if not API_KEY:
        log.error("PVOUTPUT_API_KEY not configured")
        return 1
    if not SYSTEM_IDS:
        log.error("PVOUTPUT_SYSTEM_IDS not configured")
        return 1
    if not DB_AVAILABLE:
        log.error("Database not available — cannot collect data")
        return 1

    if not args.test:
        init_db()

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)

    success = True
    for i, system_id in enumerate(SYSTEM_IDS):
        file_slug, display_name = get_system_info(system_id, i)
        if args.date_from:
            # Explicit backfill, split into MAX_ROWS-day chunks.
            start = datetime.strptime(args.date_from, '%Y-%m-%d')
            end = datetime.strptime(args.date_to, '%Y-%m-%d') if args.date_to else yesterday
            end = min(end, yesterday)
            while start <= end:
                chunk_end = min(start + timedelta(days=MAX_ROWS - 1), end)
                if not collect_system(system_id, file_slug, display_name,
                                      start, chunk_end, args.test):
                    success = False
                start = chunk_end + timedelta(days=1)
        else:
            start = catchup_start(file_slug, yesterday)
            if not collect_system(system_id, file_slug, display_name,
                                  start, yesterday, args.test):
                success = False

    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
