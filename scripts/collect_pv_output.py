#!/usr/bin/env python3
"""
collect_pv_output.py — PVOutput Daily Data Collector (SQLite)

Collects yesterday's completed solar production data from PVOutput.org API.
Writes to SQLite pvoutput_daily table.
Supports multiple systems via PVOUTPUT_SYSTEM_IDS config.

Usage:
    python3 collect_pv_output.py
    python3 collect_pv_output.py --test
"""
import argparse
import logging
import os
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
    SYSTEM_IDS = [s.strip() for s in os.getenv('PVOUTPUT_SYSTEM_IDS', '').split(',') if s.strip()]

try:
    from db import store, init_db
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    log.warning("db.py not available — cannot write data")

KNOWN_SYSTEMS = {
    '104523': ('barn', 'Barn'),
    '110645': ('house', 'House'),
}


def get_system_info(system_id, index):
    if system_id in KNOWN_SYSTEMS:
        return KNOWN_SYSTEMS[system_id]
    return (f'system_{index + 1}', f'System {index + 1} ({system_id})')


def collect_system(system_id, file_slug, display_name, date, test_mode=False):
    """Collect daily output for one system. Returns True on success."""
    url = "https://pvoutput.org/service/r2/getoutput.jsp"
    headers = {
        "X-Pvoutput-Apikey": API_KEY,
        "X-Pvoutput-SystemId": system_id
    }
    params = {"d": date.strftime("%Y%m%d")}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code != 200:
            log.error(f"{display_name}: HTTP {response.status_code}")
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

        log.info(f"{display_name}: {db_writes} DB writes")
        return True

    except Exception as e:
        log.error(f"{display_name}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description='Collect PVOutput daily data')
    parser.add_argument('--test', action='store_true', help='Print data without writing')
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

    yesterday = datetime.now() - timedelta(days=1)
    log.info(f"Collecting PVOutput data for {yesterday.strftime('%Y-%m-%d')}")

    success = True
    for i, system_id in enumerate(SYSTEM_IDS):
        file_slug, display_name = get_system_info(system_id, i)
        if not collect_system(system_id, file_slug, display_name, yesterday, args.test):
            success = False

    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
