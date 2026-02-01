#!/usr/bin/env python3
"""
Generate dashboard data JSON file from Franklin battery, solar, and savings data.

This script collects data from:
- Franklin WH API (battery status, power flow)
- continuous_monitoring.csv (historical data)
- daily_savings.csv (savings tracking)
- System logs (automation health)

Outputs: power_dashboard_data.json to WEB_DIR

Usage:
    python generate_dashboard_data.py
    
Schedule via Task Scheduler every 1 minute for near-real-time updates.
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
    # Fallback defaults
    class FallbackConfig:
        FRANKLIN_USERNAME = ""
        FRANKLIN_PASSWORD = ""
        FRANKLIN_GATEWAY_ID = ""
        LOG_DIR = Path("/volume1/docker/franklin/logs")
        DATA_DIR = Path("/volume1/docker/franklin/data")
        WEB_DIR = Path("/volume1/web")
        BATTERY_CAPACITY_KWH = 30.0
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


async def get_franklin_data():
    """Get data from Franklin API using async client."""
    if not config.FRANKLIN_USERNAME or not config.FRANKLIN_PASSWORD:
        return None
        
    try:
        fetcher = TokenFetcher(config.FRANKLIN_USERNAME, config.FRANKLIN_PASSWORD)
        client = Client(fetcher, config.FRANKLIN_GATEWAY_ID)
        
        for attempt in range(3):
            try:
                stats = await client.get_stats()
                break
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(5)
                else:
                    raise
        
        return {
            'soc': stats.current.battery_soc,
            'mode': 'TOU',  # Could enhance by checking actual mode
            'grid_status': 'NORMAL',
            'battery_power': stats.current.battery_use,
            'solar_power': stats.current.solar_production,
            'grid_power': stats.current.grid_use,
            'home_load': stats.current.home_load,
            'battery_capacity': config.BATTERY_CAPACITY_KWH,
            'available_energy': stats.current.battery_soc / 100 * config.BATTERY_CAPACITY_KWH
        }
    except Exception as e:
        print(f"Error getting Franklin data: {e}")
        return None


def get_battery_status():
    """Get current battery status from Franklin API or CSV."""
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
    
    # Get peak hour from config or default
    peak_hour = getattr(config, 'PEAK_START_HOUR', 17)
    peak_start = now.replace(hour=peak_hour, minute=0, second=0, microsecond=0)
    
    if now >= peak_start:
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
                    'available_energy': soc / 100 * config.BATTERY_CAPACITY_KWH
                }
    except Exception as e:
        print(f"Error reading monitoring data: {e}")
    
    return None


def get_today_stats():
    """Get today's charging and solar stats."""
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
        
        # Get baseline (minimum values after midnight reset)
        charge_values = [float(row.get('battery_charge_total', 0)) for row in today_rows]
        solar_values = [float(row.get('solar_total', 0)) for row in today_rows]
        
        min_charge = min(charge_values) if charge_values else 0
        min_solar = min(solar_values) if solar_values else 0
        
        current_charge = float(last_row.get('battery_charge_total', 0))
        current_solar = float(last_row.get('solar_total', 0))
        
        total_charged = current_charge - min_charge
        solar_generated = current_solar - min_solar
        
        solar_ratio = (solar_generated / total_charged * 100) if total_charged > 0 else 0
        
        return {
            'total_charged': round(total_charged, 1),
            'solar_generated': round(solar_generated, 1),
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
    
    # Check if automation is running
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


def generate_dashboard_data():
    """Generate complete dashboard data structure."""
    print(f"Generating dashboard data at {datetime.now()}")
    
    battery_status = get_battery_status()
    peak_countdown = get_peak_countdown()
    today_stats = get_today_stats()
    savings_data = get_savings_data()
    true_up_data = get_true_up_projection()
    system_health = get_system_health()
    
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
        'system_health': system_health
    }
    
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
