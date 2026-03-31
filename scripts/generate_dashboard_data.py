#!/usr/bin/env python3
"""
Generate dashboard data JSON file from Franklin battery, solar, and savings data.

This script collects data from:
- Franklin WH API (battery status, power flow, raw gateway status)
- SQLite system_readings table (historical data, replaces continuous_monitoring.csv)
- SQLite daily_savings table (savings tracking, replaces daily_savings.csv)
- System logs (automation health)

Outputs: power_dashboard_data.json to WEB_DIR

Designed to run inside Docker container via internal scheduler (every 1 minute).
Can also run standalone for testing.

v3.4 - Added extended status block with per-battery SOC, environment data,
       energy totals, mode detection via name field, and config export.
v3.5 - Switched from CSV reads to SQLite database queries.
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
import csv

# Import configuration
try:
    from config import config
    CONFIG_AVAILABLE = True
except ImportError:
    CONFIG_AVAILABLE = False
    class FallbackConfig:
        FRANKLIN_USERNAME = ""
        FRANKLIN_PASSWORD = ""
        FRANKLIN_GATEWAY_ID = ""
        LOG_DIR = Path("/app/logs")
        DATA_DIR = Path("/app/data")
        WEB_DIR = Path("/app/web")
        BATTERY_CAPACITY_KWH = 30.0
        TARGET_SOC = 95.0
        PEAK_START_HOUR = 17
        PEAK_END_HOUR = 20
        MIN_SOC_RESERVE = 20
        CHARGE_RATE_PER_HOUR = 10.0
        TOU_ENABLED = True
        SOLAR_ENABLED = True
        DYNAMIC_PRICING_ENABLED = False
        HOME_MODE = "tou"
    config = FallbackConfig()

# Import database
try:
    import db as db_mod
    db_mod.init_db()
    DB_AVAILABLE = True
except Exception:
    DB_AVAILABLE = False

# Try to import franklinwh
try:
    from franklinwh import Client, TokenFetcher
    import asyncio
    FRANKLIN_AVAILABLE = True
except ImportError:
    print("Warning: franklinwh not available, using CSV data only")
    FRANKLIN_AVAILABLE = False

# Import rate schedule for peak window awareness
try:
    from rate_schedule import load_rate_schedule
    RATE_SCHEDULE_AVAILABLE = True
except ImportError:
    RATE_SCHEDULE_AVAILABLE = False

_cached_schedule = None
_schedule_loaded_at = None


def _get_rate_schedule():
    """Load rate schedule with 5-minute caching."""
    global _cached_schedule, _schedule_loaded_at
    now = datetime.now()
    if (_cached_schedule and _schedule_loaded_at
            and (now - _schedule_loaded_at).total_seconds() < 300):
        return _cached_schedule
    if not RATE_SCHEDULE_AVAILABLE:
        return None
    try:
        json_path = config.DATA_DIR / 'rate_schedule.json'
        if json_path.exists():
            _cached_schedule = load_rate_schedule(str(json_path))
            _schedule_loaded_at = now
            return _cached_schedule
    except Exception as e:
        print(f"Warning: Could not load rate schedule: {e}")
    return None


# =============================================================================
# Franklin API data collection
# =============================================================================

async def get_franklin_data():
    """
    Get comprehensive data from Franklin API using async client.
    
    Returns both stats (power flow, SOC) and raw gateway status
    (per-battery, environment, energy totals, mode info).
    """
    if not config.FRANKLIN_USERNAME or not config.FRANKLIN_PASSWORD:
        return None
        
    try:
        fetcher = TokenFetcher(config.FRANKLIN_USERNAME, config.FRANKLIN_PASSWORD)
        client = Client(fetcher, config.FRANKLIN_GATEWAY_ID)
        
        # Get stats with retry
        stats = None
        for attempt in range(3):
            try:
                stats = await client.get_stats()
                break
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(5)
                else:
                    raise
        
        # Get raw gateway status for extended data
        status = None
        try:
            status = await client._status()
        except Exception as e:
            print(f"Warning: Could not get gateway status: {e}")
        
        # Build basic data from stats
        result = {
            'soc': stats.current.battery_soc,
            'grid_status': stats.current.grid_status.name if hasattr(stats.current.grid_status, 'name') else 'NORMAL',
            'battery_power': stats.current.battery_use,
            'solar_power': stats.current.solar_production,
            'grid_power': stats.current.grid_use,
            'home_load': stats.current.home_load,
            'battery_capacity': config.BATTERY_CAPACITY_KWH,
            'available_energy': stats.current.battery_soc / 100 * config.BATTERY_CAPACITY_KWH,
            # Totals from stats
            'battery_charge_total': getattr(stats.totals, 'battery_charge', 0),
            'battery_discharge_total': getattr(stats.totals, 'battery_discharge', 0),
            'grid_import_total': getattr(stats.totals, 'grid_import', 0),
            'grid_export_total': getattr(stats.totals, 'grid_export', 0),
            'solar_total': getattr(stats.totals, 'solar', 0),
        }
        
        # Detect mode from status using name field (reliable across firmware)
        mode_name = "Unknown"
        detected_mode = "unknown"
        if status:
            mode_name = status.get("name", "Unknown")
            name_lower = mode_name.lower()
            if "emergency" in name_lower or "backup" in name_lower:
                detected_mode = "emergency_backup"
            elif "self" in name_lower and "consumption" in name_lower:
                detected_mode = "self_consumption"
            else:
                # TOU-B, custom schedules, etc. -> user's home mode
                detected_mode = getattr(config, 'HOME_MODE', 'tou')
        
        # Map to display mode
        mode_display_map = {
            'emergency_backup': 'BACKUP',
            'self_consumption': 'SELF_CONSUMPTION',
            'tou': 'TOU',
        }
        result['mode'] = mode_display_map.get(detected_mode, detected_mode.upper())
        
        # Build extended data from raw gateway status
        if status:
            result['extended'] = build_extended_status(status, mode_name)
        
        # Cache cumulative totals for smart_decision.py CSV logging
        # (smart_decision uses Modbus ~99% of the time and can't get these)
        try:
            cache_file = config.DATA_DIR / 'energy_totals_cache.json'
            cache_data = {
                'timestamp': datetime.now().isoformat(),
                'battery_charge': getattr(stats.totals, 'battery_charge', 0),
                'battery_discharge': getattr(stats.totals, 'battery_discharge', 0),
                'grid_import': getattr(stats.totals, 'grid_import', 0),
                'solar': getattr(stats.totals, 'solar', 0),
            }
            with open(cache_file, 'w') as f:
                json.dump(cache_data, f)
        except Exception as e:
            print(f"Warning: Could not cache energy totals: {e}")
        
        return result
    except Exception as e:
        print(f"Error getting Franklin data: {e}")
        return None


def build_extended_status(status, mode_name):
    """
    Build the extended status block from raw gateway _status() response.
    
    This populates all the fields the dashboard System Info tab expects:
    - Per-battery SOC, power, and serial numbers
    - Environment (temperature, cell signal, wifi)
    - Energy totals (today's kWh for solar, grid, load, battery)
    - Lifetime totals
    - Mode info (run_status, name)
    - Switch states, generator status, V2L, etc.
    """
    ext = {}
    
    # Mode info
    ext['run_status'] = status.get('run_status', -1)
    ext['mode_name'] = mode_name
    
    # Per-battery data
    battery_socs = status.get('fhpSoc', [])
    battery_powers = status.get('fhpPower', [])
    battery_serials = status.get('fhpSn', [])
    
    if battery_socs:
        ext['per_battery_soc'] = battery_socs
        ext['battery_count'] = len(battery_socs)
    if battery_powers:
        ext['per_battery_power'] = battery_powers
    if battery_serials:
        ext['battery_serials'] = battery_serials
    
    # Charging breakdown
    ext['gridChBat'] = status.get('gridChBat', 0)
    ext['soChBat'] = status.get('soChBat', 0)
    
    # Today's energy totals (kWh)
    ext['kwh_sun'] = status.get('kwh_sun', 0)
    ext['kwh_uti_in'] = status.get('kwh_uti_in', 0)
    ext['kwh_uti_out'] = status.get('kwh_uti_out', 0)
    ext['kwh_load'] = status.get('kwh_load', 0)
    ext['kwh_fhp_chg'] = status.get('kwh_fhp_chg', 0)
    ext['kwh_fhp_di'] = status.get('kwh_fhp_di', 0)
    ext['kwh_gen'] = status.get('kwh_gen', 0)
    
    # Lifetime totals
    ext['kwhFhpLoad'] = status.get('kwhFhpLoad')
    ext['kwhGridLoad'] = status.get('kwhGridLoad')
    ext['kwhSolarLoad'] = status.get('kwhSolarLoad')
    ext['kwhGenLoad'] = status.get('kwhGenLoad')
    
    # Environment
    ext['t_amb'] = status.get('t_amb')
    ext['signal'] = status.get('signal')
    ext['wifiSignal'] = status.get('wifiSignal')
    
    # Gateway info
    ext['gateway_sn'] = status.get('sn', '')
    
    # Switch states and protected loads
    ext['main_sw'] = status.get('main_sw')
    ext['pro_load'] = status.get('pro_load')
    
    # Generator and V2L
    ext['genStat'] = status.get('genStat')
    ext['v2lModeEnable'] = status.get('v2lModeEnable', False)
    
    # BMS and PE status
    ext['bms_work'] = status.get('bms_work')
    ext['pe_stat'] = status.get('pe_stat')
    
    return ext


# =============================================================================
# Data source helpers
# =============================================================================

def get_battery_status():
    """Get current battery status from Franklin API or CSV fallback."""
    if FRANKLIN_AVAILABLE and config.FRANKLIN_USERNAME:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            status = loop.run_until_complete(get_franklin_data())
            loop.close()
            
            if status:
                return status
        except Exception as e:
            print(f"Error in get_battery_status: {e}")
    
    # Fallback to latest CSV data
    return get_latest_monitoring_data()


def get_peak_countdown():
    """Calculate time until peak period starts, using rate_schedule.json if available."""
    now = datetime.now()

    schedule = _get_rate_schedule()
    if schedule:
        if schedule.is_peak(now):
            peak_end = schedule.next_peak_end(now)
            end_str = peak_end.strftime('%I:%M %p') if peak_end else ''
            return {
                'minutes': 0,
                'time': f'In peak until {end_str}' if end_str else 'In peak'
            }

        next_peak = schedule.next_peak_start(now)
        if next_peak is None:
            return {'minutes': -1, 'time': 'No peak today'}

        delta = next_peak - now
        minutes = int(delta.total_seconds() / 60)
        return {
            'minutes': minutes,
            'time': next_peak.strftime('%I:%M %p')
        }

    # Fallback to config.py values if rate_schedule unavailable
    peak_days = getattr(config, 'PEAK_DAYS', 'all')
    if peak_days == 'weekdays' and now.weekday() >= 5:
        return {'minutes': -1, 'time': 'No peak today'}
    elif peak_days == 'weekends' and now.weekday() < 5:
        return {'minutes': -1, 'time': 'No peak today'}

    peak_hour = getattr(config, 'PEAK_START_HOUR', 17)
    peak_end_hour = getattr(config, 'PEAK_END_HOUR', 20)
    peak_start = now.replace(hour=peak_hour, minute=0, second=0, microsecond=0)
    peak_end = now.replace(hour=peak_end_hour, minute=0, second=0, microsecond=0)

    if peak_start <= now < peak_end:
        return {
            'minutes': 0,
            'time': peak_start.strftime('%I:%M %p')
        }

    if now >= peak_end:
        peak_start = peak_start + timedelta(days=1)

    delta = peak_start - now
    minutes = int(delta.total_seconds() / 60)

    return {
        'minutes': minutes,
        'time': peak_start.strftime('%I:%M %p')
    }


def get_latest_monitoring_data():
    """Get the most recent system reading from SQLite."""
    if not DB_AVAILABLE:
        print("Warning: Database not available for monitoring data")
        return None

    try:
        row = db_mod.get_latest_system()
        if row:
            soc = float(row.get('soc_pct') or 0)
            return {
                'soc': soc,
                'mode': row.get('mode', 'UNKNOWN'),
                'battery_power': float(row.get('battery_kw') or 0),
                'solar_power': float(row.get('solar_kw') or 0),
                'grid_power': float(row.get('grid_kw') or 0),
                'home_load': float(row.get('home_load_kw') or 0),
                'battery_capacity': config.BATTERY_CAPACITY_KWH,
                'available_energy': soc / 100 * config.BATTERY_CAPACITY_KWH,
                'grid_charging_kw': float(row.get('grid_to_battery_kw') or 0),
                'solar_charging_kw': float(row.get('solar_to_battery_kw') or 0),
                'mode_name': row.get('mode_detail', ''),
                'run_status': str(row.get('run_status', '')),
            }
    except Exception as e:
        print(f"Error reading monitoring data from SQLite: {e}")

    return None


def get_today_stats():
    """Get today's charging and solar stats from SQLite."""
    if not DB_AVAILABLE:
        return None

    try:
        today_str = datetime.now().strftime('%Y-%m-%d')
        rows = db_mod.get_readings_for_date(today_str)
        if not rows:
            return None

        charge_vals = [r['kwh_battery_charge'] for r in rows if r.get('kwh_battery_charge') is not None]
        discharge_vals = [r['kwh_battery_discharge'] for r in rows if r.get('kwh_battery_discharge') is not None]
        solar_vals = [r['kwh_solar'] for r in rows if r.get('kwh_solar') is not None]

        def day_delta(vals):
            return (max(vals) - min(vals)) if vals else 0

        total_charged = day_delta(charge_vals)
        total_discharged = day_delta(discharge_vals)
        solar_generated = day_delta(solar_vals)
        solar_ratio = (solar_generated / total_charged * 100) if total_charged > 0 else 0

        return {
            'total_charged': round(total_charged, 2),
            'total_discharged': round(total_discharged, 2),
            'solar_generated': round(solar_generated, 2),
            'solar_ratio': round(solar_ratio, 1)
        }
    except Exception as e:
        print(f"Error calculating today stats from SQLite: {e}")

    return None


def get_savings_data():
    """Get savings data from SQLite daily_savings table."""
    if not DB_AVAILABLE:
        return None

    try:
        rows = db_mod.get_daily_savings_recent(limit=365)
        if not rows:
            return None

        rows = sorted(rows, key=lambda r: r['date'])
        today_row = rows[-1]

        current_month = datetime.now().strftime('%Y-%m')
        month_rows = [r for r in rows if r['date'].startswith(current_month)]
        month_total = sum(float(r.get('total_savings') or 0) for r in month_rows)

        avg_daily = sum(float(r.get('total_savings') or 0) for r in rows) / len(rows)

        return {
            'today': round(float(today_row.get('total_savings') or 0), 2),
            'month_total': round(month_total, 2),
            'days_this_month': len(month_rows),
            'avg_daily': round(avg_daily, 2),
            'solar_ratio_today': round(float(today_row.get('solar_ratio') or 0) * 100, 1)
        }
    except Exception as e:
        print(f"Error reading savings data from SQLite: {e}")

    return None


def get_true_up_projection():
    """Get latest True-Up projection."""
    projection_file = config.DATA_DIR / 'true_up_projection.json'
    
    if not projection_file.exists():
        return None
    
    try:
        with open(projection_file, 'r') as f:
            data = json.load(f)
            projections = data.get('projections', [])
            if projections:
                latest = projections[-1]
                return {
                    'projected_true_up': round(latest.get('projected_final_true_up', 0), 2),
                    'improvement_percent': round(latest.get('improvement_percent', 0), 1),
                    'last_year_amount': round(latest.get('last_true_up_amount', 0), 2)
                }
    except Exception as e:
        print(f"Error reading True-Up projection: {e}")
    
    return None


def get_system_health():
    """Check system health status."""
    health = {
        'battery_api': 'ok',
        'automation_script': 'ok',
        'solar': 'ok',
        'grid_connection': 'ok',
        'generator': 'standby'
    }
    
    # Check if automation is running (recent intelligence log entries in DB?)
    try:
        import db as db_mod
        rows = db_mod.get_recent_intelligence_logs(limit=1)
        if rows:
            from datetime import datetime as dt
            last_ts = dt.strptime(rows[0]['timestamp'], '%Y-%m-%d %H:%M:%S')
            age = datetime.now() - last_ts
            if age > timedelta(hours=1):
                health['automation_script'] = 'warning'
        else:
            health['automation_script'] = 'warning'
    except Exception:
        health['automation_script'] = 'error'
    
    return health


def get_config_info():
    """Export relevant config settings for the dashboard Settings tab."""
    info = {
        'peak_soc_target': getattr(config, 'TARGET_SOC', 95),
        'min_soc_reserve': getattr(config, 'MIN_SOC_RESERVE', 20),
        'charge_rate_kw': getattr(config, 'CHARGE_RATE_PER_HOUR', 10),
        'battery_capacity_kwh': config.BATTERY_CAPACITY_KWH,
        'tou_enabled': getattr(config, 'TOU_ENABLED', True),
        'solar_enabled': getattr(config, 'SOLAR_ENABLED', True),
        'dynamic_pricing_enabled': getattr(config, 'DYNAMIC_PRICING_ENABLED', False),
        'home_mode': getattr(config, 'HOME_MODE', 'tou'),
    }

    schedule = _get_rate_schedule()
    if schedule:
        info['rate_schedule_name'] = schedule.name
        peak_windows = [w for w in schedule.windows if w.tier == 'peak']
        info['peak_windows'] = [
            {'start': w.start.strftime('%H:%M'), 'end': w.end.strftime('%H:%M'),
             'days': w.days}
            for w in peak_windows
        ]
        if peak_windows:
            info['peak_start_hour'] = peak_windows[0].start.hour
            info['peak_end_hour'] = peak_windows[0].end.hour
        else:
            info['peak_start_hour'] = getattr(config, 'PEAK_START_HOUR', 17)
            info['peak_end_hour'] = getattr(config, 'PEAK_END_HOUR', 20)
    else:
        info['peak_start_hour'] = getattr(config, 'PEAK_START_HOUR', 17)
        info['peak_end_hour'] = getattr(config, 'PEAK_END_HOUR', 20)

    return info


def get_today_from_api_or_csv(current_data, today_stats):
    """
    Get today's energy totals, preferring gateway daily kWh from _status()
    over CSV-derived values. The gateway tracks daily totals natively
    (kwh_fhp_chg, kwh_fhp_di, kwh_sun) which reset at midnight.
    """
    ext = current_data.get('extended', {}) if current_data else {}

    # Gateway daily totals (from _status() response)
    api_charged = ext.get('kwh_fhp_chg', 0)
    api_discharged = ext.get('kwh_fhp_di', 0)
    api_solar = ext.get('kwh_sun', 0)

    # Use gateway values if available (non-zero or early morning is fine)
    if api_charged or api_discharged or api_solar:
        solar_ratio = (api_solar / api_charged * 100) if api_charged > 0 else 0
        return {
            'charged': round(api_charged, 2),
            'discharged': round(api_discharged, 2),
            'solar_generated': round(api_solar, 2),
            'solar_ratio': round(solar_ratio, 1)
        }

    # Fallback to CSV-derived values
    if today_stats:
        return {
            'charged': today_stats['total_charged'],
            'discharged': today_stats['total_discharged'],
            'solar_generated': today_stats['solar_generated'],
            'solar_ratio': today_stats['solar_ratio']
        }

    return {'charged': 0, 'discharged': 0, 'solar_generated': 0, 'solar_ratio': 0}


# =============================================================================
# Main data assembly
# =============================================================================

def generate_dashboard_data():
    """Generate complete dashboard data structure."""
    print(f"Generating dashboard data at {datetime.now()}")
    
    battery_status = get_battery_status()
    peak_countdown = get_peak_countdown()
    today_stats = get_today_stats()
    savings_data = get_savings_data()
    true_up_data = get_true_up_projection()
    system_health = get_system_health()
    config_info = get_config_info()
    
    # Use battery status or defaults
    if battery_status:
        current_data = battery_status
    else:
        current_data = {
            'soc': 0,
            'mode': 'UNKNOWN',
            'battery_power': 0,
            'solar_power': 0,
            'grid_power': 0,
            'home_load': 0,
            'available_energy': 0
        }
    
    dashboard_data = {
        'timestamp': datetime.now().isoformat(),
        'gateway_id': getattr(config, 'FRANKLIN_GATEWAY_ID', ''),
        'battery': {
            'soc': current_data.get('soc', 0),
            'mode': current_data.get('mode', 'UNKNOWN'),
            'current_power': current_data.get('battery_power', 0),
            'capacity': config.BATTERY_CAPACITY_KWH,
            'available_energy': current_data.get('available_energy', 0),
            'peak_countdown_minutes': peak_countdown['minutes'],
            'peak_time': peak_countdown['time']
        },
        'energy_flow': {
            'solar': current_data.get('solar_power', 0),
            'grid': current_data.get('grid_power', 0),
            'battery': current_data.get('battery_power', 0),
            'home': current_data.get('home_load', 0),
            'generator': 0
        },
        'today': get_today_from_api_or_csv(current_data, today_stats),
        'savings': {
            'today': savings_data['today'] if savings_data else 0,
            'month_total': savings_data['month_total'] if savings_data else 0,
            'days_this_month': savings_data['days_this_month'] if savings_data else 0,
            'avg_daily': savings_data['avg_daily'] if savings_data else 0,
            'projected_annual': (savings_data['avg_daily'] * 365) if savings_data else 0,
            'true_up_projection': true_up_data['projected_true_up'] if true_up_data else 0,
            'improvement_percent': true_up_data['improvement_percent'] if true_up_data else 0
        },
        'system_health': system_health,
        'config': config_info,
    }
    
    # Add extended block if API provided it
    if battery_status and 'extended' in battery_status:
        dashboard_data['extended'] = battery_status['extended']
    
    return dashboard_data


def main():
    """Main execution."""
    data = generate_dashboard_data()
    
    # Output path
    output_file = config.WEB_DIR / 'power_dashboard_data.json'
    
    # Create directory if needed
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    try:
        with open(output_file, 'w') as f:
            json.dump(data, f, indent=2)
        print(f"Dashboard data written to {output_file}")
        return 0
    except Exception as e:
        print(f"Error writing dashboard data: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())
