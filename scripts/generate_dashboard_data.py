#!/usr/bin/env python3
"""
Generate dashboard data JSON file from Franklin battery, solar, and savings data.

This script collects data from:
- Franklin WH API (battery status, power flow, raw gateway status)
- continuous_monitoring.csv (historical data)
- daily_savings.csv (savings tracking)
- System logs (automation health)

Outputs: power_dashboard_data.json to WEB_DIR

Designed to run inside Docker container via internal scheduler (every 1 minute).
Can also run standalone for testing.

v3.4 - Added extended status block with per-battery SOC, environment data,
       energy totals, mode detection via name field, and config export.
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
        LOG_FILE = LOG_DIR / "continuous_monitoring.csv"
        INTELLIGENCE_LOG = LOG_DIR / "solar_intelligence.log"
    config = FallbackConfig()

# Try to import franklinwh
try:
    from franklinwh import Client, TokenFetcher
    import asyncio
    FRANKLIN_AVAILABLE = True
except ImportError:
    print("Warning: franklinwh not available, using CSV data only")
    FRANKLIN_AVAILABLE = False


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
    """Calculate time until peak period starts."""
    now = datetime.now()
    
    peak_hour = getattr(config, 'PEAK_START_HOUR', 17)
    peak_end_hour = getattr(config, 'PEAK_END_HOUR', 20)
    peak_start = now.replace(hour=peak_hour, minute=0, second=0, microsecond=0)
    peak_end = now.replace(hour=peak_end_hour, minute=0, second=0, microsecond=0)
    
    # Currently in peak
    if peak_start <= now < peak_end:
        return {
            'minutes': 0,
            'time': peak_start.strftime('%I:%M %p')
        }
    
    # Past today's peak, calculate to tomorrow
    if now >= peak_end:
        peak_start = peak_start + timedelta(days=1)
    
    delta = peak_start - now
    minutes = int(delta.total_seconds() / 60)
    
    return {
        'minutes': minutes,
        'time': peak_start.strftime('%I:%M %p')
    }


def get_latest_monitoring_data():
    """Get the most recent entry from continuous_monitoring.csv."""
    monitoring_file = config.LOG_FILE
    
    if not monitoring_file.exists():
        return None
    
    try:
        with open(monitoring_file, 'r') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            if rows:
                latest = rows[-1]
                soc = float(latest.get('soc_percent', 0))
                return {
                    'soc': soc,
                    'mode': latest.get('mode', 'UNKNOWN'),
                    'battery_power': float(latest.get('battery_kw', 0)),
                    'solar_power': float(latest.get('solar_kw', 0)),
                    'grid_power': float(latest.get('grid_kw', 0)),
                    'home_load': float(latest.get('home_load_kw', 0)),
                    'battery_capacity': config.BATTERY_CAPACITY_KWH,
                    'available_energy': soc / 100 * config.BATTERY_CAPACITY_KWH,
                    # CSV enriched fields (v3.2.0+)
                    'grid_charging_kw': float(latest.get('grid_charging_kw', 0)),
                    'solar_charging_kw': float(latest.get('solar_charging_kw', 0)),
                    'mode_name': latest.get('mode_name', ''),
                    'run_status': latest.get('run_status', ''),
                }
    except Exception as e:
        print(f"Error reading monitoring data: {e}")
    
    return None


def get_today_stats():
    """Get today's charging and solar stats from CSV."""
    monitoring_file = config.LOG_FILE
    
    if not monitoring_file.exists():
        return None
    
    try:
        today = datetime.now().date()
        today_rows = []
        
        with open(monitoring_file, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    timestamp = datetime.fromisoformat(row['timestamp'])
                    if timestamp.date() == today:
                        today_rows.append(row)
                except:
                    continue
        
        if not today_rows:
            return None
        
        last_row = today_rows[-1]
        
        # Calculate totals from deltas
        charge_values = [float(row.get('battery_charge_total', 0)) for row in today_rows]
        discharge_values = [float(row.get('battery_discharge_total', 0)) for row in today_rows]
        solar_values = [float(row.get('solar_total', 0)) for row in today_rows]
        
        min_charge = min(charge_values) if charge_values else 0
        min_discharge = min(discharge_values) if discharge_values else 0
        min_solar = min(solar_values) if solar_values else 0
        
        current_charge = float(last_row.get('battery_charge_total', 0))
        current_discharge = float(last_row.get('battery_discharge_total', 0))
        current_solar = float(last_row.get('solar_total', 0))
        
        total_charged = current_charge - min_charge
        total_discharged = current_discharge - min_discharge
        solar_generated = current_solar - min_solar
        
        solar_ratio = (solar_generated / total_charged * 100) if total_charged > 0 else 0
        
        return {
            'total_charged': round(total_charged, 2),
            'total_discharged': round(total_discharged, 2),
            'solar_generated': round(solar_generated, 2),
            'solar_ratio': round(solar_ratio, 1)
        }
    except Exception as e:
        print(f"Error calculating today stats: {e}")
    
    return None


def get_savings_data():
    """Get savings data from daily_savings.csv."""
    savings_file = config.DATA_DIR / 'daily_savings.csv'
    
    if not savings_file.exists():
        return None
    
    try:
        with open(savings_file, 'r') as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            
            if not rows:
                return None
            
            today = rows[-1]
            
            current_month = datetime.now().strftime('%Y-%m')
            month_rows = [r for r in rows if r['date'].startswith(current_month)]
            month_total = sum(float(r.get('total_savings', 0)) for r in month_rows)
            
            avg_daily = sum(float(r.get('total_savings', 0)) for r in rows) / len(rows)
            
            return {
                'today': round(float(today.get('total_savings', 0)), 2),
                'month_total': round(month_total, 2),
                'days_this_month': len(month_rows),
                'avg_daily': round(avg_daily, 2),
                'solar_ratio_today': round(float(today.get('solar_ratio', 0)) * 100, 1)
            }
    except Exception as e:
        print(f"Error reading savings data: {e}")
    
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
    
    # Check if automation is running (intelligence log fresh?)
    log_file = config.INTELLIGENCE_LOG
    if log_file.exists():
        try:
            mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
            age = datetime.now() - mtime
            if age > timedelta(hours=1):
                health['automation_script'] = 'warning'
        except:
            pass
    else:
        health['automation_script'] = 'error'
    
    return health


def get_config_info():
    """Export relevant config settings for the dashboard Settings tab."""
    return {
        'peak_soc_target': getattr(config, 'TARGET_SOC', 95),
        'min_soc_reserve': getattr(config, 'MIN_SOC_RESERVE', 20),
        'charge_rate_kw': getattr(config, 'CHARGE_RATE_PER_HOUR', 10),
        'peak_start_hour': getattr(config, 'PEAK_START_HOUR', 17),
        'peak_end_hour': getattr(config, 'PEAK_END_HOUR', 20),
        'battery_capacity_kwh': config.BATTERY_CAPACITY_KWH,
        'tou_enabled': getattr(config, 'TOU_ENABLED', True),
        'solar_enabled': getattr(config, 'SOLAR_ENABLED', True),
        'dynamic_pricing_enabled': getattr(config, 'DYNAMIC_PRICING_ENABLED', False),
        'home_mode': getattr(config, 'HOME_MODE', 'tou'),
    }


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
        'today': {
            'charged': today_stats['total_charged'] if today_stats else 0,
            'discharged': today_stats['total_discharged'] if today_stats else 0,
            'solar_generated': today_stats['solar_generated'] if today_stats else 0,
            'solar_ratio': today_stats['solar_ratio'] if today_stats else 0
        },
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
