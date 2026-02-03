#!/usr/bin/env python3
"""
Smart Battery Decision Engine - v3.0

Configuration-driven battery automation that supports:
- Solar-first charging (when SOLAR_ENABLED)
- Time-of-Use rate optimization (when TOU_ENABLED)
- Dynamic hourly pricing (when DYNAMIC_PRICING_ENABLED)
- Weather-informed decisions (when WEATHER_ENABLED)

Designed to be run every 15 minutes via scheduler.

Configuration is loaded from environment variables / .env file.
See .env.example for all options.
"""
import asyncio
import csv
from datetime import datetime, timedelta
from franklinwh import Client, TokenFetcher

# Import configuration
from config import config

# Optional imports for enabled features
if config.DYNAMIC_PRICING_ENABLED:
    try:
        from pricing import get_current_price, should_charge_at_current_price
    except ImportError:
        config.DYNAMIC_PRICING_ENABLED = False

if config.WEATHER_ENABLED:
    try:
        from weather import get_solar_forecast
    except ImportError:
        config.WEATHER_ENABLED = False


def log_intelligence(message: str):
    """Write to intelligence log with timestamp."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(config.INTELLIGENCE_LOG, 'a') as f:
        f.write(f"{timestamp} - {message}\n")


def get_last_mode() -> str:
    """Read last mode from state file."""
    try:
        with open(config.STATE_FILE, 'r') as f:
            return f.read().strip()
    except:
        return None


def save_mode(mode: str):
    """Save current mode to state file."""
    with open(config.STATE_FILE, 'w') as f:
        f.write(mode)


def switch_to_backup():
    """Switch to Emergency Backup mode (grid charging)."""
    import subprocess
    try:
        log_intelligence("SWITCHING TO EMERGENCY BACKUP MODE (grid charging)")
        script_path = config.BASE_DIR / 'scripts' / 'switch_to_backup_v2.py'
        result = subprocess.run(['python3', str(script_path)], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            log_intelligence(f"Switch script stderr: {result.stderr}")
        return result.returncode == 0
    except Exception as e:
        log_intelligence(f"ERROR switching to backup: {e}")
        return False


def switch_to_tou():
    """Switch to TOU mode (solar-first)."""
    import subprocess
    try:
        log_intelligence("SWITCHING TO TOU MODE (solar-first)")
        script_path = config.BASE_DIR / 'scripts' / 'switch_to_tou_v2.py'
        result = subprocess.run(['python3', str(script_path)], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            log_intelligence(f"Switch script stderr: {result.stderr}")
        return result.returncode == 0
    except Exception as e:
        log_intelligence(f"ERROR switching to TOU: {e}")
        return False


def get_peak_state() -> str:
    """Get current peak state from file."""
    try:
        with open(config.PEAK_STATE_FILE, 'r') as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def save_peak_state(state: str):
    """Save peak state to file."""
    with open(config.PEAK_STATE_FILE, 'w') as f:
        f.write(state)


def is_peak_day() -> bool:
    """Check if today is a peak pricing day based on configuration."""
    if not config.TOU_ENABLED:
        return False
    
    today = datetime.now().weekday()  # 0=Monday, 6=Sunday
    is_weekend = today >= 5
    
    if config.PEAK_DAYS == 'all':
        return True
    elif config.PEAK_DAYS == 'weekdays':
        return not is_weekend
    elif config.PEAK_DAYS == 'weekends':
        return is_weekend
    else:
        return True  # Default to all days


def update_peak_state() -> bool:
    """
    Update peak state based on current time and configuration.
    
    Returns: True if currently in peak period, False otherwise
    """
    if not config.TOU_ENABLED:
        return False
    
    now = datetime.now()
    today_date = now.strftime('%Y-%m-%d')
    current_hour = now.hour
    
    current_state = get_peak_state()
    
    # Check if today is a peak day
    if not is_peak_day():
        new_state = f"OffPeak-{today_date}-weekend"
        if current_state != new_state:
            save_peak_state(new_state)
        return False
    
    # Check primary peak period
    in_peak_window = (config.PEAK_START_HOUR <= current_hour < config.PEAK_END_HOUR)
    
    # Check secondary peak period if configured
    if config.PEAK2_START_HOUR and config.PEAK2_END_HOUR:
        in_peak_window = in_peak_window or (config.PEAK2_START_HOUR <= current_hour < config.PEAK2_END_HOUR)
    
    if in_peak_window:
        new_state = f"Peak-{today_date}"
        if current_state != new_state:
            save_peak_state(new_state)
            log_intelligence(f"Peak period started: {new_state}")
        return True
    else:
        new_state = f"OffPeak-{today_date}"
        if current_state and current_state.startswith("Peak-"):
            save_peak_state(new_state)
            log_intelligence(f"Peak period ended: {new_state}")
        elif current_state != new_state:
            save_peak_state(new_state)
        return False


def calculate_time_to_peak() -> float:
    """Calculate hours until next peak period."""
    if not config.TOU_ENABLED:
        return float('inf')  # No peak to worry about
    
    now = datetime.now()
    peak_today = now.replace(hour=config.PEAK_START_HOUR, minute=0, second=0, microsecond=0)
    peak_end_today = now.replace(hour=config.PEAK_END_HOUR, minute=0, second=0, microsecond=0)
    
    # If we're past today's peak END time, calculate to tomorrow's peak
    if now >= peak_end_today:
        peak_tomorrow = peak_today + timedelta(days=1)
        return (peak_tomorrow - now).total_seconds() / 3600
    
    # If we're before today's peak start, use today's peak
    if now < peak_today:
        return (peak_today - now).total_seconds() / 3600
    
    # We're currently IN the peak period
    return 0


def should_charge_from_grid(soc: float, solar_kw: float, hours_to_peak: float, in_peak: bool) -> tuple:
    """
    Main decision engine: Should we charge from grid or wait for solar?
    
    This function implements the decision hierarchy:
    1. Peak protection (never charge during peak)
    2. Solar-first (use solar when available)
    3. Dynamic pricing (charge when cheap)
    4. Time-based fallback (ensure ready for peak)
    
    Returns: (should_charge: bool, reason: str)
    """
    
    # ===== LAYER 1: Peak Period Protection =====
    if config.TOU_ENABLED and in_peak:
        return False, f"IN PEAK PERIOD - no charging decisions (SOC: {soc:.1f}%)"
    
    # ===== Already at target? =====
    if soc >= config.TARGET_SOC:
        return False, f"Already at target ({soc:.1f}% >= {config.TARGET_SOC}%)"
    
    # ===== LAYER 2: Solar Assessment =====
    solar_available = config.SOLAR_ENABLED and solar_kw >= config.MIN_SOLAR_FOR_WAIT
    
    # ===== LAYER 3: Dynamic Pricing (if enabled) =====
    if config.DYNAMIC_PRICING_ENABLED:
        price_should_charge, price_reason = should_charge_at_current_price()
        current_price = get_current_price()
        
        if current_price is not None:
            # Very cheap power - charge even with solar
            if current_price < 2.0:  # Under 2 cents is almost always worth it
                return True, f"Very cheap grid power ({current_price:.1f}c) - charging despite solar"
            
            # Cheap power and no solar
            if price_should_charge and not solar_available:
                return True, f"Cheap grid power ({current_price:.1f}c) and low solar ({solar_kw:.2f}kW)"
            
            # Expensive power - wait for solar if possible
            if current_price > config.PRICE_CEILING_CENTS:
                if solar_available:
                    return False, f"Grid expensive ({current_price:.1f}c) - using solar ({solar_kw:.2f}kW)"
                else:
                    return False, f"Grid expensive ({current_price:.1f}c) - waiting (low solar: {solar_kw:.2f}kW)"
    
    # ===== LAYER 4: TOU Time-Based Logic =====
    if config.TOU_ENABLED:
        # Emergency: peak imminent and SOC low
        if hours_to_peak < 0.5:
            if soc < 75:
                return True, f"EMERGENCY: Peak in {hours_to_peak*60:.0f} min, SOC only {soc:.1f}%"
            else:
                return False, f"Peak imminent, but SOC acceptable ({soc:.1f}%)"
        
        # Calculate if we have time to wait for solar
        soc_deficit = config.TARGET_SOC - soc
        hours_needed_grid = (soc_deficit / config.CHARGE_RATE_PER_HOUR) + config.SAFETY_MARGIN_HOURS
        hours_until_must_start = hours_to_peak - hours_needed_grid
        
        # Estimate solar charging potential
        if config.SOLAR_ENABLED:
            # Rough estimate: 70% efficiency, scale by battery capacity
            solar_charging_potential = solar_kw * 0.7 * hours_to_peak * (config.BATTERY_CAPACITY_KWH / 10.0)
        else:
            solar_charging_potential = 0
        
        # Out of time - must charge now
        if hours_until_must_start <= 0:
            return True, f"Out of time! Must start now (need {hours_needed_grid:.1f}h, have {hours_to_peak:.1f}h)"
        
        # Low solar decision
        if not solar_available:
            if hours_until_must_start < 1.0:
                return True, f"Low solar ({solar_kw:.2f}kW) and running out of time ({hours_until_must_start:.1f}h buffer left)"
            else:
                return False, f"Low solar ({solar_kw:.2f}kW) but time buffer OK ({hours_until_must_start:.1f}h left)"
        
        # Good solar - evaluate if it's enough
        if solar_charging_potential >= soc_deficit:
            return False, f"Solar can provide ~{solar_charging_potential:.1f}% (need {soc_deficit:.1f}%), {solar_kw:.2f}kW looks promising"
        else:
            if hours_until_must_start > 2.0:
                return False, f"Solar may fall short, but monitoring - {hours_until_must_start:.1f}h buffer remaining"
            else:
                return True, f"Solar unlikely to provide enough ({solar_charging_potential:.1f}% < {soc_deficit:.1f}%), starting grid charge"
    
    # ===== LAYER 5: No TOU - Simple Solar Logic =====
    if config.SOLAR_ENABLED:
        if solar_available:
            return False, f"Solar available ({solar_kw:.2f}kW) - using solar"
        else:
            # Check dynamic pricing if enabled
            if config.DYNAMIC_PRICING_ENABLED:
                price_should_charge, price_reason = should_charge_at_current_price()
                if price_should_charge:
                    return True, price_reason
                return False, f"Low solar, waiting for cheaper grid ({price_reason})"
            else:
                return False, f"Low solar ({solar_kw:.2f}kW) - waiting"
    
    # ===== Fallback: No features enabled =====
    return False, f"Default: maintaining current state (SOC: {soc:.1f}%)"


async def get_stats_with_retry(max_retries: int = None, delay: int = None):
    """Get stats from Franklin API with retry logic."""
    max_retries = max_retries or config.API_MAX_RETRIES
    delay = delay or config.API_RETRY_DELAY
    
    log_intelligence(f"Attempting to get battery stats (max {max_retries} attempts)...")
    
    fetcher = TokenFetcher(config.FRANKLIN_USERNAME, config.FRANKLIN_PASSWORD)
    client = Client(fetcher, config.FRANKLIN_GATEWAY_ID)
    
    for attempt in range(max_retries):
        try:
            log_intelligence(f"Attempt {attempt + 1} starting...")
            stats = await client.get_stats()
            if attempt > 0:
                log_intelligence(f"Success on attempt {attempt + 1}")
            else:
                log_intelligence(f"Success on first attempt")
            return stats
        except Exception as e:
            if attempt < max_retries - 1:
                log_intelligence(f"Attempt {attempt + 1} failed: {e}, retrying in {delay}s...")
                await asyncio.sleep(delay)
            else:
                log_intelligence(f"All {max_retries} attempts failed - final error: {e}")
                raise


async def main():
    """Main execution."""
    try:
        # Validate configuration
        errors = config.validate()
        if errors:
            for error in errors:
                log_intelligence(f"CONFIG ERROR: {error}")
            print(f"Configuration errors: {errors}")
            return 1
        
        # Get current battery stats
        stats = await get_stats_with_retry()
        
        soc = stats.current.battery_soc
        solar_kw = stats.current.solar_production
        grid_kw = stats.current.grid_use
        battery_kw = stats.current.battery_use
        home_load_kw = stats.current.home_load
        
        # Update peak state (returns True if in peak period)
        in_peak = update_peak_state()
        
        # Calculate time to peak
        hours_to_peak = calculate_time_to_peak()
        
        # Make charging decision
        should_charge, reason = should_charge_from_grid(soc, solar_kw, hours_to_peak, in_peak)
        desired_mode = "BACKUP" if should_charge else "TOU"
        last_mode = get_last_mode()
        
        # Log decision
        log_intelligence("=" * 70)
        
        # Log enabled features
        features = config.get_enabled_features()
        log_intelligence(f"Features: {', '.join(features) if features else 'Basic mode'}")
        
        # Log dynamic pricing if enabled
        if config.DYNAMIC_PRICING_ENABLED:
            current_price = get_current_price()
            if current_price:
                log_intelligence(f"Grid price: {current_price:.1f}c/kWh (threshold: {config.PRICE_THRESHOLD_CENTS}c)")
        
        peak_status = "IN PEAK" if in_peak else f"{hours_to_peak:.1f}h to peak" if config.TOU_ENABLED else "No TOU"
        log_intelligence(f"SOC: {soc:.1f}%, Solar: {solar_kw:.3f}kW, Status: {peak_status}")
        log_intelligence(f"Decision: {reason}")
        log_intelligence(f"Action: {'Grid charge' if should_charge else 'Solar-first (TOU mode)'}")
        
        # Switch modes if needed (only if NOT in peak when TOU enabled)
        if (not config.TOU_ENABLED or not in_peak) and desired_mode != last_mode:
            if desired_mode == "BACKUP":
                success = switch_to_backup()
            else:
                success = switch_to_tou()
            
            if success:
                log_intelligence(f"Mode changed: {last_mode} -> {desired_mode}")
                save_mode(desired_mode)
            else:
                log_intelligence(f"Mode switch FAILED - staying in {last_mode}")
                # Don't update the saved mode since switch failed
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
            'hours_to_peak': f'{hours_to_peak:.2f}' if config.TOU_ENABLED else 'N/A',
            'mode': desired_mode
        }
        
        # Add pricing data if enabled
        if config.DYNAMIC_PRICING_ENABLED:
            current_price = get_current_price()
            data['grid_price_cents'] = f'{current_price:.2f}' if current_price else 'N/A'
        
        # Write to CSV
        file_exists = config.LOG_FILE.exists()
        
        with open(config.LOG_FILE, 'a', newline='') as csvfile:
            fieldnames = list(data.keys())
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(data)
        
        print(f"Decision made: {desired_mode} mode ({reason})")
        
    except Exception as e:
        log_intelligence(f"ERROR: {e}")
        print(f"Error: {e}")
        return 1
    
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
