#!/volume1/docker/franklin/venv311/bin/python3
"""
Continuous Battery Monitoring
Logs battery stats every 15 minutes indefinitely
"""
import asyncio
import csv
from datetime import datetime
from franklinwh import Client, TokenFetcher

# ⚠️ REPLACE WITH YOUR FRANKLIN WH CREDENTIALS
USERNAME = "YOUR_EMAIL@example.com"
PASSWORD = "YOUR_PASSWORD"
GATEWAY_ID = "YOUR_GATEWAY_ID"

LOG_FILE = "/volume1/docker/franklin/logs/continuous_monitoring.csv"

async def continuous_monitor(interval_minutes=15):
    """
    Continuously monitor battery stats and log to CSV.
    Runs indefinitely until stopped.

    Args:
        interval_minutes: How often to log data (default 15 minutes)
    """
    fetcher = TokenFetcher(USERNAME, PASSWORD)
    client = Client(fetcher, GATEWAY_ID)

    # Check if file exists
    file_exists = False
    try:
        with open(LOG_FILE, 'r') as f:
            file_exists = True
    except FileNotFoundError:
        pass

    print("=" * 70)
    print("CONTINUOUS BATTERY MONITORING")
    print("=" * 70)
    print(f"Interval: {interval_minutes} minutes")
    print(f"Log file: {LOG_FILE}")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    print("\nPress Ctrl+C to stop monitoring")
    print("\nLogging data...\n")

    iteration = 0

    with open(LOG_FILE, 'a', newline='') as csvfile:
        fieldnames = ['timestamp', 'soc_percent', 'solar_kw', 'grid_kw',
                     'battery_kw', 'home_load_kw', 'grid_status',
                     'battery_charge_total', 'battery_discharge_total',
                     'grid_import_total', 'solar_total']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()
            csvfile.flush()

        try:
            while True:
                iteration += 1
                try:
                    stats = await client.get_stats()
                    now = datetime.now()

                    data = {
                        'timestamp': now.strftime('%Y-%m-%d %H:%M:%S'),
                        'soc_percent': f'{stats.current.battery_soc:.2f}',
                        'solar_kw': f'{stats.current.solar_production:.3f}',
                        'grid_kw': f'{stats.current.grid_use:.3f}',
                        'battery_kw': f'{stats.current.battery_use:.3f}',
                        'home_load_kw': f'{stats.current.home_load:.3f}',
                        'grid_status': stats.current.grid_status.name,
                        'battery_charge_total': f'{stats.totals.battery_charge:.3f}',
                        'battery_discharge_total': f'{stats.totals.battery_discharge:.3f}',
                        'grid_import_total': f'{stats.totals.grid_import:.3f}',
                        'solar_total': f'{stats.totals.solar:.3f}'
                    }

                    writer.writerow(data)
                    csvfile.flush()

                    # Display progress
                    print(f"[{iteration:04d}] {now.strftime('%m/%d %H:%M')} | "
                          f"SOC: {stats.current.battery_soc:5.2f}% | "
                          f"Solar: {stats.current.solar_production:6.3f}kW | "
                          f"Grid: {stats.current.grid_use:6.3f}kW | "
                          f"Battery: {stats.current.battery_use:+6.3f}kW | "
                          f"Load: {stats.current.home_load:6.3f}kW")

                except Exception as e:
                    print(f"[{iteration:04d}] Error: {e}")

                # Wait for next interval
                await asyncio.sleep(interval_minutes * 60)

        except KeyboardInterrupt:
            print("\n" + "=" * 70)
            print("MONITORING STOPPED")
            print("=" * 70)
            print(f"Stopped: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"Total iterations: {iteration}")
            print(f"Log file: {LOG_FILE}")
            print("=" * 70)

if __name__ == "__main__":
    # Log every 15 minutes by default
    asyncio.run(continuous_monitor(interval_minutes=15))
