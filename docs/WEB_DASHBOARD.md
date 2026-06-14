# Web Dashboard Setup Guide

The FranklinWH Automation includes a web-based dashboard for real-time monitoring of your battery system, energy flow, analytics, and system health.

## Features

### Live Dashboard Tab
- **Real-Time Battery Status**: SOC, charging/discharging/standby state, current power
- **Energy Flow Visualization**: Power flowing between solar, grid, battery, and home
- **Battery States**: Three distinct states with color-coded badges:
  - 🟢 **Charging** (green) — Battery receiving power (solar or grid)
  - 🟠 **Discharging** (orange) — Battery powering home
  - ⚪ **Standby** (gray) — Battery idle (±100W threshold)
- **Peak Countdown**: Time remaining until the next expensive window starts (peak or partial-peak on three-tier plans), or "PEAK ACTIVE" / "PARTIAL-PEAK ACTIVE" indicator
- **Savings Tracker**: Daily, monthly, and projected annual savings
- **Auto-Refresh**: Dashboard updates every 15 seconds

### Analytics Tab
- **Interactive Plotly.js Charts**: Zoom, pan, hover tooltips, and touch support
- **Date Range Selection**: Pick any date range for analysis
- **Carousel Navigation**: Swipe or click through chart types
- **Chart Types**: SOC timeline, power flow, solar production, savings analysis
- **Rate Window Overlays**: Amber bands mark expensive windows on time-series charts — darker for sacred peak, lighter for partial-peak (three-tier plans only)
- **Data Source**: All charts sourced directly from SQLite database
- **Touch Optimized**: Works well on Fire HD 10 tablet and other touch devices

### Script Status Tab
- **Real-Time Health Monitoring**: All scheduled scripts with current status
- **Success/Fail Counts**: Historical success and failure rates per script
- **Error History**: Recent errors for quick diagnosis
- **At-a-Glance**: System health overview

### System Info Tab
- **Per-Battery SOC**: Individual state of charge for each battery in the system
- **Environment**: Ambient temperature, cabinet temperature, cellular signal, WiFi signal
- **Energy Totals**: Today's solar generation, grid import/export, load, battery charge/discharge
- **Lifetime Totals**: Cumulative energy by source (battery, grid, solar, generator)
- **Charging Breakdown**: Grid-to-battery vs solar-to-battery rates
- **Hardware Status**: BMS, power electronics, main switch, generator, V2L mode
- **Mode Override Controls**: Manual Self Consumption / Emergency Backup with four exit conditions — until SOC reached, for custom duration (h/m), until specific time, or until manually canceled
- **About Card**: Version display with "Update Available" badge (checks GitHub releases daily)
- **Diagnostic Bundle**: One-click sanitized diagnostic report for issue reporting

### System Logs Tab
- **Intelligence Log**: Engine decisions from SQLite database
- **Scheduler Log**: Task execution history and timing
- **Auto-Refresh**: Logs update every 30 seconds when tab is active
- **Report Issue Button**: Generates sanitized diagnostic bundle with credentials stripped

### Settings Tab (v4.6)
- **Dashboard Settings**: Browser-side preferences (refresh interval, temperature unit, theme, target display)
- **Automation Summary**: Live read-only view from the configuration store — active charging strategy, today's peak window and rates, battery configuration, and the grid-charge ceiling vs. solar target
- **Configuration Health**: Validates your setup and flags conflicts — peak-window mismatches (engine vs. schedule), `SOLAR_EXPORT`-vs-net-metering mismatches, seasonal coverage gaps, unreviewed arrays, and capacity sanity
- **Rate Plan & Solar Arrays**: Your active plan with seasons and tier rates; each array with its capacity and whether it charges the battery
- **Full Configuration**: Every setting by category, with each value's source (explicitly set vs. never-reviewed default); secrets masked
- Read-only in v4.6 — editing from the UI and a guided setup wizard are planned on this foundation

---

## Docker Installation (Recommended)

If using Docker, the dashboard is **automatically included**. No additional setup needed.

```bash
docker compose up -d
# Dashboard available at http://YOUR-IP:8100
```

To use a different port, add to your `.env` file:
```bash
DASHBOARD_PORT=8080
```

