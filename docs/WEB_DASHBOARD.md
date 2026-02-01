# Web Dashboard Setup Guide

The FranklinWH Automation includes an optional web-based dashboard for real-time monitoring of your battery system, energy flow, and savings tracking.

## Features

- **Real-Time Battery Status**: SOC, charging/discharging state, current power
- **Energy Flow Visualization**: See power flowing between solar, grid, battery, and home
- **Savings Tracker**: Daily, monthly, and projected annual savings
- **Weekly Performance Charts**: Historical analysis with SOC timelines and daily summaries
- **Auto-Refresh**: Dashboard updates every 15 seconds

## Quick Start

### 1. Copy Files to Web Directory

The dashboard consists of two main components:
- `power_dashboard.html` - The dashboard interface
- `generate_dashboard_data.py` - Script that creates the data JSON file

**For Synology NAS (Web Station):**
```bash
# Copy dashboard HTML to web directory
cp web/power_dashboard.html /volume1/web/

# The data generator script stays in your scripts folder
cp scripts/generate_dashboard_data.py /volume1/docker/franklin/
```

**For other web servers:**
```bash
# Copy to your web server's document root
cp web/power_dashboard.html /var/www/html/
# Or wherever your web server serves files from
```

### 2. Create Symlink for Logs (Charts Access)

The weekly charts are stored in the logs folder. Create a symlink so the dashboard can access them:

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
4. **Schedule:** Every 1 minute (or every 15 seconds if using a custom scheduler)
5. **Command:**
   ```bash
   cd /volume1/docker/franklin
   /volume1/docker/franklin/venv311/bin/python generate_dashboard_data.py
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
   /volume1/docker/franklin/venv311/bin/python generate_weekly_charts.py
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

---

## File Structure

```
/volume1/web/                    # Web server root (Synology)
├── power_dashboard.html         # Main dashboard file
├── power_dashboard_data.json    # Generated data (auto-created)
├── weekly_reports_index.json    # Chart index (auto-created)
└── logs/                        # Symlink to logs folder
    ├── *_chart_soc_timeline.png
    ├── *_chart_daily_summary.png
    └── *_chart_power_flow.png

/volume1/docker/franklin/        # Automation directory
├── scripts/
│   ├── generate_dashboard_data.py
│   └── generate_weekly_charts.py
└── logs/
    └── (chart files generated here)
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

**Adjust for different web paths:**
If your logs aren't at `/logs/`, edit the `loadReport()` function:
```javascript
const basePath = 'logs/';  // Change this path
```

---

## Troubleshooting

### "Loading..." never updates
1. Check that `power_dashboard_data.json` exists in the web directory
2. Verify the data generator is running: `ls -la /volume1/web/power_dashboard_data.json`
3. Check browser console (F12) for errors
4. Ensure the JSON file is readable by the web server

### Charts not showing
1. Verify symlink exists: `ls -la /volume1/web/logs`
2. Check that chart files exist: `ls /volume1/docker/franklin/logs/*chart*.png`
3. Run the chart generator manually to test

### Data is stale
1. Check Task Scheduler history for errors
2. Run `generate_dashboard_data.py` manually and check for errors
3. Verify Franklin API credentials in `.env`

### Permission errors
```bash
# Synology - ensure web server can read files
chmod 644 /volume1/web/power_dashboard.html
chmod 644 /volume1/web/power_dashboard_data.json
```

---

## Alternative Web Servers

### Nginx
```nginx
server {
    listen 80;
    server_name your-server;
    root /var/www/html;
    
    location / {
        try_files $uri $uri/ =404;
    }
}
```

### Apache
```apache
<VirtualHost *:80>
    DocumentRoot /var/www/html
    <Directory /var/www/html>
        Options Indexes FollowSymLinks
        AllowOverride None
        Require all granted
    </Directory>
</VirtualHost>
```

### Python Simple Server (Testing)
```bash
cd /path/to/web/files
python -m http.server 8080
# Access at http://localhost:8080/power_dashboard.html
```

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
