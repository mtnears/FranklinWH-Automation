#!/usr/bin/env python3
"""
Smart Battery Decision Engine - v3.3.1

API-native mode management using franklinwh library v1.0.0.
Reads actual mode from gateway via _status() and switches modes
directly via set_mode() - no external scripts or state files needed.

Configuration-driven battery automation that supports:
- Solar-first charging (when SOLAR_ENABLED)
- Time-of-Use rate optimization (when TOU_ENABLED)
- Dynamic hourly pricing (when DYNAMIC_PRICING_ENABLED)
- Weather-informed decisions (when WEATHER_ENABLED)

Designed to be run on a configurable interval via scheduler.

Configuration is loaded from environment variables / .env file.
See .env.example for all options.

Changelog:
  v3.3.1 - Solar estimation fix: use observed solar_to_bat rate instead of
           theoretical formula that underestimated solar by 3-4x, causing
           unnecessary grid charging and mode flip-flopping
         - Improved logging with charge rate (%/hr) and ETA to target
  v3.3.0 - Mode detection fix: use name field instead of unreliable run_status
         - Negative pricing override (SOLAR_OVERRIDE_PRICE_CENTS)
         - Enhanced dashboard data generator
  v3.2.0 - API-native mode management via set_mode()
         - Universal mode detection via run_status field
         - Per-battery SOC and enriched status logging
         - Eliminated external switch scripts and state file dependency
         - Single API connection for stats + mode + switching
  v3.1.0 - Docker deployment, mode-aware savings tracking
  v3.0.0 - Configuration-driven with feature toggles
"""
import asyncio
import csv
from datetime import datetime, timedelta
from franklinwh import Client, TokenFetcher, Mode

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


# =============================================================================
# Run status mapping - universal mode type indicator from Franklin API
# These are consistent regardless of custom schedule names or firmware
# =============================================================================
RUN_STATUS_MAP = {
    1: "emergency_backup",
    2: "tou",
    3: "self_consumption",
}


