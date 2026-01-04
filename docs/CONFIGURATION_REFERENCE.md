# Configuration Reference Guide

**Complete guide to configuring Franklin WH Battery Automation**

This document explains ALL configuration variables, where to find them, how to set them, and which scripts need them.

---

## Table of Contents

- [Quick Start Checklist](#quick-start-checklist)
- [Required Configuration](#required-configuration)
- [Battery Performance Testing](#battery-performance-testing)
- [Optional Configuration](#optional-configuration)
- [Configuration by Script](#configuration-by-script)
- [Troubleshooting Configuration](#troubleshooting-configuration)

---

## Quick Start Checklist

**Gather these BEFORE you begin installation:**

### Required Information

- [ ] **Franklin WH Username** (your account email)
- [ ] **Franklin WH Password**
- [ ] **Franklin WH Gateway ID** (from mobile app)
- [ ] **Your Utility's Peak Period Hours** (e.g., 5 PM - 8 PM)
- [ ] **Battery Charge Rate** (requires 30-60 min test - see below)

### Optional Information

- [ ] Weather Underground Station ID (if using weather tracking)
- [ ] Weather Underground API Key
- [ ] PVOutput API Key (if using solar tracking)
- [ ] PVOutput System ID(s)
- [ ] Email SMTP settings (if using notifications)

---

## Required Configuration

### 1. Franklin WH Credentials

**What:** Your Franklin WH account credentials and gateway identifier

**Where to find:**
- **Username:** The email you use to log into Franklin WH mobile app
- **Password:** Your Franklin WH account password
- **Gateway ID:** Franklin WH mobile app → Settings → System Info → Gateway ID

**Format:**
```python
USERNAME = "your-email@example.com"
PASSWORD = "YourPassword123!"
GATEWAY_ID = "10060005A02X24470437"  # 20-character alphanumeric
```

**Which scripts need this:**
- `scripts/smart_decision.py` ⭐ CRITICAL
- `scripts/switch_to_backup_v2.py`
- `scripts/switch_to_tou_v2.py`
- `scripts/get_battery_status.py`
- `scripts/milestone_emailer.py` (optional)

---

### 2. Peak Period Hours

**What:** The hours when your utility charges peak/expensive electricity rates

**Why it matters:** System needs to know when NOT to change modes and when to have battery fully charged

**How to find:**
1. Check your utility bill for "Time-of-Use" or "TOU" rate schedule
2. Look for "Peak Period" or "On-Peak Hours"
3. Common schedules:
   - **PG&E E-TOU-D:** 5 PM - 8 PM (hours 17-20)
   - **SCE TOU-D-4-9PM:** 4 PM - 9 PM (hours 16-21)
   - **SDG&E TOU-DR1:** 4 PM - 9 PM (hours 16-21)

**Configuration:**
```python
# In scripts/smart_decision.py
PEAK_START_HOUR = 17  # 5 PM (use 24-hour format: 0-23)
PEAK_END_HOUR = 20    # 8 PM
```

**24-Hour Format Reference:**
- 12 AM (midnight) = 0
- 1 AM = 1
- 12 PM (noon) = 12
- 1 PM = 13
- 5 PM = 17
- 8 PM = 20
- 9 PM = 21

**Which scripts need this:**
- `scripts/smart_decision.py` ⭐ CRITICAL

---

### 3. Battery Charge Rate (REQUIRES TESTING)

**What:** How fast your battery charges from the grid, measured in % per hour

**Why it matters:** System uses this to calculate when it must start grid charging to reach target SOC before peak period

**⚠️ CRITICAL:** This varies by battery size and inverter capacity. You MUST test YOUR system.

#### How to Test Your Battery Charge Rate

**Prerequisites:**
- Battery SOC below 80% (more room to charge = better measurement)
- Nighttime or cloudy day (no solar interference)
- No major loads running (for cleaner measurement)

**Step-by-Step Test Procedure:**

1. **Switch to BACKUP mode** (starts grid charging):
   ```bash
   cd /volume1/docker/franklin
   source venv311/bin/activate
   ./scripts/switch_to_backup_v2.py
   ```

2. **Note starting SOC and time:**
   ```bash
   ./scripts/get_battery_status.py
   ```
   Record:
   - Starting SOC: _____% 
   - Start time: _____

3. **Wait 30-60 minutes** (let it charge)

4. **Check SOC again:**
   ```bash
   ./scripts/get_battery_status.py
   ```
   Record:
   - Ending SOC: _____%
   - End time: _____

5. **Calculate charge rate:**
   ```
   SOC Increase = Ending SOC - Starting SOC
   Time Elapsed = (End time - Start time) in hours
   
   Charge Rate = SOC Increase ÷ Time Elapsed
   ```

6. **Switch back to TOU mode:**
   ```bash
   ./scripts/switch_to_tou_v2.py
   ```

#### Example Calculation

**Test Results:**
- Starting SOC: 50%
- Start time: 8:00 PM
- Ending SOC: 66%
- End time: 8:30 PM

**Calculation:**
- SOC Increase: 66% - 50% = 16%
- Time Elapsed: 30 minutes = 0.5 hours
- **Charge Rate: 16% ÷ 0.5 = 32% per hour**

#### Expected Ranges by Battery Size

These are **estimates** - always test YOUR system:

| Battery Size | Typical Grid Charge (kW) | Estimated %/hour |
|--------------|-------------------------|------------------|
| 13.6 kWh | ~5 kW | ~35-40%/hour |
| 30 kWh | ~10 kW | ~30-35%/hour |
| 40 kWh | ~10 kW | ~22-27%/hour |
| 60 kWh | ~10 kW | ~15-18%/hour |

**Configuration:**
```python
# In scripts/smart_decision.py
CHARGE_RATE_PER_HOUR = 32.0  # Replace with YOUR measured rate
```

**Which scripts need this:**
- `scripts/smart_decision.py` ⭐ CRITICAL

---

### 4. Target State of Charge

**What:** The battery charge percentage you want to reach before peak period

**Default:** 95%

**Why 95%:** 
- Fully charged but leaves small buffer
- Ensures maximum capacity during peak period
- Accounts for minor measurement variations

**When to adjust:**
- **Lower to 90%:** If consistently reaching 95% too early (wasting solar opportunity)
- **Keep at 95%:** If occasionally falling short on cloudy days

**Configuration:**
```python
# In scripts/smart_decision.py
TARGET_SOC = 95.0  # Target battery charge % before peak
```

**Which scripts need this:**
- `scripts/smart_decision.py`

---

## Optional Configuration

### 5. Advanced Decision Thresholds

**Most users should leave these at defaults.** Adjust only if you understand the implications.

#### Safety Margin Hours

**What:** Buffer time added when calculating "must start charging by" deadline

**Default:** 0.5 hours (30 minutes)

**Why:** Adds safety buffer for API delays, charge rate variations

**When to adjust:**
- **Increase to 1.0:** If you want more conservative (earlier) grid charging
- **Decrease to 0.25:** If you want to maximize solar waiting time (riskier)

```python
SAFETY_MARGIN_HOURS = 0.5
```

#### Minimum Solar for Wait

**What:** Minimum solar production (kW) required to consider "waiting for solar" vs grid charging

**Default:** 0.5 kW

**Why:** Below this threshold, solar is too weak to meaningfully charge battery

**When to adjust:**
- **Increase to 1.0:** If you have large solar array and want to wait for better production
- **Decrease to 0.3:** If you have small solar array and want to use any available solar

```python
MIN_SOLAR_FOR_WAIT = 0.5  # Minimum kW to wait vs grid charge
```

**Which scripts need this:**
- `scripts/smart_decision.py`

---

### 6. Weather Underground (Optional)

**What:** Collects weather data for correlation analysis with solar production

**Benefits:** 
- Track weather conditions vs solar output
- Historical weather correlation
- No impact on automation decisions (just data collection)

**Setup:**

1. **Create free account:** https://www.wunderground.com/member/api-keys
2. **Get API key** from account settings
3. **Find nearby PWS (Personal Weather Station):**
   - Go to wunderground.com
   - Search your location
   - Find nearest active PWS
   - Note the Station ID (e.g., "KCAGEORG58")

**Configuration:**
```python
# In scripts/collect_weather.py
PWS_ID = "KCAGEORG58"  # Your station ID
API_KEY = "a3ce125ca7c04af88e125ca7c0aaf859"  # Your WU API key
```

**Task Scheduler:** Set up to run every 15 minutes (see TASK_SCHEDULER.md)

**Which scripts need this:**
- `scripts/collect_weather.py` (optional)

---

### 7. PVOutput (Optional)

**What:** Tracks daily solar production for long-term analysis

**Benefits:**
- Historical production tracking
- Compare with other solar systems
- Free graphing and analysis tools
- No impact on automation decisions

**Setup:**

1. **Create free account:** https://pvoutput.org/register.jsp
2. **Add your solar system(s):**
   - Settings → Add System
   - Enter system size, orientation, etc.
3. **Get API key:** Account Settings → API Settings
4. **Get System ID:** Displayed on your system page

**Configuration:**
```python
# In scripts/collect_pvoutput.py
API_KEY = "YOUR_PVOUTPUT_API_KEY"
GROUND_MOUNT_SID = "12345"  # Your first system ID
HOUSE_SID = "67890"         # Second system (if you have one)
```

**If you only have one system:**
```python
API_KEY = "YOUR_PVOUTPUT_API_KEY"
GROUND_MOUNT_SID = "12345"
# Just comment out or remove HOUSE_SID line
```

**Task Scheduler:** Set up to run hourly (see TASK_SCHEDULER.md)

**Which scripts need this:**
- `scripts/collect_pvoutput.py` (optional)

---

### 8. Email Notifications (Optional)

**What:** Send status emails during testing/monitoring

**Used by:**
- `milestone_emailer.py` - Hourly status during testing
- `daily_status_report.py` - Daily summary (can be emailed or just logged)

**Gmail Setup (Recommended):**

1. **Enable 2-Factor Authentication** on your Google account
2. **Create App Password:**
   - Google Account → Security → 2-Step Verification → App passwords
   - Generate password for "Mail"
   - Copy the 16-character password

**Configuration:**
```python
# In scripts/milestone_emailer.py
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "your-email@gmail.com"
SENDER_PASSWORD = "abcd efgh ijkl mnop"  # App password (16 chars with spaces)
RECIPIENT_EMAIL = "your-email@gmail.com"
MILESTONES = [8, 10, 12, 14, 16]  # Hours to send status emails
```

**Other Email Providers:**
- **Outlook/Hotmail:** `smtp.office365.com`, port 587
- **Yahoo:** `smtp.mail.yahoo.com`, port 587
- **Custom SMTP:** Check your email provider's documentation

**Which scripts need this:**
- `scripts/milestone_emailer.py` (optional - testing only)

---

## Configuration by Script

### Core Automation (Required)

#### smart_decision.py ⭐ MOST IMPORTANT

**Required:**
- `USERNAME` - Franklin WH email
- `PASSWORD` - Franklin WH password
- `GATEWAY_ID` - Franklin gateway ID
- `PEAK_START_HOUR` - Your TOU peak start (24-hour format)
- `PEAK_END_HOUR` - Your TOU peak end (24-hour format)
- `CHARGE_RATE_PER_HOUR` - Your tested charge rate

**Optional (Advanced):**
- `TARGET_SOC` - Default: 95.0
- `SAFETY_MARGIN_HOURS` - Default: 0.5
- `MIN_SOLAR_FOR_WAIT` - Default: 0.5

**File paths (verify these match your installation):**
- `LOG_FILE` - Default: `/volume1/docker/franklin/logs/continuous_monitoring.csv`
- `INTELLIGENCE_LOG` - Default: `/volume1/docker/franklin/logs/solar_intelligence.log`
- `STATE_FILE` - Default: `/volume1/docker/franklin/logs/last_mode.txt`
- `PEAK_STATE_FILE` - Default: `/volume1/docker/franklin/logs/peak_state.txt`

---

#### switch_to_backup_v2.py

**Required:**
- `USERNAME`
- `PASSWORD`
- `GATEWAY_ID`

---

#### switch_to_tou_v2.py

**Required:**
- `USERNAME`
- `PASSWORD`
- `GATEWAY_ID`

---

#### get_battery_status.py

**Required:**
- `USERNAME`
- `PASSWORD`
- `GATEWAY_ID`

---

### Data Collection (Optional)

#### collect_weather.py

**Required IF using:**
- `PWS_ID` - Weather Underground station ID
- `API_KEY` - Weather Underground API key
- `LOG_DIR` - Default: `/volume1/docker/franklin/logs`

---

#### collect_pvoutput.py

**Required IF using:**
- `API_KEY` - PVOutput API key
- `GROUND_MOUNT_SID` - System ID for first/only system
- `HOUSE_SID` - System ID for second system (optional)
- `LOG_DIR` - Default: `/volume1/docker/franklin/logs`

---

### Monitoring (Optional)

#### milestone_emailer.py

**Required IF using:**
- `USERNAME`
- `PASSWORD`
- `GATEWAY_ID`
- `SMTP_SERVER`
- `SMTP_PORT`
- `SENDER_EMAIL`
- `SENDER_PASSWORD`
- `RECIPIENT_EMAIL`
- `MILESTONES` - Hours to send emails (e.g., [8, 10, 12, 14, 16])

---

#### daily_status_report.py

**Required:**
- None (just runs scripts that have their own configuration)

**Optional:**
- Can be modified to email output instead of just printing

---

#### aggregate_data.py

**Required:**
- `LOG_DIR` - Default: `/volume1/docker/franklin/logs`

---

## Configuration Workflow

### Recommended Order

1. **Gather Franklin WH credentials** (5 minutes)
   - Username, password from your account
   - Gateway ID from mobile app

2. **Determine your TOU schedule** (5 minutes)
   - Check utility bill or website
   - Note peak hours in 24-hour format

3. **Test battery charge rate** (30-60 minutes)
   - Follow test procedure above
   - Calculate %/hour rate

4. **Configure core scripts** (10 minutes)
   - `smart_decision.py` with all required values
   - Mode switching scripts (same credentials)
   - `get_battery_status.py` (same credentials)

5. **Test core automation** (5 minutes)
   - Run `./scripts/smart_decision.py` manually
   - Verify it works without errors

6. **Set up Task Scheduler** (10 minutes)
   - See TASK_SCHEDULER.md
   - Every 15 minutes for smart_decision.py

7. **Optional: Configure data collection** (15 minutes if desired)
   - Weather Underground
   - PVOutput
   - Email notifications

8. **Optional: Set up monitoring tasks** (10 minutes if desired)
   - Weather collection (every 15 min)
   - PVOutput collection (hourly)
   - Daily status report (4:30 PM)

---

## Troubleshooting Configuration

### "Authentication failed"

**Problem:** Incorrect USERNAME or PASSWORD

**Solution:**
- Verify you can log into Franklin WH mobile app with same credentials
- Check for typos in script
- Ensure quotes are correct: `"email@example.com"` not `'email@example.com'`
- Password may have special characters - ensure they're escaped properly

---

### "Gateway not found"

**Problem:** Incorrect GATEWAY_ID

**Solution:**
- Verify Gateway ID in Franklin WH app: Settings → System Info
- Should be exactly 20 characters
- Common mistake: Extra spaces or missing characters
- Format: `"10060005A02X24470437"` with quotes

---

### Battery charges during peak period

**Problem:** PEAK_START_HOUR or PEAK_END_HOUR incorrect

**Solution:**
- Verify your utility's TOU schedule
- Remember 24-hour format: 5 PM = 17, not 5
- Check system timezone is correct: `date`
- Look for "Peak period started" in logs at correct time

---

### Battery not charging in time for peak

**Problem:** CHARGE_RATE_PER_HOUR set too high

**Solution:**
- Re-test your battery's actual charge rate
- System thinks it can charge faster than reality
- Lower the rate by 10-20% for safety margin
- Example: If tested at 32%/hr, try 28%/hr

---

### System charges too early (wastes solar)

**Problem:** CHARGE_RATE_PER_HOUR set too low or SAFETY_MARGIN_HOURS too high

**Solution:**
- Re-test battery charge rate (might be faster than you think)
- Reduce SAFETY_MARGIN_HOURS from 0.5 to 0.25
- Increase MIN_SOLAR_FOR_WAIT from 0.5 to 1.0

---

### Email notifications not working

**Problem:** SMTP configuration incorrect

**Solution:**
- For Gmail, ensure you're using App Password, not account password
- Verify SMTP_SERVER and SMTP_PORT for your provider
- Check firewall isn't blocking port 587
- Test with a simple Python SMTP script first

---

### Weather/PVOutput data not collecting

**Problem:** API credentials incorrect or rate limiting

**Solution:**
- Verify API keys in respective service dashboards
- Check rate limits (Weather Underground, PVOutput have limits)
- Ensure Task Scheduler is running the scripts
- Check logs for error messages

---

## Pre-Installation Worksheet

**Print or copy this section and fill it out before installation:**

```
FRANKLIN WH CREDENTIALS
-----------------------
Username (email):     _________________________________
Password:             _________________________________
Gateway ID:           _________________________________

UTILITY TOU SCHEDULE
--------------------
Utility company:      _________________________________
Peak start time:      _____ PM  = Hour _____ (24-hr)
Peak end time:        _____ PM  = Hour _____ (24-hr)

BATTERY TESTING
---------------
Test date:            _________________________________
Starting SOC:         _____%  at  _____:_____ (time)
Ending SOC:           _____%  at  _____:_____ (time)
SOC increase:         _____%
Time elapsed:         _____ hours
CHARGE RATE:          _____ % per hour

OPTIONAL SERVICES (if using)
----------------------------
Weather Underground:
  Station ID:         _________________________________
  API Key:            _________________________________

PVOutput:
  API Key:            _________________________________
  System ID 1:        _________________________________
  System ID 2:        _________________________________ (if applicable)

Email (for monitoring):
  SMTP Server:        _________________________________
  SMTP Port:          _________________________________
  Email address:      _________________________________
  App Password:       _________________________________
```

---

## Summary: Minimum Required Configuration

To get the core automation working, you MUST configure these 6 items:

1. ✅ `USERNAME` - Franklin WH email
2. ✅ `PASSWORD` - Franklin WH password
3. ✅ `GATEWAY_ID` - From Franklin mobile app
4. ✅ `PEAK_START_HOUR` - Your peak period start (24-hour)
5. ✅ `PEAK_END_HOUR` - Your peak period end (24-hour)
6. ✅ `CHARGE_RATE_PER_HOUR` - Test YOUR battery!

**Everything else is optional** and can be configured later!

---

**Next Steps:** See [INSTALLATION.md](INSTALLATION.md) for complete setup instructions.

---

**Last Updated:** January 4, 2026  
**Version:** 2.0
