# Web Dashboard Setup Guide

The FranklinWH Automation includes a web-based dashboard for real-time monitoring of your battery system, energy flow, and savings tracking.

## Features

### Live Dashboard Tab
- **Real-Time Battery Status**: SOC, charging/discharging/standby state, current power
- **Energy Flow Visualization**: Power flowing between solar, grid, battery, and home
- **Battery States**: Three distinct states with color-coded badges:
  - 🟢 **Charging** (green) — Battery receiving power (solar or grid)
  - 🟠 **Discharging** (orange) — Battery powering home
  - ⚪ **Standby** (gray) — Battery idle (±100W threshold)
- **Savings Tracker**: Daily, monthly, and projected annual savings
- **Auto-Refresh**: Dashboard updates every 15 seconds

### Weekly Reports Tab
- **SOC Timeline**: 7-day battery state of charge history
- **Daily Summary**: Charging/discharging patterns by day
- **Power Flow**: 48-hour detailed power flow analysis
- **Report Archive**: Access previous weekly reports from dropdown

### System Logs Tab
- **Intelligence Log**: Decision-making logic and automation choices
- **Scheduler Log**: Task execution history and timing
- **Monitoring Data**: CSV data viewer with table formatting
- **Auto-Refresh**: Logs update every 30 seconds when tab is active
- **Configurable**: Choose number of lines to display (10-500)

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

### 2. Create Symlink for Logs

The weekly charts and log files are stored in the logs folder. Create a symlink so the dashboard can access them:

```bash
# Synology
ln -s /volume1/docker/franklin-git/logs /volume1/web/logs

# Linux
ln -s /path/to/FranklinWH-Automation/logs /var/www/html/logs
```

### 3. Schedule Data Generation

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

### 4. Access the Dashboard

Open in your browser:
- **Docker:** `http://YOUR-SERVER-IP:8100`
- **Synology (native):** `http://YOUR-NAS-IP/power_dashboard.html`
- **Linux (native):** `http://YOUR-SERVER-IP/power_dashboard.html`

---

## Dashboard Tabs

### Live Dashboard

| Section | Information |
|---------|-------------|
| Battery Status | SOC percentage, mode (TOU/Backup), charging state |
| Current Power | Real-time charge/discharge rate in kW |
| Available Energy | Usable kWh remaining |
| Peak Countdown | Minutes until peak period starts |
| Energy Flow | Visual diagram of power flow between components |
| Savings Tracker | Financial impact of automation |

**Battery States:**
- **Charging (Solar)** — Solar panels providing power to battery
- **Charging (Grid)** — Grid power charging battery
- **Charging (Mixed)** — Both solar and grid charging
- **Discharging** — Battery powering home load
- **Standby** — Battery idle, neither charging nor discharging

### Weekly Reports

Select a report date from the dropdown to view:
- **SOC Timeline** — 7-day state of charge graph
- **Daily Summary** — Bar chart of daily charge/discharge
- **Power Flow** — Detailed 48-hour power analysis

Reports are generated weekly (default: Sunday 2:00 AM).

### System Logs

| Log Type | Contents |
|----------|----------|
| Intelligence Log | Decision logic, mode changes, API mode detection |
| Scheduler Log | Task execution times, success/failure status |
| Monitoring Data | CSV data displayed as sortable table |

Features: color-coded entries, configurable line count, auto-refresh, manual refresh button.

---

## File Structure

### Docker Setup
```
./                               # Your clone location
├── web/
│   ├── power_dashboard.html     # Dashboard interface
│   └── power_dashboard_data.json # Auto-generated data
├── logs/
│   ├── solar_intelligence.log   # Decision log
│   ├── scheduler.log            # Task execution log
│   ├── continuous_monitoring.csv # Historical data
│   └── *_chart_*.png            # Weekly chart images
└── docker-compose.yml           # Includes nginx dashboard server
```

### Native Setup
```
/volume1/web/                    # Web server root (Synology)
├── power_dashboard.html         # Main dashboard file
├── power_dashboard_data.json    # Generated data
└── logs/ → symlink              # Points to logs folder

/volume1/docker/franklin-git/    # Automation directory
├── scripts/
│   ├── generate_dashboard_data.py
│   └── generate_weekly_charts.py
└── logs/
    └── (log and chart files)
```

---

## Troubleshooting

### "Loading..." never updates
1. Check that `power_dashboard_data.json` exists in the web directory
2. Verify the data generator is running
3. Check browser console (F12) for errors
4. For Docker: `docker exec franklin-automation ls -la /app/web/power_dashboard_data.json`

### Charts not showing
1. Verify logs folder is accessible (symlink for native, volume mount for Docker)
2. Check that chart files exist: `ls logs/*chart*.png`
3. Run the chart generator manually to test

### Status shows wrong state
The battery state is determined by `current_power`:
- Less than -0.1 kW = Charging
- Greater than +0.1 kW = Discharging
- Between -0.1 and +0.1 kW = Standby

### Docker dashboard not accessible
1. Check container is running: `docker compose ps`
2. Verify port mapping: `docker compose logs franklin-dashboard`
3. Check firewall allows the port (default 8100)
4. Try: `curl http://localhost:8100/health`

---

## Security Considerations

The dashboard displays energy data but contains no sensitive credentials. However:

1. **Don't expose to public internet** without authentication
2. **Use a reverse proxy** with authentication if remote access needed
3. **The data JSON** contains only energy metrics, no credentials

For remote access, consider: VPN, Tailscale, or a reverse proxy with authentication.

---

**Last Updated:** February 2026
**Version:** 3.2.0