See [DOCKER_INSTALLATION.md](DOCKER_INSTALLATION.md) for complete Docker setup.

---

## Native Installation

### 1. Copy Files to Web Directory

**For Synology NAS (Web Station):**
```bash
cp web/power_dashboard.html /volume1/web/
```

**For other web servers:**
```bash
cp web/power_dashboard.html /var/www/html/
```

### 2. Schedule Data Generation

The dashboard reads from `power_dashboard_data.json`. For native installs, schedule the data generator to run every minute:

**Synology Task Scheduler:**
1. Control Panel → Task Scheduler → Create → Scheduled Task
2. Schedule: Every 1 minute
3. Command:
   ```bash
   cd /volume1/docker/franklin-git
   source venv311/bin/activate
   python scripts/generate_dashboard_data.py
   ```

**Linux Cron:**
```bash
* * * * * cd /path/to/FranklinWH-Automation && ./venv311/bin/python scripts/generate_dashboard_data.py
```

### 3. Access the Dashboard

Open in your browser:
- **Docker:** `http://YOUR-SERVER-IP:8100`
- **Synology (native):** `http://YOUR-NAS-IP/power_dashboard.html`
- **Linux (native):** `http://YOUR-SERVER-IP/power_dashboard.html`

---

## Dashboard Tabs Detail

### Live Dashboard

| Section | Information |
|---------|-------------|
| Battery Status | SOC percentage, mode (TOU/SC/Backup), charging state |
| Current Power | Real-time charge/discharge rate in kW |
| Available Energy | Usable kWh remaining |
| Peak Countdown | Time until next expensive window starts (peak / partial-peak) or "PEAK ACTIVE" / "PARTIAL-PEAK ACTIVE" |
| Energy Flow | Visual diagram of power flow between components |
| Savings Tracker | Financial impact of automation |

**Battery States:**
- **Charging (Solar)** — Solar panels providing power to battery
- **Charging (Grid)** — Grid power charging battery
- **Charging (Mixed)** — Both solar and grid charging
- **Discharging** — Battery powering home load
- **Standby** — Battery idle, neither charging nor discharging

### Analytics

Interactive Plotly.js charts sourced from the SQLite database. Select date ranges, zoom into specific time periods, and hover for exact values. Charts include SOC timeline with mode markers, power flow breakdown, solar production vs forecast, and savings analysis. Time-series charts overlay amber bands for expensive windows — darker for sacred peak, lighter for partial-peak (three-tier plans). Optimized for touch interaction on the Fire HD 10 tablet (1507×943 CSS pixels) in Fully Kiosk Browser.

### Script Status

Shows every scheduled task with its run frequency, last execution time, success/fail count, and any recent errors. Use this to quickly verify the system is healthy — all scripts should show recent successful runs.

### System Info

| Section | Information |
|---------|-------------|
| Per-Battery SOC | Individual battery state of charge and power |
| Environment | Ambient temperature, cabinet temperature, cell signal, WiFi |
| Today's Energy | Solar, grid in/out, load, battery charge/discharge, generator |
| Lifetime Totals | Cumulative kWh by source |
| Charging Source | Grid-to-battery vs solar-to-battery breakdown |
| Hardware | BMS status, power electronics, main switch, generator, V2L |
| Mode Overrides | Manual mode control with duration selection |
| About | Version, update available badge, system info |
| Diagnostics | One-click sanitized diagnostic bundle |

### System Logs

| Log Type | Contents |
|----------|----------|
| Intelligence Log | Engine decisions, mode changes, forecast data, errors |
| Scheduler Log | Task execution times, success/failure status |

Features: color-coded entries, auto-refresh, manual refresh button, Report Issue button.

---

## Override System

Quick-access mode overrides from the dashboard or API. Four outcome-oriented options:

- **Until SOC reached** — charge up or drain down to a target percentage
- **For duration** — custom hours + minutes (up to 24 h)
- **Until specific time** — HH:MM, rolls to tomorrow if past
- **Until canceled** — runs until manually cancelled

