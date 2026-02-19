#!/usr/bin/env python3
"""
Smart Battery Decision Engine - v3.5.1 / v4.0 Adaptive Engine Bridge

Unified data collection with Modbus TCP and Franklin Cloud API support.
Features midnight-crossing peak period handling and performance monitoring.

v4.0 bridge:
- When ADAPTIVE_ENGINE_ENABLED=true, delegates decisions to adaptive_engine.py
- All data collection, mode switching, and CSV logging remain in this file
- The adaptive engine replaces should_charge_from_grid() only
- Falls back to v3.5 logic if adaptive engine fails

Configuration-driven battery automation that supports:
- Hybrid Modbus/Cloud API data collection
- Solar-first charging (when SOLAR_ENABLED)
- Time-of-Use rate optimization (when TOU_ENABLED, now with midnight-crossing)
- Dynamic hourly pricing (when DYNAMIC_PRICING_ENABLED)
- Weather-informed decisions (when WEATHER_ENABLED)
- Connection performance tracking and automatic fallback

Key Changes in v3.5.0:
- NEW: Modbus TCP integration for 100x faster local data collection
- NEW: Midnight-crossing peak period support (e.g., 22:00-06:00)
- NEW: Connection health monitoring and automatic fallback
- NEW: Real-time decision making with faster polling when using Modbus
- FIXED: Peak period detection now handles midnight-crossing correctly
- Enhanced: Performance counters for Modbus vs Cloud API comparison

Architecture:
- DataSourceManager: Unified interface for Modbus TCP and Cloud API
- ModbusDataSource: Fast local readings (46ms avg) via SunSpec registers
- CloudDataSource: Franklin cloud API (5000ms avg) with retry logic
- Automatic fallback when primary data source fails

Changelog:
  v4.0.0 - Adaptive engine bridge: ADAPTIVE_ENGINE_ENABLED toggle
  v3.5.0 - Modbus TCP integration with automatic fallback
         - Fixed midnight-crossing peak periods (PEAK_END_HOUR can now be < PEAK_START_HOUR)
         - Connection performance monitoring and health tracking
         - Real-time decision frequency when using Modbus
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

# Import configuration and data sources
from config import config, is_peak_period
from data_sources import get_battery_data, switch_battery_mode, data_manager

# Optional imports for enabled features
if config.DYNAMIC_PRICING_ENABLED:
    try:
        from pricing import get_current_price, should_charge_at_current_price
    except ImportError:
        config.DYNAMIC_PRICING_ENABLED = False

# v4.0 Adaptive Engine (optional)
ADAPTIVE_ENGINE_LOADED = False
adaptive_engine_instance = None

if getattr(config, 'ADAPTIVE_ENGINE_ENABLED', False):
    try:
        from adaptive_engine import create_engine, SystemState, Decision
        adaptive_engine_instance = create_engine(
            csv_path=str(config.LOG_FILE),
            profile_path=str(config.DATA_DIR / 'system_profile.json'),
            rate_schedule_path=str(config.DATA_DIR / 'rate_schedule.json'),
            config={
                'battery_count': getattr(config, 'BATTERY_COUNT', 2),
                'capacity_per_battery_kwh': getattr(config, 'BATTERY_CAPACITY_KWH', 13.6),
                'backup_reserve_pct': getattr(config, 'BACKUP_RESERVE_PCT', 20),
                'target_soc': config.TARGET_SOC,
                'decision_interval_minutes': getattr(config, 'DECISION_INTERVAL_MINUTES', 15),
                'override_path': str(config.LOG_DIR / 'override.json'),
            },
        )
        ADAPTIVE_ENGINE_LOADED = True
    except Exception as e:
        print(f"Warning: Adaptive engine failed to load, falling back to v3.5 logic: {e}")
        import traceback
        traceback.print_exc()

# Weather/forecast integration placeholder
# Note: weather.py module not yet implemented. The WEATHER_ENABLED toggle
# and get_solar_forecast() interface are reserved for v4.0 forecast engine.
# collect_weather.py handles raw weather data collection separately.


def log_intelligence(message: str):
    """Write to intelligence log with timestamp."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(config.INTELLIGENCE_LOG, 'a') as f:
        f.write(f"{timestamp} - {message}\n")


