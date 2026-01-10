#!/volume1/docker/franklin/venv311/bin/python3
"""
Battery Automation Performance Visualization - Weekly Report
Generates charts showing 7-day automation effectiveness
Run weekly (Sunday morning) to capture previous week's data
"""
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend for Docker/headless systems
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime, timedelta
import re

# File paths
LOG_FILE = "/volume1/docker/franklin/logs/continuous_monitoring.csv"
INTELLIGENCE_LOG = "/volume1/docker/franklin/logs/solar_intelligence.log"
OUTPUT_DIR = "/volume1/docker/franklin/logs"

# Peak period hours
PEAK_START_HOUR = 17  # 5 PM
PEAK_END_HOUR = 20    # 8 PM

def parse_mode_switches():
    """Extract mode switch events from intelligence log"""
    switches = []
    
    with open(INTELLIGENCE_LOG, 'r') as f:
        for line in f:
            if 'Mode changed' in line:
                # Parse: "2026-01-08 12:30:58 - Mode changed: TOU → BACKUP"
                match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - Mode changed: (\w+) → (\w+)', line)
                if match:
                    timestamp_str, from_mode, to_mode = match.groups()
                    timestamp = pd.to_datetime(timestamp_str)
                    switches.append({
                        'timestamp': timestamp,
                        'from_mode': from_mode,
                        'to_mode': to_mode
                    })
    
    return pd.DataFrame(switches)

def load_monitoring_data(days=7):
    """Load last N days of monitoring data"""
    df = pd.read_csv(LOG_FILE)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    
    # Filter to last N days
    cutoff = datetime.now() - timedelta(days=days)
    df = df[df['timestamp'] >= cutoff]
    
    return df

def create_soc_timeline_chart(df, switches):
    """Chart 1: SOC over time with mode switches"""
    fig, ax = plt.subplots(figsize=(16, 7))
    
    # Get unique dates
    df['date'] = df['timestamp'].dt.date
    dates = sorted(df['date'].unique())[-7:]  # Last 7 days
    
    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2']
    
    for i, date in enumerate(dates):
        day_data = df[df['date'] == date].copy()
        day_data['hour'] = day_data['timestamp'].dt.hour + day_data['timestamp'].dt.minute/60
        
        ax.plot(day_data['hour'], day_data['soc_percent'], 
                label=date.strftime('%a %m/%d'), 
                color=colors[i], linewidth=2, alpha=0.8)
        
        # Add mode switch markers for this day
        day_switches = switches[switches['timestamp'].dt.date == date]
        for _, switch in day_switches.iterrows():
            hour = switch['timestamp'].hour + switch['timestamp'].minute/60
            # Find closest SOC value
            closest_idx = (day_data['hour'] - hour).abs().idxmin()
            soc = day_data.loc[closest_idx, 'soc_percent']
            
            if switch['to_mode'] == 'BACKUP':
                marker = 'o'  # Grid charging
                color = 'red'
            else:
                marker = 'o'  # Solar mode
                color = 'green'
            
            ax.plot(hour, soc, marker=marker, markersize=10, 
                   color=color, markeredgecolor='black', markeredgewidth=1.5,
                   zorder=10)
    
    # Shade peak period
    ax.axvspan(PEAK_START_HOUR, PEAK_END_HOUR, alpha=0.2, color='red', 
               label='Peak Period (5-8 PM)')
    
    # Formatting
    ax.set_xlabel('Hour of Day', fontsize=12, fontweight='bold')
    ax.set_ylabel('Battery SOC (%)', fontsize=12, fontweight='bold')
    ax.set_title('Battery State of Charge - 7 Day History\nwith Mode Switches', 
                 fontsize=14, fontweight='bold', pad=20)
    ax.grid(True, alpha=0.3)
    ax.legend(loc='best', fontsize=10, ncol=2)
    ax.set_xlim(0, 24)
    ax.set_ylim(0, 105)
    
    # Add reference lines
    ax.axhline(y=95, color='green', linestyle='--', alpha=0.5, linewidth=1, label='Target SOC')
    ax.axhline(y=20, color='orange', linestyle='--', alpha=0.5, linewidth=1, label='Low SOC')
    
    # X-axis formatting
    ax.set_xticks(range(0, 25, 3))
    ax.set_xticklabels([f'{h:02d}:00' for h in range(0, 25, 3)])
    
    plt.tight_layout()
    return fig

