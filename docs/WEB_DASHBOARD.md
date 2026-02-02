# Web Dashboard Setup Guide

The FranklinWH Automation includes an optional web-based dashboard for real-time monitoring of your battery system, energy flow, and savings tracking.

## Features

### Live Dashboard Tab
- **Real-Time Battery Status**: SOC, charging/discharging/standby state, current power
- **Energy Flow Visualization**: See power flowing between solar, grid, battery, and home
- **Battery States**: Three distinct states with color-coded badges:
  - 🟢 **Charging** (green) - Battery receiving power (solar or grid)
  - 🟠 **Discharging** (orange) - Battery powering home
  - ⚪ **Standby** (gray) - Battery idle (±100W threshold)
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

If using Docker, the dashboard is **automatically included**. No additional setup needed!

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

The dashboard consists of two main components:
- `power_dashboard.html` - The dashboard interface
- `generate_dashboard_data.py` - Script that creates the data JSON file

**For Synology NAS (Web Station):**
```bash
# Copy dashboard HTML to web directory
cp web/power_dashboard.html /volume1/web/

# The data generator script stays in your scripts folder
# (it's already in scripts/generate_dashboard_data.py)
```

**For other web servers:**
```bash
# Copy to your web server's document root
cp web/power_dashboard.html /var/www/html/
```

### 2. Create Symlink for Logs (Charts & Logs Access)

The weekly charts and log files are stored in the logs folder. Create a symlink so the dashboard can access them:

```bash
# Synology
ln -s /volume1/docker/franklin/logs /volume1/web/logs

# Linux
ln -s /path/to/FranklinWH-Automation/logs /var/www/html/logs
```

### 3. Schedule Data Generation

The dashboard reads from `power_dashboard_data.json`. Schedule the data generator to run frequently:

**Synology Task Scheduler:**
1. Control Panel → Task Scheduler
2. Create → Scheduled Task → User-defined script
3. **Name:** "Dashboard Data Update"
4. **Schedule:** Every 1 minute
5. **Command:**
   ```bash
   cd /volume1/docker/franklin
   /volume1/docker/franklin/venv311/bin/python scripts/generate_dashboard_data.py
   ```

**Linux Cron:**
```bash
# Every minute
* * * * * cd /path/to/FranklinWH-Automation && ./venv311/bin/python scripts/generate_dashboard_data.py
```

### 4. Schedule Weekly Charts (Optional)

For the Weekly Reports tab to show historical charts:

**Synology Task Scheduler:**
1. Create task to run weekly (e.g., Sunday 2:00 AM)
2. **Command:**
   ```bash
   cd /volume1/docker/franklin
   /volume1/docker/franklin/venv311/bin/python scripts/generate_weekly_charts.py
   ```

**Linux Cron:**
```bash
# Every Sunday at 2 AM
0 2 * * 0 cd /path/to/FranklinWH-Automation && ./venv311/bin/python scripts/generate_weekly_charts.py
```

### 5. Access the Dashboard

Open in your browser:
- **Synology:** `http://YOUR-NAS-IP/power_dashboard.html`
- **Linux:** `http://YOUR-SERVER-IP/power_dashboard.html`
- **Docker:** `http://YOUR-SERVER-IP:8100`

---

## Dashboard Tabs

### Live Dashboard

The main monitoring view showing:

| Section | Information |
|---------|-------------|
| Battery Status | SOC percentage, mode (TOU/Backup), charging state |
| Current Power | Real-time charge/discharge rate in kW |
| Available Energy | Usable kWh remaining |
| Peak Countdown | Minutes until peak period starts |
| Energy Flow | Visual diagram of power flow between components |
| Savings Tracker | Financial impact of automation |

**Battery States:**
- **Charging (Solar)** - Solar panels providing power to battery
- **Charging (Grid)** - Grid power charging battery
- **Charging (Mixed)** - Both solar and grid charging
- **Discharging** - Battery powering home load
- **Standby** - Battery idle, neither charging nor discharging

### Weekly Reports

Select a report date from the dropdown to view:
- **SOC Timeline** - 7-day state of charge graph
- **Daily Summary** - Bar chart of daily charge/discharge
- **Power Flow** - Detailed 48-hour power analysis

