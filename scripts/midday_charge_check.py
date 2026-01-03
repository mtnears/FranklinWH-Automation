#!/volume1/docker/franklin/venv311/bin/python3
"""
Mid-Day Charge Check
Evaluates if grid charging is needed to be ready for peak period
"""
import subprocess
from datetime import datetime

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

        return None

    except Exception as e:
        log(f"ERROR getting battery SOC: {e}")
        return None

def get_recent_solar_production():
    """Get average solar production from last 15 minutes"""
    try:
        result = subprocess.run(
            ['tail', '-60', '/volume1/docker/franklin/logs/continuous_monitoring.csv'],
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
                    solar = abs(float(parts[2]))
                    solar_values.append(solar)
                except:
                    continue

        if solar_values:
            return sum(solar_values) / len(solar_values)
        return 0.0

    except Exception as e:
        log(f"ERROR getting solar production: {e}")
        return 0.0

def start_grid_charge():
    """Start grid charging"""
    try:
        log("Starting grid charge to 95%")
        result = subprocess.run(
            ['/volume1/docker/franklin/switch_to_backup_v2.py'],
            capture_output=True,
            text=True,
            timeout=60
        )
        return True
    except Exception as e:
        log(f"ERROR starting grid charge: {e}")
        return False

def main():
    log("="*60)
    log("MID-DAY CHARGE CHECK (2:00 PM)")
    log("="*60)

    current_soc = get_battery_soc()

    if current_soc is None:
        log("ERROR: Cannot read battery SOC")
        return

    log(f"Current SOC: {current_soc:.1f}%")

    solar_production = get_recent_solar_production()
    log(f"Current solar production: {solar_production:.1f} kW")

    # Decision logic
    if current_soc >= 85:
        log("✓ SOC >= 85% - Excellent! Ready for peak period")
        log("✓ No action needed")

    elif current_soc >= 80 and solar_production > 1.0:
        log("✓ SOC >= 80% and solar still producing well")
        log("✓ Let solar finish charging - should reach 85%+ by peak")

    else:
        log(f"⚠ SOC at {current_soc:.1f}%, below target")
        log(f"⚠ Solar production: {solar_production:.1f} kW")
        log(f"⚠ Starting grid charge to ensure readiness for peak period")
        start_grid_charge()

    log("")

if __name__ == "__main__":
    main()
