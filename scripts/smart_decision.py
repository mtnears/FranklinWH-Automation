#!/usr/bin/env python3
"""
Smart Battery Decision - Single Run
Makes one intelligent decision about charging vs. waiting for solar
Designed to be run every 15 minutes via scheduler

This is the CORE AUTOMATION script that replaces the old three-tier system.
It uses peak state tracking to ensure intelligent decisions throughout the day.
"""
import asyncio
import csv
import subprocess
from datetime import datetime, timedelta
from franklinwh import Client, TokenFetcher

# REPLACE WITH YOUR FRANKLIN WH CREDENTIALS
USERNAME = "YOUR_EMAIL@example.com"
PASSWORD = "YOUR_PASSWORD"
GATEWAY_ID = "YOUR_GATEWAY_ID"

LOG_FILE = "/volume1/docker/franklin/logs/continuous_monitoring.csv"
INTELLIGENCE_LOG = "/volume1/docker/franklin/logs/solar_intelligence.log"
STATE_FILE = "/volume1/docker/franklin/logs/last_mode.txt"
PEAK_STATE_FILE = "/volume1/docker/franklin/logs/peak_state.txt"

# Configuration
TARGET_SOC = 95.0                    # Target battery charge before peak
PEAK_START_HOUR = 17                 # 5 PM (adjust for your TOU schedule)
PEAK_END_HOUR = 20                   # 8 PM (adjust for your TOU schedule)
CHARGE_RATE_PER_HOUR = 32.0          # Battery charges at ~32%/hour from grid
SAFETY_MARGIN_HOURS = 0.5            # Buffer time for charging
MIN_SOLAR_FOR_WAIT = 0.5             # Minimum solar kW to wait vs grid charge

