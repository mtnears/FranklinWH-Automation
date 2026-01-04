# Migration Guide: V1 (Three-Tier) → V2 (Smart 15-Minute)

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

**Write down your task IDs:**
- Morning Solar Intelligence: Task ID ___
- Midday Charge Check: Task ID ___
- Final Safety Check: Task ID ___
- Continuous Monitoring (if used): Task ID ___

### Backup Logs (Optional)

```bash
cd /volume1/docker/franklin/logs
tar -czf backup_logs_$(date +%Y%m%d).tar.gz *.log *.csv
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

Download these files from GitHub:
- `scripts/smart_decision.py`
- `scripts/run_smart_decision.sh`
- Updated versions of monitoring scripts (optional)

Copy to `/volume1/docker/franklin/`

### Set Permissions

```bash
chmod +x smart_decision.py
chmod +x run_smart_decision.sh
chmod +x switch_to_backup_v2.py
chmod +x switch_to_tou_v2.py
chmod +x get_battery_status.py
```

---

## Step 3: Configure New Scripts

### Update Credentials in smart_decision.py

```bash
nano smart_decision.py
```

Find and replace:
```python
USERNAME = "YOUR_EMAIL@example.com"  # Replace with your Franklin WH email
PASSWORD = "YOUR_PASSWORD"           # Replace with your password
GATEWAY_ID = "YOUR_GATEWAY_ID"      # Replace with your gateway ID
```

**Important:** These should be the SAME credentials you used in the old scripts.

### Verify Peak Hours Match Your Utility

```python
PEAK_START_HOUR = 17  # 5 PM - Adjust if your peak is different
PEAK_END_HOUR = 20    # 8 PM - Adjust if your peak is different
```

### Test the New Script

```bash
cd /volume1/docker/franklin
source venv311/bin/activate
./smart_decision.py
```

**Expected output:**
```
Attempting to get battery stats (max 5 attempts)...
Attempt 1 starting...
✓ Success on first attempt
✓ Decision made: TOU mode (Low solar (0.45kW) but time buffer OK (18.2h left))
```

If you see errors, verify credentials and network connectivity.

---

## Step 4: Disable Old Tasks

**IMPORTANT:** Do NOT delete old tasks yet, just disable them in case you need to rollback.

### Via Synology Web Interface

1. Open **Control Panel** → **Task Scheduler**
2. Find these tasks:
   - Morning Solar Intelligence
   - Midday Charge Check
   - Final Safety Check
   - Continuous Monitoring (if present)
3. For each task:
   - Select it
   - Click **Edit**
   - **Uncheck "Enabled"**
   - Click **OK**

### Via Command Line

```bash
# List tasks to find IDs
sudo /usr/syno/bin/synoschedtask --get | grep -B 5 "morning_solar\|midday_charge\|final_safety"

# Disable each task (replace X with actual task ID)
sudo /usr/syno/bin/synoschedtask --disable id=X
```

**Verify tasks are disabled:**
```bash
sudo /usr/syno/bin/synoschedtask --get | grep "State:"
```

Should show `State: [disabled]` for old tasks.

---

## Step 5: Create New Task

### Via Synology Web Interface (Recommended)

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
   - Send run details by email: ✓ (checked if you want notifications)
   - Email: `your-email@example.com`
   - Send run details only when script terminates abnormally: ✓ (checked)
   - User-defined script:
     ```bash
     #!/bin/bash
     cd /volume1/docker/franklin
     /volume1/docker/franklin/run_smart_decision.sh
     ```

5. Click **OK**

### Via Command Line

```bash
# Create task
sudo /usr/syno/bin/synoschedtask --create \
  name="Smart Battery Decision - Every 15 minutes" \
  user="root" \
  enabled=true \
  type="daily" \
  run_hour=0 \
  run_min=0 \
  repeat_min=15 \
  last_work_hour=23 \
  cmd="#!/bin/bash\ncd /volume1/docker/franklin\n/volume1/docker/franklin/run_smart_decision.sh"
