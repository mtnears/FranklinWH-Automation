# Peak State Logic - How It Works

## Overview

The peak state tracking system is a critical component that prevents the automation from changing battery modes during expensive peak electricity periods. This document explains why it's needed and how it works.

---

## The Problem It Solves

### Original Bug

Without peak state tracking, the system had a critical bug that occurred after 8 PM:

**Scenario:**
1. Peak period ends at 8:00 PM
2. Script runs at 8:15 PM
3. Calculates "hours to peak" for next day's 5 PM = ~20.75 hours
4. BUT: Midnight is approaching in ~3.75 hours
5. Time calculation gets confused at midnight rollover
6. System thinks "peak is imminent" 
7. **Starts emergency grid charging during expensive late evening hours**

### Solution

Peak state tracking prevents ANY mode changes during the defined peak period (5-8 PM by default), eliminating the possibility of this bug.

---

## How It Works

### State File

**Location:** `/volume1/docker/franklin/logs/peak_state.txt`

**Contents:** One of two states
- `Peak-YYYY-MM-DD` - Currently in peak period
- `OffPeak-YYYY-MM-DD` - Currently outside peak period

**Example:**
```
Peak-2026-01-03
```

### State Transitions

The system updates the state file every 15 minutes when `smart_decision.py` runs:

```
4:45 PM → OffPeak-2026-01-03 (not in peak yet)
5:00 PM → Peak-2026-01-03     (peak starts - log message!)
5:15 PM → Peak-2026-01-03     (no change)
5:30 PM → Peak-2026-01-03     (no change)
...
7:45 PM → Peak-2026-01-03     (still in peak)
8:00 PM → OffPeak-2026-01-03  (peak ends - log message!)
8:15 PM → OffPeak-2026-01-03  (no change)
```

### Key Functions

**`update_peak_state()`** - Main state management
```python
def update_peak_state():
    """
    Update peak state based on current time.
    Returns: True if in peak period, False otherwise
    """
    now = datetime.now()
    current_hour = now.hour
    
    # Check if we're in the peak window
    in_peak_window = (PEAK_START_HOUR <= current_hour < PEAK_END_HOUR)
    
    if in_peak_window:
        # Save Peak state and return True
        new_state = f"Peak-{today_date}"
        if current_state != new_state:
            log_intelligence(f"📊 Peak period started: {new_state}")
        save_peak_state(new_state)
        return True
    else:
        # Save OffPeak state and return False
        new_state = f"OffPeak-{today_date}"
        if current_state.startswith("Peak-"):
            log_intelligence(f"📊 Peak period ended: {new_state}")
        save_peak_state(new_state)
        return False
```

**`calculate_time_to_peak()`** - Time calculation
```python
def calculate_time_to_peak():
    """Calculate hours until next peak period"""
    now = datetime.now()
    peak_today = now.replace(hour=PEAK_START_HOUR, ...)
    peak_end_today = now.replace(hour=PEAK_END_HOUR, ...)
    
    # If past today's peak END, calculate to tomorrow
    if now >= peak_end_today:
        peak_tomorrow = peak_today + timedelta(days=1)
        return (peak_tomorrow - now).total_seconds() / 3600
    
    # If before today's peak START, use today
    if now < peak_today:
        return (peak_today - now).total_seconds() / 3600
    
    # Currently IN peak, return 0
    return 0
```

**`should_charge_from_grid()`** - Decision logic with peak protection
```python
def should_charge_from_grid(soc, solar_kw, hours_to_peak, in_peak):
    """Decide: grid charge or wait for solar?"""
    
    # CRITICAL: Never change modes during peak!
    if in_peak:
        return False, f"IN PEAK PERIOD - no charging decisions"
    
    # Rest of decision logic only runs during off-peak...
```

---

## Decision Flow

### Every 15 Minutes

```
┌─────────────────────────────────────┐
│   smart_decision.py runs            │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  Get battery stats (with retry)     │
└──────────────┬──────────────────────┘
               │
               ▼
┌─────────────────────────────────────┐
│  update_peak_state()                │
│  - Check current hour               │
│  - Update state file if changed     │
│  - Return True if in peak           │
└──────────────┬──────────────────────┘
               │
               ▼
        ┌──────┴───────┐
        │              │
        ▼              ▼
    IN PEAK       OFF PEAK
        │              │
        ▼              ▼
┌──────────────┐  ┌──────────────────────┐
│ NO ACTION!   │  │ calculate_time_to... │
│              │  │ should_charge_from...│
│ Log status   │  │ Make decision        │
│ Update CSV   │  │ Switch modes if needed│
│ Exit         │  └──────────────────────┘
└──────────────┘
```

