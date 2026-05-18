# Docker Installation Guide

**The easiest way to run FranklinWH Automation — single command startup with everything included.**

---

## What's Included

The Docker setup provides a complete, self-contained package:

- ✅ **v4 Adaptive Decision Engine** with 8-phase priority system
- ✅ **Solar forecast engine** with Open-Meteo integration
- ✅ **Hybrid data collection** — Modbus TCP local + cloud API
- ✅ **SQLite database** — all data stored locally, no CSV files
- ✅ **Built-in web dashboard** with interactive Plotly.js analytics
- ✅ **Internal scheduler** — no external cron or Task Scheduler needed
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
BATTERY_CAPACITY_KWH=13.6
ADAPTIVE_ENGINE_ENABLED=true
```

**Set your TOU schedule:**
```bash
PEAK_START_HOUR=17
PEAK_END_HOUR=20
PEAK_DAYS=weekdays
HOME_MODE=tou
```

If you run Self Consumption as your normal mode instead of TOU, set `HOME_MODE=self_consumption`.

> **For three-tier rate plans (peak / partial-peak / off-peak) or plans with seasonal rate switching**, configure `data/rate_schedule.json` instead of (or in addition to) the `.env` PEAK_* vars. The shipped `rate_schedule.json` is pre-configured for PG&E EV2-A; copy from `data/rate_schedule.example.json` for other plans (E-TOU-D, SMUD, SCE, Pepco, ComEd). See [CONFIGURATION_REFERENCE.md](CONFIGURATION_REFERENCE.md#rate-schedule-rate_schedulejson) for full details.

### 3. Create Data Directories

```bash
mkdir -p logs data web
```

### 4. Build and Start

```bash
docker compose build --no-cache
docker compose up -d
```

### 5. Verify

```bash
docker logs franklin-automation 2>&1 | head -25
```

You should see:
- `FranklinWH Automation Scheduler` with the version number
- Your enabled features listed
- Scheduled tasks with their run times
- Initial smart decision completing successfully

### 6. Access Dashboard

Open in browser: `http://YOUR-SERVER-IP:8100`

To use a different port, set `DASHBOARD_PORT` in your `.env` file.

---

## What's Running

The Docker setup runs two containers:

| Container | Purpose |
|-----------|---------|
| `franklin-automation` | Main automation + internal scheduler + API server |
| `franklin-dashboard` | Nginx web server for dashboard |

### Scheduled Tasks (Automatic)

All tasks run automatically inside the container:

| Task | Frequency | Description |
|------|-----------|-------------|
| Smart Decision | Every 10-30 min (auto) | Core battery management (v4 adaptive engine) |
| Pre-peak Check | Daily (e.g., 16:55) | Guaranteed check before peak |
| Post-peak Check | Daily (e.g., 20:01) | Resume normal mode after peak |
| Modbus Collection | Every 5 min | Local hardware data (only if `MODBUS_ENABLED=true`) |
| Enphase Collection | Every 5 min | House solar production (if configured) |
| Dashboard Data | Every 1 min | Updates live dashboard |
| Weather Collection | Every 15 min | Weather data logging (if enabled) |
| SolarEdge Panels | Every 15 min | Barn panel health (if enabled) |
| PVOutput Upload | Hourly | Solar production tracking (if enabled) |
| Daily Energy Rollup | Daily at 11:55 PM | Daily energy summary calculation |
| Daily Savings | Daily at 11:58 PM | Savings calculation |
| Device Inventory | Daily at 2:00 AM | Hardware serial/firmware tracking |
| System Profile | Weekly (Sunday 3 AM) | Battery charge curve rebuild |
| Panel Health Report | Daily at 8:30 PM | SolarEdge optimizer health (if enabled) |
| Telemetry | Daily at 3:00 AM | Anonymous usage stats (if opted in) |

---

## Dashboard Features

The built-in dashboard has five tabs:

### Live Dashboard
- Real-time battery status and SOC
- Energy flow visualization (solar, grid, battery, home)
- Battery states: Charging / Discharging / Standby
- Peak countdown timer
- System health indicators

### Analytics
- Interactive Plotly.js charts with zoom, pan, hover tooltips
- Date range selection and carousel navigation
- SOC timeline, power flow, solar production, savings
- Touch-optimized for tablet displays
- All data sourced from SQLite

### Script Status
- Real-time health monitoring of all scheduled scripts
- Success/fail counts, last run time, error history
- At-a-glance system health

### System Info
- Per-battery SOC and power output
- Environment data (temperature, signal strength)
- Today's energy totals and lifetime totals
- Hardware status
- Mode override controls with auto-expiring timers
- Version info with update-available badge
- One-click diagnostic bundle for issue reporting

### System Logs
- Intelligence log (engine decisions from SQLite)
- Scheduler log (task execution)
- Auto-refresh every 30 seconds

---

## Managing the System

### View Logs

```bash
docker logs franklin-automation -f

docker logs --tail 100 franklin-automation
```

### Check Recent Engine Decisions

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log ORDER BY id DESC LIMIT 20\").fetchall(); [print(r) for r in rows]"
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