```

---

## Step 6: Monitor New System

### First Hour - Watch Closely

```bash
# Watch the log in real-time
tail -f /volume1/docker/franklin/logs/solar_intelligence.log
```

**What to look for:**
- Script runs every 15 minutes
- "✓ Success" messages for API calls
- Decision reasoning logged
- Peak state updates (if near 5 PM or 8 PM)

**Example good output:**
```
2026-01-04 09:00:23 - Attempting to get battery stats (max 5 attempts)...
2026-01-04 09:00:23 - Attempt 1 starting...
2026-01-04 09:00:25 - ✓ Success on first attempt
2026-01-04 09:00:25 - ======================================================================
2026-01-04 09:00:25 - SOC: 67.3%, Solar: 3.245kW, Status: 8.0h to peak
2026-01-04 09:00:25 - Decision: Solar can provide ~18.7% (need 27.7%), 3.245kW looks promising
2026-01-04 09:00:25 - Action: Solar-first (TOU mode)
2026-01-04 09:00:25 - Mode unchanged: TOU
```

### First 24 Hours - Key Checkpoints

**9:00 AM** - Morning decision
```bash
grep "2026-01-04 09:" /volume1/docker/franklin/logs/solar_intelligence.log | tail -20
```

**5:00 PM** - Peak period start
```bash
grep "Peak period started" /volume1/docker/franklin/logs/solar_intelligence.log
```

**During peak (5-8 PM)** - Verify no mode changes
```bash
grep "2026-01-04 1[7-9]:" /volume1/docker/franklin/logs/solar_intelligence.log | grep "SWITCHING"
# Should return NOTHING
```

**8:00 PM** - Peak period end
```bash
grep "Peak period ended" /volume1/docker/franklin/logs/solar_intelligence.log
```

**After 8 PM** - Verify NO emergency charging
```bash
grep "2026-01-04 2[0-3]:" /volume1/docker/franklin/logs/solar_intelligence.log | grep "EMERGENCY"
# Should return NOTHING
```

---

## Step 7: Verify Task Scheduler

### Check Task is Running

```bash
# View task details
sudo /usr/syno/bin/synoschedtask --get | grep -A 20 "Smart Battery Decision"
```

**Should show:**
- State: `[enabled]`
- Repeat every: `[15] min`
- Last Run Time: Recent timestamp
- Status: `[Success]`

### Check Task Scheduler History

1. **Control Panel** → **Task Scheduler**
2. Select your new task
3. Click **Action** → **View Result**
4. Should show successful runs every 15 minutes

---

## Step 8: Update Monitoring (Optional)

If you were using email notifications or status reports:

### Update Milestone Emailer

```bash
nano milestone_emailer.py
```

Replace credentials and configure as desired. This is now optional since the new system is more self-sufficient.

### Update Daily Status Report

```bash
nano daily_status_report.py
```

Same credentials as `smart_decision.py`. Keep this task if you want daily summaries.

---

## Step 9: Clean Up Old Scripts (After 1 Week)

**Wait at least 1 week** to ensure new system is working properly before cleaning up.

### Delete Old Scripts

```bash
cd /volume1/docker/franklin

# Remove old three-tier scripts
rm morning_solar_intelligence.py
rm midday_charge_check.py
rm final_safety_check.py
rm continuous_monitor.py  # If you had this

# Or move to archive
mkdir archive-old-system
mv morning_solar_intelligence.py archive-old-system/
mv midday_charge_check.py archive-old-system/
mv final_safety_check.py archive-old-system/
mv continuous_monitor.py archive-old-system/
```

### Delete Old Tasks

1. **Control Panel** → **Task Scheduler**
2. Select old disabled tasks
3. Click **Delete**
4. Confirm deletion

Or via command line:
```bash
# Delete task by ID
sudo /usr/syno/bin/synoschedtask --delete id=X
```

---

## Rollback Plan (If Needed)

If the new system isn't working:

### Quick Rollback

```bash
# Stop new task
sudo /usr/syno/bin/synoschedtask --disable id=NEW_TASK_ID

# Re-enable old tasks
sudo /usr/syno/bin/synoschedtask --enable id=OLD_TASK_ID_1
sudo /usr/syno/bin/synoschedtask --enable id=OLD_TASK_ID_2
sudo /usr/syno/bin/synoschedtask --enable id=OLD_TASK_ID_3