---

## Log Messages

### Peak Period Start
```
2026-01-03 17:00:15 - 📊 Peak period started: Peak-2026-01-03
```

### During Peak (every 15 min)
```
2026-01-03 17:15:23 - ======================================================================
2026-01-03 17:15:23 - SOC: 95.2%, Solar: 2.145kW, Status: IN PEAK
2026-01-03 17:15:23 - Decision: IN PEAK PERIOD - no charging decisions (SOC: 95.2%)
2026-01-03 17:15:23 - Action: Solar-first (TOU mode)
2026-01-03 17:15:23 - Mode unchanged: TOU
```

### Peak Period End
```
2026-01-03 20:00:18 - 📊 Peak period ended: OffPeak-2026-01-03
```

### After Peak Ends
```
2026-01-03 20:15:22 - ======================================================================
2026-01-03 20:15:22 - SOC: 62.3%, Solar: 0.000kW, Status: 20.7h to peak
2026-01-03 20:15:22 - Decision: Low solar (0.00kW) but time buffer OK (18.5h left)
2026-01-03 20:15:22 - Action: Solar-first (TOU mode)
2026-01-03 20:15:22 - Mode unchanged: TOU
```

---

## Midnight Rollover

The system handles midnight gracefully:

**Before Midnight (11:45 PM):**
```
State file: OffPeak-2026-01-03
Time to peak: 17.25 hours (to 5 PM tomorrow)
```

**After Midnight (12:00 AM):**
```
State file: OffPeak-2026-01-04  (date updated)
Time to peak: 17.0 hours (to 5 PM today)
```

No mode changes occur, just state file date updates.

---

## Configuration

### Adjusting Peak Hours

Edit `smart_decision.py`:

```python
# For 4 PM - 9 PM peak period
PEAK_START_HOUR = 16  # 4 PM
PEAK_END_HOUR = 21    # 9 PM
```

### Multiple Peak Periods

Current system handles one continuous peak period. For split peaks (e.g., morning + evening), you would need to modify `update_peak_state()`:

```python
# Example: 7-9 AM and 5-8 PM peaks
def update_peak_state():
    current_hour = now.hour
    
    # Morning peak
    morning_peak = (7 <= current_hour < 9)
    # Evening peak  
    evening_peak = (17 <= current_hour < 20)
    
    in_peak_window = morning_peak or evening_peak
    
    # Rest of logic same...
```

---

## Verification

### Check Current State
```bash
cat /volume1/docker/franklin/logs/peak_state.txt
```

### Watch State Transitions
```bash
grep "Peak period" /volume1/docker/franklin/logs/solar_intelligence.log | tail -20
```

### Verify No Mode Changes During Peak
```bash
# Should show NO "SWITCHING" messages between 5-8 PM
grep "2026-01-03 1[7-9]:" /volume1/docker/franklin/logs/solar_intelligence.log | grep "SWITCHING"
```

---

## Benefits

1. **Prevents Expensive Mistakes:** No accidental grid charging during peak hours
2. **Handles Edge Cases:** Midnight rollover, daylight saving time changes
3. **Simple and Reliable:** State file is easy to inspect and debug
4. **Clear Logging:** All transitions logged with timestamps
5. **Extensible:** Easy to modify for different TOU schedules

---

## Troubleshooting

### State File Missing or Incorrect

**Symptom:** System behaves erratically around peak times

**Solution:**
```bash
# Manually set state (if needed)
echo "OffPeak-$(date +%Y-%m-%d)" > /volume1/docker/franklin/logs/peak_state.txt

# System will auto-correct at next run
```

### Peak Period Not Being Detected

**Check:**
1. System time/timezone is correct: `date`
2. Peak hours configured correctly in `smart_decision.py`
3. Script is actually running every 15 minutes

### Mode Changed During Peak

**Investigate:**
```bash
# Find any mode changes during peak hours (5-8 PM)
grep "SWITCHING" /volume1/docker/franklin/logs/solar_intelligence.log | \
    grep " 1[7-9]:\|20:0[0-7]"
```

Should return nothing. If you see mode changes during peak, there's a bug.

---

## Future Enhancements

Potential improvements to peak state logic:

1. **Holiday/Weekend Rates:** Different peak hours on weekends
2. **Seasonal Schedules:** Winter vs summer TOU periods
3. **Dynamic Pricing:** API integration for real-time electricity prices
4. **Pre-Peak Preparation:** More aggressive charging before peak starts
5. **Post-Peak Analysis:** Review if target SOC was achieved

---

**Last Updated:** January 3, 2026  
**Version:** 2.0
