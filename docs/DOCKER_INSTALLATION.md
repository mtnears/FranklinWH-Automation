# Docker Installation Guide

**The easiest way to run FranklinWH Automation - single command startup with everything included.**

---

## What's Included

The Docker setup provides a complete, self-contained package:

- ✅ **Automated battery management** - Runs every 15 minutes
- ✅ **Built-in web dashboard** - No separate web server needed
- ✅ **Scheduler included** - All tasks run automatically
- ✅ **Dashboard with 3 tabs** - Live view, Weekly Reports, System Logs
- ✅ **Configurable port** - Default 8100, customizable via `.env`

---

## Prerequisites

- Docker and Docker Compose installed
- Franklin WH account credentials
- Gateway ID (from Franklin mobile app: Settings → System Info)

---

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/mtnears/FranklinWH-Automation.git
cd FranklinWH-Automation
```

### 2. Create Configuration File

```bash
cp .env.example .env
nano .env   # or use: vi .env
```

**Set these required values:**
```bash
FRANKLIN_USERNAME=your_email@example.com
FRANKLIN_PASSWORD=your_password
FRANKLIN_GATEWAY_ID=your_gateway_id
BATTERY_CAPACITY_KWH=30
CHARGE_RATE_PER_HOUR=32
```

### 3. Create Data Directories

```bash
mkdir -p logs data web
```

### 4. Build and Start

```bash
docker compose build
docker compose up -d
```

### 5. Access Dashboard

Open in browser: `http://YOUR-SERVER-IP:8100`

To use a different port, add to your `.env` file:
```bash
DASHBOARD_PORT=8080
```

---

## What's Running

The Docker setup runs two containers:

| Container | Purpose |
|-----------|---------|
| `franklin-automation` | Main automation + internal scheduler |
| `franklin-dashboard` | Nginx web server for dashboard |

### Scheduled Tasks (Automatic)

All tasks run automatically inside the container:

| Task | Frequency |
|------|-----------|
| Smart Decision | Every 15 minutes |
| Dashboard Data | Every 1 minute |
| Weather Collection | Every 15 minutes (if enabled) |
| PVOutput Collection | Hourly (if enabled) |
| Daily Status Report | 4:30 PM (if email enabled) |
| Weekly Charts | Sunday 2:00 AM |

---

## Dashboard Features

The built-in dashboard has three tabs:

### Live Dashboard
- Real-time battery status and SOC
- Energy flow visualization
- Battery states: Charging / Discharging / Standby
- Savings tracker
- Peak countdown timer

### Weekly Reports
- 7-day SOC timeline charts
- Daily summary graphs
- Power flow analysis
- Historical report archive

### System Logs
- Intelligence log (decision making)
- Scheduler log (task execution)
- Monitoring data (CSV table view)
- Auto-refresh every 30 seconds

---

## Managing the System

### View Logs

```bash
# All container logs
docker compose logs -f

# Just automation logs
docker compose logs -f franklin-automation

# Last 100 lines
docker compose logs --tail 100 franklin-automation
```

### Check Status

```bash
docker compose ps
```

### Restart

```bash
docker compose restart
```

### Stop

```bash
docker compose down
```

### Update to Latest Version

```bash
git pull
docker compose down
docker compose build
docker compose up -d
```

---

## Configuration Options

All settings are in your `.env` file. See `.env.example` for all available options.

### Feature Toggles

```bash
SOLAR_ENABLED=true
TOU_ENABLED=true
DYNAMIC_PRICING_ENABLED=false
WEATHER_ENABLED=false
PVOUTPUT_ENABLED=false
EMAIL_ENABLED=false
```

### TOU Peak Period

```bash
PEAK_START_HOUR=17
PEAK_END_HOUR=20
PEAK_DAYS=weekdays
```

### Dashboard Port

```bash
DASHBOARD_PORT=8100   # Default, change if needed
```

### Dynamic Pricing (ComEd)

```bash
DYNAMIC_PRICING_ENABLED=true
PRICING_PROVIDER=comed
PRICE_THRESHOLD_CENTS=4.0
```

---

## File Locations