# Restore old scripts from backup
cd /volume1/docker/franklin
cp backup-v1-YYYYMMDD/*.py .
```

---

## Comparison: Side-by-Side

### Old System (V1)
```
Scheduled Tasks:
├─ Morning Solar Intelligence (8:00 AM)
│  └─ Analyze overnight, decide if grid charge needed
├─ Midday Charge Check (2:00 PM)
│  └─ Check progress, start grid charge if needed
└─ Final Safety Check (3:30 PM)
   └─ Emergency charge if SOC < 75%

Problems:
❌ Only 3 decision points per day
❌ Could miss changing solar conditions
❌ Bug: Emergency charging after 8 PM
❌ No peak period protection
❌ API timeouts caused failures
```

### New System (V2)
```
Scheduled Task:
└─ Smart Battery Decision (Every 15 minutes)
   ├─ Get battery stats (with 5 retries)
   ├─ Update peak state
   ├─ IF in peak: NO ACTION
   └─ ELSE: Make intelligent decision
       ├─ Analyze current situation
       ├─ Calculate time to peak
       ├─ Decide: grid charge or wait for solar
       └─ Switch modes if needed

Benefits:
✓ 96 decision points per day
✓ Responds to changing conditions
✓ NO charging during peak period
✓ Handles midnight rollover properly
✓ Robust retry logic
✓ Simpler maintenance
```

---

## Troubleshooting Migration Issues

### New script not running every 15 minutes

**Check task schedule:**
```bash
sudo cat /usr/syno/etc/synoschedule.d/root/TASK_ID.task
```

Should show `repeat min=15`

### "Cannot import franklinwh" error

**Verify virtual environment:**
```bash
cd /volume1/docker/franklin
source venv311/bin/activate
pip list | grep franklinwh
```

Should show `franklinwh 0.13.0` or similar.

If missing:
```bash
pip install --break-system-packages franklinwh
```

### Peak state file not being created

**Check directory permissions:**
```bash
ls -la /volume1/docker/franklin/logs/
```

Should have write permissions for root.

**Manually create state file (system will update it):**
```bash
mkdir -p /volume1/docker/franklin/logs
echo "OffPeak-$(date +%Y-%m-%d)" > /volume1/docker/franklin/logs/peak_state.txt
```

### Still seeing old behavior

**Verify old tasks are disabled:**
```bash
sudo /usr/syno/bin/synoschedtask --get | grep -E "morning_solar|midday_charge|final_safety" -A 5
```

All should show `State: [disabled]`

---

## Post-Migration Checklist

After 1 week of successful operation:

- [ ] New task running reliably every 15 minutes
- [ ] No API timeout failures (or only occasional with successful retries)
- [ ] Peak state transitions logging correctly at 5 PM and 8 PM
- [ ] NO mode changes during peak period (5-8 PM)
- [ ] NO emergency charging after 8 PM
- [ ] Battery reaching 95% SOC before 5 PM consistently
- [ ] Old tasks deleted
- [ ] Old scripts removed or archived
- [ ] Backup directory can be deleted (optional)

---

## Getting Help

### Check Logs First

```bash
# Last 100 lines of intelligence log
tail -100 /volume1/docker/franklin/logs/solar_intelligence.log

# Search for errors
grep -i error /volume1/docker/franklin/logs/solar_intelligence.log

# Check peak transitions
grep "Peak period" /volume1/docker/franklin/logs/solar_intelligence.log
```

### Common Issues

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for detailed solutions.

### Still Having Problems?

1. Check GitHub Issues for similar problems
2. Post your issue with:
   - Log excerpts (sanitize credentials!)
   - System details (Synology model, DSM version)
   - What you expected vs what happened
3. Include output from:
   ```bash
   ./smart_decision.py
   cat /volume1/docker/franklin/logs/peak_state.txt
   ```

---

## Success Indicators

**You'll know migration was successful when you see:**

1. **Consistent logging every 15 minutes**
2. **Peak state transitions at correct times:**
   ```
   17:00:XX - 📊 Peak period started: Peak-YYYY-MM-DD
   20:00:XX - 📊 Peak period ended: OffPeak-YYYY-MM-DD
   ```
3. **No mode changes during peak:**
   ```
   17:15:XX - Decision: IN PEAK PERIOD - no charging decisions
   ```
4. **Intelligent decisions with reasoning:**
   ```
   09:23:XX - Decision: Solar can provide ~18.7%, 3.245kW looks promising
   14:47:XX - Decision: Low solar (0.89kW) and running out of time (0.3h buffer left)
   ```
5. **95%+ SOC achieved before 5 PM daily**

---

**Congratulations on migrating to V2!** 🎉

Your battery automation is now more intelligent, more reliable, and bug-free.

**Questions?** Open a GitHub issue or discussion.

---

**Last Updated:** January 4, 2026  
**Migration Guide Version:** 2.0
