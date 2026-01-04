# Task Scheduler Configuration Guide

**Complete guide for setting up automated scheduling on Synology NAS and Linux systems**

---

## Table of Contents

- [Synology Task Scheduler](#synology-task-scheduler)
- [Linux Cron](#linux-cron)
- [Systemd Timers (Advanced)](#systemd-timers-advanced)
- [Verification](#verification)
- [Troubleshooting](#troubleshooting)

---

## Synology Task Scheduler

### Core Automation Task (Required)

**Task:** Smart Battery Decision - Runs every 15 minutes

#### Via Web Interface (Recommended)

1. **Open Task Scheduler:**
   - DSM → Control Panel → Task Scheduler

2. **Create New Task:**
   - Create → Scheduled Task → User-defined script

3. **General Tab:**
   - **Task:** `Smart Battery Decision - Every 15 minutes`
   - **User:** `root` (important!)
   - **Enabled:** ✓ checked
   - **Send run details by email:** ✓ checked (optional)
   - **Email:** `your-email@example.com`
   - **Send run details only when script terminates abnormally:** ✓ checked

4. **Schedule Tab:**
   - **Date:** `Run on the following days` → Daily
   - **First run time:** `00:00`
   - **Frequency:** `Every 15 minutes`
   - **Last run time:** `23:45`

5. **Task Settings Tab:**
   - **Run command:** `User-defined script`
   - **Script:**
     ```bash
     #!/bin/bash
     cd /volume1/docker/franklin
     /volume1/docker/franklin/scripts/run_smart_decision.sh
     ```

6. **Click OK**

7. **Test immediately:**
   - Select the task
   - Click **Run**
   - Click **Action** → **View Result** to see output

#### Via Command Line

```bash
# SSH to your NAS
ssh admin@YOUR-NAS-IP
sudo -i

# Create the task
cat > /tmp/create_smart_decision_task.sh << 'EOF'
#!/bin/bash
/usr/syno/bin/synoschedtask --create \
  name="Smart Battery Decision - Every 15 minutes" \
  user="root" \
  enabled=true \
  type="daily" \
  run_hour=0 \
  run_min=0 \
  repeat_min=15 \
  last_work_hour=23 \
  cmd="#!/bin/bash
cd /volume1/docker/franklin
/volume1/docker/franklin/scripts/run_smart_decision.sh"
EOF

chmod +x /tmp/create_smart_decision_task.sh
/tmp/create_smart_decision_task.sh
```

#### Example Task File

Location: `/usr/syno/etc/synoschedule.d/root/7.task` (task ID varies)

```ini
id=7
last work hour=23
can edit owner=1
can delete from ui=1
edit dialog=SYNO.SDS.TaskScheduler.EditDialog
type=daily
action=#common:run#: #!/bin/bash cd /volume1/docker/franklin /volume1/docker/franklin/scripts/run_smart_decision.sh
week=1111111
app name=#common:command_line#
name=Smart Battery Decision - Every 15 minutes
owner=0
repeat min=15
state=enabled
run hour=0
run min=0
```

---

### Optional Monitoring Tasks

#### Weather Data Collection (Every 15 minutes)

**Via Web Interface:**
1. Create → Scheduled Task → User-defined script
2. **General:**
   - Task: `Data Collection - Weather 15 min`
   - User: `root`
3. **Schedule:**
   - Daily, every 15 minutes (00:00 to 23:45)
4. **Script:**
   ```bash
   #!/bin/bash
   cd /volume1/docker/franklin
   source venv311/bin/activate
   ./scripts/collect_weather.py
   ```

#### PVOutput Solar Data (Hourly)

**Via Web Interface:**
1. Create → Scheduled Task → User-defined script
2. **General:**
   - Task: `Data Collection - PVOutput hourly`
   - User: `root`
3. **Schedule:**
   - Daily, every 1 hour (00:00 to 23:00)
4. **Script:**
   ```bash
   #!/bin/bash
   cd /volume1/docker/franklin
   source venv311/bin/activate
   ./scripts/collect_pvoutput.py
   ```

#### Daily Status Report (4:30 PM)

**Via Web Interface:**
1. Create → Scheduled Task → User-defined script
2. **General:**
   - Task: `Daily Battery Status Report`
   - User: `root`
3. **Schedule:**
   - Daily, 16:30 (one time)
4. **Script:**
   ```bash
   #!/bin/bash
   cd /volume1/docker/franklin
   source venv311/bin/activate
   /volume1/docker/franklin/scripts/daily_status_report.py
   ```

#### Milestone Emailer (Hourly during testing)

**Via Web Interface:**
1. Create → Scheduled Task → User-defined script
2. **General:**
   - Task: `Milestone Status Emails - Testing`
   - User: `root`
3. **Schedule:**
   - Daily, every 1 hour (00:00 to 23:00)
4. **Script:**
   ```bash
   #!/bin/bash
   cd /volume1/docker/franklin
   source venv311/bin/activate
   /volume1/docker/franklin/scripts/milestone_emailer.py
   ```

**Note:** Disable this task after testing is complete.

#### Data Aggregation (Daily at 6 AM)

**Via Web Interface:**
1. Create → Scheduled Task → User-defined script
2. **General:**
   - Task: `Data Collection - Aggregate Daily`
   - User: `root`
3. **Schedule:**
   - Daily, 06:00 (one time)
4. **Script:**
   ```bash
   #!/bin/bash
   cd /volume1/docker/franklin
   source venv311/bin/activate
   ./scripts/aggregate_data.py
   ```

---

### Managing Tasks

#### View All Tasks

**Web Interface:**
- Control Panel → Task Scheduler

**Command Line:**
```bash
sudo /usr/syno/bin/synoschedtask --get | grep -A 20 "Name:"
```

#### Enable/Disable Task

**Web Interface:**
- Select task → Edit → Check/uncheck "Enabled"

**Command Line:**
```bash
# Find task ID
sudo /usr/syno/bin/synoschedtask --get | grep "Smart Battery"

# Disable
sudo /usr/syno/bin/synoschedtask --disable id=7

# Enable
sudo /usr/syno/bin/synoschedtask --enable id=7
```

#### Delete Task

**Web Interface:**
- Select task → Delete

**Command Line:**
```bash
sudo /usr/syno/bin/synoschedtask --delete id=7
```

#### View Task History

**Web Interface:**
- Select task → Action → View Result

**Shows:**
- Start time
- Exit code (0 = success)
- Output/errors

---

## Linux Cron

### Core Automation (Every 15 minutes)

```bash
# Edit crontab
crontab -e

# Add this line (adjust paths as needed):
*/15 * * * * cd /opt/franklin && /opt/franklin/scripts/run_smart_decision.sh >> /opt/franklin/logs/cron.log 2>&1
```

**Explanation:**
- `*/15 * * * *` - Every 15 minutes
- `cd /opt/franklin` - Change to project directory
- `&&` - And then...
- `/opt/franklin/scripts/run_smart_decision.sh` - Run script
- `>>` - Append output to...
- `/opt/franklin/logs/cron.log` - Log file
- `2>&1` - Redirect errors to log too

### Optional Tasks

```bash
# Edit crontab
crontab -e

# Weather collection (every 15 minutes)
*/15 * * * * cd /opt/franklin && source venv311/bin/activate && ./scripts/collect_weather.py >> /opt/franklin/logs/weather_cron.log 2>&1

# PVOutput collection (hourly at :05)
5 * * * * cd /opt/franklin && source venv311/bin/activate && ./scripts/collect_pvoutput.py >> /opt/franklin/logs/pvoutput_cron.log 2>&1

# Daily status report (4:30 PM)
30 16 * * * cd /opt/franklin && source venv311/bin/activate && ./scripts/daily_status_report.py >> /opt/franklin/logs/daily_report.log 2>&1

# Data aggregation (6:00 AM)
0 6 * * * cd /opt/franklin && source venv311/bin/activate && ./scripts/aggregate_data.py >> /opt/franklin/logs/aggregate_cron.log 2>&1
```

### Cron Schedule Format

```
* * * * * command
│ │ │ │ │
│ │ │ │ └─── Day of week (0-7, Sunday=0 or 7)
│ │ │ └───── Month (1-12)
│ │ └─────── Day of month (1-31)
│ └───────── Hour (0-23)
└─────────── Minute (0-59)
```

**Examples:**
- `*/15 * * * *` - Every 15 minutes
- `0 * * * *` - Every hour at :00
- `30 16 * * *` - Daily at 4:30 PM
- `0 6 * * 0` - Sundays at 6:00 AM
- `0 0 1 * *` - First day of month at midnight

### Managing Cron

```bash
# View current crontab
crontab -l

# Edit crontab
crontab -e

# Remove all cron jobs (careful!)
crontab -r

# View system-wide cron logs
grep CRON /var/log/syslog
# or
journalctl -u cron
```

---

## Systemd Timers (Advanced)

**Alternative to cron on modern Linux systems**

### Create Service File

```bash
sudo nano /etc/systemd/system/franklin-smart-decision.service
```

```ini
[Unit]
Description=Franklin WH Smart Battery Decision
After=network.target

[Service]
Type=oneshot
User=root
WorkingDirectory=/opt/franklin
ExecStart=/opt/franklin/scripts/run_smart_decision.sh
StandardOutput=append:/opt/franklin/logs/systemd.log
StandardError=append:/opt/franklin/logs/systemd_error.log

[Install]
WantedBy=multi-user.target
```

### Create Timer File

```bash
sudo nano /etc/systemd/system/franklin-smart-decision.timer
```

```ini
[Unit]
Description=Run Franklin WH Smart Decision every 15 minutes
Requires=franklin-smart-decision.service

[Timer]
OnBootSec=2min
OnUnitActiveSec=15min
AccuracySec=1s

[Install]
WantedBy=timers.target
```

### Enable and Start

```bash
# Reload systemd
sudo systemctl daemon-reload

# Enable timer (start on boot)
sudo systemctl enable franklin-smart-decision.timer

# Start timer now
sudo systemctl start franklin-smart-decision.timer

# Check status
sudo systemctl status franklin-smart-decision.timer

# View logs
sudo journalctl -u franklin-smart-decision.service -f
```

### Manage Timer

```bash
# List all timers
systemctl list-timers

# Stop timer
sudo systemctl stop franklin-smart-decision.timer

# Disable timer (won't start on boot)
sudo systemctl disable franklin-smart-decision.timer

# Manually run service once
sudo systemctl start franklin-smart-decision.service
```

---

## Verification

### Check Task is Running

**Synology:**
```bash
# View recent task runs
sudo /usr/syno/bin/synoschedtask --get | grep -A 15 "Smart Battery"

# Check task history via web interface
# Control Panel → Task Scheduler → Select task → Action → View Result
```

**Linux Cron:**
```bash
# View cron log
grep "run_smart_decision" /var/log/syslog | tail -20
# or
journalctl -u cron | grep franklin | tail -20

# Check if cron is running
sudo systemctl status cron
```

**Systemd:**
```bash
# Check timer status
systemctl status franklin-smart-decision.timer

# View recent runs
sudo journalctl -u franklin-smart-decision.service --since today
```

### Verify Logs Are Being Written

```bash
# Check intelligence log has recent entries
tail -20 /volume1/docker/franklin/logs/solar_intelligence.log

# Should show entries from last 15 minutes
# Example:
# 2026-01-04 10:45:23 - ======================================================================
# 2026-01-04 10:45:23 - SOC: 72.1%, Solar: 3.456kW, Status: 6.2h to peak
```

### Count Runs in Last 24 Hours

```bash
# Should be ~96 (4 per hour × 24 hours)
grep "$(date +%Y-%m-%d)" logs/solar_intelligence.log | grep "Decision made" | wc -l
```

---

## Troubleshooting

### Task Not Running

**Synology:**
1. Check task is enabled: Control Panel → Task Scheduler
2. Check task history for errors: Action → View Result
3. Verify script path is correct and absolute
4. Test script manually: `sudo /volume1/docker/franklin/scripts/run_smart_decision.sh`

**Linux:**
1. Check cron is running: `sudo systemctl status cron`
2. View cron logs: `grep CRON /var/log/syslog | tail`
3. Test cron entry manually: Copy exact command from crontab and run it
4. Check file permissions: `ls -la /opt/franklin/scripts/run_smart_decision.sh`

### Script Runs But Fails

**Check exit code:**
- Synology: Task history shows exit code (should be 0)
- Linux: Check logs in `/opt/franklin/logs/cron.log`

**Common issues:**
- Virtual environment not activated → Add `source venv311/bin/activate` to script
- Wrong working directory → Add `cd /path/to/franklin` at start
- Missing dependencies → Reinstall: `pip install -r requirements.txt`
- Python not found → Use absolute path: `/usr/bin/python3`

### Task Runs Too Often or Not Enough

**Synology:**
- Check "Repeat every X minutes" setting
- Verify "Last run time" is set correctly (23:45 for 15-min intervals)

**Linux Cron:**
- Verify cron syntax: https://crontab.guru/
- Check for typos in crontab: `crontab -l`

### Email Notifications Not Working

**Synology:**
1. Control Panel → Notification → Email
2. Verify SMTP settings are configured
3. Send test email
4. Check task has "Send run details by email" enabled
5. Verify email address is correct

---

## Best Practices

### Logging

**Always log script output:**
- Synology: Use email notifications or check task history
- Linux: Redirect to log files with `>> /path/to/log 2>&1`

**Rotate logs periodically:**
```bash
# Add to weekly cron
0 0 * * 0 cd /opt/franklin/logs && tar -czf archive-$(date +%Y%m%d).tar.gz *.log && rm *.log
```

### Monitoring

**Set up alerts for failures:**
- Synology: Enable email notifications for abnormal termination
- Linux: Use a monitoring tool or check return codes

**Weekly verification:**
```bash
# Check system is running properly
grep "Decision made" logs/solar_intelligence.log | tail -100 | wc -l
# Should show ~672 (96 per day × 7 days)
```

### Recovery

**If tasks stop working:**
1. Check system time/timezone is correct
2. Verify network connectivity
3. Test Franklin WH credentials manually
4. Check disk space: `df -h`
5. Review recent system changes/updates

---

## Summary

### Minimum Required Task

**One task is required for the system to work:**
- **Smart Battery Decision** - Runs `smart_decision.py` every 15 minutes

### Recommended Optional Tasks

**These enhance monitoring and data collection:**
- Weather data collection (every 15 min)
- PVOutput data collection (hourly)
- Daily status report (4:30 PM)
- Data aggregation (6:00 AM)

### During Setup/Testing Only

**Disable after system is validated:**
- Milestone emailer (hourly status emails)

---

**Next:** [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for common issues and solutions.

---

**Last Updated:** January 4, 2026  
**Version:** 2.0
