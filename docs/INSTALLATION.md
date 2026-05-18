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

mkdir -p /volume1/docker/franklin-git
cd /volume1/docker/franklin-git
git clone https://github.com/mtnears/FranklinWH-Automation.git .

python3 -m venv venv311
source venv311/bin/activate
pip install --upgrade pip
pip install --break-system-packages -r requirements.txt

chmod +x scripts/*.py

cp .env.example .env
nano .env

mkdir -p logs data web
```

### 3. Test

```bash
source venv311/bin/activate
python scripts/smart_decision.py
```

You should see a successful decision with the v4 adaptive engine priority level and data source in the output.

### 4. Schedule with Task Scheduler

The native install requires external scheduling since there is no built-in scheduler. Create tasks in Synology Task Scheduler for:

**Smart Decision (every 30 minutes):**
1. Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script
2. Schedule: Every 30 minutes
3. Script:
   ```bash
   cd /volume1/docker/franklin-git && ./venv311/bin/python scripts/smart_decision.py
   ```

**Dashboard Data (every 1 minute):**
   ```bash
   cd /volume1/docker/franklin-git && ./venv311/bin/python scripts/generate_dashboard_data.py
   ```

**Daily Energy Rollup (daily at 11:55 PM):**
   ```bash
   cd /volume1/docker/franklin-git && ./venv311/bin/python scripts/rollup_daily_energy.py
   ```

**Daily Savings (daily at 11:58 PM):**
   ```bash
   cd /volume1/docker/franklin-git && ./venv311/bin/python scripts/calculate_daily_savings.py
   ```

Note: The Docker installation includes an internal scheduler that handles all of these automatically. Native installs miss some convenience features like automatic pre-peak/post-peak checks and the web API server.

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
nano .env

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

# Smart decision every 30 minutes
*/30 * * * * cd /path/to/FranklinWH-Automation && ./venv311/bin/python scripts/smart_decision.py >> logs/cron.log 2>&1

# Dashboard data every minute
* * * * * cd /path/to/FranklinWH-Automation && ./venv311/bin/python scripts/generate_dashboard_data.py >> logs/cron.log 2>&1

# Daily energy rollup at 11:55 PM
55 23 * * * cd /path/to/FranklinWH-Automation && ./venv311/bin/python scripts/rollup_daily_energy.py >> logs/cron.log 2>&1

# Daily savings at 11:58 PM
58 23 * * * cd /path/to/FranklinWH-Automation && ./venv311/bin/python scripts/calculate_daily_savings.py >> logs/cron.log 2>&1
```

---

## Configuration

All settings are in your `.env` file. See [CONFIGURATION_REFERENCE.md](CONFIGURATION_REFERENCE.md) for complete details.

**Minimum required:**
```bash
FRANKLIN_USERNAME=your_email@example.com
FRANKLIN_PASSWORD=your_password
FRANKLIN_GATEWAY_ID=your_gateway_id
BATTERY_CAPACITY_KWH=13.6
ADAPTIVE_ENGINE_ENABLED=true
PEAK_START_HOUR=17
PEAK_END_HOUR=20
PEAK_DAYS=weekdays
HOME_MODE=tou
```

The `PEAK_*` vars above work for simple single-peak two-tier plans. **For three-tier plans (e.g., PG&E EV2-A) or plans with seasonal rate switching**, configure `data/rate_schedule.json` instead — see [CONFIGURATION_REFERENCE.md](CONFIGURATION_REFERENCE.md#rate-schedule-rate_schedulejson).

---

## Verification

After 24 hours, check the system is working:

```bash
source venv311/bin/activate

# Check recent decisions in the database
python3 -c "import sqlite3; conn=sqlite3.connect('data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log WHERE message LIKE 'Decision%' ORDER BY id DESC LIMIT 5\").fetchall(); [print(r) for r in rows]"

# Check daily energy data
python3 -c "import sqlite3; conn=sqlite3.connect('data/franklin.db'); rows=conn.execute(\"SELECT date, solar_kwh, grid_import_kwh, home_load_kwh FROM daily_energy_summary ORDER BY date DESC LIMIT 3\").fetchall(); [print(r) for r in rows]"
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

Check the [CHANGELOG.md](../CHANGELOG.md) for any new `.env` settings to add.

---

**Last Updated:** May 2026
**Version:** 4.4.1
