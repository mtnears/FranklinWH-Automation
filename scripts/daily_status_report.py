#!/volume1/docker/franklin/venv311/bin/python3
"""
Daily Battery Status Report
Sends summary of the day's solar intelligence decisions and current status
Runs at 10:15 PM after peak period ends
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

def get_five_day_performance():
    """Get rolling 5-day performance table"""
    try:
        with open('/volume1/docker/franklin/logs/solar_intelligence.log', 'r') as f:
            lines = f.readlines()
        
        # Get last 5 days including today
        days = []
        for i in range(4, -1, -1):  # 4, 3, 2, 1, 0 (today is 0)
            date = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
            days.append(date)
        
        performance = []
        
        for date in days:
            day_data = {
                'date': date,
                'grid_charge_times': [],
                'soc_445pm': 'N/A',
                'mode_switches': 0,
                'peak_protection': 'OK'
            }
            
            # Get mode switches (grid charge start times and count)
            for line in lines:
                if date in line:
                    if 'Mode changed: TOU → BACKUP' in line:
                        time = line.split()[1]  # Extract HH:MM:SS
                        day_data['grid_charge_times'].append(time[:5])  # HH:MM only
                    if 'Mode changed' in line:
                        day_data['mode_switches'] += 1
            
            # Get SOC at 4:45 PM
            target_time = f"{date} 16:45"
            for line in lines:
                if target_time in line and 'SOC:' in line:
                    # Extract SOC value from line like "SOC: 97.4%, Solar: ..."
                    try:
                        soc_part = line.split('SOC:')[1].split('%')[0].strip()
                        day_data['soc_445pm'] = f"{soc_part}%"
                    except:
                        pass
            
            # Check for peak violations (mode changes during 5-8 PM)
            peak_violations = [l for l in lines if date in l and 
                             any(f"{date} {h}:" in l for h in ['17:', '18:', '19:']) and
                             'SWITCHING' in l]
            if peak_violations:
                day_data['peak_protection'] = 'FAIL'
            
            performance.append(day_data)
        
        # Build table (newest first)
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
            
            # Determine notes based on switches and charges
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
            
            # Format row with padding
            row = f"{date_str} | {charges:17} | {soc:10} | {switches:8} | {peak:4} | {notes}"
            table += f"\n{row}"
        
        table += "\n--------------------------------------------------------------------------------"
        return table
        
    except Exception as e:
        return f"ERROR generating 5-day performance table: {e}"

def get_todays_mode_switches():
    """Get today's mode switches only (not full decision log)"""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open('/volume1/docker/franklin/logs/solar_intelligence.log', 'r') as f:
            lines = f.readlines()

        # Filter for today's mode switch events
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

def get_peak_summary():
    """Get peak period summary for today"""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open('/volume1/docker/franklin/logs/solar_intelligence.log', 'r') as f:
            lines = f.readlines()
        
        peak_start = None
        peak_end = None
        
        for line in lines:
            if today in line:
                if 'Peak period started' in line:
                    peak_start = line.split()[1]  # Get time
                if 'Peak period ended' in line:
                    peak_end = line.split()[1]  # Get time
        
        if peak_start and peak_end:
            return f"Peak period: {peak_start} - {peak_end} (completed)"
        elif peak_start:
            return f"Peak period: {peak_start} - ongoing"
        else:
            return "Peak period: Not yet started"
            
    except Exception as e:
        return f"ERROR reading peak summary: {e}"

def main():
    print("="*80)
    print("FRANKLIN BATTERY - DAILY STATUS REPORT")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}")
    print("="*80)
    print()

    # Current battery status
    print("CURRENT BATTERY STATUS:")
    print("-"*80)
    print(get_battery_status())
    print()

    # Today's energy summary
    print("TODAY'S ENERGY SUMMARY:")
    print("-"*80)
    print(get_todays_energy_summary())
    print()

    # 5-day performance table
    print(get_five_day_performance())
    print()

    # Peak period summary
    print("TODAY'S PEAK PERIOD:")
    print("-"*80)
    print(get_peak_summary())
    print()

    # Today's mode switches (not full log)
    print("TODAY'S MODE SWITCHES:")
    print("-"*80)
    print(get_todays_mode_switches())
    print()

    print("="*80)
    print("End of Report")
    print("="*80)

if __name__ == "__main__":
    main()