def log_intelligence(message: str):
    """Write to intelligence log with timestamp."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(config.INTELLIGENCE_LOG, 'a') as f:
        f.write(f"{timestamp} - {message}\n")


def save_mode_log(mode: str):
    """Write current mode to state file for logging/debugging only.
    
    NOTE: As of v3.2.0 this is purely a log artifact.
    Mode detection is done via the API, not from this file.
    """
    try:
        with open(config.STATE_FILE, 'w') as f:
            f.write(mode)
    except Exception:
        pass  # Non-critical, just logging


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


def should_charge_from_grid(soc: float, solar_kw: float, hours_to_peak: float, in_peak: bool, solar_to_bat_kw: float = 0.0) -> tuple:
    """
    Main decision engine: Should we charge from grid or wait for solar?
    
    This function implements the decision hierarchy:
    0. Credit/negative price override (overrides everything including peak & solar)
    1. Peak protection (never charge during peak, unless overridden)
    2. Already at target check
    3. Solar assessment
    4. Dynamic pricing (normal thresholds)
    5. TOU time-based fallback (ensure ready for peak)
    6. No-TOU simple solar logic
    
    Returns: (should_charge: bool, reason: str)
    """
    
    # Get current price once if dynamic pricing is enabled
    current_price = None
    if config.DYNAMIC_PRICING_ENABLED:
        current_price = get_current_price()
    
    # ===== LAYER 0: Credit/Negative Price Override =====
    # If the grid price is at or below the solar override threshold,
    # charge from grid regardless of solar, peak period, or anything else.
    # This captures negative pricing (utility credits) where it's profitable
    # to consume grid power even when solar is producing.
    if (config.DYNAMIC_PRICING_ENABLED and
            config.SOLAR_OVERRIDE_PRICE_CENTS is not None and
            current_price is not None and
            current_price <= config.SOLAR_OVERRIDE_PRICE_CENTS):
        solar_note = f", solar at {solar_kw:.2f}kW" if solar_kw > 0 else ""
        peak_note = " (overriding peak protection)" if in_peak else ""
        return True, (f"PRICE OVERRIDE: Grid at {current_price:.1f}c "
                     f"<= {config.SOLAR_OVERRIDE_PRICE_CENTS:.1f}c threshold"
                     f"{solar_note}{peak_note} - charging for credit/savings")
    
    # ===== LAYER 1: Peak Period Protection =====
    if config.TOU_ENABLED and in_peak:
        return False, f"IN PEAK PERIOD - no charging decisions (SOC: {soc:.1f}%)"
    
    # ===== Already at target? =====
    if soc >= config.TARGET_SOC:
        return False, f"Already at target ({soc:.1f}% >= {config.TARGET_SOC}%)"
    
    # ===== LAYER 2: Solar Assessment =====
    solar_available = config.SOLAR_ENABLED and solar_kw >= config.MIN_SOLAR_FOR_WAIT
    
    # ===== LAYER 3: Dynamic Pricing (if enabled) =====
    if config.DYNAMIC_PRICING_ENABLED and current_price is not None:
        price_should_charge, price_reason = should_charge_at_current_price()
        
        # Cheap power (at or below threshold) - charge even with solar
        if current_price <= config.PRICE_THRESHOLD_CENTS:
            return True, f"Cheap grid power ({current_price:.1f}c <= {config.PRICE_THRESHOLD_CENTS:.1f}c threshold) - charging"
        
        # Cheap power and no solar
        if price_should_charge and not solar_available:
            return True, f"Favorable grid price ({current_price:.1f}c) and low solar ({solar_kw:.2f}kW)"
        
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
        
        # Estimate solar charging potential using observed solar-to-battery rate
        # The API's soChBat field shows actual kW flowing from solar into the battery,
        # which is 2-4x higher than panel production (solar_kw) due to MPPT conversion
        # and the fact that solar_kw is net panel output, not what reaches the battery.
        if config.SOLAR_ENABLED and solar_to_bat_kw > 0:
            # Use observed rate: convert kW into %/hr based on battery capacity
            solar_rate_pct_hr = (solar_to_bat_kw / config.BATTERY_CAPACITY_KWH) * 100
            solar_charging_potential = solar_rate_pct_hr * hours_to_peak
        elif config.SOLAR_ENABLED and solar_kw >= config.MIN_SOLAR_FOR_WAIT:
            # Fallback if solar_to_bat not available: conservative theoretical estimate
            solar_rate_pct_hr = (solar_kw * 0.7 / config.BATTERY_CAPACITY_KWH) * 100
            solar_charging_potential = solar_rate_pct_hr * hours_to_peak
        else:
            solar_charging_potential = 0
            solar_rate_pct_hr = 0
        
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
            solar_eta = soc_deficit / solar_rate_pct_hr if solar_rate_pct_hr > 0 else 999
            return False, (f"Solar on track: {solar_rate_pct_hr:.1f}%/hr, "
                          f"ETA {solar_eta:.1f}h (have {hours_to_peak:.1f}h), "
                          f"{solar_to_bat_kw:.2f}kW to battery")
        else:
            if hours_until_must_start > 2.0:
                return False, f"Solar may fall short, but monitoring - {hours_until_must_start:.1f}h buffer remaining"
            else:
                solar_eta = soc_deficit / solar_rate_pct_hr if solar_rate_pct_hr > 0 else 999
                return True, (f"Solar too slow: {solar_rate_pct_hr:.1f}%/hr, "
                             f"ETA {solar_eta:.1f}h > {hours_to_peak:.1f}h available, "
                             f"need {soc_deficit:.1f}% - starting grid charge")
    
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


# =============================================================================
# API interaction - single client for stats, status, and mode control
# =============================================================================

async def create_client() -> Client:
    """Create and return a Franklin API client."""
    fetcher = TokenFetcher(config.FRANKLIN_USERNAME, config.FRANKLIN_PASSWORD)
    return Client(fetcher, config.FRANKLIN_GATEWAY_ID)


async def get_gateway_status(client: Client) -> dict:
    """
    Get raw gateway status including mode, per-battery SOC, and system data.
    
    Returns dict with keys like:
        run_status, mode, name, soc, fhpSoc, fhpSn, fhpPower,
        t_amb, signal, gridChBat, soChBat, kwh_sun, etc.
    """
    return await client._status()


def detect_mode(status: dict) -> str:
    """
    Detect current operating mode from gateway status.
    
    Uses the 'name' field as the primary indicator since run_status
    has been found unreliable on some firmware versions (can stay
    stuck at 1 regardless of actual mode).
    
    The name field reliably reflects mode changes:
        "Emergency Backup" = emergency_backup (grid charging)
        "TOU-*" or similar = tou (user's TOU schedule)
        "Self Consumption" = self_consumption
    
    Falls back to run_status if name is unavailable.
    """
    mode_name = status.get("name", "")
    run_status = status.get("run_status")
    
    # Primary: use name field (reliably tracks mode changes)
    if mode_name:
        name_lower = mode_name.lower()
        if "emergency" in name_lower or "backup" in name_lower:
            return "emergency_backup"
        if "self" in name_lower and "consumption" in name_lower:
            return "self_consumption"
        # Any other name (TOU-B, TOU-Summer, custom schedule, etc.)
        # is the user's home mode - not emergency_backup
        return config.HOME_MODE
    
    # Fallback: run_status (may be unreliable on some firmware)
    if run_status in RUN_STATUS_MAP:
        return RUN_STATUS_MAP[run_status]
    
    # Unknown - log it and assume home mode
    log_intelligence(f"Unknown mode: run_status={run_status}, name='{mode_name}' - assuming home mode")
    return config.HOME_MODE


def get_home_mode_object() -> Mode:
    """Get the Mode object for the user's configured home mode."""
    if config.HOME_MODE == 'self_consumption':
        return Mode.self_consumption(soc=20)
    else:
        return Mode.time_of_use(soc=20)


async def switch_mode(client: Client, target: str) -> bool:
    """
    Switch to the specified mode via API.
    
    Args:
        client: Franklin API client
        target: "emergency_backup" or "home" (uses HOME_MODE config)
    
    Returns: True if switch succeeded (or likely succeeded)
    """
    try:
        if target == "emergency_backup":
            mode_obj = Mode.emergency_backup(soc=100)
            mode_label = "Emergency Backup"
        else:
            mode_obj = get_home_mode_object()
            mode_label = config.HOME_MODE.upper().replace('_', ' ')
        
        log_intelligence(f"SWITCHING MODE -> {mode_label}")
        await client.set_mode(mode_obj)
        
        # Verify the switch took effect (gateway may need 3-5s to reflect)
        for verify_attempt in range(2):
            wait_time = 5 if verify_attempt == 0 else 8
            await asyncio.sleep(wait_time)
            verify_status = await get_gateway_status(client)
            actual_mode = detect_mode(verify_status)
            actual_name = verify_status.get("name", "?")
            
            if target == "emergency_backup" and actual_mode == "emergency_backup":
                log_intelligence(f"Mode switch verified: {mode_label} (name={actual_name})")
                return True
            elif target != "emergency_backup" and actual_mode != "emergency_backup":
                log_intelligence(f"Mode switch verified: {mode_label} (name={actual_name})")
                return True
            
            if verify_attempt == 0:
                log_intelligence(f"Mode not yet confirmed (name={actual_name}), rechecking...")
        
        # Both verification attempts showed old mode
        log_intelligence(f"WARNING: Mode verification inconclusive after switch. "
                        f"Expected: {target}, API reports: name={actual_name}. "
                        f"set_mode() call succeeded - gateway may need more time to apply.")
        # Return True since the API call itself didn't error - the switch was sent
        return True
            
    except Exception as e:
        log_intelligence(f"ERROR switching to {target}: {e}")
        return False


async def get_stats_with_retry(client: Client, max_retries: int = None, delay: int = None):
    """Get stats from Franklin API with retry logic."""
    max_retries = max_retries or config.API_MAX_RETRIES
    delay = delay or config.API_RETRY_DELAY
    
    log_intelligence(f"Attempting to get battery stats (max {max_retries} attempts)...")
    
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


# =============================================================================
# Main execution
# =============================================================================

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
        
        # Create a single API client for everything
        client = await create_client()
        
        # Get battery stats (with retry) and gateway status
        stats = await get_stats_with_retry(client)
        status = await get_gateway_status(client)
        
        # Extract power flow data from stats
        soc = stats.current.battery_soc
        solar_kw = stats.current.solar_production
        grid_kw = stats.current.grid_use
        battery_kw = stats.current.battery_use
        home_load_kw = stats.current.home_load
        
        # Extract enriched data from raw status
        run_status = status.get("run_status", -1)
        mode_number = status.get("mode", -1)
        mode_name = status.get("name", "Unknown")
        current_mode = detect_mode(status)
        
        # Per-battery data
        battery_socs = status.get("fhpSoc", [])
        battery_serials = status.get("fhpSn", [])
        battery_powers = status.get("fhpPower", [])
        num_batteries = len(battery_socs)
        
        # Environment and system data
        ambient_temp_c = status.get("t_amb", None)
        cell_signal = status.get("signal", None)
        grid_charging_kw = status.get("gridChBat", 0)
        solar_charging_kw = status.get("soChBat", 0)
        
        # The Franklin API reports stale gridChBat/soChBat values when the battery
        # is discharging (not charging). Zero them out to avoid misleading logs
        # and incorrect solar estimation during peak/discharge periods.
        if battery_kw > 0.1:  # Battery is discharging (positive = discharge)
            grid_charging_kw = 0.0
            solar_charging_kw = 0.0
        
        # Today's energy totals from status
        today_solar_kwh = status.get("kwh_sun", 0)
        today_grid_import_kwh = status.get("kwh_uti_in", 0)
        today_load_kwh = status.get("kwh_load", 0)
        today_bat_charge_kwh = status.get("kwh_fhp_chg", 0)
        today_bat_discharge_kwh = status.get("kwh_fhp_di", 0)
        
        # Update peak state (returns True if in peak period)
        in_peak = update_peak_state()
        
        # Calculate time to peak
        hours_to_peak = calculate_time_to_peak()
        
        # Make charging decision
        should_charge, reason = should_charge_from_grid(soc, solar_kw, hours_to_peak, in_peak, solar_charging_kw)
        desired_mode = "emergency_backup" if should_charge else "home"
        desired_mode_label = "BACKUP" if should_charge else config.HOME_MODE.upper()
        
        # Log decision
        log_intelligence("=" * 70)
        
        # Log enabled features
        features = config.get_enabled_features()
        log_intelligence(f"Features: {', '.join(features) if features else 'Basic mode'}")
        
        # Log current mode from API
        log_intelligence(f"API Mode: {mode_name} (run_status={run_status}, detected={current_mode})")
        
        # Log per-battery SOC if multiple batteries
        if num_batteries > 1:
            bat_soc_str = ", ".join([f"Bat{i+1}: {s:.1f}%" for i, s in enumerate(battery_socs)])
            log_intelligence(f"Per-battery SOC: {bat_soc_str} (combined: {soc:.1f}%)")
        
        # Log environment data
        env_parts = []
        if ambient_temp_c is not None:
            temp_f = ambient_temp_c * 9 / 5 + 32
            env_parts.append(f"Temp: {temp_f:.0f}F/{ambient_temp_c:.1f}C")
        if cell_signal is not None:
            env_parts.append(f"Signal: {cell_signal}")
        if env_parts:
            log_intelligence(f"Environment: {', '.join(env_parts)}")
        
        # Log dynamic pricing if enabled
        if config.DYNAMIC_PRICING_ENABLED:
            current_price = get_current_price()
            if current_price:
                log_intelligence(f"Grid price: {current_price:.1f}c/kWh (threshold: {config.PRICE_THRESHOLD_CENTS}c)")
        
        peak_status = "IN PEAK" if in_peak else f"{hours_to_peak:.1f}h to peak" if config.TOU_ENABLED else "No TOU"
        log_intelligence(f"SOC: {soc:.1f}%, Solar: {solar_kw:.3f}kW, Grid->Bat: {grid_charging_kw:.2f}kW, Solar->Bat: {solar_charging_kw:.2f}kW")
        log_intelligence(f"Status: {peak_status}")
        log_intelligence(f"Decision: {reason}")
        log_intelligence(f"Action: {'Grid charge (backup mode)' if should_charge else f'Solar-first ({config.HOME_MODE} mode)'}")
        
        # Determine if mode switch is needed
        # Use a cooldown to avoid re-issuing the same switch every cycle
        # when the API is slow to reflect mode changes
        mode_switched = False
        if not config.TOU_ENABLED or not in_peak:
            need_backup = should_charge and current_mode != "emergency_backup"
            need_home = not should_charge and current_mode == "emergency_backup"
            
            if need_backup or need_home:
                # Check cooldown - don't re-issue same switch within 10 minutes
                switch_target = "emergency_backup" if need_backup else "home"
                cooldown_ok = True
                try:
                    cooldown_file = config.LOG_DIR / "last_mode_switch.txt"
                    if cooldown_file.exists():
                        with open(cooldown_file, 'r') as f:
                            parts = f.read().strip().split('|')
                            if len(parts) == 2:
                                last_target = parts[0]
                                last_time = datetime.strptime(parts[1], '%Y-%m-%d %H:%M:%S')
                                elapsed = (datetime.now() - last_time).total_seconds()
                                if last_target == switch_target and elapsed < 600:
                                    cooldown_ok = False
                                    log_intelligence(f"Mode switch cooldown: {switch_target} already sent "
                                                   f"{elapsed:.0f}s ago, skipping re-issue")
                except Exception:
                    pass  # Cooldown is best-effort, don't fail on it
                
                if cooldown_ok:
                    mode_switched = await switch_mode(client, switch_target)
                    if mode_switched:
                        label = "emergency_backup" if need_backup else config.HOME_MODE
                        from_mode = current_mode
                        log_intelligence(f"Mode changed: {from_mode} -> {label}")
                        # Record switch for cooldown
                        try:
                            with open(config.LOG_DIR / "last_mode_switch.txt", 'w') as f:
                                f.write(f"{switch_target}|{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                        except Exception:
                            pass
                    else:
                        log_intelligence(f"Mode switch to {switch_target} failed")
            else:
                log_intelligence(f"Mode unchanged: {current_mode} ({desired_mode_label})")
        else:
            log_intelligence(f"In peak - no mode changes (current: {current_mode})")
        
        # Write mode to state file (logging only, not used for decisions)
        save_mode_log(desired_mode_label)
        
        # Log to CSV with enriched data
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
            'mode': desired_mode_label,
            'run_status': str(run_status),
            'mode_name': mode_name,
            'grid_charging_kw': f'{grid_charging_kw:.3f}',
            'solar_charging_kw': f'{solar_charging_kw:.3f}',
        }
        
        # Add per-battery SOC columns
        for i, bat_soc in enumerate(battery_socs):
            data[f'battery_{i+1}_soc'] = f'{bat_soc:.1f}'
        
        # Add environment data if available
        if ambient_temp_c is not None:
            data['ambient_temp_c'] = f'{ambient_temp_c:.1f}'
        if cell_signal is not None:
            data['cell_signal'] = str(cell_signal)
        
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
        
        # Summary output
        bat_info = f" ({num_batteries} batteries)" if num_batteries > 1 else ""
        switch_info = " [SWITCHED]" if mode_switched else ""
        print(f"Decision: {desired_mode_label} mode ({reason}){bat_info}{switch_info}")
        
    except Exception as e:
        log_intelligence(f"ERROR: {e}")
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
