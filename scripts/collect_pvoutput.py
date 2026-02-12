#!/usr/bin/env python3
"""
PVOutput Daily Data Collector
Collects yesterday's completed solar production data from PVOutput.org API.
Supports multiple systems via PVOUTPUT_SYSTEM_IDS config.

Reads credentials from config (.env file):
  PVOUTPUT_API_KEY - Your PVOutput API key
  PVOUTPUT_SYSTEM_IDS - Comma-separated system IDs

v3.5.0 - Config-based credentials, multi-system support
"""
import requests
import csv
from datetime import datetime, timedelta
from pathlib import Path

# Load config
try:
    from config import config
    API_KEY = config.PVOUTPUT_API_KEY
    SYSTEM_IDS = config.PVOUTPUT_SYSTEM_IDS  # List of system ID strings
    LOG_DIR = config.LOG_DIR
except ImportError:
    import os
    API_KEY = os.getenv('PVOUTPUT_API_KEY', '')
    SYSTEM_IDS = [s.strip() for s in os.getenv('PVOUTPUT_SYSTEM_IDS', '').split(',') if s.strip()]
    LOG_DIR = Path(os.getenv('LOG_DIR', '/app/logs'))

# System name mapping - maps system IDs to friendly names for log files
# Users with different system IDs will get generic names (system_1, system_2)
KNOWN_SYSTEMS = {
    '104523': ('ground_mount', 'Ground Mount'),
    '110645': ('house', 'House'),
}

FIELDNAMES = ['date', 'energy_generated_wh', 'efficiency_kwh_kw', 'energy_exported_wh',
              'energy_used_wh', 'peak_power_w', 'peak_time', 'condition']


def get_system_info(system_id, index):
    """Get file slug and display name for a system ID."""
    if system_id in KNOWN_SYSTEMS:
        return KNOWN_SYSTEMS[system_id]
    return (f'system_{index + 1}', f'System {index + 1} ({system_id})')


def get_log_path(file_slug):
    """Get the CSV path for a system."""
    return LOG_DIR / f"pvoutput_{file_slug}_daily.csv"


def init_csv(filepath):
    """Initialize CSV with headers if it doesn't exist."""
    if not filepath.exists():
        with open(filepath, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(FIELDNAMES)


def load_existing_data(filepath):
    """Load existing CSV data into a dict keyed by date."""
    data = {}
    if filepath.exists():
        with open(filepath, 'r', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                data[row['date']] = row
    return data


def save_data(filepath, data):
    """Save data dict back to CSV, sorted by date descending."""
    sorted_dates = sorted(data.keys(), reverse=True)
    with open(filepath, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for date in sorted_dates:
            writer.writerow(data[date])


def get_and_save_daily_output(system_id, filepath, system_name, date):
    """Get daily output and save to CSV, updating existing records."""
    url = "https://pvoutput.org/service/r2/getoutput.jsp"
    headers = {
        "X-Pvoutput-Apikey": API_KEY,
        "X-Pvoutput-SystemId": system_id
    }
    params = {
        "d": date.strftime("%Y%m%d")
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        if response.status_code != 200:
            print(f"Error for {system_name}: HTTP {response.status_code}")
            return False

        # Initialize CSV if needed
        init_csv(filepath)

        # Load existing data
        existing_data = load_existing_data(filepath)

        # Split by SEMICOLONS first (multiple records), then by commas (fields within record)
        records = response.text.strip().split(';')
        updates = 0

        for record in records:
            parts = record.split(',')

            if len(parts) < 6:
                continue

            date_str = parts[0]

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

            # Skip if energy is 0 (bad data) and we already have non-zero data
            if energy == 0 and date_str in existing_data:
                existing_energy = float(existing_data[date_str].get('energy_generated_wh', 0))
                if existing_energy > 0:
                    continue  # Keep the existing good data

            new_row = {
                'date': date_str,
                'energy_generated_wh': energy,
                'efficiency_kwh_kw': efficiency,
                'energy_exported_wh': exported,
                'energy_used_wh': used,
                'peak_power_w': peak_power,
                'peak_time': peak_time,
                'condition': condition
            }

            # Check if this is new or an update
            if date_str in existing_data:
                old_energy = float(existing_data[date_str].get('energy_generated_wh', 0))
                if energy != old_energy:
                    print(f"  Updating {date_str}: {old_energy} -> {energy} Wh")
                    updates += 1
            else:
                print(f"  Adding {date_str}: {energy} Wh")
                updates += 1

            existing_data[date_str] = new_row

        # Save all data back
        save_data(filepath, existing_data)

        if updates > 0:
            print(f"  {system_name}: {updates} records updated")
        else:
            print(f"  {system_name}: No updates needed")

        return True

    except Exception as e:
        print(f"Error for {system_name}: {e}")
        return False


def backfill_zeros(filepath, system_id, system_name):
    """Find and fix any zero-value records by re-fetching from API."""
    if not filepath.exists():
        return

    existing_data = load_existing_data(filepath)
    zeros = [date for date, row in existing_data.items()
             if float(row.get('energy_generated_wh', 0)) == 0]

    if zeros:
        print(f"  {system_name}: Found {len(zeros)} records with zero energy, attempting to fix...")
        get_and_save_daily_output(system_id, filepath, system_name, datetime.now())


def main():
    """Collect PVOutput daily data for all configured systems."""
    if not API_KEY:
        print("ERROR: PVOUTPUT_API_KEY not configured")
        return False

    if not SYSTEM_IDS:
        print("ERROR: PVOUTPUT_SYSTEM_IDS not configured")
        return False

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    yesterday = datetime.now() - timedelta(days=1)
    print(f"Collecting PVOutput data for {yesterday.strftime('%Y-%m-%d')}")

    success = True
    for i, system_id in enumerate(SYSTEM_IDS):
        file_slug, display_name = get_system_info(system_id, i)
        filepath = get_log_path(file_slug)

        print(f"\n{display_name}:")
        if not get_and_save_daily_output(system_id, filepath, display_name, yesterday):
            success = False

        # Check for and fix any zero records
        backfill_zeros(filepath, system_id, display_name)

    print(f"\nPVOutput collection {'complete' if success else 'completed with errors'}")
    return success


if __name__ == "__main__":
    import sys
    result = main()
    sys.exit(0 if result else 1)