```bash
# Emergency backup until SOC reaches 90%
curl -X POST http://your-server:8100/api/override \
  -H "Content-Type: application/json" \
  -d '{"mode": "emergency_backup", "duration": "until_soc", "exit_soc_pct": 90}'

# Self-consumption for 2 hours 30 minutes
curl -X POST http://your-server:8100/api/override \
  -H "Content-Type: application/json" \
  -d '{"mode": "self_consumption", "duration": "custom", "duration_minutes": 150}'

# Emergency backup until 18:30 (rolls to tomorrow if already past)
curl -X POST http://your-server:8100/api/override \
  -H "Content-Type: application/json" \
  -d '{"mode": "emergency_backup", "duration": "until_time", "until_time": "18:30"}'

# Self-consumption until manually cancelled
curl -X POST http://your-server:8100/api/override \
  -H "Content-Type: application/json" \
  -d '{"mode": "self_consumption", "duration": "until_cancel"}'

# Cancel active override (engine resumes)
curl -X DELETE http://your-server:8100/api/override
```

For "until SOC" overrides, the engine reads the latest SOC from the database (with a 30-second cloud API fallback if the database has no recent reading). The override exits when SOC crosses the target — no expiration timer needed.

The override status is displayed as a banner on the dashboard. Time-based overrides show remaining time; SOC-based overrides show the target threshold. The engine resumes normal operation when the override expires, the SOC target is reached, or it's manually cancelled.

---

## File Structure

### Docker Setup
```
./                               # Your clone location
├── web/
│   ├── power_dashboard.html     # Dashboard interface
│   ├── power_dashboard_data.json # Auto-generated data (every 1 min)
│   └── House_Power_Graphic.png  # Energy flow diagram
├── data/
│   ├── franklin.db              # SQLite database (all system data)
│   ├── rate_schedule.json       # Rate schedule configuration
│   └── solar_forecast_cache.json # Forecast cache
├── logs/
│   └── data_source_health.json  # Modbus/cloud/Enphase health stats
└── docker-compose.yml           # Includes nginx dashboard server
```

### Native Setup
```
/volume1/web/                    # Web server root (Synology)
├── power_dashboard.html         # Main dashboard file
└── power_dashboard_data.json    # Generated data

/volume1/docker/franklin-git/    # Automation directory
├── scripts/
│   └── generate_dashboard_data.py
├── data/
│   └── franklin.db              # SQLite database
└── logs/
```

---

## Tablet Kiosk Setup (Fire HD 10)

The dashboard is optimized for the Fire HD 10 tablet running Fully Kiosk Browser as a dedicated wall display.

1. Install **Fully Kiosk Browser** from the Amazon Appstore
2. Set the start URL to `http://YOUR-SERVER-IP:8100`
3. Enable kiosk mode and auto-refresh
4. The dashboard auto-detects the 1507×943 CSS pixel viewport

The layout works in any modern browser but is specifically tuned for the Fire HD 10 form factor.

---

## Troubleshooting

### "Loading..." never updates
1. Check that `power_dashboard_data.json` exists: `docker exec franklin-automation ls -la /app/web/power_dashboard_data.json`
2. Verify the data generator is running: `docker logs --tail 20 franklin-automation | grep Dashboard`
3. Check browser console (F12) for errors

### Charts not showing in Analytics
1. Verify the SQLite database has data: `docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); print(conn.execute('SELECT COUNT(*) FROM system_readings').fetchone())"`
2. Check that `generate_dashboard_data.py` is running successfully in Script Status tab

### Status shows wrong state
The battery state is determined by `battery_kw`:
- Negative = Charging (power flowing into battery)
- Positive = Discharging (power flowing out of battery)
- Near zero (±0.1 kW) = Standby

### Docker dashboard not accessible
1. Check containers are running: `docker compose ps`
2. Verify port mapping: `docker compose logs franklin-dashboard`
3. Check firewall allows the port (default 8100)
4. Test: `curl http://localhost:8100/health`

---

## Security Considerations

The dashboard displays energy data but contains no sensitive credentials. However:

1. **Don't expose to public internet** without authentication
2. **Use a reverse proxy** with authentication if remote access needed
3. **The data JSON** contains only energy metrics, no credentials
4. **Diagnostic bundles** automatically strip all credentials before export

For remote access, consider: VPN, Tailscale, or a reverse proxy with authentication.

---

**Last Updated:** June 2026
**Version:** 4.6.0