| Path | Contents |
|------|----------|
| `./logs/` | Log files, scheduler output, weekly charts |
| `./data/` | Savings data and projections |
| `./web/` | Dashboard HTML and JSON data |
| `./.env` | Your configuration (not committed to git) |

---

## Troubleshooting

### Container won't start

```bash
# Check logs for errors
docker compose logs franklin-automation

# Verify .env file exists and has required values
cat .env | grep FRANKLIN
```

### Dashboard shows "Loading..."

```bash
# Check if data file exists
ls -la web/power_dashboard_data.json

# Check automation logs
docker compose logs --tail 50 franklin-automation | grep Dashboard
```

### Dashboard not accessible

```bash
# Check both containers are running
docker compose ps

# Check dashboard container logs
docker compose logs franklin-dashboard

# Test health endpoint
curl http://localhost:8100/health
```

### Logs tab shows "Unable to load log file"

```bash
# Check logs are being written
ls -la logs/

# Verify scheduler log exists (created after first run)
cat logs/scheduler.log
```

### API connection errors

```bash
# Test config loads correctly
docker compose exec franklin-automation python -c "from scripts.config import config; print(config.get_config_summary())"
```

### Permission errors

The container runs as root to avoid permission issues. If you still have problems:

```bash
# Fix ownership of data directories
chmod -R 777 logs data web
```

### Status shows wrong state (Charging when idle)

The dashboard uses ±0.1 kW threshold. If battery power is between -0.1 and +0.1 kW, it shows "Standby". Check the actual value:

```bash
cat web/power_dashboard_data.json | grep current_power
```

---

## Synology NAS Notes

### Install Docker

1. Package Center → Search "Container Manager"
2. Install Container Manager

### SSH Access

```bash
ssh admin@YOUR-NAS-IP
sudo -i
cd /volume1/docker
```

### Clone and Setup

```bash
git clone https://github.com/mtnears/FranklinWH-Automation.git franklin
cd franklin
cp .env.example .env
nano .env  # Configure your settings
mkdir -p logs data web
docker compose build
docker compose up -d
```

### Access Dashboard

Open: `http://YOUR-NAS-IP:8100`

### Disable Conflicting Task Scheduler Jobs

If you previously used native installation, disable those tasks:
1. Control Panel → Task Scheduler
2. Uncheck all Franklin-related tasks
3. Click away from the page and save when prompted

---

## Raspberry Pi Notes

### Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in
```

### Setup

```bash
git clone https://github.com/mtnears/FranklinWH-Automation.git
cd FranklinWH-Automation
cp .env.example .env
nano .env  # Configure
mkdir -p logs data web
docker compose build
docker compose up -d
```

### Access Dashboard

Open: `http://YOUR-PI-IP:8100`

---

## Migrating from Native Installation

If you were running the native (non-Docker) installation:

1. **Backup your data:**
   ```bash
   cp -r /volume1/docker/franklin/logs /volume1/docker/franklin-backup/
   cp -r /volume1/docker/franklin/data /volume1/docker/franklin-backup/
   ```

2. **Disable Task Scheduler jobs** (see Synology notes above)

3. **Copy your existing `.env`** to the new Docker directory

4. **Update paths in `.env`:**
   ```bash
   # Change from:
   BASE_DIR=/volume1/docker/franklin
   LOG_DIR=/volume1/docker/franklin/logs
   
   # To (Docker defaults):
   BASE_DIR=/app
   LOG_DIR=/app/logs
   DATA_DIR=/app/data
   WEB_DIR=/app/web
   ```

5. **Build and start Docker**

6. **Optionally restore historical data:**
   ```bash
   cp /volume1/docker/franklin-backup/logs/*.csv ./logs/
   cp /volume1/docker/franklin-backup/logs/*.log ./logs/
   ```

---

## Support

- Check container logs first: `docker compose logs -f`
- Review [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- Check the System Logs tab in the dashboard
- Open GitHub issue with log excerpts

---

**That's it!** Your Franklin WH battery is now automatically optimized. 🎉

---

**Last Updated:** February 2026  
**Version:** 3.0