def save_mode_log(mode: str):
    """Write current mode to state file for logging/debugging only.
    
    NOTE: Mode detection is done via the data sources, not from this file.
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
    Now supports midnight-crossing peak periods.
    
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
    
    # Check primary peak period (now supports midnight-crossing)
    in_primary_peak = is_peak_period(current_hour, config.PEAK_START_HOUR, config.PEAK_END_HOUR)
    
    # Check secondary peak period if configured
    in_secondary_peak = False
    if config.PEAK2_START_HOUR is not None and config.PEAK2_END_HOUR is not None:
        in_secondary_peak = is_peak_period(current_hour, config.PEAK2_START_HOUR, config.PEAK2_END_HOUR)
    
    in_peak_window = in_primary_peak or in_secondary_peak
    
    if in_peak_window:
        peak_type = "Primary" if in_primary_peak else "Secondary"
        new_state = f"Peak-{today_date}-{peak_type}"
        if current_state != new_state:
            save_peak_state(new_state)
            peak_desc = f"{config.PEAK_START_HOUR}:00-{config.PEAK_END_HOUR}:00"
            if config.PEAK_START_HOUR > config.PEAK_END_HOUR:
                peak_desc += " (midnight-crossing)"
            log_intelligence(f"Peak period started: {new_state} ({peak_desc})")
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
    """
    Calculate hours until next peak period.
    Now handles midnight-crossing peak periods correctly.
    """
    if not config.TOU_ENABLED:
        return float('inf')  # No peak to worry about
    
    now = datetime.now()
    current_hour = now.hour
    current_minute = now.minute
    
    # Handle midnight-crossing peak periods
    if config.PEAK_START_HOUR > config.PEAK_END_HOUR:
        # Midnight-crossing period (e.g., 22:00-06:00)
        if current_hour >= config.PEAK_START_HOUR:
            # We're after start time today, peak is ongoing
            return 0
        elif current_hour < config.PEAK_END_HOUR:
            # We're in the early morning part of the peak
            return 0
        else:
            # We're between peak end and start (e.g., 06:00-22:00)
            # Next peak starts today at PEAK_START_HOUR
            peak_start = now.replace(hour=config.PEAK_START_HOUR, minute=0, second=0, microsecond=0)
            return (peak_start - now).total_seconds() / 3600
    else:
        # Normal peak period (e.g., 17:00-20:00)
        peak_start_today = now.replace(hour=config.PEAK_START_HOUR, minute=0, second=0, microsecond=0)
        peak_end_today = now.replace(hour=config.PEAK_END_HOUR, minute=0, second=0, microsecond=0)
        
        if now >= peak_end_today:
            # Past today's peak, calculate to tomorrow's peak
            peak_start_tomorrow = peak_start_today + timedelta(days=1)
            return (peak_start_tomorrow - now).total_seconds() / 3600
        elif now < peak_start_today:
            # Before today's peak
            return (peak_start_today - now).total_seconds() / 3600
        else:
            # Currently in peak period
            return 0


def should_charge_from_grid(soc: float, solar_kw: float, hours_to_peak: float, in_peak: bool, solar_to_bat_kw: float = 0.0) -> tuple:
    """
    Main decision engine: Should we charge from grid or wait for solar?
    
    This function implements the decision hierarchy:
    0. Credit/negative price override (overrides everything including peak & solar)
    1. Peak protection (never charge during peak, unless overridden)
    2. Dynamic pricing thresholds (if enabled)
    3. Weather forecast integration (if enabled)
    4. Solar production assessment
    5. Time-to-peak calculation with safety margins
    
    Returns: (should_charge: bool, reason: str)
    """
    
    # 0. NEGATIVE PRICING OVERRIDE - grab credits even during peak or with solar
    if (config.DYNAMIC_PRICING_ENABLED and 
        config.SOLAR_OVERRIDE_PRICE_CENTS is not None):
        try:
            current_price = get_current_price()
            if current_price and current_price <= config.SOLAR_OVERRIDE_PRICE_CENTS:
                return True, f"Negative price override: {current_price:.1f}c ≤ {config.SOLAR_OVERRIDE_PRICE_CENTS}c (grabbing credits!)"
        except Exception:
            pass  # Don't fail on pricing errors
    
    # 1. PEAK PROTECTION - Never charge during peak periods
    if in_peak and config.TOU_ENABLED:
        return False, "Peak period - no charging (peak protection)"
    
    # 2. DYNAMIC PRICING - Check if price is favorable for grid charging
    if config.DYNAMIC_PRICING_ENABLED:
        try:
            current_price = get_current_price()
            if current_price:
                if current_price <= config.PRICE_THRESHOLD_CENTS:
                    return True, f"Low grid price: {current_price:.1f}c ≤ {config.PRICE_THRESHOLD_CENTS}c threshold"
                elif current_price >= config.PRICE_CEILING_CENTS:
                    return False, f"High grid price: {current_price:.1f}c ≥ {config.PRICE_CEILING_CENTS}c ceiling"
        except Exception:
            pass  # Don't fail on pricing errors, continue with other logic
    
    # 3. BATTERY SOC CHECK - Already at target?
    if soc >= config.TARGET_SOC:
        return False, f"Battery full: {soc:.1f}% ≥ {config.TARGET_SOC}% target"
    
    # 4. TIME TO PEAK - Do we have time to wait for solar?
    if hours_to_peak <= 0:
        # We're in or past peak - no urgency for TOU rates
        # Let solar charge naturally
        return False, "Past peak period - solar charging preferred"
    
    # 5. WEATHER FORECAST INTEGRATION
    # Note: Reserved for v4.0 forecast-aware engine. The weather module
    # interface (get_solar_forecast) is not yet implemented.
    cloudy_forecast = False
    
    if cloudy_forecast:
        return True, f"Cloudy forecast - charge overnight (weather override)"
    
    # 6. SOLAR ASSESSMENT - Is there good solar production?
    if config.SOLAR_ENABLED and solar_kw >= config.MIN_SOLAR_FOR_WAIT:
        # Calculate if current solar rate can reach target before peak
        soc_needed = config.TARGET_SOC - soc
        
        # Use observed solar-to-battery rate if available, otherwise estimate
        if solar_to_bat_kw > 0:
            # Real-time solar charging rate
            percent_per_hour = (solar_to_bat_kw / config.BATTERY_CAPACITY_KWH) * 100
            if percent_per_hour > 0:
                hours_needed = soc_needed / percent_per_hour
                time_buffer = config.SAFETY_MARGIN_HOURS
                
                log_intelligence(f"Solar analysis: need {soc_needed:.1f}%, "
                               f"charging at {percent_per_hour:.1f}%/hr "
                               f"({solar_to_bat_kw:.2f}kW->bat), "
                               f"ETA: {hours_needed:.1f}h vs {hours_to_peak:.1f}h available")
                
                if hours_needed + time_buffer <= hours_to_peak:
                    return False, f"Solar sufficient: {hours_needed:.1f}h + {time_buffer:.1f}h buffer ≤ {hours_to_peak:.1f}h to peak"
                else:
                    return True, f"Solar insufficient: {hours_needed:.1f}h + {time_buffer:.1f}h > {hours_to_peak:.1f}h to peak"
        
        # Fallback: basic production vs time check
        if hours_to_peak > 4:  # Plenty of time for solar
            return False, f"Good solar + time: {solar_kw:.3f}kW production, {hours_to_peak:.1f}h to peak"
        else:
            return True, f"Good solar but low time: {hours_to_peak:.1f}h < 4h safety margin"
    
    # 7. LOW/NO SOLAR - Check time urgency
    if hours_to_peak <= 2:
        return True, f"Time critical: {hours_to_peak:.1f}h to peak, minimal solar ({solar_kw:.3f}kW)"
    elif hours_to_peak <= 4:
        # Moderate urgency - depends on strategy
        if config.CHARGING_STRATEGY == "aggressive":
            return False, f"Aggressive strategy: wait for solar ({hours_to_peak:.1f}h buffer)"
        elif config.CHARGING_STRATEGY == "conservative":
            return True, f"Conservative strategy: charge now ({hours_to_peak:.1f}h buffer)"
        else:  # balanced
            return True, f"Balanced strategy: charge now (moderate urgency, {hours_to_peak:.1f}h to peak)"
    else:
        # Plenty of time - wait for solar even if minimal
        return False, f"Time available: {hours_to_peak:.1f}h to peak, wait for solar"


def adaptive_engine_decision(battery_data, current_mode: str, in_peak: bool, hours_to_peak: float) -> tuple:
    """
    Bridge to the v4.0 adaptive engine.
    
    Translates battery_data into a SystemState, runs the engine,
    and returns (should_charge: bool, reason: str) in the same format
    as should_charge_from_grid() for seamless integration.
    """
    now = datetime.now()
    
    # Map grid status to boolean
    grid_online = True
    if hasattr(battery_data, 'grid_status'):
        gs = str(battery_data.grid_status).lower()
        if 'disconnect' in gs or 'offline' in gs or 'island' in gs:
            grid_online = False
    
    # Build SystemState
    state = SystemState(
        timestamp=now,
        soc_percent=battery_data.soc_percent,
        solar_kw=battery_data.solar_power_kw,
        grid_kw=battery_data.grid_power_kw,
        battery_kw=battery_data.battery_power_kw,
        home_load_kw=battery_data.home_load_kw,
        grid_online=grid_online,
        current_mode=current_mode,
    )
    
    # Add dynamic pricing if available
    if config.DYNAMIC_PRICING_ENABLED:
        try:
            state.dynamic_price_cents = get_current_price()
        except Exception:
            pass
    
    # Run the engine
    decision = adaptive_engine_instance.evaluate(state)
    
    # Translate to v3.5 format: (should_charge, reason)
    should_charge = (decision.action == "switch_to_backup")
    reason = f"[v4 P{decision.priority_level}] {decision.reason}"
    
    # Log engine metrics if present
    if decision.metrics:
        metrics_str = ", ".join(f"{k}={v}" for k, v in decision.metrics.items())
        log_intelligence(f"Engine metrics: {metrics_str}")
    
    return should_charge, reason


def detect_mode(battery_data) -> str:
    """
    Detect current battery operating mode from data.
    Priority: mode_name (if available), then run_status mapping.
    """
    # Try mode name first (from detailed status)
    if hasattr(battery_data, 'mode_name') and battery_data.mode_name:
        mode_name = battery_data.mode_name.lower()
        if 'backup' in mode_name or 'emergency' in mode_name:
            return "emergency_backup"
        elif 'tou' in mode_name or 'time' in mode_name:
            return "tou" 
        elif 'self' in mode_name or 'consumption' in mode_name:
            return "self_consumption"
    
    # Fallback to run_status mapping
    if hasattr(battery_data, 'run_status') and battery_data.run_status:
        status_map = {
            1: "emergency_backup",
            2: "tou", 
            3: "self_consumption",
        }
        return status_map.get(battery_data.run_status, "unknown")
    
    return "unknown"


async def switch_mode(mode_target: str) -> bool:
    """
    Switch battery mode using the data source manager.
    Mode switching only works via Cloud API.
    """
    return await switch_battery_mode(mode_target)


async def check_grid_connected() -> bool:
    """
    Check if the grid is connected via Modbus before attempting mode switches.
    
    Reads conn_state (register 75, Model 701 offset 3):
      1 = grid connected
      0 = grid disconnected / islanded
    
    Returns True if grid is connected (safe to switch modes).
    Returns True if Modbus is unavailable (fail-open for cloud-only users).
    Returns False if grid is confirmed disconnected.
    """
    try:
        if not config.MODBUS_ENABLED:
            return True  # No Modbus configured, fail-open
        
        modbus_source = data_manager.modbus_source
        if not hasattr(modbus_source, 'client') or modbus_source.client is None:
            # Try to connect if not already
            if hasattr(modbus_source, 'connect'):
                modbus_source.connect()
            if not hasattr(modbus_source, 'client') or modbus_source.client is None:
                return True  # Can't connect, fail-open
        
        # Read Model 701 (base address 72) — conn_state is at offset 3
        result = modbus_source.client.read_holding_registers(72, count=8)
        if result and not result.isError() and hasattr(result, 'registers'):
            conn_state = result.registers[3]  # offset 3 = connection state
            off7_state = result.registers[7]  # offset 7 = DER connect status
            
            if conn_state == 0:
                log_intelligence(f"⚡ GRID DISCONNECTED — conn_state={conn_state}, "
                               f"der_connect={off7_state} (island mode)")
                return False
            
            return True
        
        return True  # Read failed, fail-open
        
    except Exception as e:
        log_intelligence(f"Grid check error (fail-open): {e}")
        return True  # Error reading, fail-open — don't block mode switches


def check_manual_override() -> dict:
    """Check if manual override is active."""
    try:
        override_file = config.LOG_DIR / "override.json"
        if override_file.exists():
            import json
            with open(override_file, 'r') as f:
                override = json.load(f)
                
                if override.get('active'):
                    # Check if expired
                    if override.get('expires_at'):
                        from datetime import datetime
                        expires = datetime.fromisoformat(override['expires_at'])
                        if datetime.now() >= expires:
                            override['active'] = False
                            with open(override_file, 'w') as f:
                                json.dump(override, f, indent=2)
                            return override
                
                return override
    except Exception:
        pass
    
    return {'active': False}


async def main() -> int:
    """Main automation logic."""
    
    try:
        log_intelligence("=" * 70)
        engine_label = "v4.0 Adaptive" if ADAPTIVE_ENGINE_LOADED else "v3.5.1"
        log_intelligence(f"FranklinWH Smart Decision Engine {engine_label}")
        
        # Check for manual override first
        override = check_manual_override()
        if override.get('active'):
            mode = override.get('mode', 'unknown')
            expires = override.get('expires_at', 'indefinite')
            log_intelligence(f"Manual override active: {mode} (expires: {expires})")
            print(f"Manual override active: {mode}")
            return 0
        
        # Read battery data from unified data source
        battery_data = await get_battery_data()
        if not battery_data:
            log_intelligence("ERROR: Failed to read battery data from all sources")
            print("Error: Could not read battery data")
            return 1
        
        # Extract data
        soc = battery_data.soc_percent
        solar_kw = battery_data.solar_power_kw
        grid_kw = battery_data.grid_power_kw
        battery_kw = battery_data.battery_power_kw
        home_load_kw = battery_data.home_load_kw
        solar_to_bat_kw = battery_data.solar_to_battery_kw
        grid_to_bat_kw = battery_data.grid_to_battery_kw
        
        # Detect current mode
        current_mode = detect_mode(battery_data)
        
        # Update peak state (returns True if in peak period)
        in_peak = update_peak_state()
        
        # Calculate time to peak
        hours_to_peak = calculate_time_to_peak()
        
        # Make charging decision — v4.0 adaptive engine or v3.5 legacy
        if ADAPTIVE_ENGINE_LOADED:
            try:
                should_charge, reason = adaptive_engine_decision(
                    battery_data, current_mode, in_peak, hours_to_peak
                )
            except Exception as e:
                log_intelligence(f"Adaptive engine error, falling back to v3.5: {e}")
                should_charge, reason = should_charge_from_grid(
                    soc, solar_kw, hours_to_peak, in_peak, solar_to_bat_kw
                )
                reason = f"[v3.5 fallback] {reason}"
        else:
            should_charge, reason = should_charge_from_grid(
                soc, solar_kw, hours_to_peak, in_peak, solar_to_bat_kw
            )
        
        desired_mode = "emergency_backup" if should_charge else "home"
        desired_mode_label = "BACKUP" if should_charge else config.HOME_MODE.upper()
        
        # Log decision with data source info
        log_intelligence("=" * 70)
        
        # Log enabled features and data source
        features = config.get_enabled_features()
        data_source_info = f"Data source: {battery_data.source}"
        if hasattr(data_manager, 'last_fallback') and data_manager.last_fallback:
            mins_ago = (datetime.now() - data_manager.last_fallback).total_seconds() / 60
            if mins_ago < 30:  # Recent fallback
                data_source_info += f" (fallback {mins_ago:.0f}m ago)"
        
        log_intelligence(f"Features: {', '.join(features) if features else 'Basic mode'}")
        log_intelligence(data_source_info)
        
        # Log current mode
        log_intelligence(f"API Mode: {battery_data.mode_name} (detected={current_mode})")
        
        # Log per-battery SOC if available
        if battery_data.per_battery_soc and len(battery_data.per_battery_soc) > 1:
            bat_soc_str = ", ".join([f"Bat{i+1}: {s:.1f}%" for i, s in enumerate(battery_data.per_battery_soc)])
            log_intelligence(f"Per-battery SOC: {bat_soc_str} (combined: {soc:.1f}%)")
        
        # Log environment data
        env_parts = []
        if battery_data.ambient_temp_c is not None:
            temp_f = battery_data.ambient_temp_c * 9 / 5 + 32
            env_parts.append(f"Temp: {temp_f:.0f}F/{battery_data.ambient_temp_c:.1f}C")
        if battery_data.cell_signal is not None:
            env_parts.append(f"Signal: {battery_data.cell_signal}")
        if battery_data.grid_frequency_hz is not None:
            env_parts.append(f"Freq: {battery_data.grid_frequency_hz:.2f}Hz")
        if env_parts:
            log_intelligence(f"Environment: {', '.join(env_parts)}")
        
        # Log dynamic pricing if enabled
        if config.DYNAMIC_PRICING_ENABLED:
            try:
                current_price = get_current_price()
                if current_price:
                    log_intelligence(f"Grid price: {current_price:.1f}c/kWh (threshold: {config.PRICE_THRESHOLD_CENTS}c)")
            except Exception:
                pass
        
        peak_status = "IN PEAK" if in_peak else f"{hours_to_peak:.1f}h to peak" if config.TOU_ENABLED else "No TOU"
        if config.TOU_ENABLED and config.PEAK_START_HOUR > config.PEAK_END_HOUR:
            peak_status += " (midnight-crossing)"
        
        log_intelligence(f"SOC: {soc:.1f}%, Solar: {solar_kw:.3f}kW, Grid: {grid_kw:.3f}kW, Battery: {battery_kw:.3f}kW")
        log_intelligence(f"Charging: Grid→Bat: {grid_to_bat_kw:.2f}kW, Solar→Bat: {solar_to_bat_kw:.2f}kW")
        log_intelligence(f"Status: {peak_status}")
        log_intelligence(f"Decision: {reason}")
        log_intelligence(f"Action: {'Grid charge (backup mode)' if should_charge else f'Solar-first ({config.HOME_MODE} mode)'}")
        
        # Determine if mode switch is needed
        mode_switched = False
        if not config.TOU_ENABLED or not in_peak:
            need_backup = should_charge and current_mode != "emergency_backup"
            need_home = not should_charge and current_mode == "emergency_backup"
            
            if need_backup or need_home:
                # Grid disconnect guard — don't attempt cloud API mode switches
                # while the system is islanded (grid outage)
                grid_ok = await check_grid_connected()
                if not grid_ok:
                    switch_target = "emergency_backup" if need_backup else "home"
                    log_intelligence(f"⚡ Grid disconnected — skipping mode switch to {switch_target}")
                    log_intelligence(f"Mode unchanged: {current_mode} (grid offline, island mode)")
                    # Skip the mode switch entirely — jump to CSV logging
                    need_backup = False
                    need_home = False
            
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
                    mode_switched = await switch_mode(switch_target)
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
        
        # Save data source health statistics
        data_manager.save_health_stats()
        
        # Log to CSV with enriched data
        now = datetime.now()
        data = {
            'timestamp': now.strftime('%Y-%m-%d %H:%M:%S'),
            'soc_percent': f'{soc:.2f}',
            'solar_kw': f'{solar_kw:.3f}',
            'grid_kw': f'{grid_kw:.3f}',
            'battery_kw': f'{battery_kw:.3f}',
            'home_load_kw': f'{home_load_kw:.3f}',
            'grid_status': battery_data.grid_status,
            'hours_to_peak': f'{hours_to_peak:.2f}' if config.TOU_ENABLED else 'N/A',
            'mode': desired_mode_label,
            'run_status': str(battery_data.run_status) if battery_data.run_status else 'N/A',
            'mode_name': battery_data.mode_name,
            'grid_charging_kw': f'{grid_to_bat_kw:.3f}',
            'solar_charging_kw': f'{solar_to_bat_kw:.3f}',
            'data_source': battery_data.source,
        }
        
        # Add per-battery SOC columns
        for i, bat_soc in enumerate(battery_data.per_battery_soc):
            data[f'battery_{i+1}_soc'] = f'{bat_soc:.1f}'
        
        # Add environment data if available
        if battery_data.ambient_temp_c is not None:
            data['ambient_temp_c'] = f'{battery_data.ambient_temp_c:.1f}'
        if battery_data.cell_signal is not None:
            data['cell_signal'] = str(battery_data.cell_signal)
        if battery_data.grid_frequency_hz is not None:
            data['grid_frequency_hz'] = f'{battery_data.grid_frequency_hz:.2f}'
        
        # Add grid connection state (from Modbus if available)
        try:
            grid_ok = await check_grid_connected()
            data['grid_connected'] = '1' if grid_ok else '0'
        except Exception:
            data['grid_connected'] = 'N/A'
        
        # Add pricing data if enabled
        if config.DYNAMIC_PRICING_ENABLED:
            try:
                current_price = get_current_price()
                data['grid_price_cents'] = f'{current_price:.2f}' if current_price else 'N/A'
            except Exception:
                data['grid_price_cents'] = 'N/A'
        
        # Add adaptive engine info if active
        if ADAPTIVE_ENGINE_LOADED:
            data['engine'] = 'v4_adaptive'
            status = adaptive_engine_instance.get_status()
            if status.get('last_decision'):
                data['engine_priority'] = str(status['last_decision']['priority_level'])
            if status.get('curtailed_kwh', 0) > 0:
                data['curtailed_kwh'] = f"{status['curtailed_kwh']:.3f}"
        
        # Write to CSV
        file_exists = config.LOG_FILE.exists()
        
        with open(config.LOG_FILE, 'a', newline='') as csvfile:
            fieldnames = list(data.keys())
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(data)
        
        # Summary output
        num_batteries = len(battery_data.per_battery_soc) if battery_data.per_battery_soc else 1
        bat_info = f" ({num_batteries} batteries)" if num_batteries > 1 else ""
        switch_info = " [SWITCHED]" if mode_switched else ""
        source_info = f" via {battery_data.source.upper()}"
        engine_info = " [v4]" if ADAPTIVE_ENGINE_LOADED else ""
        print(f"Decision: {desired_mode_label} mode ({reason}){bat_info}{switch_info}{source_info}{engine_info}")
        
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
