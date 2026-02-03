# Native Installation Guide

> **Recommended:** For most users, Docker is the easiest path. See [DOCKER_INSTALLATION.md](DOCKER_INSTALLATION.md).
>
> This guide covers native installation for advanced users or environments where Docker is not available.

---

## Prerequisites

- Python 3.11 or newer
- Git
- SSH/terminal access to your server
- Franklin WH account credentials and Gateway ID

---

## Synology NAS

### 1. Install Python and Enable SSH

- **Package Center** → Install "Python 3.11" or newer
- **Control Panel** → **Terminal & SNMP** → Enable SSH

### 2. Connect and Set Up

```bash
ssh admin@YOUR-NAS-IP
sudo -i

# Clone repository
mkdir -p /volume1/docker/franklin-git
cd /volume1/docker/franklin-git
git clone https://github.com/mtnears/FranklinWH-Automation.git .

# Create virtual environment
python3 -m venv venv311
source venv311/bin/activate
pip install --upgrade pip
pip install --break-system-packages -r requirements.txt

# Make scripts executable
chmod +x scripts/*.py

# Configure
cp .env.example .env
nano .env   # Set your credentials and preferences

# Create directories
mkdir -p logs data web
```

### 3. Test

```bash
source venv311/bin/activate
python scripts/smart_decision.py
```

You should see a successful decision with API mode detection and per-battery data.

### 4. Schedule with Task Scheduler

1. **Control Panel** → **Task Scheduler** → **Create** → **Scheduled Task** → **User-defined script**
2. Name: `Smart Battery Decision`
3. User: `root`
4. Schedule: Daily, every 15 minutes (00:00 to 23:45)
5. Script:
   ```bash
   #!/bin/bash
   cd /volume1/docker/franklin-git
   /volume1/docker/franklin-git/scripts/run_smart_decision.sh
   ```

---

## Raspberry Pi / Linux

### 1. Install Dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip git
```

### 2. Set Up

```bash
git clone https://github.com/mtnears/FranklinWH-Automation.git
cd FranklinWH-Automation

python3 -m venv venv311
source venv311/bin/activate
pip install -r requirements.txt

chmod +x scripts/*.py

cp .env.example .env
nano .env   # Configure

mkdir -p logs data web
```

### 3. Test

```bash
source venv311/bin/activate
python scripts/smart_decision.py
```

### 4. Schedule with Cron

```bash
crontab -e

# Add:
*/15 * * * * cd /path/to/FranklinWH-Automation && /path/to/FranklinWH-Automation/scripts/run_smart_decision.sh >> logs/cron.log 2>&1
```

---

## Configuration

All settings are in your `.env` file. See [CONFIGURATION_REFERENCE.md](CONFIGURATION_REFERENCE.md) for complete details.

**Minimum required:**
```bash
FRANKLIN_USERNAME=your_email@example.com
FRANKLIN_PASSWORD=your_password
FRANKLIN_GATEWAY_ID=your_gateway_id
BATTERY_CAPACITY_KWH=30
CHARGE_RATE_PER_HOUR=32
CHECK_INTERVAL_MINUTES=15
PEAK_TRANSITION_BUFFER_MINUTES=5
HOME_MODE=tou
```

---

## Verification

After 24 hours, check the system is working:

```bash
# Count decisions (should be ~96 per day)
grep "$(date +%Y-%m-%d)" logs/solar_intelligence.log | grep "======" | wc -l

# Check for API mode detection (v3.3.0)
grep "API Mode" logs/solar_intelligence.log | tail -3

# Verify no mode changes during peak
grep "SWITCHING" logs/solar_intelligence.log | grep " 1[7-9]:\| 20:0[0-7]"
# Should return nothing
```

---

## Web Dashboard (Native)

See [WEB_DASHBOARD.md](WEB_DASHBOARD.md) for dashboard setup on native installations. Docker includes the dashboard automatically.

---

## Updating

```bash
cd /path/to/FranklinWH-Automation
git pull
source venv311/bin/activate
pip install -r requirements.txt
```

Check the [CHANGELOG.md](CHANGELOG.md) for any new `.env` settings to add.

---

**Last Updated:** February 2026
**Version:** 3.3.0
