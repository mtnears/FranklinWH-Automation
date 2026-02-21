#!/usr/bin/env python3
"""
Battery Automation Performance Visualization - Weekly Report (v4.0 Enhanced)

Generates charts showing 7-day automation effectiveness:
1. SOC Timeline: Battery state of charge with v4-aware mode switch markers
2. Daily Summary: SOC ranges, solar production, grid charge events, costs
3. Power Flow: 48-hour detailed power flow and battery activity
4. Decision Engine Activity: Priority phase timeline with gap/forecast data (v4 only)
5. Solar Curtailment Tracker: Wasted solar and utilization metrics (v4 only)

Backward-compatible: v3.5 log entries are parsed with their format, v4 with theirs.
Transition weeks (mixed v3.5/v4 data) are handled gracefully.

Run weekly (e.g., Sunday morning) to capture previous week's data.
Charts are saved with date prefixes for historical tracking.
"""
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.dates as mdates
from matplotlib.lines import Line2D
from datetime import datetime, timedelta
import re
import os
import json
import sys

# Import configuration
try:
    from config import config
    LOG_FILE = config.LOG_FILE
    INTELLIGENCE_LOG = config.INTELLIGENCE_LOG
    OUTPUT_DIR = config.LOG_DIR
    WEB_DIR = config.WEB_DIR
    PEAK_START_HOUR = config.PEAK_START_HOUR
    PEAK_END_HOUR = config.PEAK_END_HOUR
except ImportError:
    # Fallback defaults
    LOG_FILE = "/app/logs/continuous_monitoring.csv"
    INTELLIGENCE_LOG = "/app/logs/solar_intelligence.log"
    OUTPUT_DIR = "/app/logs"
    WEB_DIR = "/app/web"
    PEAK_START_HOUR = 17
    PEAK_END_HOUR = 20

# ─── Color palette ───────────────────────────────────────────────────────────
COLORS = {
    'solar': '#FFB300',        # Amber
    'grid': '#E53935',         # Red
    'home_load': '#1E88E5',    # Blue
    'battery_charge': '#43A047',  # Green
    'battery_discharge': '#F4511E', # Deep orange
    'soc': '#2E7D32',          # Dark green
    'peak_shade': '#E53935',   # Red (transparent)
    'curtailment': '#FF6F00',  # Orange
    # Priority phase colors
    'P1': '#B71C1C',   # Override/emergency - dark red
    'P2': '#D32F2F',   # Manual override - red
    'P3': '#F57C00',   # Grid disconnect - orange
    'P4': '#E53935',   # Peak protection - red
    'P5': '#FFB300',   # Curtailment/near-full - amber
    'P6': '#66BB6A',   # Solar charging - green
    'P7': '#1E88E5',   # Gap charging - blue
    'P8': '#78909C',   # No action - grey
    # Mode switch markers
    'to_backup': '#D32F2F',       # Red triangle up
    'to_self_consumption': '#2E7D32', # Green triangle down
    'to_tou': '#1565C0',          # Blue square
    'override': '#9C27B0',        # Purple diamond
    # Day line colors for SOC timeline
    'days': ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2'],
}


# ─── Log parsing ─────────────────────────────────────────────────────────────

