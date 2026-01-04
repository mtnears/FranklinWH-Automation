# Troubleshooting Guide

**Common issues and solutions for Franklin WH Battery Automation**

---

## Table of Contents

- [Quick Diagnostics](#quick-diagnostics)
- [Installation Issues](#installation-issues)
- [API Connection Problems](#api-connection-problems)
- [Task Scheduler Issues](#task-scheduler-issues)
- [Peak State Problems](#peak-state-problems)
- [Mode Switching Issues](#mode-switching-issues)
- [Log Analysis](#log-analysis)
- [Getting Help](#getting-help)

---

## Quick Diagnostics

### Run These Commands First

```bash
# Change to your installation directory
cd /volume1/docker/franklin  # or /opt/franklin

# Check Python environment
source venv311/bin/activate
python --version  # Should be 3.11+

# Test core script manually
./scripts/smart_decision.py

# Check recent logs
tail -50 logs/solar_intelligence.log

# Verify peak state
cat logs/peak_state.txt

# Check last mode
cat logs/last_mode.txt
```

---

## Installation Issues

### "ModuleNotFoundError: No module named 'franklinwh'"

**Cause:** Virtual environment not activated or franklinwh not installed

**Solution:**
```bash
cd /volume1/docker/franklin
source venv311/bin/activate
pip list | grep franklinwh

# If not listed:
pip install --break-system-packages franklinwh requests
```

### "Permission denied" when running scripts

**Cause:** Scripts not executable

**Solution:**
```bash
chmod +x scripts/*.py
chmod +x scripts/*.sh

# Verify
ls -la scripts/
# Should show: -rwxr-xr-x
```

### "python3: command not found"

**Cause:** Python not installed or not in PATH

**Solution:**
```bash
# Synology - Install Python 3.11 via Package Center

# Raspberry Pi/Linux
sudo apt install python3.11 python3.11-venv

# Find Python location
which python3
# Use full path in scripts if needed
```

### "pip install" fails on Synology

**Cause:** DSM 7.2+ externally managed environment

**Solution:**
```bash
# Always use --break-system-packages flag on Synology
pip install --break-system-packages -r requirements.txt

# Or create virtual environment (recommended)
python3 -m venv venv311
source venv311/bin/activate
pip install -r requirements.txt
```

---

## API Connection Problems

### "Device response timed out" - Frequent Timeouts

**Cause:** Franklin Cloud API is slow/unreliable

**Current Behavior:** Script retries 5 times with 10-second delays

**Check retry attempts in logs:**
```bash
grep "Attempt" logs/solar_intelligence.log | tail -20
```

**Good output (successful retry):**
```
Attempt 1 starting...
✗ Attempt 1 failed: Device response timed out, retrying in 10s...
Attempt 2 starting...
✓ Success on attempt 2
```

**If all 5 attempts fail repeatedly:**
1. Check Franklin WH system status in mobile app
2. Verify internet connection
3. Test API directly:
   ```bash
   ./scripts/get_battery_status.py
   ```
4. Check Franklin WH service status (may be maintenance)
5. Wait 1 hour and retry

### "Authentication failed"

**Cause:** Incorrect username or password

**Solution:**
```bash
# Verify credentials in scripts
grep "USERNAME\|PASSWORD" scripts/smart_decision.py

# Should match Franklin WH mobile app credentials
# Test login in Franklin WH app first
```

**Still failing?**
- Password may have special characters that need escaping
- Try resetting password in Franklin WH app
- Ensure username is full email address

### "Gateway not found" or "Invalid gateway ID"

**Cause:** Wrong GATEWAY_ID

**Solution:**
```bash
# Find Gateway ID in Franklin WH mobile app:
# Settings → System Info → Gateway ID

# Verify in script
grep "GATEWAY_ID" scripts/smart_decision.py

# Format should be: 10060005A02X24470437 (20 characters)
```

### "SSL Certificate verification failed"

**Cause:** System time/date incorrect or missing CA certificates

**Solution:**
```bash
# Check system time
date
# Should be current time in your timezone

# Update system time (if wrong)
sudo ntpdate -s time.nist.gov

# Install CA certificates (Linux)
sudo apt install ca-certificates
sudo update-ca-certificates
```

---

## Task Scheduler Issues

### Task Not Running

**Synology:**

1. **Check task is enabled:**
   ```bash
   sudo /usr/syno/bin/synoschedtask --get | grep -A 5 "Smart Battery"
   ```
   Should show `State: [enabled]`

2. **Check task history:**
   - Control Panel → Task Scheduler
   - Select task → Action → View Result
   - Should show recent runs with exit code 0

3. **Test manually:**
   ```bash
   # Run as root
   sudo -i
   cd /volume1/docker/franklin
   /volume1/docker/franklin/scripts/run_smart_decision.sh
   ```

4. **Check script path:**
   - Task must use absolute paths
   - `/volume1/docker/franklin/...` not `./...`

**Linux/Cron:**

1. **Verify cron is running:**
   ```bash
   sudo systemctl status cron
   ```

2. **Check crontab:**
   ```bash
   crontab -l
   # Should show your scheduled task
   ```

3. **View cron logs:**
   ```bash
   grep CRON /var/log/syslog | tail -30
   # or
   journalctl -u cron | grep franklin | tail -30
   ```

4. **Test cron entry manually:**
   ```bash
   # Copy exact command from crontab and run it
   cd /opt/franklin && /opt/franklin/scripts/run_smart_decision.sh
   ```

### Task Runs But Script Fails

**Check exit code:**
- Synology: Task history shows exit code
- Linux: Check logs

**Exit codes:**
- `0` = Success
- `1` = General error
- `127` = Command not found
- `126` = Permission denied

**Common fixes:**

```bash
# Activate virtual environment in wrapper script
# Edit run_smart_decision.sh:
#!/bin/bash
cd /volume1/docker/franklin || exit 1
source venv311/bin/activate  # Add this line
exec /volume1/docker/franklin/venv311/bin/python3 /volume1/docker/franklin/scripts/smart_decision.py
```

### Task Runs at Wrong Times

**Synology:**
- Verify "Repeat every 15 minutes" setting
- Check "Last run time" is 23:45 (not 23:59)
- "First run time" should be 00:00

**Linux/Cron:**
- Test cron syntax: https://crontab.guru/
- Verify: `*/15 * * * *` (every 15 minutes)
- Not: `15 * * * *` (every hour at :15)

---

## Peak State Problems

### Peak State File Not Created

**Check:**
```bash
ls -la logs/peak_state.txt
```

**If missing:**
```bash
# Create manually (system will update it)
mkdir -p logs
echo "OffPeak-$(date +%Y-%m-%d)" > logs/peak_state.txt
```

**Verify directory permissions:**
```bash
ls -ld logs/
# Should be writable by root/user running scripts
```

### Peak Period Not Being Detected

**Symptoms:**
- No "Peak period started" log messages at 5 PM
- No "Peak period ended" messages at 8 PM
- Mode changes happening during peak hours

**Check configuration:**
```bash
grep "PEAK_START_HOUR\|PEAK_END_HOUR" scripts/smart_decision.py
```

Should match your TOU schedule:
- PG&E E-TOU-D: `PEAK_START_HOUR = 17` (5 PM), `PEAK_END_HOUR = 20` (8 PM)

**Check system time:**
```bash
date
# Should be current local time in correct timezone
```

**View peak transitions:**
```bash
grep "Peak period" logs/solar_intelligence.log | tail -10
```

Should show:
```
2026-01-04 17:00:XX - 📊 Peak period started: Peak-2026-01-04
2026-01-04 20:00:XX - 📊 Peak period ended: OffPeak-2026-01-04
```

### Mode Changed During Peak Period

**This is a BUG - should never happen**

**Investigate:**
```bash
# Find any mode changes during peak hours (5-8 PM)
grep "SWITCHING" logs/solar_intelligence.log | grep " 1[7-9]:\| 20:0[0-7]"
```

**Should return NOTHING.** If you see output:

1. Check peak state configuration
2. Verify system time/timezone
3. Review code logic in `smart_decision.py`
4. Open GitHub issue with log excerpts

**Expected during peak:**
```
17:15:XX - Decision: IN PEAK PERIOD - no charging decisions (SOC: 92.3%)
17:15:XX - Mode unchanged: TOU
```

### System Charging After 8 PM

**After 8 PM, charging is allowed if needed**

**Check if this is emergency charging:**
```bash
grep "2026-01-04 2[0-3]:" logs/solar_intelligence.log | grep -i "emergency\|out of time"
```

**Normal post-peak behavior:**
- 8:00 PM: Peak ends, system resumes decisions
- 8:00-11:59 PM: Will charge if SOC low and peak tomorrow imminent
- This is correct if SOC is below target

**Abnormal:**
- Emergency charging immediately after 8 PM with high SOC
- This was the bug in V1 that V2 fixes

---

## Mode Switching Issues

### Modes Not Switching When Expected

**Verify modes are actually different:**
```bash
cat logs/last_mode.txt
# Should show: BACKUP or TOU
```

**Check recent decisions:**
```bash
tail -50 logs/solar_intelligence.log | grep "Decision:\|Mode"
```

**Expected patterns:**

**Switching to grid charging:**
```
Decision: Out of time! Must start now (need 2.1h, have 1.8h)
Action: Grid charge
🔌 SWITCHING TO EMERGENCY BACKUP MODE (grid charging)
Mode changed: TOU → BACKUP
```

**Staying on solar:**
```
Decision: Solar can provide ~18.7% (need 15.3%), 3.245kW looks promising
Action: Solar-first (TOU mode)
Mode unchanged: TOU
```

### Switch Scripts Fail

**Test mode switching manually:**
```bash
cd /volume1/docker/franklin
source venv311/bin/activate

# Try switching to BACKUP
./scripts/switch_to_backup_v2.py

# Expected output:
# Authenticating with Franklin WH...
# Creating client...
# Switching to Emergency Backup mode...
# ✓ Successfully switched to Emergency Backup mode
```

**If it fails:**
1. Check API credentials
2. Verify internet connection
3. Test with `get_battery_status.py` first
4. Check Franklin WH app shows system online

**Switch back:**
```bash
./scripts/switch_to_tou_v2.py
```

### Battery Not Charging in BACKUP Mode

**Verify mode in Franklin WH app:**
- Open Franklin WH mobile app
- Check current mode setting
- Should show "Emergency Backup" when BACKUP mode

**If mode switch succeeded but battery not charging:**
1. Check battery SOC - may already be at target
2. Check time - may be in off-peak period with solar available
3. Verify battery system is functioning (check app for errors)
4. Check grid connection is active

---

## Log Analysis

### Understanding Log Entries

**Normal decision log entry:**
```
2026-01-04 10:45:23 - ======================================================================
2026-01-04 10:45:23 - SOC: 67.3%, Solar: 3.245kW, Status: 6.2h to peak
2026-01-04 10:45:23 - Decision: Solar can provide ~18.7% (need 27.7%), 3.245kW looks promising
2026-01-04 10:45:23 - Action: Solar-first (TOU mode)
2026-01-04 10:45:23 - Mode unchanged: TOU
```

**Components:**
- **SOC:** Current battery charge percentage
- **Solar:** Current solar production in kW
- **Status:** Hours until next peak OR "IN PEAK"
- **Decision:** Reasoning for choice (grid charge vs solar wait)
- **Action:** What system will do
- **Mode:** Current/new battery mode

### Finding Specific Events

**Peak transitions:**
```bash
grep "Peak period" logs/solar_intelligence.log
```

**Mode changes:**
```bash
grep "Mode changed" logs/solar_intelligence.log
```

**Emergency charging:**
```bash
grep -i "emergency\|out of time" logs/solar_intelligence.log
```

**API errors:**
```bash
grep -i "error\|failed\|timeout" logs/solar_intelligence.log
```

**Successful decisions:**
```bash
grep "Decision made" logs/solar_intelligence.log | tail -20
```

### Log Rotation

**Logs growing too large:**
```bash
# Check log size
du -h logs/*.log

# Archive old logs
cd logs
tar -czf archive-$(date +%Y%m%d).tar.gz *.log
rm *.log

# System will create new log files automatically
```

**Set up automatic log rotation (Linux):**
```bash
sudo nano /etc/logrotate.d/franklin

# Add:
/opt/franklin/logs/*.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
```

---

## Getting Help

### Before Asking for Help

1. **Check this troubleshooting guide**
2. **Review logs for errors:**
   ```bash
   tail -100 logs/solar_intelligence.log
   ```
3. **Test scripts manually:**
   ```bash
   ./scripts/smart_decision.py
   ./scripts/get_battery_status.py
   ```
4. **Verify configuration:**
   ```bash
   grep "USERNAME\|GATEWAY_ID\|PEAK" scripts/smart_decision.py
   ```

### Gathering Information for Support

**Include this information when reporting issues:**

```bash
# System info
uname -a
python --version

# Recent logs (sanitize credentials first!)
tail -100 logs/solar_intelligence.log > debug_log.txt

# Current state
cat logs/peak_state.txt
cat logs/last_mode.txt

# Configuration (remove actual credentials!)
grep -E "PEAK_START|PEAK_END|TARGET_SOC" scripts/smart_decision.py

# Task schedule
# Synology:
sudo /usr/syno/bin/synoschedtask --get | grep -A 10 "Smart Battery"
# Linux:
crontab -l
```

### Where to Get Help

**GitHub Issues:**
1. Check existing issues: https://github.com/YOUR-USERNAME/FranklinWH-Automation/issues
2. Open new issue with:
   - Descriptive title
   - System details (NAS model, OS version, Python version)
   - Log excerpts (sanitized)
   - What you expected vs what happened
   - Steps to reproduce

**GitHub Discussions:**
- For questions about setup or configuration
- Sharing your customizations
- Discussing feature ideas

### Common Issue Templates

**API Timeout Issue:**
```
**System:** Synology DS1234+ DSM 7.2.2
**Python:** 3.11.2
**Problem:** Smart decision script times out on first 2-3 attempts

**Log excerpt:**
Attempt 1 starting...
✗ Attempt 1 failed: Device response timed out
Attempt 2 starting...
✗ Attempt 2 failed: Device response timed out
Attempt 3 starting...
✓ Success on attempt 3

**Question:** Is this normal? Should I increase retry count?
```

**Peak State Issue:**
```
**System:** Raspberry Pi 4, Ubuntu 22.04
**Python:** 3.11.4
**Problem:** No "Peak period started" log at 5 PM

**Configuration:**
PEAK_START_HOUR = 17
PEAK_END_HOUR = 20

**System time:**
Sat Jan 4 17:15:00 PST 2026

**Peak state file:**
OffPeak-2026-01-04

**Expected:** Should transition to Peak-2026-01-04 at 5 PM
```

---

## Advanced Debugging

### Enable More Verbose Logging

**Edit smart_decision.py to add debug output:**
```python
# After imports, add:
import logging
logging.basicConfig(level=logging.DEBUG)

# Or modify log_intelligence function to be more verbose
```

### Monitor Real-Time

**Watch logs as they happen:**
```bash
tail -f logs/solar_intelligence.log
```

**Watch task execution (Synology):**
```bash
# In separate terminal
watch -n 60 'sudo /usr/syno/bin/synoschedtask --get | grep -A 5 "Smart Battery"'
```

### Test API Connectivity

```bash
# Python interactive test
source venv311/bin/activate
python3

>>> from franklinwh import Client, TokenFetcher
>>> fetcher = TokenFetcher("YOUR_EMAIL", "YOUR_PASSWORD")
>>> client = Client(fetcher, "YOUR_GATEWAY_ID")
>>> import asyncio
>>> stats = asyncio.run(client.get_stats())
>>> print(f"SOC: {stats.current.battery_soc}%")
>>> exit()
```

---

## Known Limitations

### Franklin Cloud API
- **Timeout frequency:** API can be slow, that's why we retry 5 times
- **Rate limiting:** Not documented, but excessive requests may be throttled
- **Maintenance windows:** Franklin may have service disruptions

### System Limitations
- **Local API:** Not currently available (installer access required)
- **Multiple peak periods:** Current code supports one continuous peak
- **Holiday rates:** No automatic adjustment for holiday TOU schedules

### Workarounds

**For multiple peak periods:**
Modify `update_peak_state()` in `smart_decision.py` to check multiple time ranges.

**For slow API:**
Increase retry count and delay in `get_stats_with_retry()`.

**For holiday schedules:**
Manually adjust `PEAK_START_HOUR` and `PEAK_END_HOUR` on holidays or disable task.

---

**Still stuck?** Open a GitHub issue with your logs and system details!

---

**Last Updated:** January 4, 2026  
**Version:** 2.0
