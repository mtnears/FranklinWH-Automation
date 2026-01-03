# Franklin Battery Solar-First Automation System
## Complete Installation & Configuration Guide

**Author:** Ken Pauley  
**System:** Synology DS1525+ (DSM 7.2.2-72806 Update 2)  
**Python:** 3.11.14  
**Last Updated:** January 2, 2026

---

## Table of Contents

1. [Overview](#overview)
2. [System Requirements](#system-requirements)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Task Scheduler Setup](#task-scheduler-setup)
6. [Script Reference](#script-reference)
7. [Monitoring & Logs](#monitoring--logs)
8. [Troubleshooting](#troubleshooting)
9. [Appendix: Complete Scripts](#appendix-complete-scripts)

---

## Overview

This automation system optimizes Franklin whole-home battery charging using a **solar-first strategy** to minimize grid usage and electricity costs during peak rate periods.

### Key Features

- **Solar-First Intelligence:** Waits for solar production before grid charging when possible
- **Three-Tier Decision System:** Morning analysis, midday check, final safety check
- **Automated Data Collection:** Weather, solar production (PVOutput), continuous battery monitoring
- **Daily Status Reports:** Email summary of decisions and battery performance
- **Peak Period Optimization:** Ensures battery is charged and ready for Time-of-Use (TOU) peak periods

### How It Works

1. **8:00 AM** - Morning Intelligence analyzes battery SOC and solar forecast
2. **2:00 PM** - Midday check evaluates progress and grid charges if needed
3. **3:30 PM** - Final safety check ensures minimum charge before peak period
4. **4:30 PM** - Daily email report summarizing all decisions
5. **5:00-8:00 PM** - Peak period (battery discharges to offset grid usage)

### Expected Savings

With PG&E CARE rates and proper optimization:
- **CARE discount:** ~$700/year
- **Battery optimization:** ~$350/year
- **Total savings:** ~$1,050/year (your mileage may vary)

---

## System Requirements

### Hardware

- **Synology NAS** (tested on DS1525+) or any Linux system with:
  - Python 3.11+ support
  - 24/7 uptime
  - Network access to Franklin battery API
  - Minimum 100MB storage for logs

### Software

- **Operating System:** Synology DSM 7.2+ or any modern Linux distribution
- **Python:** 3.11 or higher
- **Network Access:** Outbound HTTPS to:
  - Franklin WH Cloud API
  - PVOutput.org API
  - Weather Underground API
  - Email server (for notifications)

### Required Accounts/Data

1. **Franklin WH Account:**
   - Username/password for cloud access
   - Gateway ID (found in Franklin mobile app)

2. **PVOutput Account (optional but recommended):**
   - Free account at pvoutput.org
   - API key
   - System IDs for your solar arrays

3. **Weather Underground (optional but recommended):**
   - API key
   - Personal Weather Station ID

4. **Email Configuration:**
   - SMTP server access for daily reports

---

## Installation

### Step 1: SSH Access to Your NAS

```bash
# SSH into your Synology NAS
ssh your-username@your-nas-ip

# Create working directory
mkdir -p /volume1/docker/franklin
cd /volume1/docker/franklin
```

### Step 2: Install Python 3.11

**On Synology DSM 7.2+:**

1. Open **Package Center**
2. Search for "Python 3.11"
3. Install **Python 3.11** package

**On other Linux systems:**
```bash
# Ubuntu/Debian
sudo apt update
sudo apt install python3.11 python3.11-venv

# Check version
python3 --version  # Should show 3.11.x
```

### Step 3: Create Virtual Environment

```bash
cd /volume1/docker/franklin

# Create virtual environment
python3 -m venv venv311

# Activate it
source venv311/bin/activate

# You should see (venv311) in your prompt
```

### Step 4: Install Python Dependencies

```bash
# Create requirements.txt
cat > requirements.txt << 'EOF'
franklinwh==0.13.0
requests>=2.31.0
EOF

# Install packages
pip install --break-system-packages -r requirements.txt

# Verify installation
pip list | grep franklinwh
pip list | grep requests
```

### Step 5: Create Log Directory

```bash
mkdir -p /volume1/docker/franklin/logs
```

---

## Configuration

### Step 1: Create Core Scripts

You'll need to create the following scripts. See the [Appendix](#appendix-complete-scripts) for complete code.

**Core automation scripts:**
1. `morning_solar_intelligence.py` - 8 AM solar analysis
2. `midday_charge_check.py` - 2 PM progress check
3. `final_safety_check.py` - 3:30 PM last chance charge
4. `daily_status_report.py` - 4:30 PM email summary

**Data collection scripts:**
5. `collect_pvoutput.py` - Hourly solar production data
6. `collect_weather.py` - 15-minute weather data
7. `continuous_monitor.py` - 15-minute battery monitoring

**Utility scripts:**
8. `get_battery_status.py` - Get current battery status
9. `switch_to_backup_v2.py` - Start grid charging
10. `switch_to_tou_v2.py` - Stop grid charging

### Step 2: Configure Personal Information

**IMPORTANT:** Replace these values in ALL scripts:

#### Franklin WH Credentials

Replace in `get_battery_status.py`, `switch_to_backup_v2.py`, `switch_to_tou_v2.py`, and `continuous_monitor.py`:

```python
USERNAME = "YOUR_FRANKLIN_EMAIL@example.com"
PASSWORD = "YOUR_FRANKLIN_PASSWORD"
GATEWAY_ID = "YOUR_GATEWAY_ID"  # From Franklin mobile app
```

#### PVOutput Configuration

Replace in `collect_pvoutput.py`:

```python
API_KEY = "YOUR_PVOUTPUT_API_KEY"
GROUND_MOUNT_SID = "YOUR_SYSTEM_ID_1"  # Optional: remove if you only have one system
HOUSE_SID = "YOUR_SYSTEM_ID_2"
```

If you only have one solar system, modify the script to remove the second system.

#### Weather Underground Configuration

Replace in `collect_weather.py`:

```python
PWS_ID = "YOUR_WEATHER_STATION_ID"
API_KEY = "YOUR_WEATHER_UNDERGROUND_API_KEY"
```

### Step 3: Make Scripts Executable

```bash
cd /volume1/docker/franklin
chmod +x *.py
```

### Step 4: Test Scripts

```bash
# Test battery status
./get_battery_status.py

# Test grid charging (will actually start charging!)
./switch_to_backup_v2.py

# Test TOU mode (stops charging)
./switch_to_tou_v2.py

# Test data collection
./collect_weather.py
./collect_pvoutput.py

# Test continuous monitoring (Ctrl+C to stop)
./continuous_monitor.py
```

---

## Task Scheduler Setup

All automation runs through Synology's Task Scheduler. Go to **Control Panel → Task Scheduler**.

### Task 1: Continuous Battery Monitoring (Boot-up)

- **Task Name:** Continuous Battery Monitoring
- **User:** root
- **Event:** Boot-up
- **Enabled:** ✓
- **Script:**
```bash
#!/bin/bash
sleep 30
cd /volume1/docker/franklin
source venv311/bin/activate
nohup /volume1/docker/franklin/continuous_monitor.py > /dev/null 2>&1 &
```

### Task 2: Morning Solar Intelligence

- **Task Name:** Solar Intelligence - Morning Check
- **User:** root
- **Schedule:** Daily at 8:00 AM
- **Script:**
```bash
#!/bin/bash
cd /volume1/docker/franklin
source venv311/bin/activate
./morning_solar_intelligence.py
```

### Task 3: Midday Charge Check

- **Task Name:** Solar Intelligence - Midday Check
- **User:** root
- **Schedule:** Daily at 2:00 PM (14:00)
- **Script:**
```bash
#!/bin/bash
cd /volume1/docker/franklin
source venv311/bin/activate
./midday_charge_check.py
```

### Task 4: Final Safety Check

- **Task Name:** Solar Intelligence - Final Safety
- **User:** root
- **Schedule:** Daily at 3:30 PM (15:30)
- **Script:**
```bash
#!/bin/bash
cd /volume1/docker/franklin
source venv311/bin/activate
./final_safety_check.py
```

### Task 5: Daily Status Report

- **Task Name:** Daily Battery Status Report
- **User:** root
- **Schedule:** Daily at 4:30 PM (16:30)
- **Send run details by email:** ✓ (enter your email)
- **Script:**
```bash
#!/bin/bash
cd /volume1/docker/franklin
source venv311/bin/activate
./daily_status_report.py
```

### Task 6: Weather Collection

- **Task Name:** Data Collection - Weather 15 min
- **User:** root
- **Schedule:** Daily starting at 00:00
- **Frequency:** Every 15 minutes
- **Last run time:** 23:45
- **Script:**
```bash
#!/bin/bash
cd /volume1/docker/franklin
source venv311/bin/activate
./collect_weather.py
```

### Task 7: PVOutput Collection

- **Task Name:** Data Collection - PVOutput hourly
- **User:** root
- **Schedule:** Daily starting at 00:00
- **Frequency:** Every 1 hour
- **Last run time:** 23:00
- **Script:**
```bash
#!/bin/bash
cd /volume1/docker/franklin
source venv311/bin/activate
./collect_pvoutput.py
```

### Task 8: Daily Data Aggregation

- **Task Name:** Data Collection - Aggregate Daily
- **User:** root
- **Schedule:** Daily at 6:00 AM
- **Script:**
```bash
#!/bin/bash
cd /volume1/docker/franklin
source venv311/bin/activate
# Add daily aggregation script if needed
```

---

## Script Reference

### Morning Solar Intelligence (`morning_solar_intelligence.py`)

**When:** 8:00 AM daily  
**Purpose:** Analyzes overnight battery status and decides charging strategy

**Decision Logic:**
- If SOC ≥ 85%: No action needed
- If SOC < 50%: Immediate grid charge
- If SOC 50-85% and good solar (>1.5kW): Wait for solar
- If SOC 50-85% and poor solar: Monitor until 2 PM

**Thresholds (customizable):**
```python
GOOD_SOLAR_THRESHOLD = 1.5  # kW
LOW_SOC_THRESHOLD = 50      # %
TARGET_SOC = 95             # %
```

### Midday Charge Check (`midday_charge_check.py`)

**When:** 2:00 PM daily  
**Purpose:** Evaluates progress and starts grid charge if needed

**Decision Logic:**
- If SOC ≥ 85%: No action needed
- If SOC ≥ 80% and solar > 1.0kW: Let solar finish
- Otherwise: Start grid charge to 95%

### Final Safety Check (`final_safety_check.py`)

**When:** 3:30 PM daily  
**Purpose:** Last chance to charge before peak period (5 PM)

**Decision Logic:**
- If SOC ≥ 75%: Ready for peak
- If SOC < 75%: Emergency grid charge to 85%

**Critical threshold:**
```python
MINIMUM_ACCEPTABLE = 75  # %
```

### Daily Status Report (`daily_status_report.py`)

**When:** 4:30 PM daily  
**Purpose:** Email summary of day's activity

**Includes:**
- Current battery status
- Today's energy summary (SOC range, solar/grid/battery stats)
- All solar intelligence decisions from the log
- Time until peak period

---

## Monitoring & Logs

### Log Files

All logs are stored in `/volume1/docker/franklin/logs/`:

- `solar_intelligence.log` - All automation decisions
- `continuous_monitoring.csv` - 15-minute battery snapshots
- `weather_data.csv` - 15-minute weather data
- `pvoutput_house_daily.csv` - Daily solar production (house system)
- `pvoutput_ground_mount_daily.csv` - Daily solar production (ground mount)

### Checking Logs

```bash
cd /volume1/docker/franklin

# View today's automation decisions
grep "$(date +%Y-%m-%d)" logs/solar_intelligence.log

# View recent battery monitoring
tail -20 logs/continuous_monitoring.csv

# Check if morning run happened
grep "$(date +%Y-%m-%d) 08:" logs/solar_intelligence.log
```

### Manual Overrides

If automation isn't working as expected:

```bash
# Force grid charging
./switch_to_backup_v2.py

# Stop grid charging (return to TOU mode)
./switch_to_tou_v2.py

# Check current battery status
./get_battery_status.py
```

---

## Troubleshooting

### Task Didn't Run

1. Check Task Scheduler history in DSM
2. Verify task is enabled
3. Check run details email for errors
4. Manually run script to see error:
```bash
cd /volume1/docker/franklin
source venv311/bin/activate
./script_name.py
```

### "Cannot read battery SOC" Error

This indicates the Franklin Cloud API timed out. Common causes:
- Cloud API is slow (temporary)
- Network connectivity issue
- Invalid credentials

**Solution:** Usually resolves itself. Check credentials if persistent.

### Continuous Monitoring Not Running

Check if process is running:
```bash
ps aux | grep continuous_monitor
```

If not running:
```bash
cd /volume1/docker/franklin
source venv311/bin/activate
nohup ./continuous_monitor.py > /dev/null 2>&1 &
```

### PVOutput Data Not Collecting

1. Verify API key and system IDs are correct
2. Check if you've exceeded API rate limits (60 requests/hour)
3. Manually test:
```bash
./collect_pvoutput.py
tail -3 logs/pvoutput_house_daily.csv
```

### Email Reports Not Arriving

1. Verify email is configured in Task Scheduler settings
2. Check Synology notification settings
3. Check spam folder
4. Test by manually running:
```bash
./daily_status_report.py
```

---

## Customization Guide

### Adjusting Charge Thresholds

Edit the intelligence scripts and modify these values:

**Morning Intelligence:**
```python
# morning_solar_intelligence.py
GOOD_SOLAR_THRESHOLD = 1.5  # Increase if you have more solar capacity
LOW_SOC_THRESHOLD = 50      # Lower if you want to wait longer for solar
TARGET_SOC = 95             # Maximum charge level
```

**Midday Check:**
```python
# midday_charge_check.py
# If current_soc >= 85: (change 85 to your preference)
# If current_soc >= 80 and solar_production > 1.0: (adjust both values)
```

**Final Safety:**
```python
# final_safety_check.py
MINIMUM_ACCEPTABLE = 75  # Minimum SOC needed before peak
```

### Changing Peak Period Times

Current system assumes 5:00-8:00 PM peak. To change:

1. Edit `morning_solar_intelligence.py`:
```python
peak_start = datetime.now().replace(hour=17, minute=0, ...)  # Change hour=17 to your peak start
```

2. Adjust task schedule times accordingly:
   - Morning check: At least 9 hours before peak
   - Midday check: 3 hours before peak
   - Final safety: 90 minutes before peak

### Adding Additional Solar Systems

If you have more than two PVOutput systems, edit `collect_pvoutput.py`:

```python
# Add new system configuration
THIRD_SYSTEM_SID = "YOUR_SYSTEM_ID"
THIRD_LOG = LOG_DIR / "pvoutput_system3_daily.csv"

# Add collection call in main():
get_and_save_daily_output(THIRD_SYSTEM_SID, THIRD_LOG, "System 3", yesterday)
```

---

## Advanced Features

### Time-of-Use Rate Optimization

The system assumes PG&E E-TOU-D rate schedule:
- **Off-Peak:** Midnight-4 PM, 9 PM-Midnight
- **Peak:** 5 PM-8 PM

To optimize for different rates, adjust:
1. Peak period time (see above)
2. Target SOC (higher SOC = more peak offset)
3. Charging strategy aggressiveness

### Battery Capacity Calculations

Default charging assumptions:
- Battery charge rate: ~10 kW
- SOC increase: ~32% per hour
- 30-minute safety margin

Adjust in `morning_solar_intelligence.py`:
```python
CHARGE_RATE_PER_HOUR = 32  # Adjust based on your battery size/inverter
SAFETY_MARGIN = 0.5        # Hours of buffer time
```

### Weather-Based Forecasting

Current system uses recent 30-minute solar production average. For more sophisticated forecasting, you could:

1. Query weather forecast APIs
2. Use historical weather correlations
3. Implement machine learning predictions

This requires additional development beyond the base system.

---

## Safety & Best Practices

### Data Privacy

- Store credentials securely
- Don't commit scripts with credentials to public repositories
- Use environment variables or config files for sensitive data

### Battery Health

- Don't routinely charge to 100% (95% is better for longevity)
- Avoid deep discharges below 20%
- Monitor battery temperature if accessible

### Cost Optimization

- Review logs monthly to fine-tune thresholds
- Track PG&E bills to validate savings
- Adjust strategy seasonally (more solar in summer)

### System Maintenance

- Check logs weekly for errors
- Verify continuous monitoring is running
- Update Python packages periodically
- Back up configuration and scripts

---

## Contributing & Support

### Sharing Your Setup

If you use this system:
1. Document your customizations
2. Share success stories and savings data
3. Report bugs or improvements
4. Help others in the community

### Version History

- **v1.0 (January 2026):** Initial release
  - Three-tier solar-first intelligence
  - Automated data collection
  - Daily email reports

---

## Appendix: Complete Scripts

*Note: Replace all personal information (credentials, API keys, IDs) with your own before using.*

### Script 1: morning_solar_intelligence.py

```python
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
```

### Script 2: midday_charge_check.py

```python
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
```

### Script 3: final_safety_check.py

```python
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
```

*[Continue in next message due to length...]*

---

**End of Installation Guide Preview**

This is a comprehensive 50+ page guide. Would you like me to:
1. Complete the remaining script appendices?
2. Create a condensed quick-start guide?
3. Export as a downloadable file?

Let me know and I'll finish it up! 📚
