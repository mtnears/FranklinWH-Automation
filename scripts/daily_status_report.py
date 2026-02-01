#!/usr/bin/env python3
"""
Daily Battery Status Report - v3.0

Sends summary of the day's solar intelligence decisions and current status.
Runs at 10:15 PM after peak period ends.

Now includes configuration summary at the top showing enabled/disabled features.
"""
import subprocess
from datetime import datetime, timedelta

# Import configuration
from config import config


def get_configuration_summary() -> str:
    """Generate configuration summary header."""
    lines = [
        "SYSTEM CONFIGURATION:",
        "-" * 60,
    ]
    
    # Battery info
    lines.append(f"  Battery: {config.BATTERY_CAPACITY_KWH} kWh @ {config.CHARGE_RATE_PER_HOUR}%/hr")
    lines.append(f"  Target SOC: {config.TARGET_SOC}% | Strategy: {config.CHARGING_STRATEGY}")
    lines.append("")
    
    # Feature status
    lines.append("  Features:")
    
    # Solar
    if config.SOLAR_ENABLED:
        lines.append(f"    [ON]  Solar ({config.SOLAR_CAPACITY_KW} kW capacity)")
    else:
        lines.append(f"    [OFF] Solar")
    
    # TOU
    if config.TOU_ENABLED:
        peak_time = f"{config.PEAK_START_HOUR}:00-{config.PEAK_END_HOUR}:00"
        lines.append(f"    [ON]  TOU Peak Protection ({peak_time}, {config.PEAK_DAYS})")
    else:
        lines.append(f"    [OFF] TOU Peak Protection")
    
    # Dynamic Pricing
    if config.DYNAMIC_PRICING_ENABLED:
        lines.append(f"    [ON]  Dynamic Pricing ({config.PRICING_PROVIDER}, threshold: {config.PRICE_THRESHOLD_CENTS}c)")
    else:
        lines.append(f"    [OFF] Dynamic Pricing")
    
    # Weather
    if config.WEATHER_ENABLED:
        lines.append(f"    [ON]  Weather ({config.WEATHER_STATION_ID})")
    else:
        lines.append(f"    [OFF] Weather Forecasting")
    
    # PVOutput
    if config.PVOUTPUT_ENABLED:
        lines.append(f"    [ON]  PVOutput Tracking")
    else:
        lines.append(f"    [OFF] PVOutput Tracking")
    
    return "\n".join(lines)