def create_daily_summary_chart(df, switches):
    """Chart 2: Daily summary bars showing charge sources"""
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 8))
    
    # Get last 7 days
    df['date'] = df['timestamp'].dt.date
    dates = sorted(df['date'].unique())[-7:]
    
    summaries = []
    for date in dates:
        day_data = df[df['date'] == date]
        day_switches = switches[switches['timestamp'].dt.date == date]
        
        # Count mode switch events
        to_backup = len(day_switches[day_switches['to_mode'] == 'BACKUP'])
        to_tou = len(day_switches[day_switches['to_mode'] == 'TOU'])
        
        # Calculate energy metrics
        avg_solar = day_data['solar_kw'].mean()
        peak_solar = day_data['solar_kw'].max()
        avg_grid = day_data['grid_kw'].mean()
        soc_start = day_data.iloc[0]['soc_percent']
        soc_end = day_data.iloc[-1]['soc_percent']
        soc_min = day_data['soc_percent'].min()
        soc_max = day_data['soc_percent'].max()
        
        summaries.append({
            'date': date,
            'mode_switches': to_backup + to_tou,
            'grid_charges': to_backup,
            'avg_solar': avg_solar,
            'peak_solar': peak_solar,
            'avg_grid': avg_grid,
            'soc_start': soc_start,
            'soc_end': soc_end,
            'soc_min': soc_min,
            'soc_max': soc_max,
            'soc_swing': soc_max - soc_min
        })
    
    summary_df = pd.DataFrame(summaries)
    
    # Chart 1: SOC Range per day
    x = range(len(dates))
    labels = [d.strftime('%a\n%m/%d') for d in dates]
    
    ax1.bar(x, summary_df['soc_max'], color='lightblue', alpha=0.6, label='SOC Range')
    ax1.bar(x, summary_df['soc_min'], color='white', edgecolor='none')
    
    # Add start/end markers
    ax1.plot(x, summary_df['soc_start'], 'go', markersize=8, label='Start of Day', zorder=10)
    ax1.plot(x, summary_df['soc_end'], 'ro', markersize=8, label='End of Day', zorder=10)
    
    ax1.axhline(y=95, color='green', linestyle='--', alpha=0.5, linewidth=2, label='Target')
    ax1.set_ylabel('Battery SOC (%)', fontsize=11, fontweight='bold')
    ax1.set_title('Daily SOC Range and Endpoints', fontsize=12, fontweight='bold')
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels)
    ax1.legend(loc='best')
    ax1.grid(True, alpha=0.3, axis='y')
    ax1.set_ylim(0, 105)
    
    # Chart 2: Solar production and grid charges
    width = 0.35
    x_solar = [i - width/2 for i in x]
    x_grid = [i + width/2 for i in x]
    
    ax2.bar(x_solar, summary_df['peak_solar'], width, label='Peak Solar (kW)', 
            color='gold', alpha=0.8)
    ax2.bar(x_grid, summary_df['grid_charges'], width, label='Grid Charge Events', 
            color='red', alpha=0.8)
    
    ax2.set_ylabel('kW / Count', fontsize=11, fontweight='bold')
    ax2.set_xlabel('Date', fontsize=11, fontweight='bold')
    ax2.set_title('Daily Solar Production vs Grid Charge Events', fontsize=12, fontweight='bold')
    ax2.set_xticks(x)
    ax2.set_xticklabels(labels)
    ax2.legend(loc='best')
    ax2.grid(True, alpha=0.3, axis='y')
    
    plt.tight_layout()
    return fig

