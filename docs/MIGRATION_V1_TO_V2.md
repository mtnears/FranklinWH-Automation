# Migration Guide: V1 (Three-Tier) → V2 (Smart 15-Minute)

> **Note:** This guide is for migrating from V1 to V2. If you're already on V2 and want to upgrade to V3.0 (configuration-driven), see [UPGRADE_v3.md](UPGRADE_v3.md).

**If you're already running the old three-tier automation system, this guide will help you migrate to the new 15-minute smart decision system.**

---

## Overview

### What Changed

**Version 1 (OLD):**
- Three separate scripts: `morning_solar_intelligence.py`, `midday_charge_check.py`, `final_safety_check.py`
- Ran at fixed times: 8 AM, 2 PM, 3:30 PM
- No protection against mode changes during peak
- Bug: Could start emergency charging after 8 PM thinking peak was imminent

**Version 2 (NEW):**
- Single script: `smart_decision.py`
- Runs every 15 minutes throughout the day
- Peak state tracking prevents mode changes during 5-8 PM
- Handles midnight rollover and edge cases properly
- More robust with 5-attempt API retry logic

**Version 3 (CURRENT):**
- All settings via `.env` file - no editing Python scripts
- Optional features: dynamic pricing, weather integration
- Web dashboard for monitoring
- See [UPGRADE_v3.md](UPGRADE_v3.md) for details

### Why Migrate?

1. **Fixes Critical Bug** - Old system could charge during expensive evening hours
2. **More Intelligent** - Makes decisions every 15 minutes instead of 3 times/day
3. **Better Reliability** - Retry logic handles Franklin Cloud API timeouts
4. **Simpler Maintenance** - One script instead of three
5. **More Responsive** - Adapts to changing solar conditions throughout the day

---

## Pre-Migration Checklist

Before you start, verify you have:

- [ ] SSH/terminal access to your NAS or server
- [ ] Root/sudo access
- [ ] Backup of your current scripts (just in case)
- [ ] Note of your current Task Scheduler task IDs
- [ ] Franklin WH credentials handy

---

## Step 1: Backup Current System

### Create Backup Directory

```bash
# SSH into your NAS
ssh admin@your-nas-ip
sudo -i

# Create backup
cd /volume1/docker/franklin
mkdir -p backup-v1-$(date +%Y%m%d)
cp *.py backup-v1-$(date +%Y%m%d)/
```

### Document Current Tasks

```bash
# List all current tasks
sudo /usr/syno/bin/synoschedtask --get | grep -A 10 "Name:"

# Save output to file for reference
sudo /usr/syno/bin/synoschedtask --get > task_backup_$(date +%Y%m%d).txt
```

---

## Step 2: Download New Scripts

### Option A: Git Pull (Recommended)

```bash
cd /volume1/docker/franklin
git fetch origin
git checkout main
git pull
```

### Option B: Manual Download

Download files from GitHub and copy to `/volume1/docker/franklin/`

### Set Permissions

```bash
chmod +x *.py
chmod +x *.sh
```

---

## Step 3: Configure Using .env File

As of v3.0, configuration is done via a `.env` file:

```bash
# Copy example file
cp .env.example .env

# Edit with your settings
nano .env
```

**Set your credentials:**
```bash
FRANKLIN_USERNAME=your_email@example.com
FRANKLIN_PASSWORD=your_password
FRANKLIN_GATEWAY_ID=your_gateway_id
BATTERY_CAPACITY_KWH=30
CHARGE_RATE_PER_HOUR=32
```

### Verify Peak Hours Match Your Utility

```bash
PEAK_START_HOUR=17
PEAK_END_HOUR=20
```

### Test the New Script

```bash
cd /volume1/docker/franklin
source venv311/bin/activate
python smart_decision.py
```

**Expected output:**
```
Attempting to get battery stats (max 5 attempts)...
Attempt 1 starting...
✓ Success on first attempt
✓ Decision made: TOU mode (Low solar (0.45kW) but time buffer OK (18.2h left))
```

---

## Step 4: Disable Old Tasks

**IMPORTANT:** Do NOT delete old tasks yet, just disable them in case you need to rollback.

### Via Synology Web Interface

1. Open **Control Panel** → **Task Scheduler**
2. Find these tasks:
   - Morning Solar Intelligence
   - Midday Charge Check
   - Final Safety Check
3. For each task: Select → Edit → **Uncheck "Enabled"** → OK

---

## Step 5: Create New Task

### Via Synology Web Interface

1. **Control Panel** → **Task Scheduler** → **Create** → **Scheduled Task** → **User-defined script**

2. **General tab:**
   - Task: `Smart Battery Decision - Every 15 minutes`
   - User: `root`
   - Enabled: ✓ (checked)

3. **Schedule tab:**
   - Date: Daily
   - First run time: `00:00`
   - Frequency: `Every 15 minutes`
   - Last run time: `23:45`

4. **Task Settings tab:**
   - User-defined script:
     ```bash
     #!/bin/bash
     cd /volume1/docker/franklin
     /volume1/docker/franklin/run_smart_decision.sh
     ```

5. Click **OK**

---

## Step 6: Monitor New System

### First Hour - Watch Closely

```bash
tail -f /volume1/docker/franklin/logs/solar_intelligence.log
```

### First 24 Hours - Key Checkpoints

**5:00 PM** - Peak period start
```bash
grep "Peak period started" logs/solar_intelligence.log
```

**During peak (5-8 PM)** - Verify no mode changes
```bash
grep "SWITCHING" logs/solar_intelligence.log | grep "1[7-9]:"
# Should return NOTHING
```

**8:00 PM** - Peak period end
```bash
grep "Peak period ended" logs/solar_intelligence.log
```

---

## Step 7: Clean Up (After 1 Week)

**Wait at least 1 week** before cleaning up old scripts.

```bash
cd /volume1/docker/franklin
mkdir archive-old-system
mv morning_solar_intelligence.py archive-old-system/
mv midday_charge_check.py archive-old-system/
mv final_safety_check.py archive-old-system/
```

Delete old disabled tasks from Task Scheduler.

---

## Rollback Plan

If the new system isn't working:

```bash
# Disable new task
# Re-enable old tasks in Task Scheduler

# Restore old scripts from backup
cd /volume1/docker/franklin
cp backup-v1-YYYYMMDD/*.py .
```

---

## Next: Upgrade to V3.0

Once you're comfortable with V2, consider upgrading to V3.0 for:
- Configuration via `.env` file (no more editing Python scripts)
- Optional dynamic pricing support
- Web dashboard
- Better modularity

See [UPGRADE_v3.md](UPGRADE_v3.md) for instructions.

---

**Last Updated:** February 2026  
**Migration Guide Version:** 2.1
