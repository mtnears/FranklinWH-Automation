# Docker Installation Guide

**The easiest way to run FranklinWH Automation — single command startup with everything included.**

---

## What's Included

The Docker setup provides a complete, self-contained package:

- ✅ **Automated battery management** with API-native mode control
- ✅ **Schedule-aware timing** with peak-pinned checks
- ✅ **Per-battery monitoring** for multi-battery systems
- ✅ **Built-in web dashboard** — no separate web server needed
- ✅ **Internal scheduler** — no external cron or Task Scheduler needed
- ✅ **Dashboard with 3 tabs** — Live view, Weekly Reports, System Logs
- ✅ **Configurable everything** via `.env` file

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

**Set your scheduling preferences:**
```bash
CHECK_INTERVAL_MINUTES=15
PEAK_TRANSITION_BUFFER_MINUTES=5
HOME_MODE=tou
```

If you run Self Consumption as your normal mode instead of TOU, set `HOME_MODE=self_consumption`.

### 3. Create Data Directories

```bash
mkdir -p logs data web
```

### 4. Build and Start

```bash
docker compose build
docker compose up -d
```

### 5. Verify

```bash
docker logs franklin-automation 2>&1 | head -25
```

You should see:
- `Scheduler v3.2.0` in the banner
- Your enabled features listed
- `Pre-peak check: Daily at XX:XX` and `Post-peak check: Daily at XX:XX`
- Initial smart decision completing successfully

### 6. Access Dashboard

Open in browser: `http://YOUR-SERVER-IP:8100`

To use a different port, set `DASHBOARD_PORT` in your `.env` file.

---

## What's Running

The Docker setup runs two containers:

| Container | Purpose |
|-----------|---------|
| `franklin-automation` | Main automation + internal scheduler |
| `franklin-dashboard` | Nginx web server for dashboard |

### Scheduled Tasks (Automatic)

All tasks run automatically inside the container:

| Task | Frequency | Description |
|------|-----------|-------------|
| Smart Decision | Every 15 min (configurable) | Core battery management |
| Pre-peak Check | Daily (e.g., 16:55) | Guaranteed check before peak |
| Post-peak Check | Daily (e.g., 20:01) | Resume normal mode after peak |
| Dashboard Data | Every 1 minute | Updates live dashboard |
| Weather Collection | Every 15 minutes (if enabled) | Weather data logging |
| PVOutput Collection | Hourly (if enabled) | Solar production tracking |
| Daily Savings | Daily at 11:55 PM | Savings calculation |
| Weekly Charts | Sunday 2:00 AM | Performance visualization |

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
# Container scheduler logs
docker logs franklin-automation -f

# Intelligence log (decision details)
docker exec franklin-automation tail -30 /app/logs/solar_intelligence.log

# Last 100 lines of container logs
docker logs --tail 100 franklin-automation
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
docker compose build --no-cache
docker compose up -d
```

**Important:** Since scripts are built into the Docker image (not volume-mounted), you must rebuild with `--no-cache` when updating to pick up script changes. A simple `docker restart` is not sufficient for code updates.

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

### Scheduling

```bash
CHECK_INTERVAL_MINUTES=15           # Decision frequency (1-60 min)
PEAK_TRANSITION_BUFFER_MINUTES=5    # Minutes before peak to check
HOME_MODE=tou                       # Normal mode: tou or self_consumption
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

| Host Path | Container Path | Contents |
|-----------|---------------|----------|
| `./logs/` | `/app/logs/` | Log files, charts, CSV data |
| `./data/` | `/app/data/` | Savings data and projections |
| `./web/` | `/app/web/` | Dashboard HTML and JSON data |
| `./.env` | (loaded at build) | Configuration (not in git) |

**Note:** The `logs`, `data`, and `web` directories are volume-mounted, so data persists across container restarts. The `scripts` directory is built into the image — see "Update to Latest Version" above.

---

## Troubleshooting

### Container won't start

```bash
# Check logs for errors
docker logs franklin-automation

# Verify .env file exists and has required values
cat .env | grep FRANKLIN
```

### Scripts not updating after git pull

Scripts are built into the Docker image, not volume-mounted. You must rebuild:

```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```

### Dashboard shows "Loading..."

```bash
# Check if data file exists
docker exec franklin-automation ls -la /app/web/power_dashboard_data.json

# Check automation logs
docker logs --tail 50 franklin-automation | grep Dashboard
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
docker exec franklin-automation ls -la /app/logs/

# Check intelligence log exists
docker exec franklin-automation tail -5 /app/logs/solar_intelligence.log
```

### API connection errors

```bash
# Check recent decisions for retry patterns
docker exec franklin-automation tail -20 /app/logs/solar_intelligence.log
```

### Permission errors

The container runs as root to avoid permission issues. If you still have problems:

```bash
chmod -R 777 logs data web
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
```

### Clone and Setup

```bash
cd /volume1/docker
git clone https://github.com/mtnears/FranklinWH-Automation.git franklin-git
cd franklin-git
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
3. Docker's internal scheduler handles everything now

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

## Support

- Check container logs first: `docker logs franklin-automation -f`
- Review [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- Check the System Logs tab in the dashboard
- Open GitHub issue with log excerpts

---

**That's it!** Your Franklin WH battery is now automatically optimized. 🎉

---

**Last Updated:** February 2026
**Version:** 3.2.0
