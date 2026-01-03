#!/volume1/docker/franklin/venv311/bin/python3
"""
Morning Solar-First Charging Intelligence
Analyzes battery status and solar conditions to optimize charging strategy
"""
import subprocess
import json
from datetime import datetime, timedelta
import re

LOG_FILE = "/volume1/docker/franklin/logs/solar_intelligence.log"

def log(message):
    """Write to log file with timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, 'a') as f:
        f.write(f"{timestamp} - {message}\n")
    print(message)

def get_battery_soc():
    """Get current battery SOC percentage"""
    try:
        result = subprocess.run(
            ['/volume1/docker/franklin/get_battery_status.py'],
            capture_output=True,
            text=True,
            timeout=30
        )

        for line in result.stdout.split('\n'):
            if 'Battery SOC:' in line:
                soc_str = line.split(':')[1].strip().replace('%', '')
                return float(soc_str)

        log("ERROR: Could not parse SOC from battery status")
        return None

    except Exception as e:
        log(f"ERROR getting battery SOC: {e}")
        return None

def get_recent_solar_production():
    """Get average solar production from last 30 minutes of monitoring data"""
    try:
        # Read last 120 lines (30 min at 15-sec intervals)
        result = subprocess.run(
            ['tail', '-120', '/volume1/docker/franklin/logs/continuous_monitoring.csv'],
            capture_output=True,
            text=True
        )

        solar_values = []
        for line in result.stdout.strip().split('\n'):
            if not line or line.startswith('timestamp'):
                continue
            parts = line.split(',')
            if len(parts) >= 2:
                try:
                    # Column 2 is solar production
                    solar = abs(float(parts[2]))
                    solar_values.append(solar)
                except:
                    continue

        if solar_values:
            avg_solar = sum(solar_values) / len(solar_values)
            log(f"Recent solar production: {avg_solar:.2f} kW (avg of {len(solar_values)} readings)")
            return avg_solar
        else:
            log("No recent solar data available")
            return 0.0

    except Exception as e:
        log(f"ERROR getting solar production: {e}")
        return 0.0

def start_grid_charge():
    """Start grid charging by switching to Emergency Backup mode"""
    try:
        log("Starting grid charge - switching to Emergency Backup mode")
        result = subprocess.run(
            ['/volume1/docker/franklin/switch_to_backup_v2.py'],
            capture_output=True,
            text=True,
            timeout=60
        )
        log("Grid charging started successfully")
        return True
    except Exception as e:
        log(f"ERROR starting grid charge: {e}")
        return False

def main():
    log("="*60)
    log("MORNING SOLAR-FIRST INTELLIGENCE")
    log("="*60)

    # Get current battery status
    current_soc = get_battery_soc()

    if current_soc is None:
        log("CRITICAL: Cannot read battery SOC, aborting")
        return

    log(f"Current SOC: {current_soc:.1f}%")

    # Check if already adequately charged
    if current_soc >= 85:
        log("✓ SOC >= 85%, battery already well-charged")
        log("✓ No charging needed today - ready for peak period")
        return

    # Calculate charging requirements
    soc_deficit = 95 - current_soc
    CHARGE_RATE_PER_HOUR = 32  # Approximately 32% per hour at ~10kW
    hours_needed = soc_deficit / CHARGE_RATE_PER_HOUR
    SAFETY_MARGIN = 0.5  # 30 minutes
    total_hours_needed = hours_needed + SAFETY_MARGIN

    # Calculate deadline (5 PM - time needed)
    peak_start = datetime.now().replace(hour=17, minute=0, second=0, microsecond=0)
    deadline = peak_start - timedelta(hours=total_hours_needed)

    log(f"SOC deficit: {soc_deficit:.1f}%")
    log(f"Estimated charging time needed: {hours_needed:.1f} hours")
    log(f"With safety margin: {total_hours_needed:.1f} hours")
    log(f"Must start charging by: {deadline.strftime('%I:%M %p')}")

    # Check solar production
    solar_production = get_recent_solar_production()

    # Decision logic
    GOOD_SOLAR_THRESHOLD = 1.5  # kW
    LOW_SOC_THRESHOLD = 50

    if solar_production >= GOOD_SOLAR_THRESHOLD:
        log(f"✓ Good solar production ({solar_production:.1f} kW)")
        log(f"✓ Strategy: Let solar charge, check again at deadline")
        log(f"✓ Mid-day check will run at 2:00 PM (fixed schedule)")

    elif current_soc < LOW_SOC_THRESHOLD:
        log(f"⚠ Low SOC ({current_soc:.1f}%) and solar production low ({solar_production:.1f} kW)")
        log(f"⚠ Not enough time to wait - starting grid charge NOW")
        start_grid_charge()

    else:
        log(f"⚠ Solar production marginal ({solar_production:.1f} kW)")
        log(f"✓ SOC acceptable ({current_soc:.1f}%), will monitor and check at 2:00 PM")

    log("")

if __name__ == "__main__":
    main()