**Important:** Scripts are built into the Docker image (not volume-mounted). You must rebuild with `--no-cache` when updating to pick up code changes. A simple `docker restart` is not sufficient for code updates.

---

## Configuration Options

All settings are in your `.env` file. See [CONFIGURATION_REFERENCE.md](CONFIGURATION_REFERENCE.md) for the full reference.

### Feature Toggles

```bash
ADAPTIVE_ENGINE_ENABLED=true     # v4 adaptive engine (recommended)
SOLAR_ENABLED=true               # Solar-first charging logic
TOU_ENABLED=true                 # TOU peak protection
MODBUS_ENABLED=false             # Local Modbus TCP (100x faster)
DYNAMIC_PRICING_ENABLED=false    # Hourly pricing (ComEd, etc.)
WEATHER_ENABLED=false            # Weather data collection
PVOUTPUT_ENABLED=false           # PVOutput solar tracking
```

### Scheduling

```bash
CHECK_INTERVAL_MINUTES=0              # 0 = auto-calculate
PEAK_TRANSITION_BUFFER_MINUTES=5      # Minutes before peak to check
HOME_MODE=tou                         # Normal mode: tou or self_consumption
```

### TOU Peak Period

```bash
PEAK_START_HOUR=17
PEAK_END_HOUR=20
PEAK_DAYS=weekdays
```

### Solar Forecast

```bash
FORECAST_ENABLED=true
FORECAST_LATITUDE=38.91
FORECAST_LONGITUDE=-120.84
FORECAST_HOUSE_TILT=22
FORECAST_HOUSE_AZIMUTH=0            # 0=South, -90=East, 90=West
FORECAST_HOUSE_KWP=6.96             # Total panel watts / 1000
```

### Modbus TCP

```bash
MODBUS_ENABLED=true
MODBUS_HOST=192.168.x.x             # Your aGate's IP
MODBUS_PORT=502
```

### Dashboard Port

```bash
DASHBOARD_PORT=8100   # Default, change if needed
```

---

## File Locations

| Host Path | Container Path | Contents |
|-----------|---------------|----------|
| `./logs/` | `/app/logs/` | Log files |
| `./data/` | `/app/data/` | SQLite database (`franklin.db`), rate schedule, forecast cache |
| `./web/` | `/app/web/` | Dashboard HTML and JSON data |
| `./.env` | (loaded at build) | Configuration (not in git) |

**Note:** The `logs`, `data`, and `web` directories are volume-mounted, so data persists across container restarts. The `scripts` directory is built into the image — see "Update to Latest Version" above.

---

## Troubleshooting

### Container won't start

```bash
docker logs franklin-automation

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
docker exec franklin-automation ls -la /app/web/power_dashboard_data.json

docker logs --tail 50 franklin-automation | grep Dashboard
```

### Dashboard not accessible

```bash
docker compose ps

docker compose logs franklin-dashboard

curl http://localhost:8100/health
```

### API connection errors

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log WHERE message LIKE '%error%' OR message LIKE '%failed%' ORDER BY id DESC LIMIT 10\").fetchall(); [print(r) for r in rows]"
```

### Check data source health

```bash
docker exec franklin-automation cat /app/logs/data_source_health.json | python3 -m json.tool
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
nano .env
mkdir -p logs data web
docker compose build --no-cache
docker compose up -d
```

### Access Dashboard

Open: `http://YOUR-NAS-IP:8100`

### Disable Conflicting Task Scheduler Jobs

If you previously used native installation, disable those tasks:
1. Control Panel → Task Scheduler
2. Uncheck all Franklin-related tasks
3. Docker's internal scheduler handles everything now

### DSM Firewall — Allow Docker Bridge Subnets

If the build fails with DNS or network errors, or the container starts but cannot reach external APIs, your DSM Firewall is likely blocking Docker's bridge network. Add an allow rule:

1. Control Panel → Security → Firewall → Edit Rules
2. Add a new rule **above** any "Allow LAN only" or deny rules:
   - Ports: All
   - Source IP: Specific IP / Subnet
   - IP: `172.16.0.0`, Subnet mask: `255.240.0.0`
   - Action: Allow
3. Save and apply

This covers all Docker bridge subnets (172.16.0.0 through 172.31.255.255). See [TROUBLESHOOTING.md](TROUBLESHOOTING.md#synology--docker-bridge-subnet-blocked-by-dsm-firewall) for related DNS daemon config if the build still fails after the firewall rule is in place.

---

## Raspberry Pi Notes

### Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```

Log out and back in, then:

```bash
git clone https://github.com/mtnears/FranklinWH-Automation.git
cd FranklinWH-Automation
cp .env.example .env
nano .env
mkdir -p logs data web
docker compose build --no-cache
docker compose up -d
```

### Access Dashboard

Open: `http://YOUR-PI-IP:8100`

---

## Support

- Check container logs: `docker logs franklin-automation -f`
- Review [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- Check the System Logs tab in the dashboard
- Open a [GitHub Issue](https://github.com/mtnears/FranklinWH-Automation/issues) with log excerpts

---

**Last Updated:** May 2026
**Version:** 4.4.1