def create_power_flow_chart(df):
    """Chart 3: Power flow over time (last 48 hours for detail)"""
    # Get last 48 hours for detailed view
    cutoff = datetime.now() - timedelta(hours=48)
    recent = df[df['timestamp'] >= cutoff].copy()
    
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 8), sharex=True)
    
    # Chart 1: Power flows
    ax1.plot(recent['timestamp'], recent['solar_kw'], 
             label='Solar Production', color='gold', linewidth=2)
    ax1.plot(recent['timestamp'], recent['grid_kw'], 
             label='Grid Use', color='red', linewidth=2, alpha=0.7)
    ax1.plot(recent['timestamp'], recent['home_load_kw'], 
             label='Home Load', color='blue', linewidth=2, alpha=0.7)
    
    ax1.set_ylabel('Power (kW)', fontsize=11, fontweight='bold')
    ax1.set_title('48-Hour Power Flow', fontsize=12, fontweight='bold')
    ax1.legend(loc='best')
    ax1.grid(True, alpha=0.3)
    
    # Shade peak periods (last 2 days)
    now = datetime.now()
    for i in range(2):
        day = now - timedelta(days=i)
        peak_start = day.replace(hour=PEAK_START_HOUR, minute=0, second=0, microsecond=0)
        peak_end = day.replace(hour=PEAK_END_HOUR, minute=0, second=0, microsecond=0)
        ax1.axvspan(peak_start, peak_end, alpha=0.15, color='red')
    
    # Chart 2: Battery SOC and activity
    ax2.plot(recent['timestamp'], recent['soc_percent'], 
             label='Battery SOC', color='green', linewidth=2)
    ax2_twin = ax2.twinx()
    ax2_twin.plot(recent['timestamp'], recent['battery_kw'], 
                  label='Battery Power', color='purple', linewidth=2, alpha=0.6)
    
    ax2.set_ylabel('SOC (%)', fontsize=11, fontweight='bold', color='green')
    ax2_twin.set_ylabel('Battery Power (kW)', fontsize=11, fontweight='bold', color='purple')
    ax2.set_xlabel('Time', fontsize=11, fontweight='bold')
    ax2.set_title('Battery State and Activity', fontsize=12, fontweight='bold')
    ax2.grid(True, alpha=0.3)
    ax2.axhline(y=95, color='green', linestyle='--', alpha=0.5)
    
    # Format x-axis
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
    ax2.xaxis.set_major_locator(mdates.HourLocator(interval=6))
    plt.setp(ax2.xaxis.get_majorticklabels(), rotation=45, ha='right')
    
    # Combine legends
    lines1, labels1 = ax2.get_legend_handles_labels()
    lines2, labels2 = ax2_twin.get_legend_handles_labels()
    ax2.legend(lines1 + lines2, labels1 + labels2, loc='best')
    
    plt.tight_layout()
    return fig

def main():
    print("="*70)
    print("FRANKLIN BATTERY - WEEKLY PERFORMANCE CHARTS")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}")
    print("="*70)
    print()
    
    # Generate date prefix for filenames (MM-DD-YYYY format)
    date_prefix = datetime.now().strftime('%m-%d-%Y')
    
    print("Loading data...")
    df = load_monitoring_data(days=7)
    switches = parse_mode_switches()
    
    print(f"Loaded {len(df)} monitoring records")
    print(f"Found {len(switches)} mode switches")
    
    if len(df) == 0:
        print("\n✗ No data found! Check log files.")
        return 1
    
    print("\nGenerating Chart 1: SOC Timeline (7 days)...")
    fig1 = create_soc_timeline_chart(df, switches)
    filename1 = f'{OUTPUT_DIR}/{date_prefix}_chart_soc_timeline.png'
    fig1.savefig(filename1, dpi=150, bbox_inches='tight')
    print(f"  ✓ Saved: {filename1}")
    
    print("Generating Chart 2: Daily Summary (7 days)...")
    fig2 = create_daily_summary_chart(df, switches)
    filename2 = f'{OUTPUT_DIR}/{date_prefix}_chart_daily_summary.png'
    fig2.savefig(filename2, dpi=150, bbox_inches='tight')
    print(f"  ✓ Saved: {filename2}")
    
    print("Generating Chart 3: Power Flow (48 hours)...")
    fig3 = create_power_flow_chart(df)
    filename3 = f'{OUTPUT_DIR}/{date_prefix}_chart_power_flow.png'
    fig3.savefig(filename3, dpi=150, bbox_inches='tight')
    print(f"  ✓ Saved: {filename3}")
    
    print("\n" + "="*70)
    print("✓ All charts generated successfully!")
    print(f"\nView charts at: {OUTPUT_DIR}/{date_prefix}_chart_*.png")
    print("="*70)
    
    plt.close('all')
    return 0

if __name__ == "__main__":
    exit(main())