def parse_intelligence_log(days=7):
    """Parse solar_intelligence.log for decisions, mode switches, engine metrics.
    
    Returns a dict with:
        - decisions: DataFrame of timestamp, version, priority, reason, action
        - switches: DataFrame of timestamp, from_mode, to_mode, version
        - metrics: DataFrame of engine metrics (gap_kwh, curtailed_kw, etc.)
    """
    cutoff = datetime.now() - timedelta(days=days)
    
    decisions = []
    switches = []
    metrics = []
    
    # Track engine version per entry
    current_version = 'v3'  # default
    
    try:
        with open(str(INTELLIGENCE_LOG), 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                
                # Parse timestamp
                ts_match = re.match(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - (.*)', line)
                if not ts_match:
                    continue
                    
                try:
                    timestamp = pd.to_datetime(ts_match.group(1))
                except:
                    continue
                    
                if timestamp < cutoff:
                    continue
                    
                content = ts_match.group(2)
                
                # Detect engine version
                if 'Smart Decision Engine v4' in content:
                    current_version = 'v4'
                elif 'Smart Decision Engine v3' in content:
                    current_version = 'v3'
                
                # ── Parse decisions ──
                if content.startswith('Decision:'):
                    decision_text = content[len('Decision:'):].strip()
                    
                    if current_version == 'v4':
                        # v4 format: [v4 P7] Charging gap: ...
                        p_match = re.match(r'\[v4 (P\d+)\] (.*)', decision_text)
                        if p_match:
                            priority = p_match.group(1)
                            reason = p_match.group(2)
                        else:
                            priority = 'unknown'
                            reason = decision_text
                    else:
                        # v3 format: freeform text, map to approximate priority
                        priority = _classify_v3_decision(decision_text)
                        reason = decision_text
                    
                    decisions.append({
                        'timestamp': timestamp,
                        'version': current_version,
                        'priority': priority,
                        'reason': reason,
                    })
                
                # ── Parse mode switches ──
                elif 'Mode changed:' in content:
                    # Both v3 and v4 formats:
                    # v3: "Mode changed: BACKUP -> TOU" or "BACKUP → TOU"
                    # v4: "Mode changed: self_consumption -> emergency_backup"
                    sw_match = re.match(
                        r'Mode changed:\s*(\S+)\s*(?:->|→)\s*(\S+)', content)
                    if sw_match:
                        from_mode = _normalize_mode(sw_match.group(1))
                        to_mode = _normalize_mode(sw_match.group(2))
                        switches.append({
                            'timestamp': timestamp,
                            'from_mode': from_mode,
                            'to_mode': to_mode,
                            'version': current_version,
                        })
                
                # ── Parse engine metrics (v4 only) ──
                elif content.startswith('Engine metrics:'):
                    metrics_text = content[len('Engine metrics:'):].strip()
                    m = {}
                    m['timestamp'] = timestamp
                    for kv in metrics_text.split(', '):
                        parts = kv.split('=')
                        if len(parts) == 2:
                            try:
                                m[parts[0].strip()] = float(parts[1].strip())
                            except ValueError:
                                pass
                    if len(m) > 1:  # has at least timestamp + one metric
                        metrics.append(m)
    
    except Exception as e:
        print(f"Error parsing intelligence log: {e}")
    
    return {
        'decisions': pd.DataFrame(decisions) if decisions else pd.DataFrame(),
        'switches': pd.DataFrame(switches) if switches else pd.DataFrame(),
        'metrics': pd.DataFrame(metrics) if metrics else pd.DataFrame(),
    }


def _classify_v3_decision(text):
    """Map v3.5 freeform decision text to approximate priority labels."""
    t = text.lower()
    if 'peak period' in t:
        return 'P4'
    elif 'battery full' in t:
        return 'P5'
    elif 'solar available' in t or 'wait for solar' in t or 'time available' in t:
        return 'P8'
    elif 'charge now' in t or 'balanced strategy' in t or 'urgent' in t:
        return 'P7'
    elif 'override' in t:
        return 'P2'
    else:
        return 'P8'


def _normalize_mode(mode_str):
    """Normalize mode names across v3 and v4 log formats."""
    m = mode_str.upper().strip()
    mapping = {
        'BACKUP': 'emergency_backup',
        'EMERGENCY_BACKUP': 'emergency_backup',
        'TOU': 'time_of_use',
        'TIME_OF_USE': 'time_of_use',
        'TOU-B': 'time_of_use',
        'SELF_CONSUMPTION': 'self_consumption',
        'HOME': 'self_consumption',
        'NONE': 'unknown',
        'UNKNOWN': 'unknown',
    }
    return mapping.get(m, mode_str.lower())


def load_monitoring_data(days=7):
    """Load last N days of monitoring data.
    
    Handles variable column counts across versions (v3.2 had 13 columns,
    v3.4+ added run_status, mode_name, per-battery SOC, temp, etc.)
    by padding shorter rows to match the widest row.
    """
    try:
        import csv
        
        rows = []
        max_cols = 0
        with open(str(LOG_FILE), 'r') as f:
            reader = csv.reader(f)
            header = next(reader)
            max_cols = len(header)
            for row in reader:
                if len(row) > max_cols:
                    max_cols = len(row)
                rows.append(row)
        
        # Pad all rows to max width
        for row in rows:
            while len(row) < max_cols:
                row.append(None)
        
        # Build column names
        col_names = list(header)
        while len(col_names) < max_cols:
            col_names.append(f'extra_{len(col_names)}')
        
        df = pd.DataFrame(rows, columns=col_names)
        
        df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
        df = df.dropna(subset=['timestamp'])
        
        # Ensure numeric columns are numeric
        for col in ['soc_percent', 'solar_kw', 'grid_kw', 'battery_kw', 'home_load_kw']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        cutoff = datetime.now() - timedelta(days=days)
        df = df[df['timestamp'] >= cutoff]
        
        return df
    except Exception as e:
        print(f"Error loading monitoring data: {e}")
        return pd.DataFrame()


# ─── Chart 1: SOC Timeline with v4-aware mode switch markers ────────────────

def create_soc_timeline_chart(df, intel):
    """SOC over time with differentiated mode switch markers.
    
    v4 switches show:
      ▲ red    = → emergency_backup (grid charge burst)
      ▼ green  = → self_consumption (charge complete / solar-first)
      ■ blue   = → time_of_use (legacy/fallback)
      ◆ purple = override / unknown
    
    v3 switches show the same logic but with lighter alpha to distinguish eras.
    """
    switches = intel['switches']
    fig, ax = plt.subplots(figsize=(16, 7))
    
    df['date'] = df['timestamp'].dt.date
    dates = sorted(df['date'].unique())[-7:]
    
    for i, date in enumerate(dates):
        day_data = df[df['date'] == date].copy()
        day_data['hour'] = day_data['timestamp'].dt.hour + day_data['timestamp'].dt.minute / 60
        
        ax.plot(day_data['hour'], day_data['soc_percent'],
                label=date.strftime('%a %m/%d'),
                color=COLORS['days'][i % len(COLORS['days'])],
                linewidth=2, alpha=0.8)
        
        # Mode switch markers for this day
        if not switches.empty:
            day_switches = switches[switches['timestamp'].dt.date == date]
            for _, sw in day_switches.iterrows():
                hour = sw['timestamp'].hour + sw['timestamp'].minute / 60
                # Find closest SOC value
                if len(day_data) == 0:
                    continue
                closest_idx = (day_data['hour'] - hour).abs().idxmin()
                soc = day_data.loc[closest_idx, 'soc_percent']
                
                is_v4 = sw['version'] == 'v4'
                alpha = 1.0 if is_v4 else 0.5
                size = 11 if is_v4 else 8
                
                marker, color = _get_mode_switch_marker(sw['to_mode'])
                ax.plot(hour, soc, marker=marker, markersize=size,
                        color=color, markeredgecolor='black',
                        markeredgewidth=1.0, alpha=alpha, zorder=10)
    
    # Shade peak periods
    peaks = _get_peak_periods()
    for i, (pk_start, pk_end) in enumerate(peaks):
        label = f'Peak ({pk_start}:00-{pk_end}:00)' if i == 0 else f'Peak ({pk_start}:00-{pk_end}:00)'
        ax.axvspan(pk_start, pk_end, alpha=0.15, color=COLORS['peak_shade'],
                   label=label)
    
    # Reference lines
    ax.axhline(y=95, color='green', linestyle='--', alpha=0.4, linewidth=1)
    ax.axhline(y=20, color='orange', linestyle='--', alpha=0.4, linewidth=1)
    
    ax.set_xlabel('Hour of Day', fontsize=12, fontweight='bold')
    ax.set_ylabel('Battery SOC (%)', fontsize=12, fontweight='bold')
    ax.set_title('Battery State of Charge — 7 Day History\nwith Decision Engine Mode Switches',
                 fontsize=14, fontweight='bold', pad=20)
    ax.grid(True, alpha=0.3)
    ax.set_xlim(0, 24)
    ax.set_ylim(0, 105)
    ax.set_xticks(range(0, 25, 3))
    ax.set_xticklabels([f'{h:02d}:00' for h in range(0, 25, 3)])
    
    # Build legend: day lines + mode switch markers
    handles, labels = ax.get_legend_handles_labels()
    
    # Add mode switch legend entries
    switch_legend = [
        Line2D([0], [0], marker='^', color='w', markerfacecolor=COLORS['to_backup'],
               markeredgecolor='black', markersize=10, label='→ Backup (grid charge)'),
        Line2D([0], [0], marker='v', color='w', markerfacecolor=COLORS['to_self_consumption'],
               markeredgecolor='black', markersize=10, label='→ Self-Consumption'),
        Line2D([0], [0], marker='s', color='w', markerfacecolor=COLORS['to_tou'],
               markeredgecolor='black', markersize=10, label='→ Time-of-Use'),
    ]
    handles.extend(switch_legend)
    
    # Place legend below the chart so it never overlaps data
    ax.legend(handles=handles, loc='upper center', bbox_to_anchor=(0.5, -0.12),
              fontsize=9, ncol=5, framealpha=0.9, columnspacing=1.5)
    
    plt.tight_layout()
    fig.subplots_adjust(bottom=0.18)
    return fig


def _get_mode_switch_marker(to_mode):
    """Return (marker, color) for a mode switch target."""
    if to_mode == 'emergency_backup':
        return '^', COLORS['to_backup']           # ▲ red
    elif to_mode == 'self_consumption':
        return 'v', COLORS['to_self_consumption']  # ▼ green
    elif to_mode == 'time_of_use':
        return 's', COLORS['to_tou']               # ■ blue
    else:
        return 'D', COLORS['override']             # ◆ purple


# ─── Chart 2: Daily Summary (enhanced) ──────────────────────────────────────

def create_daily_summary_chart(df, intel):
    """Daily summary with SOC range, solar production, grid charges, and cost estimate."""
    switches = intel['switches']
    metrics = intel['metrics']
    
    fig, axes = plt.subplots(3, 1, figsize=(16, 10), 
                             gridspec_kw={'height_ratios': [3, 2, 2]})
    ax_soc, ax_solar, ax_energy = axes
    
    df['date'] = df['timestamp'].dt.date
    dates = sorted(df['date'].unique())[-7:]
    
    summaries = []
    for date in dates:
        day_data = df[df['date'] == date]
        
        # Count mode switches by type
        if switches.empty:
            day_sw = pd.DataFrame()
        else:
            day_sw = switches[switches['timestamp'].dt.date == date]
        
        to_backup = len(day_sw[day_sw['to_mode'] == 'emergency_backup']) if not day_sw.empty else 0
        to_sc = len(day_sw[day_sw['to_mode'] == 'self_consumption']) if not day_sw.empty else 0
        to_tou = len(day_sw[day_sw['to_mode'] == 'time_of_use']) if not day_sw.empty else 0
        
        # Get curtailment from engine metrics
        daily_curtailment = 0
        if not metrics.empty and 'curtailed_kw' in metrics.columns:
            day_metrics = metrics[metrics['timestamp'].dt.date == date]
            if not day_metrics.empty and 'curtailed_kw' in day_metrics.columns:
                # Each reading is ~30min apart, so curtailed_kw * 0.5h ≈ curtailed_kwh per interval
                daily_curtailment = day_metrics['curtailed_kw'].sum() * 0.5
        
        # Estimate grid energy consumed (rough: grid_kw * interval hours)
        # Positive grid_kw = importing from grid
        grid_import = day_data[day_data['grid_kw'] > 0]['grid_kw']
        # ~5 min intervals in monitoring data → each reading ≈ 5/60 hours
        interval_hours = 5 / 60  # approximate
        grid_kwh = grid_import.sum() * interval_hours if len(grid_import) > 0 else 0
        
        # Solar energy produced
        solar_kwh = day_data['solar_kw'].sum() * interval_hours if 'solar_kw' in day_data.columns else 0
        
        summaries.append({
            'date': date,
            'soc_start': day_data.iloc[0]['soc_percent'] if len(day_data) > 0 else 0,
            'soc_end': day_data.iloc[-1]['soc_percent'] if len(day_data) > 0 else 0,
            'soc_min': day_data['soc_percent'].min(),
            'soc_max': day_data['soc_percent'].max(),
            'avg_solar': day_data['solar_kw'].mean(),
            'peak_solar': day_data['solar_kw'].max(),
            'grid_charge_events': to_backup,
            'mode_switches_total': to_backup + to_sc + to_tou,
            'curtailed_kwh': daily_curtailment,
            'solar_kwh': solar_kwh,
            'grid_kwh': grid_kwh,
        })
    
    sdf = pd.DataFrame(summaries)
    x = range(len(dates))
    labels = [d.strftime('%a\n%m/%d') for d in dates]
    
    # ── Panel 1: SOC Range ──
    # Draw bars for SOC range (min to max)
    for i in range(len(sdf)):
        ax_soc.bar(i, sdf.iloc[i]['soc_max'] - sdf.iloc[i]['soc_min'],
                    bottom=sdf.iloc[i]['soc_min'],
                    color='lightblue', alpha=0.6, edgecolor='steelblue', linewidth=0.5)
    
    ax_soc.plot(x, sdf['soc_start'], 'o', color='#2E7D32', markersize=9,
                markeredgecolor='black', markeredgewidth=0.5, label='Start of Day', zorder=10)
    ax_soc.plot(x, sdf['soc_end'], 'o', color='#E53935', markersize=9,
                markeredgecolor='black', markeredgewidth=0.5, label='End of Day', zorder=10)
    ax_soc.axhline(y=95, color='green', linestyle='--', alpha=0.5, linewidth=1.5, label='Target (95%)')
    
    # Annotate grid charge event counts above each bar
    for i, row in sdf.iterrows():
        if row['grid_charge_events'] > 0:
            ax_soc.annotate(f"{int(row['grid_charge_events'])} chg",
                           (i, row['soc_max'] + 2),
                           ha='center', fontsize=8, color=COLORS['to_backup'], fontweight='bold')
    
    ax_soc.set_ylabel('Battery SOC (%)', fontsize=11, fontweight='bold')
    ax_soc.set_title('Daily SOC Range, Endpoints & Grid Charge Events', fontsize=12, fontweight='bold')
    ax_soc.set_xticks(x)
    ax_soc.set_xticklabels(labels)
    ax_soc.legend(loc='lower right', fontsize=9)
    ax_soc.grid(True, alpha=0.3, axis='y')
    ax_soc.set_ylim(0, 110)
    
    # ── Panel 2: Solar production ──
    ax_solar.bar(x, sdf['peak_solar'], color=COLORS['solar'], alpha=0.8,
                 label='Peak Solar (kW)', edgecolor='#E65100', linewidth=0.5)
    
    # Overlay curtailment as hatched section on top
    if sdf['curtailed_kwh'].sum() > 0:
        ax_solar.bar(x, sdf['curtailed_kwh'], bottom=sdf['peak_solar'],
                     color=COLORS['curtailment'], alpha=0.6, hatch='///',
                     edgecolor=COLORS['curtailment'], linewidth=0.5,
                     label='Est. Curtailed (kWh)')
    
    ax_solar.set_ylabel('kW / kWh', fontsize=11, fontweight='bold')
    ax_solar.set_title('Daily Solar Production & Curtailment', fontsize=12, fontweight='bold')
    ax_solar.set_xticks(x)
    ax_solar.set_xticklabels(labels)
    ax_solar.legend(loc='best', fontsize=9)
    ax_solar.grid(True, alpha=0.3, axis='y')
    
    # ── Panel 3: Energy balance ──
    width = 0.35
    x_solar_bar = [i - width / 2 for i in x]
    x_grid_bar = [i + width / 2 for i in x]
    
    ax_energy.bar(x_solar_bar, sdf['solar_kwh'], width, label='Solar Energy (kWh)',
                  color=COLORS['solar'], alpha=0.8)
    ax_energy.bar(x_grid_bar, sdf['grid_kwh'], width, label='Grid Import (kWh)',
                  color=COLORS['grid'], alpha=0.7)
    
    ax_energy.set_ylabel('Energy (kWh)', fontsize=11, fontweight='bold')
    ax_energy.set_xlabel('Date', fontsize=11, fontweight='bold')
    ax_energy.set_title('Daily Energy Balance: Solar vs Grid Import', fontsize=12, fontweight='bold')
    ax_energy.set_xticks(x)
    ax_energy.set_xticklabels(labels)
    ax_energy.legend(loc='best', fontsize=9)
    ax_energy.grid(True, alpha=0.3, axis='y')
    
    plt.tight_layout()
    return fig


# ─── Chart 3: Power Flow (48h, improved battery visibility) ─────────────────

def create_power_flow_chart(df):
    """48-hour power flow with improved battery charge/discharge visualization."""
    cutoff = datetime.now() - timedelta(hours=48)
    recent = df[df['timestamp'] >= cutoff].copy()
    
    if recent.empty:
        return None
    
    fig, (ax_power, ax_batt) = plt.subplots(2, 1, figsize=(16, 9), sharex=True,
                                             gridspec_kw={'height_ratios': [3, 2]})
    
    # ── Top panel: Power flow ──
    ax_power.fill_between(recent['timestamp'], 0, recent['solar_kw'],
                          alpha=0.3, color=COLORS['solar'], label='Solar')
    ax_power.plot(recent['timestamp'], recent['solar_kw'],
                  color=COLORS['solar'], linewidth=1.5)
    ax_power.plot(recent['timestamp'], recent['grid_kw'],
                  color=COLORS['grid'], linewidth=1.5, alpha=0.8, label='Grid')
    ax_power.plot(recent['timestamp'], recent['home_load_kw'],
                  color=COLORS['home_load'], linewidth=1.5, alpha=0.8, label='Home Load')
    
    ax_power.set_ylabel('Power (kW)', fontsize=11, fontweight='bold')
    ax_power.set_title('48-Hour Power Flow', fontsize=13, fontweight='bold')
    ax_power.legend(loc='upper left', fontsize=10)
    ax_power.grid(True, alpha=0.3)
    
    # Shade peak periods
    _shade_peak_periods(ax_power, recent['timestamp'].min(), recent['timestamp'].max())
    
    # ── Bottom panel: Battery SOC + charge/discharge as filled area ──
    ax_batt.plot(recent['timestamp'], recent['soc_percent'],
                 color=COLORS['soc'], linewidth=2, label='SOC (%)')
    ax_batt.axhline(y=95, color='green', linestyle='--', alpha=0.4, linewidth=1)
    ax_batt.set_ylabel('SOC (%)', fontsize=11, fontweight='bold', color=COLORS['soc'])
    ax_batt.set_ylim(0, 105)
    
    # Battery power on twin axis with filled charge/discharge areas
    ax_bat_pwr = ax_batt.twinx()
    
    if 'battery_kw' in recent.columns:
        bat = recent['battery_kw'].fillna(0)
        # Positive = charging, Negative = discharging (convention may vary, adjust if needed)
        ax_bat_pwr.fill_between(recent['timestamp'], 0, bat,
                                where=(bat >= 0), alpha=0.25,
                                color=COLORS['battery_charge'], label='Charging')
        ax_bat_pwr.fill_between(recent['timestamp'], 0, bat,
                                where=(bat < 0), alpha=0.25,
                                color=COLORS['battery_discharge'], label='Discharging')
        ax_bat_pwr.plot(recent['timestamp'], bat,
                        color='purple', linewidth=0.8, alpha=0.5)
    
    ax_bat_pwr.set_ylabel('Battery Power (kW)', fontsize=11, fontweight='bold', color='purple')
    
    _shade_peak_periods(ax_batt, recent['timestamp'].min(), recent['timestamp'].max())
    
    # Combined legend
    lines1, labels1 = ax_batt.get_legend_handles_labels()
    lines2, labels2 = ax_bat_pwr.get_legend_handles_labels()
    ax_batt.legend(lines1 + lines2, labels1 + labels2, loc='upper left', fontsize=9)
    
    ax_batt.set_xlabel('Time', fontsize=11, fontweight='bold')
    ax_batt.set_title('Battery State & Activity', fontsize=12, fontweight='bold')
    ax_batt.grid(True, alpha=0.3)
    
    ax_batt.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
    ax_batt.xaxis.set_major_locator(mdates.HourLocator(interval=6))
    plt.setp(ax_batt.xaxis.get_majorticklabels(), rotation=45, ha='right')
    
    plt.tight_layout()
    return fig


def _get_peak_periods():
    """Get list of peak period tuples [(start_hour, end_hour), ...].
    
    Supports config.PEAK_PERIODS (list of dicts with start/end) if available,
    otherwise falls back to single PEAK_START_HOUR/PEAK_END_HOUR.
    """
    try:
        from config import config
        if hasattr(config, 'PEAK_PERIODS') and config.PEAK_PERIODS:
            # config.PEAK_PERIODS expected as list of dicts:
            # [{"start": 16, "end": 21}, {"start": 12, "end": 14}]
            return [(p['start'], p['end']) for p in config.PEAK_PERIODS]
    except (ImportError, AttributeError):
        pass
    
    return [(PEAK_START_HOUR, PEAK_END_HOUR)]


def _shade_peak_periods(ax, ts_min, ts_max):
    """Shade all configured peak periods between ts_min and ts_max."""
    peaks = _get_peak_periods()
    current = ts_min.normalize()
    end = ts_max.normalize() + timedelta(days=1)
    while current <= end:
        for start_hour, end_hour in peaks:
            peak_start = current.replace(hour=start_hour, minute=0, second=0)
            peak_end = current.replace(hour=end_hour, minute=0, second=0)
            if peak_end > ts_min and peak_start < ts_max:
                ax.axvspan(peak_start, peak_end, alpha=0.12, color=COLORS['peak_shade'])
        current += timedelta(days=1)


# ─── Chart 4: Decision Engine Activity (v4 only) ────────────────────────────

def create_decision_engine_chart(intel):
    """Timeline of v4 decision engine priority phases with gap/forecast metrics.
    
    Only generated if v4 data exists. Shows:
    - Top: Priority phase color bars over time
    - Bottom: Gap kWh and forecast solar kWh from engine metrics
    """
    decisions = intel['decisions']
    metrics = intel['metrics']
    
    # Filter to v4 only
    if decisions.empty:
        return None
    
    v4_decisions = decisions[decisions['version'] == 'v4'].copy()
    if v4_decisions.empty:
        return None
    
    fig, axes = plt.subplots(2, 1, figsize=(16, 7), sharex=True,
                              gridspec_kw={'height_ratios': [1, 2]})
    ax_phases, ax_metrics = axes
    
    # ── Top: Priority phase timeline ──
    # Create colored spans for each decision period
    v4_sorted = v4_decisions.sort_values('timestamp').reset_index(drop=True)
    
    for i in range(len(v4_sorted)):
        start = v4_sorted.iloc[i]['timestamp']
        if i + 1 < len(v4_sorted):
            end = v4_sorted.iloc[i + 1]['timestamp']
        else:
            end = start + timedelta(minutes=30)
        
        priority = v4_sorted.iloc[i]['priority']
        color = COLORS.get(priority, '#BDBDBD')
        ax_phases.axvspan(start, end, alpha=0.7, color=color)
    
    # Priority legend
    priority_labels = {
        'P1': 'P1: Override/Emergency',
        'P2': 'P2: Manual Override',
        'P3': 'P3: Grid Disconnect',
        'P4': 'P4: Peak Protection',
        'P5': 'P5: Near-Full/Curtailment',
        'P6': 'P6: Solar Charging',
        'P7': 'P7: Gap Charging',
        'P8': 'P8: No Action Needed',
    }
    # Only include priorities that actually appear
    seen = set(v4_sorted['priority'].unique())
    legend_patches = [
        mpatches.Patch(color=COLORS.get(p, '#BDBDBD'), label=label, alpha=0.7)
        for p, label in priority_labels.items() if p in seen
    ]
    ax_phases.legend(handles=legend_patches, loc='upper left', fontsize=8,
                     ncol=len(legend_patches), framealpha=0.9)
    
    ax_phases.set_yticks([])
    ax_phases.set_title('v4 Decision Engine — Priority Phase Timeline', fontsize=13, fontweight='bold')
    
    # ── Bottom: Engine metrics ──
    if not metrics.empty and 'gap_kwh' in metrics.columns:
        m = metrics.dropna(subset=['timestamp']).sort_values('timestamp')
        
        # Gap kWh (positive = need to charge, negative = surplus)
        if 'gap_kwh' in m.columns:
            gap_data = m.dropna(subset=['gap_kwh'])
            if not gap_data.empty:
                ax_metrics.fill_between(gap_data['timestamp'], 0, gap_data['gap_kwh'],
                                        where=(gap_data['gap_kwh'] > 0),
                                        alpha=0.4, color=COLORS['P7'], label='Charging Gap (kWh)')
                ax_metrics.fill_between(gap_data['timestamp'], 0, gap_data['gap_kwh'],
                                        where=(gap_data['gap_kwh'] <= 0),
                                        alpha=0.3, color=COLORS['P5'], label='Surplus (kWh)')
                ax_metrics.plot(gap_data['timestamp'], gap_data['gap_kwh'],
                               color=COLORS['P7'], linewidth=1.5)
        
        # Forecast solar overlay
        if 'forecast_solar_kwh' in m.columns:
            fcast = m.dropna(subset=['forecast_solar_kwh'])
            if not fcast.empty:
                ax_metrics.plot(fcast['timestamp'], fcast['forecast_solar_kwh'],
                               color=COLORS['solar'], linewidth=2, linestyle='--',
                               label='Forecast Solar (kWh)')
        
        # Target line
        if 'target_kwh' in m.columns:
            target_vals = m['target_kwh'].dropna()
            if not target_vals.empty:
                ax_metrics.axhline(y=target_vals.iloc[0], color='green',
                                   linestyle=':', alpha=0.5, label=f'Target ({target_vals.iloc[0]:.0f} kWh)')
        
        ax_metrics.axhline(y=0, color='black', linewidth=0.5)
    
    # Curtailment on secondary axis if present
    if not metrics.empty and 'curtailed_kw' in metrics.columns:
        curt = metrics.dropna(subset=['curtailed_kw']).sort_values('timestamp')
        if not curt.empty:
            ax_curt = ax_metrics.twinx()
            ax_curt.bar(curt['timestamp'], curt['curtailed_kw'],
                        width=timedelta(minutes=25), alpha=0.3,
                        color=COLORS['curtailment'], label='Curtailed (kW)')
            ax_curt.set_ylabel('Curtailed (kW)', fontsize=10, color=COLORS['curtailment'])
            ax_curt.tick_params(axis='y', labelcolor=COLORS['curtailment'])
    
    ax_metrics.set_ylabel('Energy (kWh)', fontsize=11, fontweight='bold')
    ax_metrics.set_xlabel('Time', fontsize=11, fontweight='bold')
    ax_metrics.set_title('Charging Gap & Solar Forecast', fontsize=12, fontweight='bold')
    ax_metrics.legend(loc='upper left', fontsize=9)
    ax_metrics.grid(True, alpha=0.3)
    
    _shade_peak_periods(ax_phases, v4_sorted['timestamp'].min(), v4_sorted['timestamp'].max())
    _shade_peak_periods(ax_metrics, v4_sorted['timestamp'].min(), v4_sorted['timestamp'].max())
    
    ax_metrics.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
    ax_metrics.xaxis.set_major_locator(mdates.HourLocator(interval=6))
    plt.setp(ax_metrics.xaxis.get_majorticklabels(), rotation=45, ha='right')
    
    plt.tight_layout()
    return fig


# ─── Chart 5: Solar Curtailment & Utilization (v4 only) ─────────────────────

def create_curtailment_chart(df, intel):
    """Solar utilization chart showing production vs consumed/wasted.
    
    Only generated if v4 curtailment data exists.
    """
    metrics = intel['metrics']
    
    if metrics.empty or 'curtailed_kw' not in metrics.columns:
        return None
    
    curt_data = metrics.dropna(subset=['curtailed_kw'])
    if curt_data.empty:
        return None
    
    fig, (ax_time, ax_daily) = plt.subplots(2, 1, figsize=(16, 7),
                                             gridspec_kw={'height_ratios': [3, 2]})
    
    # ── Top: Timeline of solar production vs curtailment ──
    # Get solar data for matching time range
    curt_min = curt_data['timestamp'].min()
    curt_max = curt_data['timestamp'].max()
    
    solar_window = df[(df['timestamp'] >= curt_min - timedelta(hours=2)) &
                      (df['timestamp'] <= curt_max + timedelta(hours=2))].copy()
    
    if not solar_window.empty:
        ax_time.fill_between(solar_window['timestamp'], 0, solar_window['solar_kw'],
                             alpha=0.3, color=COLORS['solar'], label='Solar Production')
        ax_time.plot(solar_window['timestamp'], solar_window['solar_kw'],
                     color=COLORS['solar'], linewidth=1.5)
    
    # Overlay curtailment bars
    ax_time.bar(curt_data['timestamp'], curt_data['curtailed_kw'],
                width=timedelta(minutes=25), alpha=0.6,
                color=COLORS['curtailment'], edgecolor='#BF360C',
                linewidth=0.5, label='Curtailed Solar (kW)')
    
    ax_time.set_ylabel('Power (kW)', fontsize=11, fontweight='bold')
    ax_time.set_title('Solar Curtailment — Wasted Energy When Battery Full',
                      fontsize=13, fontweight='bold')
    ax_time.legend(loc='upper left', fontsize=10)
    ax_time.grid(True, alpha=0.3)
    
    ax_time.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d %H:%M'))
    ax_time.xaxis.set_major_locator(mdates.HourLocator(interval=3))
    plt.setp(ax_time.xaxis.get_majorticklabels(), rotation=45, ha='right')
    
    # ── Bottom: Daily utilization summary ──
    curt_data_copy = curt_data.copy()
    curt_data_copy['date'] = curt_data_copy['timestamp'].dt.date
    daily_curt = curt_data_copy.groupby('date')['curtailed_kw'].agg(['sum', 'count', 'max'])
    daily_curt.columns = ['total_curt_kw', 'readings', 'peak_curt_kw']
    daily_curt['est_curtailed_kwh'] = daily_curt['total_curt_kw'] * 0.5  # ~30min intervals
    
    if not solar_window.empty:
        solar_window_copy = solar_window.copy()
        solar_window_copy['date'] = solar_window_copy['timestamp'].dt.date
        interval_hours = 5 / 60
        daily_solar = solar_window_copy.groupby('date')['solar_kw'].sum() * interval_hours
        daily_solar.name = 'solar_kwh'
        daily_curt = daily_curt.join(daily_solar, how='left')
        daily_curt['solar_kwh'] = daily_curt['solar_kwh'].fillna(0)
        daily_curt['utilization_pct'] = 0.0
        mask = daily_curt['solar_kwh'] > 0
        daily_curt.loc[mask, 'utilization_pct'] = (
            (daily_curt.loc[mask, 'solar_kwh'] - daily_curt.loc[mask, 'est_curtailed_kwh'])
            / daily_curt.loc[mask, 'solar_kwh'] * 100
        ).clip(0, 100)
    
    dates = daily_curt.index
    x = range(len(dates))
    labels = [d.strftime('%a\n%m/%d') for d in dates]
    
    ax_daily.bar(x, daily_curt['est_curtailed_kwh'], color=COLORS['curtailment'],
                 alpha=0.7, label='Est. Curtailed (kWh)')
    
    if 'utilization_pct' in daily_curt.columns:
        ax_util = ax_daily.twinx()
        ax_util.plot(x, daily_curt['utilization_pct'], 'o-',
                     color=COLORS['soc'], linewidth=2, markersize=8,
                     label='Solar Utilization %')
        ax_util.set_ylabel('Utilization (%)', fontsize=10, color=COLORS['soc'])
        ax_util.set_ylim(0, 105)
        ax_util.tick_params(axis='y', labelcolor=COLORS['soc'])
        ax_util.legend(loc='upper right', fontsize=9)
    
    ax_daily.set_ylabel('Energy (kWh)', fontsize=11, fontweight='bold')
    ax_daily.set_xlabel('Date', fontsize=11, fontweight='bold')
    ax_daily.set_title('Daily Curtailment & Solar Utilization', fontsize=12, fontweight='bold')
    ax_daily.set_xticks(x)
    ax_daily.set_xticklabels(labels)
    ax_daily.legend(loc='upper left', fontsize=9)
    ax_daily.grid(True, alpha=0.3, axis='y')
    
    plt.tight_layout()
    return fig


# ─── Reports Index ───────────────────────────────────────────────────────────

def generate_reports_index():
    """Generate JSON index of available weekly reports for dashboard."""
    print("\nGenerating reports index for dashboard...")
    
    reports = {}
    pattern = re.compile(r'^(\d{2}-\d{2}-\d{4})_chart_(\w+)\.png$')
    
    output_dir = str(OUTPUT_DIR)
    for filename in os.listdir(output_dir):
        match = pattern.match(filename)
        if match:
            date_str = match.group(1)
            chart_type = match.group(2)
            
            if date_str not in reports:
                reports[date_str] = {'date': date_str, 'charts': [], 'timestamp': None}
            
            reports[date_str]['charts'].append(chart_type)
            
            filepath = os.path.join(output_dir, filename)
            mtime = os.path.getmtime(filepath)
            if reports[date_str]['timestamp'] is None or mtime > reports[date_str]['timestamp']:
                reports[date_str]['timestamp'] = mtime
    
    if not reports:
        print("  No chart files found to index")
        return
    
    sorted_dates = sorted(reports.keys(),
                          key=lambda x: datetime.strptime(x, '%m-%d-%Y'),
                          reverse=True)
    
    output = {'generated': datetime.now().isoformat(), 'reports': []}
    
    for date_str in sorted_dates:
        report = reports[date_str]
        try:
            date = datetime.strptime(date_str, '%m-%d-%Y')
            label = date.strftime('%A, %B %d, %Y')
        except ValueError:
            label = date_str
        
        output['reports'].append({
            'date': date_str,
            'label': label,
            'charts': sorted(report['charts']),
            'generated': datetime.fromtimestamp(report['timestamp']).isoformat() if report['timestamp'] else None
        })
    
    index_file = os.path.join(str(WEB_DIR), 'weekly_reports_index.json')
    with open(index_file, 'w') as f:
        json.dump(output, f, indent=2)
    
    print(f"  Indexed {len(output['reports'])} weekly reports")
    print(f"  Saved: {index_file}")


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("FRANKLIN BATTERY — WEEKLY PERFORMANCE CHARTS (v4.0 Enhanced)")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %I:%M %p')}")
    print("=" * 70)
    print()
    
    date_prefix = datetime.now().strftime('%m-%d-%Y')
    output_dir = str(OUTPUT_DIR)
    
    # Load data
    print("Loading monitoring data...")
    df = load_monitoring_data(days=7)
    print(f"  {len(df)} monitoring records")
    
    print("Parsing intelligence log...")
    intel = parse_intelligence_log(days=7)
    n_decisions = len(intel['decisions'])
    n_switches = len(intel['switches'])
    n_metrics = len(intel['metrics'])
    
    # Detect engine versions present
    versions = set()
    if not intel['decisions'].empty:
        versions = set(intel['decisions']['version'].unique())
    version_str = ' + '.join(sorted(versions)) if versions else 'unknown'
    
    print(f"  {n_decisions} decisions, {n_switches} mode switches, {n_metrics} engine metrics")
    print(f"  Engine versions in data: {version_str}")
    
    if len(df) == 0:
        print("\nNo monitoring data found! Check log files.")
        return 1
    
    charts_generated = []
    
    # Chart 1: SOC Timeline
    print("\nChart 1: SOC Timeline (7 days)...")
    fig1 = create_soc_timeline_chart(df, intel)
    f1 = f'{output_dir}/{date_prefix}_chart_soc_timeline.png'
    fig1.savefig(f1, dpi=150, bbox_inches='tight')
    charts_generated.append(f1)
    print(f"  Saved: {f1}")
    
    # Chart 2: Daily Summary
    print("Chart 2: Daily Summary (7 days)...")
    fig2 = create_daily_summary_chart(df, intel)
    f2 = f'{output_dir}/{date_prefix}_chart_daily_summary.png'
    fig2.savefig(f2, dpi=150, bbox_inches='tight')
    charts_generated.append(f2)
    print(f"  Saved: {f2}")
    
    # Chart 3: Power Flow
    print("Chart 3: Power Flow (48 hours)...")
    fig3 = create_power_flow_chart(df)
    if fig3:
        f3 = f'{output_dir}/{date_prefix}_chart_power_flow.png'
        fig3.savefig(f3, dpi=150, bbox_inches='tight')
        charts_generated.append(f3)
        print(f"  Saved: {f3}")
    
    # Chart 4: Decision Engine Activity (v4 only)
    if 'v4' in versions:
        print("Chart 4: Decision Engine Activity (v4)...")
        fig4 = create_decision_engine_chart(intel)
        if fig4:
            f4 = f'{output_dir}/{date_prefix}_chart_decision_engine.png'
            fig4.savefig(f4, dpi=150, bbox_inches='tight')
            charts_generated.append(f4)
            print(f"  Saved: {f4}")
        else:
            print("  Skipped (insufficient v4 data)")
    else:
        print("Chart 4: Decision Engine — skipped (no v4 data)")
    
    # Chart 5: Solar Curtailment (v4 only)
    if 'v4' in versions:
        print("Chart 5: Solar Curtailment Tracker (v4)...")
        fig5 = create_curtailment_chart(df, intel)
        if fig5:
            f5 = f'{output_dir}/{date_prefix}_chart_curtailment.png'
            fig5.savefig(f5, dpi=150, bbox_inches='tight')
            charts_generated.append(f5)
            print(f"  Saved: {f5}")
        else:
            print("  Skipped (no curtailment data)")
    else:
        print("Chart 5: Curtailment — skipped (no v4 data)")
    
    # Generate index
    generate_reports_index()
    
    print("\n" + "=" * 70)
    print(f"Generated {len(charts_generated)} charts successfully!")
    if 'v3' in versions and 'v4' in versions:
        print("NOTE: Transition week — data includes both v3.5 and v4.0 entries.")
        print("      v3.5 mode switches shown with lighter markers.")
    print(f"\nView charts at: {output_dir}/{date_prefix}_chart_*.png")
    print("=" * 70)
    
    plt.close('all')
    return 0


if __name__ == "__main__":
    sys.exit(main())