def log_intelligence(message):
    """Write to intelligence log with timestamp"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(INTELLIGENCE_LOG, 'a') as f:
        f.write(f"{timestamp} - {message}\n")

def get_last_mode():
    """Read last mode from state file"""
    try:
        with open(STATE_FILE, 'r') as f:
            return f.read().strip()
    except:
        return None

def save_mode(mode):
    """Save current mode to state file"""
    with open(STATE_FILE, 'w') as f:
        f.write(mode)

def switch_to_backup():
    """Switch to Emergency Backup mode"""
    try:
        log_intelligence("🔌 SWITCHING TO EMERGENCY BACKUP MODE (grid charging)")
        subprocess.run(['/volume1/docker/franklin/switch_to_backup_v2.py'],
                      capture_output=True, text=True, timeout=60)
        return True
    except Exception as e:
        log_intelligence(f"ERROR switching to backup: {e}")
        return False

def switch_to_tou():
    """Switch to TOU mode"""
    try:
        log_intelligence("☀️ SWITCHING TO TOU MODE (solar-first)")
        subprocess.run(['/volume1/docker/franklin/switch_to_tou_v2.py'],
                      capture_output=True, text=True, timeout=60)
        return True
    except Exception as e:
        log_intelligence(f"ERROR switching to TOU: {e}")
        return False

def get_peak_state():
    """Get current peak state from file: 'Peak-YYYY-MM-DD' or 'OffPeak-YYYY-MM-DD'"""
    try:
        with open(PEAK_STATE_FILE, 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        return None

def save_peak_state(state):
    """Save peak state to file"""
    with open(PEAK_STATE_FILE, 'w') as f:
        f.write(state)

def update_peak_state():
    """
    Update peak state based on current time.
    Returns: True if in peak period, False otherwise
    """
    now = datetime.now()
    today_date = now.strftime('%Y-%m-%d')
    current_hour = now.hour

    current_state = get_peak_state()

    # Determine if we're in peak period (5 PM - 8 PM by default)
    in_peak_window = (PEAK_START_HOUR <= current_hour < PEAK_END_HOUR)

    if in_peak_window:
        # We're in the peak time window
        new_state = f"Peak-{today_date}"
        if current_state != new_state:
            save_peak_state(new_state)
            log_intelligence(f"📊 Peak period started: {new_state}")
        return True
    else:
        # We're outside peak window
        new_state = f"OffPeak-{today_date}"
        if current_state and current_state.startswith("Peak-"):
            # We just exited peak period
            save_peak_state(new_state)
            log_intelligence(f"📊 Peak period ended: {new_state}")
        elif current_state != new_state:
            # Normal update (midnight rollover, etc)
            save_peak_state(new_state)
        return False

def calculate_time_to_peak():
    """Calculate hours until next peak period (5 PM by default)"""
    now = datetime.now()
    peak_today = now.replace(hour=PEAK_START_HOUR, minute=0, second=0, microsecond=0)

    # If we're past today's peak END time, calculate to tomorrow's peak
    peak_end_today = now.replace(hour=PEAK_END_HOUR, minute=0, second=0, microsecond=0)
    if now >= peak_end_today:
        # Peak is tomorrow
        peak_tomorrow = peak_today + timedelta(days=1)
        return (peak_tomorrow - now).total_seconds() / 3600

    # If we're before today's peak start, use today's peak
    if now < peak_today:
        return (peak_today - now).total_seconds() / 3600

    # We're currently IN the peak period, return 0
    return 0

def should_charge_from_grid(soc, solar_kw, hours_to_peak, in_peak):
    """
    Decide: grid charge or wait for solar?
    Returns: (should_charge, reason)
    """
    # NEVER change modes during peak period
    if in_peak:
        return False, f"IN PEAK PERIOD - no charging decisions (SOC: {soc:.1f}%)"

    if soc >= TARGET_SOC:
        return False, f"Already at target ({soc:.1f}% >= {TARGET_SOC}%)"

    if hours_to_peak < 0.5:
        if soc < 75:
            return True, f"EMERGENCY: Peak in {hours_to_peak*60:.0f} min, SOC only {soc:.1f}%"
        else:
            return False, f"Peak imminent, but SOC acceptable ({soc:.1f}%)"

    soc_deficit = TARGET_SOC - soc
    hours_needed_grid = (soc_deficit / CHARGE_RATE_PER_HOUR) + SAFETY_MARGIN_HOURS
    hours_until_must_start = hours_to_peak - hours_needed_grid

    solar_charging_potential = solar_kw * 0.7 * hours_to_peak * (30.0 / 10.0)

    if hours_until_must_start <= 0:
        return True, f"Out of time! Must start now (need {hours_needed_grid:.1f}h, have {hours_to_peak:.1f}h)"

    if solar_kw < MIN_SOLAR_FOR_WAIT:
        if hours_until_must_start < 1.0:
            return True, f"Low solar ({solar_kw:.2f}kW) and running out of time ({hours_until_must_start:.1f}h buffer left)"
        else:
            return False, f"Low solar ({solar_kw:.2f}kW) but time buffer OK ({hours_until_must_start:.1f}h left)"

    if solar_charging_potential >= soc_deficit:
        return False, f"Solar can provide ~{solar_charging_potential:.1f}% (need {soc_deficit:.1f}%), {solar_kw:.2f}kW looks promising"
    else:
        if hours_until_must_start > 2.0:
            return False, f"Solar may fall short, but monitoring - {hours_until_must_start:.1f}h buffer remaining"
        else:
            return True, f"Solar unlikely to provide enough ({solar_charging_potential:.1f}% < {soc_deficit:.1f}%), starting grid charge"

async def get_stats_with_retry(max_retries=5, delay=10):
    """Get stats with retry logic for cloud API timeouts"""
    log_intelligence(f"Attempting to get battery stats (max {max_retries} attempts)...")
    fetcher = TokenFetcher(USERNAME, PASSWORD)
    client = Client(fetcher, GATEWAY_ID)

    for attempt in range(max_retries):
        try:
            log_intelligence(f"Attempt {attempt + 1} starting...")
            stats = await client.get_stats()
            if attempt > 0:
                log_intelligence(f"✓ Success on attempt {attempt + 1}")
            else:
                log_intelligence(f"✓ Success on first attempt")
            return stats
        except Exception as e:
            if attempt < max_retries - 1:
                log_intelligence(f"✗ Attempt {attempt + 1} failed: {e}, retrying in {delay}s...")
                await asyncio.sleep(delay)
            else:
                log_intelligence(f"✗ All {max_retries} attempts failed - final error: {e}")
                raise

async def main():
    """Main execution"""
    try:
        # Get current stats with retry logic
        stats = await get_stats_with_retry(max_retries=5, delay=10)

        soc = stats.current.battery_soc
        solar_kw = stats.current.solar_production
        grid_kw = stats.current.grid_use
        battery_kw = stats.current.battery_use
        home_load_kw = stats.current.home_load

        # Update peak state (returns True if in peak period)
        in_peak = update_peak_state()

        # Calculate decision (only if NOT in peak)
        hours_to_peak = calculate_time_to_peak()
        should_charge, reason = should_charge_from_grid(soc, solar_kw, hours_to_peak, in_peak)
        desired_mode = "BACKUP" if should_charge else "TOU"
        last_mode = get_last_mode()

        # Log decision
        log_intelligence("="*70)
        peak_status = "IN PEAK" if in_peak else f"{hours_to_peak:.1f}h to peak"
        log_intelligence(f"SOC: {soc:.1f}%, Solar: {solar_kw:.3f}kW, Status: {peak_status}")
        log_intelligence(f"Decision: {reason}")
        log_intelligence(f"Action: {'Grid charge' if should_charge else 'Solar-first (TOU mode)'}")

        # Switch modes if needed (only if NOT in peak)
        if not in_peak and desired_mode != last_mode:
            if desired_mode == "BACKUP":
                switch_to_backup()
            else:
                switch_to_tou()
            log_intelligence(f"Mode changed: {last_mode} → {desired_mode}")
            save_mode(desired_mode)
        else:
            log_intelligence(f"Mode unchanged: {desired_mode}")
            save_mode(desired_mode)

        # Log to CSV
        now = datetime.now()
        data = {
            'timestamp': now.strftime('%Y-%m-%d %H:%M:%S'),
            'soc_percent': f'{soc:.2f}',
            'solar_kw': f'{solar_kw:.3f}',
            'grid_kw': f'{grid_kw:.3f}',
            'battery_kw': f'{battery_kw:.3f}',
            'home_load_kw': f'{home_load_kw:.3f}',
            'grid_status': stats.current.grid_status.name,
            'battery_charge_total': f'{stats.totals.battery_charge:.3f}',
            'battery_discharge_total': f'{stats.totals.battery_discharge:.3f}',
            'grid_import_total': f'{stats.totals.grid_import:.3f}',
            'solar_total': f'{stats.totals.solar:.3f}',
            'hours_to_peak': f'{hours_to_peak:.2f}',
            'mode': desired_mode
        }

        file_exists = False
        try:
            with open(LOG_FILE, 'r') as f:
                file_exists = True
        except FileNotFoundError:
            pass

        with open(LOG_FILE, 'a', newline='') as csvfile:
            fieldnames = list(data.keys())
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(data)

        print(f"✓ Decision made: {desired_mode} mode ({reason})")

    except Exception as e:
        log_intelligence(f"ERROR: {e}")
        print(f"✗ Error: {e}")
        return 1

    return 0

if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
