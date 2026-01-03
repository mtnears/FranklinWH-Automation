#!/volume1/docker/franklin/venv311/bin/python3
"""
PVOutput Daily Data Collector
Collects yesterday's completed solar production data
"""
import requests
import csv
from datetime import datetime, timedelta
from pathlib import Path

# ⚠️ REPLACE WITH YOUR PVOUTPUT CREDENTIALS
API_KEY = "YOUR_PVOUTPUT_API_KEY"
GROUND_MOUNT_SID = "YOUR_SYSTEM_ID_1"  # Remove if you only have one system
HOUSE_SID = "YOUR_SYSTEM_ID_2"

LOG_DIR = Path("/volume1/docker/franklin/logs")
GROUND_LOG = LOG_DIR / "pvoutput_ground_mount_daily.csv"
HOUSE_LOG = LOG_DIR / "pvoutput_house_daily.csv"

def get_and_save_daily_output(system_id, filepath, system_name, date):
    """Get daily output and save to CSV"""
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

        # Split by SEMICOLONS first (multiple records), then by commas (fields within record)
        records = response.text.strip().split(';')

        for record in records:
            # Split each record by commas to get individual fields
            parts = record.split(',')

            if len(parts) < 6:
                continue

            # Extract only the fields we need (first 8)
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

            # Check if already exists
            if filepath.exists():
                with open(filepath, 'r') as f:
                    if date_str in f.read():
                        continue

            # Append to CSV
            with open(filepath, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([date_str, energy, efficiency, exported, used, peak_power, peak_time, condition])

            print(f"✓ Saved {date_str} to {system_name} ({energy} Wh)")

        return True

    except Exception as e:
        print(f"Error for {system_name}: {e}")
        return False

def main():
    yesterday = datetime.now() - timedelta(days=1)
    print(f"Collecting PVOutput data for {yesterday.strftime('%Y-%m-%d')}")

    get_and_save_daily_output(GROUND_MOUNT_SID, GROUND_LOG, "Ground Mount", yesterday)
    get_and_save_daily_output(HOUSE_SID, HOUSE_LOG, "House", yesterday)

    print("✓ Complete")

if __name__ == "__main__":
    main()