Reports are generated weekly (default: Sunday 2:00 AM).

### System Logs

View automation logs directly in the browser:

| Log Type | Contents |
|----------|----------|
| Intelligence Log | Decision logic, mode changes, solar calculations |
| Scheduler Log | Task execution times, success/failure status |
| Monitoring Data | CSV data displayed as sortable table |

**Features:**
- Color-coded entries (red=error, green=success, yellow=warning)
- Configurable line count (10-500 lines)
- Auto-refresh every 30 seconds
- Manual refresh button

---

## File Structure

### Docker Setup
```
/volume1/docker/franklin-git/    # Or your clone location
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
├── power_dashboard_data.json    # Generated data (auto-created)
└── logs/                        # Symlink to logs folder
    ├── solar_intelligence.log
    ├── scheduler.log
    ├── *_chart_soc_timeline.png
    ├── *_chart_daily_summary.png
    └── *_chart_power_flow.png

/volume1/docker/franklin/        # Automation directory
├── scripts/
│   ├── generate_dashboard_data.py
│   └── generate_weekly_charts.py
└── logs/
    └── (log and chart files)
```

---

## Configuration

### Dashboard Data Generator

The `generate_dashboard_data.py` script uses your `.env` configuration automatically. It reads:
- Franklin WH API credentials for real-time data
- Log files for historical data
- Savings data if available

### Customization

**Change refresh rate:**
Edit `power_dashboard.html`, find the `setInterval` call near the bottom:
```javascript
setInterval(updateDashboard, 15000);  // 15000ms = 15 seconds
```

**Adjust battery state threshold:**
The dashboard uses ±0.1 kW (100W) as the threshold for idle/standby. Values between -0.1 and +0.1 kW show as "Standby".

**Change log auto-refresh:**
```javascript
// Currently 30 seconds
setInterval(() => {
    if (document.getElementById('tab-logs').classList.contains('active')) {
        loadLogs();
    }
}, 30000);
```

---

## Troubleshooting

### "Loading..." never updates
1. Check that `power_dashboard_data.json` exists in the web directory
2. Verify the data generator is running: `ls -la web/power_dashboard_data.json`
3. Check browser console (F12) for errors
4. Ensure the JSON file is readable by the web server

### Charts not showing
1. Verify logs folder is accessible (symlink for native, volume mount for Docker)
2. Check that chart files exist: `ls logs/*chart*.png`
3. Run the chart generator manually to test:
   ```bash
   python scripts/generate_weekly_charts.py
   ```

### Logs tab shows "Unable to load log file"
1. Verify logs are being written: `ls -la logs/`
2. Check file permissions: `chmod 644 logs/*.log`
3. For Docker: ensure logs volume is mounted correctly
4. Try the scheduler log first - it's created by Docker automatically

### Status shows wrong state
The battery state is determined by `current_power`:
- Less than -0.1 kW = Charging
- Greater than +0.1 kW = Discharging  
- Between -0.1 and +0.1 kW = Standby

If status seems wrong, check `power_dashboard_data.json`:
```bash
cat web/power_dashboard_data.json | grep current_power
```

### Data is stale
1. Check Task Scheduler/cron history for errors
2. Run data generator manually:
   ```bash
   python scripts/generate_dashboard_data.py
   ```
3. Verify Franklin API credentials in `.env`

### Docker: Dashboard not accessible
1. Check container is running: `docker compose ps`
2. Verify port mapping: `docker compose logs franklin-dashboard`
3. Check firewall allows port 8100 (or your custom port)
4. Try: `curl http://localhost:8100/health`

---

## Security Considerations

The dashboard displays your energy data but contains no sensitive credentials. However:

1. **Don't expose to public internet** without authentication
2. **Use a reverse proxy** with authentication if remote access needed
3. **The data JSON** contains only energy metrics, no credentials
4. **Log files** may contain timestamps and usage patterns

For remote access, consider:
- VPN to your home network
- Tailscale or similar zero-config VPN
- Reverse proxy with basic auth or OAuth

---

**Last Updated:** February 2026  
**Version:** 3.0