def get_battery_status() -> str:
    """Get current battery status."""
    try:
        script_path = config.BASE_DIR / 'get_battery_status.py'
        result = subprocess.run(
            [str(script_path)],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.stdout
    except Exception as e:
        return f"ERROR getting battery status: {e}"


def get_todays_energy_summary() -> str:
    """Get today's energy flow summary from continuous monitoring."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        result = subprocess.run(
            ['grep', today, str(config.LOG_FILE)],
            capture_output=True,
            text=True
        )

        lines = result.stdout.strip().split('\n')
        if not lines or lines[0] == '':
            return "No monitoring data available for today."

        soc_values = []
        solar_values = []
        grid_values = []
        battery_values = []
        price_values = []

        for line in lines:
            if not line or 'timestamp' in line:
                continue
            parts = line.split(',')
            if len(parts) >= 5:
                try:
                    soc_values.append(float(parts[1]))
                    solar_values.append(float(parts[2]))
                    grid_values.append(float(parts[3]))
                    battery_values.append(float(parts[4]))
                    # Check for price data (if dynamic pricing enabled)
                    if config.DYNAMIC_PRICING_ENABLED and len(parts) > 13:
                        try:
                            price = float(parts[13]) if parts[13] != 'N/A' else None
                            if price:
                                price_values.append(price)
                        except:
                            pass
                except:
                    continue

        if soc_values:
            summary = f"""
Today's Energy Summary (based on {len(soc_values)} readings):
  SOC Range: {min(soc_values):.1f}% - {max(soc_values):.1f}%
  Current SOC: {soc_values[-1]:.1f}%

  Solar Production:
    Average: {sum(solar_values)/len(solar_values):.2f} kW
    Peak: {max(solar_values):.2f} kW

  Grid Usage:
    Average: {sum(grid_values)/len(grid_values):.2f} kW
    Peak Import: {max(grid_values):.2f} kW

  Battery Activity:
    Peak Charge: {min(battery_values):.2f} kW
    Peak Discharge: {max(battery_values):.2f} kW
"""
            # Add pricing summary if available
            if price_values:
                summary += f"""
  Grid Pricing (Dynamic):
    Average: {sum(price_values)/len(price_values):.2f} cents/kWh
    Range: {min(price_values):.2f} - {max(price_values):.2f} cents/kWh
"""
            return summary
        else:
            return "No valid monitoring data for today."

    except Exception as e:
        return f"ERROR analyzing energy data: {e}"


def get_five_day_performance() -> str:
    """Get rolling 5-day performance table."""
    try:
        with open(config.INTELLIGENCE_LOG, 'r') as f:
            lines = f.readlines()
        
        days = []
        for i in range(4, -1, -1):
            date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            days.append(date)
        
        performance = []
        
        for date in days:
            day_data = {
                'date': date,
                'grid_charge_times': [],
                'soc_445pm': 'N/A',
                'mode_switches': 0,
                'peak_protection': 'OK' if config.TOU_ENABLED else 'N/A'
            }
            
            for line in lines:
                if date in line:
                    if 'Mode changed: TOU' in line and 'BACKUP' in line:
                        time = line.split()[1]
                        day_data['grid_charge_times'].append(time[:5])
                    if 'Mode changed' in line:
                        day_data['mode_switches'] += 1
            
            # Get SOC at 4:45 PM (if TOU enabled)
            if config.TOU_ENABLED:
                target_time = f"{date} 16:45"
                for line in lines:
                    if target_time in line and 'SOC:' in line:
                        try:
                            soc_part = line.split('SOC:')[1].split('%')[0].strip()
                            day_data['soc_445pm'] = f"{soc_part}%"
                        except:
                            pass
                
                # Check for peak violations
                peak_violations = [l for l in lines if date in l and 
                                 any(f"{date} {h}:" in l for h in ['17:', '18:', '19:']) and
                                 'SWITCHING' in l]
                if peak_violations:
                    day_data['peak_protection'] = 'FAIL'
            
            performance.append(day_data)
        
        performance.reverse()
        
        table = """
ROLLING 5-DAY PERFORMANCE:
--------------------------------------------------------------------------------
Date       | Grid Charge Start | SOC@4:45PM | Switches | Peak | Notes
-----------|-------------------|------------|----------|------|------------------"""
        
        for day in performance:
            date_str = day['date']
            charges = ', '.join(day['grid_charge_times']) if day['grid_charge_times'] else 'None'
            soc = day['soc_445pm']
            switches = str(day['mode_switches'])
            peak = day['peak_protection']
            
            if day['mode_switches'] == 0:
                notes = "No activity"
            elif day['mode_switches'] >= 6:
                notes = "Variable clouds"
            elif day['mode_switches'] >= 4:
                notes = "Multiple cycles"
            elif day['grid_charge_times']:
                notes = "Grid charge"
            else:
                notes = "Solar only"
            
            row = f"{date_str} | {charges:17} | {soc:10} | {switches:8} | {peak:4} | {notes}"
            table += f"\n{row}"
        
        table += "\n--------------------------------------------------------------------------------"
        return table
        
    except Exception as e:
        return f"ERROR generating 5-day performance table: {e}"


def get_todays_mode_switches() -> str:
    """Get today's mode switches only."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(config.INTELLIGENCE_LOG, 'r') as f:
            lines = f.readlines()

        switch_lines = []
        for line in lines:
            if line.startswith(today) and ('SWITCHING' in line or 'Mode changed' in line):
                switch_lines.append(line.strip())

        if switch_lines:
            return '\n'.join(switch_lines)
        else:
            return "No mode switches today (stayed in solar-first mode all day)."

    except Exception as e:
        return f"ERROR reading mode switches: {e}"


def get_peak_summary() -> str:
    """Get peak period summary for today."""
    if not config.TOU_ENABLED:
        return "TOU peak protection is not enabled."
    
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(config.INTELLIGENCE_LOG, 'r') as f:
            lines = f.readlines()
        
        peak_start = None
        peak_end = None
        
        for line in lines:
            if today in line:
                if 'Peak period started' in line:
                    peak_start = line.split()[1]
                if 'Peak period ended' in line:
                    peak_end = line.split()[1]
        
        if peak_start and peak_end:
            return f"Peak period: {peak_start} - {peak_end} (completed)"
        elif peak_start:
            return f"Peak period: {peak_start} - ongoing"
        else:
            return "Peak period: Not yet started"
            
    except Exception as e:
        return f"ERROR reading peak summary: {e}"


def get_pricing_summary() -> str:
    """Get dynamic pricing summary if enabled."""
    if not config.DYNAMIC_PRICING_ENABLED:
        return None
    
    try:
        from pricing import get_provider
        provider = get_provider()
        if not provider:
            return "Dynamic pricing provider unavailable."
        
        stats = provider.get_price_stats(hours=24)
        if not stats:
            return "Unable to fetch pricing statistics."
        
        return f"""
DYNAMIC PRICING (24h Summary):
  Provider: {config.PRICING_PROVIDER.upper()}
  Current: {stats.get('current', 'N/A'):.1f} cents/kWh
  Average: {stats.get('avg', 0):.1f} cents/kWh
  Range: {stats.get('min', 0):.1f} - {stats.get('max', 0):.1f} cents/kWh
  Trend: {stats.get('trend_direction', 'unknown')}
  Threshold: {config.PRICE_THRESHOLD_CENTS} cents (charge below this)
"""
    except Exception as e:
        return f"ERROR getting pricing summary: {e}"


def main():
    print("=" * 80)
    print("FRANKLIN BATTERY - DAILY STATUS REPORT")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}")
    print("=" * 80)
    print()

    # Configuration summary (NEW in v3.0)
    print(get_configuration_summary())
    print()

    # Current battery status
    print("CURRENT BATTERY STATUS:")
    print("-" * 80)
    print(get_battery_status())
    print()

    # Today's energy summary
    print("TODAY'S ENERGY SUMMARY:")
    print("-" * 80)
    print(get_todays_energy_summary())
    print()

    # Dynamic pricing summary (if enabled)
    pricing_summary = get_pricing_summary()
    if pricing_summary:
        print("-" * 80)
        print(pricing_summary)
        print()

    # 5-day performance table
    print(get_five_day_performance())
    print()

    # Peak period summary
    print("TODAY'S PEAK PERIOD:")
    print("-" * 80)
    print(get_peak_summary())
    print()

    # Today's mode switches
    print("TODAY'S MODE SWITCHES:")
    print("-" * 80)
    print(get_todays_mode_switches())
    print()

    print("=" * 80)
    print("End of Report")
    print("=" * 80)


if __name__ == "__main__":
    main()
