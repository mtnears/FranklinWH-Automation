#!/volume1/docker/franklin/venv311/bin/python3
"""
Final Safety Check Before Peak Period
Last chance to ensure battery is adequately charged
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

def start_emergency_charge():
    """Start emergency charging"""
    try:
        log("EMERGENCY: Starting grid charge")
        result = subprocess.run(
            ['/volume1/docker/franklin/switch_to_backup_v2.py'],
            capture_output=True,
            text=True,
            timeout=60
        )
        return True
    except Exception as e:
        log(f"ERROR starting emergency charge: {e}")
        return False

def main():
    log("="*60)
    log("FINAL SAFETY CHECK (3:30 PM)")
    log("="*60)
    log("Peak period starts in 90 minutes (5:00 PM)")

    current_soc = get_battery_soc()

    if current_soc is None:
        log("CRITICAL: Cannot read battery SOC!")
        return

    log(f"Current SOC: {current_soc:.1f}%")

    MINIMUM_ACCEPTABLE = 75

    if current_soc >= MINIMUM_ACCEPTABLE:
        log(f"✓ SOC >= {MINIMUM_ACCEPTABLE}% - Ready for peak period")
        log("✓ No action needed")
    else:
        log(f"🚨 ALERT: SOC only {current_soc:.1f}%!")
        log(f"🚨 Below minimum acceptable level ({MINIMUM_ACCEPTABLE}%)")
        log(f"🚨 Starting emergency grid charge")
        log(f"🚨 Will charge to 85% (partial charge better than nothing)")
        start_emergency_charge()

    log("")

if __name__ == "__main__":
    main()
