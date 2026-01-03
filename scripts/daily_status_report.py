#!/volume1/docker/franklin/venv311/bin/python3
"""
Daily Battery Status Report
Sends summary of the day's solar intelligence decisions and current status
"""
import subprocess
from datetime import datetime, timedelta

def get_battery_status():
    """Get current battery status"""
    try:
        result = subprocess.run(
            ['/volume1/docker/franklin/get_battery_status.py'],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.stdout
    except Exception as e:
        return f"ERROR getting battery status: {e}"

def get_todays_intelligence_log():
    """Get today's solar intelligence decisions"""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open('/volume1/docker/franklin/logs/solar_intelligence.log', 'r') as f:
            lines = f.readlines()

        todays_lines = [line for line in lines if line.startswith(today)]

        if todays_lines:
            return ''.join(todays_lines)
        else:
            return "No solar intelligence activity logged today."

    except Exception as e:
        return f"ERROR reading intelligence log: {e}"

def get_todays_energy_summary():
    """Get today's energy flow summary from continuous monitoring"""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        result = subprocess.run(
            ['grep', today, '/volume1/docker/franklin/logs/continuous_monitoring.csv'],
            capture_output=True,
            text=True
        )

        lines = result.stdout.strip().split('\n')
        if not lines or lines[0] == '':
            return "No monitoring data available for today."

        # Parse data to get summary
        soc_values = []
        solar_values = []
        grid_values = []
        battery_values = []

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
            return summary
        else:
            return "No valid monitoring data for today."

    except Exception as e:
        return f"ERROR analyzing energy data: {e}"

def main():
    print("="*70)
    print("FRANKLIN BATTERY - DAILY STATUS REPORT")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}")
    print("="*70)
    print()

    # Current battery status
    print("CURRENT BATTERY STATUS:")
    print("-"*70)
    print(get_battery_status())
    print()

    # Today's energy summary
    print("TODAY'S ENERGY SUMMARY:")
    print("-"*70)
    print(get_todays_energy_summary())
    print()

    # Today's solar intelligence decisions
    print("TODAY'S SOLAR INTELLIGENCE DECISIONS:")
    print("-"*70)
    print(get_todays_intelligence_log())
    print()

    # Peak period readiness
    now = datetime.now()
    peak_start = now.replace(hour=17, minute=0, second=0, microsecond=0)

    if now < peak_start:
        time_to_peak = peak_start - now
        hours = int(time_to_peak.total_seconds() / 3600)
        minutes = int((time_to_peak.total_seconds() % 3600) / 60)
        print(f"PEAK PERIOD STATUS:")
        print("-"*70)
        print(f"Peak period starts in: {hours}h {minutes}m (5:00 PM)")
        print()
    else:
        print(f"PEAK PERIOD STATUS:")
        print("-"*70)
        print("Currently in peak period (5:00 PM - 8:00 PM)")
        print()

    print("="*70)
    print("End of Report")
    print("="*70)

if __name__ == "__main__":
    main()
