# Installation Guide

**Complete step-by-step installation for Franklin WH Battery Automation**

This guide covers fresh installations on Synology NAS and Linux systems. If you're migrating from the old three-tier system, see [MIGRATION_V1_TO_V2.md](MIGRATION_V1_TO_V2.md).

---

## Table of Contents

- [Before You Begin](#before-you-begin)
- [Synology NAS Installation](#synology-nas-installation)
- [Raspberry Pi / Linux Installation](#raspberry-pi--linux-installation)
- [Configuration](#configuration)
- [Testing](#testing)
- [Optional Components](#optional-components)
- [Verification](#verification)
- [Next Steps](#next-steps)

---

## Before You Begin

### Prerequisites

**Hardware:**
- Franklin WH battery system with cloud access enabled
- 24/7 server (Synology NAS, Raspberry Pi, or Linux server)
- Stable internet connection

**Software:**
- Python 3.11 or newer
- Git (for cloning repository)
- SSH/terminal access to your server

**Accounts:**
- Franklin WH account credentials
- Email account for notifications (optional)
- Weather Underground API key (optional)
- PVOutput account (optional)

### Gather Information

Before starting, collect:

1. **Franklin WH Credentials:**
   - Username (email): `________@______`
   - Password: `________________`
   - Gateway ID: `____________________`
     - Find in Franklin WH mobile app: Settings → System Info

2. **Your TOU Peak Period:**
   - Peak start hour: `____ PM` (e.g., 5 PM = hour 17)
   - Peak end hour: `____ PM` (e.g., 8 PM = hour 20)

3. **Installation Path** (recommendations):
   - Synology: `/volume1/docker/franklin`
   - Linux: `/opt/franklin` or `/home/pi/franklin`

---

## Synology NAS Installation

**Recommended for:** Users wanting 24/7 reliable operation without dedicated hardware.

### Step 1: Install Python

1. Open **Package Center**
2. Search for "Python 3.11" or newer
3. Click **Install**
4. Wait for installation to complete

### Step 2: Enable SSH

1. **Control Panel** → **Terminal & SNMP**
2. Check **Enable SSH service**
3. Click **Apply**

### Step 3: Connect via SSH

```bash
# From your computer's terminal
ssh admin@YOUR-NAS-IP

# Enter admin password when prompted
# Switch to root
sudo -i
```

### Step 4: Create Project Directory

```bash
# Create directory
mkdir -p /volume1/docker/franklin
cd /volume1/docker/franklin

# Verify location
pwd
# Should output: /volume1/docker/franklin
```

### Step 5: Clone Repository

```bash
# Install git if needed
# (Usually pre-installed on Synology DSM 7+)

# Clone repository
git clone https://github.com/YOUR-USERNAME/FranklinWH-Automation.git .

# Verify files
ls -la
# Should see: scripts/, docs/, README.md, requirements.txt, etc.
```

### Step 6: Set Up Python Environment

```bash
# Create virtual environment
python3 -m venv venv311

# Activate environment
source venv311/bin/activate

# Upgrade pip
pip install --upgrade pip

# Install dependencies
pip install --break-system-packages -r requirements.txt

# Verify installation
pip list
# Should see: franklinwh, requests, and dependencies
```

**Note:** The `--break-system-packages` flag is required on Synology DSM 7.2+.

### Step 7: Set Permissions

```bash
# Make scripts executable
chmod +x scripts/*.py
chmod +x scripts/*.sh

# Verify
ls -la scripts/
# Should show: -rwxr-xr-x for .py and .sh files
```

### Step 8: Configure Scripts

See [Configuration](#configuration) section below.

### Step 9: Set Up Task Scheduler

See [Task Scheduler Setup](TASK_SCHEDULER.md) for detailed instructions.

**Quick version:**
1. **Control Panel** → **Task Scheduler** → **Create** → **User-defined script**
2. Name: `Smart Battery Decision - Every 15 minutes`
3. User: `root`
4. Schedule: Daily, every 15 minutes (00:00 to 23:45)
5. Script:
   ```bash
   #!/bin/bash
   cd /volume1/docker/franklin
   /volume1/docker/franklin/scripts/run_smart_decision.sh
   ```

---

## Raspberry Pi / Linux Installation

**Recommended for:** Users comfortable with Linux and want dedicated automation hardware.

### Step 1: Update System

```bash
sudo apt update
sudo apt upgrade -y
```

### Step 2: Install Python 3.11+

**Ubuntu/Debian:**
```bash
sudo apt install -y python3.11 python3.11-venv python3-pip git
```

**Raspberry Pi OS:**
```bash
# Python 3.11 should be included in recent versions
python3 --version

# If < 3.11, use:
sudo apt install -y python3 python3-venv python3-pip git
```

### Step 3: Create Project Directory

```bash
# Create directory
sudo mkdir -p /opt/franklin
sudo chown $USER:$USER /opt/franklin
cd /opt/franklin
```

**Or use home directory:**
```bash
mkdir -p ~/franklin
cd ~/franklin
```

### Step 4: Clone Repository

```bash
git clone https://github.com/YOUR-USERNAME/FranklinWH-Automation.git .
```

### Step 5: Set Up Python Environment

```bash
# Create virtual environment
python3 -m venv venv311

# Activate
source venv311/bin/activate

# Install dependencies
pip install -r requirements.txt

# Verify
pip list | grep franklinwh
```

### Step 6: Set Permissions

```bash
chmod +x scripts/*.py
chmod +x scripts/*.sh
```

### Step 7: Configure Scripts

See [Configuration](#configuration) section below.

### Step 8: Set Up Cron Job

See [Task Scheduler Setup](TASK_SCHEDULER.md) for detailed instructions.

**Quick version:**
```bash
# Edit crontab
crontab -e

# Add this line (adjust path as needed):
*/15 * * * * cd /opt/franklin && /opt/franklin/scripts/run_smart_decision.sh >> /opt/franklin/logs/cron.log 2>&1
```

---

## Configuration

### 1. Configure Franklin WH Credentials

Edit all core scripts to add your credentials.

**Files to edit:**
- `scripts/smart_decision.py`
- `scripts/switch_to_backup_v2.py`
- `scripts/switch_to_tou_v2.py`
- `scripts/get_battery_status.py`

**In each file, replace:**
```python
USERNAME = "YOUR_EMAIL@example.com"  # Your Franklin WH email
PASSWORD = "YOUR_PASSWORD"            # Your Franklin WH password
GATEWAY_ID = "YOUR_GATEWAY_ID"       # From Franklin mobile app
```

**Example:**
```python
USERNAME = "john.smith@gmail.com"
PASSWORD = "MySecurePass123!"
GATEWAY_ID = "10060005A02X24470437"
```

### 2. Configure Peak Period Hours

Edit `scripts/smart_decision.py`:

```python
# Adjust these for your utility's TOU schedule
PEAK_START_HOUR = 17  # 5 PM (use 24-hour format)
PEAK_END_HOUR = 20    # 8 PM
```

**Common TOU Schedules:**
- **PG&E E-TOU-D:** 5 PM - 8 PM → `17` to `20`
- **SCE TOU-D-4-9PM:** 4 PM - 9 PM → `16` to `21`
- **SDG&E TOU-DR1:** 4 PM - 9 PM → `16` to `21`

### 3. Configure Charging Parameters (Optional)

Advanced users can adjust decision logic in `scripts/smart_decision.py`:

```python
TARGET_SOC = 95.0                 # Target charge before peak (%)
CHARGE_RATE_PER_HOUR = 32.0       # Your battery's grid charge rate
SAFETY_MARGIN_HOURS = 0.5         # Buffer time before must-charge deadline
MIN_SOLAR_FOR_WAIT = 0.5          # Min solar (kW) to wait vs grid charge
```

### 4. Create Log Directory

```bash
mkdir -p logs
```

On Synology:
```bash
mkdir -p /volume1/docker/franklin/logs
```

### 5. Initialize State Files

```bash
# Create initial peak state file
echo "OffPeak-$(date +%Y-%m-%d)" > logs/peak_state.txt

# Create initial mode file
echo "TOU" > logs/last_mode.txt
```

---

## Testing

### Test Core Automation

```bash
cd /volume1/docker/franklin  # Or your install path
source venv311/bin/activate
./scripts/smart_decision.py
```

**Expected output:**
```
Attempting to get battery stats (max 5 attempts)...
Attempt 1 starting...
✓ Success on first attempt
======================================================================
SOC: 67.3%, Solar: 2.145kW, Status: 8.5h to peak
Decision: Solar can provide ~15.2% (need 27.7%), 2.145kW looks promising
Action: Solar-first (TOU mode)
Mode unchanged: TOU
✓ Decision made: TOU mode (Solar can provide ~15.2%...)
```

**If you see errors:**
- `ModuleNotFoundError: No module named 'franklinwh'` → Activate venv: `source venv311/bin/activate`
- `Authentication failed` → Check USERNAME and PASSWORD
- `Gateway not found` → Check GATEWAY_ID
- `Connection timeout` → Check internet connection and Franklin WH system status

### Test Mode Switching

```bash
# Test switching to BACKUP mode (grid charging)
./scripts/switch_to_backup_v2.py

# Expected output:
# Authenticating with Franklin WH...
# Creating client...
# Switching to Emergency Backup mode...
# ✓ Successfully switched to Emergency Backup mode

# Switch back to TOU
./scripts/switch_to_tou_v2.py
```

### Test Battery Status

```bash
./scripts/get_battery_status.py

# Expected output:
# ==================================================
# FRANKLIN BATTERY STATUS
# ==================================================
# Battery SOC:        67.1%
# Solar Production:   2.340 kW
# Grid Use:           0.145 kW
# Battery Use:        -1.897 kW
# Home Load:          0.588 kW
# Grid Status:        normal
# ==================================================
```

### Check Logs

```bash
# View intelligence log
cat logs/solar_intelligence.log

# Should show recent decision
# Example:
# 2026-01-04 10:23:45 - ======================================================================
# 2026-01-04 10:23:45 - SOC: 67.3%, Solar: 2.145kW, Status: 6.5h to peak
# 2026-01-04 10:23:45 - Decision: Solar can provide ~12.8%...
```

---

## Optional Components

### Weather Data Collection

**Benefits:** Historical weather correlation with solar production.

**Setup:**
1. Sign up for Weather Underground API: https://www.wunderground.com/member/api-keys
2. Get your Personal Weather Station (PWS) ID
3. Edit `scripts/collect_weather.py`:
   ```python
   PWS_ID = "YOUR_STATION_ID"      # e.g., "KCAGEORG58"
   API_KEY = "YOUR_WU_API_KEY"
   ```
4. Set up task to run every 15 minutes:
   ```bash
   #!/bin/bash
   cd /volume1/docker/franklin
   source venv311/bin/activate
   ./scripts/collect_weather.py
   ```

### PVOutput Solar Tracking

**Benefits:** Track daily solar production trends over time.

**Setup:**
1. Create free account: https://pvoutput.org/register.jsp
2. Add your solar system(s)
3. Get API key: Account Settings → API Settings
4. Edit `scripts/collect_pvoutput.py`:
   ```python
   API_KEY = "YOUR_PVOUTPUT_API_KEY"
   GROUND_MOUNT_SID = "YOUR_SYSTEM_ID"
   # If you have a second system:
   HOUSE_SID = "YOUR_SECOND_SYSTEM_ID"
   ```
5. Set up hourly task

### Email Notifications

**For testing/monitoring during setup:**

Edit `scripts/milestone_emailer.py`:
```python
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "your-email@gmail.com"
SENDER_PASSWORD = "your-app-password"  # Gmail App Password
RECIPIENT_EMAIL = "your-email@gmail.com"
MILESTONES = [8, 10, 12, 14, 16]  # Hours to send emails
```

**Gmail App Password:**
1. Google Account → Security
2. 2-Step Verification → App passwords
3. Generate password for "Mail"

---

## Verification

### After 24 Hours

Check that the system is working correctly:

**1. Task is running every 15 minutes:**
```bash
grep "$(date +%Y-%m-%d)" logs/solar_intelligence.log | wc -l
# Should show ~96 entries (24 hours × 4 per hour)
```

**2. Peak transitions logged:**
```bash
grep "Peak period" logs/solar_intelligence.log
# Should show:
# 17:00:XX - 📊 Peak period started: Peak-YYYY-MM-DD
# 20:00:XX - 📊 Peak period ended: OffPeak-YYYY-MM-DD
```

**3. No mode changes during peak:**
```bash
grep "$(date +%Y-%m-%d) 1[7-9]:" logs/solar_intelligence.log | grep "SWITCHING"
# Should return NOTHING
```

**4. CSV data being logged:**
```bash
tail -5 logs/continuous_monitoring.csv
# Should show recent 15-minute intervals
```

**5. Battery reaching target before peak:**
```bash
# Check SOC at 4:45 PM (before peak)
grep "$(date +%Y-%m-%d) 16:45:" logs/solar_intelligence.log
# SOC should be ≥90% on sunny days
```

---

## Next Steps

### Fine-Tuning

After a week of operation, consider adjusting:

1. **TARGET_SOC** - Lower to 90% if consistently reaching 95% early
2. **SAFETY_MARGIN_HOURS** - Reduce if overly conservative
3. **MIN_SOLAR_FOR_WAIT** - Adjust based on your solar capacity

### Monitoring

Set up optional monitoring:
- Daily status report at 4:30 PM
- Hourly milestone emails (during setup)
- Data aggregation for analysis

See [TASK_SCHEDULER.md](TASK_SCHEDULER.md) for details.

### Documentation

Bookmark these docs:
- [PEAK_STATE_LOGIC.md](PEAK_STATE_LOGIC.md) - How peak protection works
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) - Common issues
- [Task Scheduler Setup](TASK_SCHEDULER.md) - Scheduler configuration

---

## Troubleshooting Installation

### Python version too old

```bash
python3 --version
# If < 3.11, install newer version or use system Python if ≥3.9
```

### pip install fails

```bash
# Try with --break-system-packages (Synology)
pip install --break-system-packages franklinwh

# Or create virtual environment first
python3 -m venv venv311
source venv311/bin/activate
pip install franklinwh
```

### Permission denied

```bash
# Make scripts executable
chmod +x scripts/*.py
chmod +x scripts/*.sh

# Or run with Python explicitly
python3 scripts/smart_decision.py
```

### Task doesn't run

**Synology:**
- Check Task Scheduler history for errors
- Verify script path is absolute
- Test script manually first

**Linux/Cron:**
```bash
# Check cron is running
sudo systemctl status cron

# View cron logs
grep CRON /var/log/syslog

# Test cron entry manually
/opt/franklin/scripts/run_smart_decision.sh
```

---

## Support

**Need help?**
- Check [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- Review logs: `tail -100 logs/solar_intelligence.log`
- Open GitHub issue with:
  - Your platform (Synology model, Linux distro, etc.)
  - Error messages
  - Log excerpts (sanitize credentials!)

---

**Installation complete!** Your Franklin WH battery is now intelligently automated. 🎉

Monitor the logs for the first few days to ensure everything is working as expected.

---

**Last Updated:** January 4, 2026  
**Version:** 2.0
