#!/usr/bin/env python3
"""
FranklinWH Automation Scheduler - v4.0

Master scheduler that runs all automation tasks on their configured intervals.
This allows the Docker container to be fully self-contained - no external
cron or Task Scheduler needed.

v4.0 changes:
- Anonymous opt-in telemetry: daily submission to private GitHub repo
- GET/POST /api/telemetry-consent endpoints for dashboard modal

v3.5.0 changes:
- Manual override API: POST /api/override, DELETE /api/override, GET /api/override
- Override writes state file + immediately switches mode via library
- Supports configurable duration (1h, 2h, 4h, 8h, until_cancel)

v3.2.0 changes:
- Configurable check interval via CHECK_INTERVAL_MINUTES
- Schedule-aware peak transition checks pinned to exact times
- Guarantees mode switches happen before peak starts and after peak ends

Tasks:
- smart_decision.py: Every N minutes (configurable, default 15)
  + Pinned checks at PEAK_START - buffer and PEAK_END + 1 min
- generate_dashboard_data.py: Every 1 minute (dashboard updates)
- collect_weather_db.py: Every 15 minutes (if WEATHER_ENABLED) — writes to SQLite
- collect_pv_output.py: Every hour (if PVOUTPUT_ENABLED)
- collect_solar_enphase.py: Every 5 minutes per array (SQLite + dashboard JSON)
- daily_status_report.py: Daily at 4:30 PM (if EMAIL_ENABLED)
- generate_weekly_charts.py: Weekly on Sunday at 2:00 AM
- calculate_daily_savings.py: Daily at 00:05 AM (previous day)
- telemetry_reporter.py: Daily at 6:00 AM (if opted in, retry at 7:00 AM)

API:
- Internal HTTP server on port 8101 for dashboard operations
- POST /api/save-layout: Save solar array layout JSON
- POST /api/override: Activate manual mode override
- DELETE /api/override: Cancel active override
- GET /api/override: Get current override status
- POST /api/diagnostic-bundle: Generate sanitized diagnostic bundle for issue reporting
- GET /api/telemetry-consent: Get current telemetry consent status
- POST /api/telemetry-consent: Set telemetry consent (from dashboard modal)
"""
import schedule
import time
import subprocess
import sys
import os
import json
import asyncio
import threading
from datetime import datetime, timedelta
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

# Add script directory to path for imports
SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

# Import config to check enabled features
try:
    from config import config, configure_logging, VERSION
    CONFIG_LOADED = True
except ImportError:
    print("Warning: Could not load config, using defaults")
    CONFIG_LOADED = False
    VERSION = '0.0.0'


def log(message: str):
    """Print timestamped log message and store in DB."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    print(log_line, flush=True)

    # Write to DB
    try:
        import db as db_mod
        db_mod.store.scheduler_log(timestamp=timestamp, message=message)
    except Exception:
        pass


def run_script(script_name: str, description: str):
    """Run a Python script and log the result."""
    return run_script_with_args(script_name, [], description)


def run_script_with_args(script_name: str, args: list, description: str):
    """Run a Python script with optional arguments and log the result."""
    script_path = SCRIPT_DIR / script_name
    
    if not script_path.exists():
        log(f"WARNING: {description}: Script not found ({script_name})")
        return False
    
    log(f">> {description}: Starting...")
    
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)] + args,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
            cwd=str(SCRIPT_DIR)
        )
        
        if result.returncode == 0:
            log(f"OK {description}: Completed successfully")
            if result.stdout.strip():
                # Print last line of output as summary
                last_line = result.stdout.strip().split('\n')[-1]
                log(f"  -> {last_line}")
            return True
        else:
            log(f"FAIL {description}: Failed (exit code {result.returncode})")
            if result.stderr.strip():
                log(f"  -> Error: {result.stderr.strip()[:200]}")
            elif result.stdout.strip():
                # Some scripts print errors to stdout
                last_line = result.stdout.strip().split('\n')[-1]
                log(f"  -> Error: {last_line[:200]}")
            return False
            
    except subprocess.TimeoutExpired:
        log(f"FAIL {description}: Timed out after 5 minutes")
        return False
    except Exception as e:
        log(f"FAIL {description}: Exception - {e}")
        return False


def job_smart_decision():
    """Core automation - runs on configurable interval + peak transitions."""
    run_script("smart_decision.py", "Smart Decision")


def job_smart_decision_peak_start():
    """Critical pre-peak check - ensures mode is correct before peak begins."""
    log("--- PEAK TRANSITION CHECK: Pre-peak ---")
    run_script("smart_decision.py", "Smart Decision [pre-peak]")


def job_smart_decision_peak_end():
    """Critical post-peak check - restores home mode after peak ends."""
    log("--- PEAK TRANSITION CHECK: Post-peak ---")
    run_script("smart_decision.py", "Smart Decision [post-peak]")


def job_dashboard_data():
    """Dashboard data update - runs every minute."""
    run_script("generate_dashboard_data.py", "Dashboard Data")


def job_weather():
    """Legacy weather collection — DISABLED in v4.0 Iteration 2.
    Replaced by collect_weather_db.py which writes directly to SQLite.
    Kept as stub for backward compatibility if referenced elsewhere.
    """
    pass


def job_pvoutput():
    """PVOutput collection - runs hourly if enabled."""
    if CONFIG_LOADED and config.PVOUTPUT_ENABLED:
        run_script("collect_pv_output.py", "PVOutput Collection")
    else:
        pass  # Silently skip if not enabled


def job_solar_arrays():
    """Solar array data collection - runs every 5 minutes if any arrays configured.

    For Enphase arrays: collect_solar_enphase.py handles both SQLite storage
    and dashboard JSON generation in a single gateway call.
    For SolarEdge arrays: handled by collect_solaredge_panels.py separately.
    """
    if not CONFIG_LOADED:
        return
    
    # Multi-array mode: SOLAR_ARRAYS=house,barn
    solar_arrays = getattr(config, 'SOLAR_ARRAYS', '')
    if solar_arrays:
        for array_id in [a.strip() for a in solar_arrays.split(',') if a.strip()]:
            array_type_key = f'SOLAR_ARRAY_{array_id.upper()}_TYPE'
            array_type = os.getenv(array_type_key, 'enphase')
            if array_type == 'enphase':
                run_script_with_args(
                    "collect_solar_enphase.py",
                    ["--array-id", array_id],
                    f"Solar [{array_id}]"
                )
            elif array_type == 'solaredge':
                # SolarEdge barn collection is handled by collect_solaredge_panels.py
                # (job_solaredge_panels, every 15 min) which writes both
                # solaredge_panel_current.json AND solar_barn.json with real
                # serial numbers and health data. No need to run the old
                # collect_solaredge.py which used synthetic serial numbers.
                pass
            else:
                log(f"  Unknown solar array type '{array_type}' for {array_id}")
    # Legacy fallback: ENPHASE_ENABLED=true
    elif getattr(config, 'ENPHASE_ENABLED', False):
        run_script("collect_solar_enphase.py", "Enphase Solar Collection")


def job_daily_report():
    """Daily status report - runs at 4:30 PM if email enabled."""
    if CONFIG_LOADED and getattr(config, 'EMAIL_ENABLED', False):
        run_script("daily_status_report.py", "Daily Status Report")
    else:
        pass  # Silently skip if not enabled


def job_weekly_charts():
    """Weekly charts - runs Sunday at 2:00 AM."""
    run_script("generate_weekly_charts.py", "Weekly Charts")


def job_daily_savings():
    """Daily savings calculation - runs at 00:05 AM for the previous day."""
    run_script_with_args("calculate_daily_savings.py", ["--yesterday", "--quiet"], "Daily Savings")


def job_solaredge_panels():
    """SolarEdge per-panel data collection - runs every 15 minutes if enabled.
    
    Collects real per-optimizer energy data from the SolarEdge monitoring portal.
    Only 5 HTTP calls per run regardless of panel count. Data logged to CSV
    for health monitoring and anomaly detection.
    """
    if CONFIG_LOADED and getattr(config, 'SOLAREDGE_PANEL_MONITORING', False):
        run_script("collect_solaredge_panels.py", "SolarEdge Panel Monitoring")


def job_solar_health():
    """Solar health monitor - runs daily after sunset.
    
    Analyzes all solar arrays (SolarEdge + Enphase) for panel health using
    historical data, recent production trends, and cyclical failure detection.
    Outputs solar_health_report.json for dashboard consumption and maintains
    a persistent watchlist for tracking intermittent failures.
    """
    run_script("solar_health_monitor.py", "Solar Health Monitor")


def job_collect_modbus_db():
    """SQLite Modbus collector - runs every 5 minutes.
    Stores parsed system readings + raw register blocks to franklin.db.
    Independent of existing data_sources.py Modbus reads.
    """
    run_script("collect_modbus.py", "SQLite Modbus Collection")


def job_collect_system_snapshot():
    """Modbus collection — runs every 5 minutes.
    Enphase collection is now handled by job_solar_arrays calling
    the consolidated collect_solar_enphase.py (SQLite + dashboard JSON).
    """
    run_script("collect_modbus.py", "SQLite Modbus Collection")


def job_collect_weather_db():
    """SQLite Weather collector - runs every 15 minutes.
    Stores WU observations + updates daily aggregates in franklin.db.
    Sole weather collector — replaces retired collect_weather.py.
    """
    run_script("collect_weather_db.py", "SQLite Weather Collection")


def job_collect_device_inventory():
    """Device inventory collector - runs daily at 3:00 AM.
    Tracks serial numbers, firmware versions, and models across all systems.
    Only writes when something changes (firmware update, new device, etc).
    """
    run_script("collect_device_inventory.py", "Device Inventory Collection")


def job_collect_franklin_cloud():
    """Franklin cloud API collector - runs every 15 minutes.
    Enriches Modbus system_readings with cloud-only fields:
    per-battery SOC, energy totals, signal strength, charging breakdown.
    """
    run_script("collect_franklin_cloud.py", "Franklin Cloud Collection")


def job_collect_pv_output():
    """PVOutput daily collector - runs daily after midnight.
    Collects yesterday's production data from PVOutput.org API.
    Writes to both CSV (backward compat) and SQLite pvoutput_daily table.
    """
    run_script("collect_pv_output.py", "PVOutput Daily Collection")


def job_rollup_daily_energy():
    """Nightly rollup of system_readings into daily_energy_summary.
    Aggregates yesterday's 5-min readings into a single summary row
    for fast dashboard/report queries.
    """
    run_script("rollup_daily_energy.py", "Daily Energy Rollup")


def job_rebuild_system_profile():
    """Weekly rebuild of system_profile.json from database.

    The system profile contains learned charge curves, consumption patterns,
    and solar profiles used by the adaptive engine for time-to-charge
    estimates and gap calculations. Rebuilds from system_readings DB.
    """
    run_script_with_args("system_profile.py", ["--db"], "System Profile Rebuild")


def job_prune_logs():
    """Prune old scheduler_log and intelligence_log DB entries.
    Keeps 30 days of history to prevent unbounded growth.
    """
    try:
        import db as db_mod
        db_mod.prune_old_logs(days=30)
        log("Log pruning complete (30-day retention)")
    except Exception as e:
        log(f"Log pruning error (non-fatal): {e}")


def job_telemetry():
    """Anonymous telemetry — runs daily at 6:00 AM if opted in.

    Submits aggregate usage data to the private GitHub collection repo.
    Failure is silent — never impacts automation.
    """
    try:
        from telemetry_reporter import run_telemetry
        run_telemetry()
    except ImportError:
        pass  # telemetry_reporter.py not present — silently skip
    except Exception as e:
        log(f"  Telemetry error (non-fatal): {e}")


def job_telemetry_retry():
    """Telemetry retry — runs 1 hour after the daily job.

    Only actually retries if the daily run failed.
    """
    try:
        from telemetry_reporter import run_telemetry_retry
        run_telemetry_retry()
    except ImportError:
        pass
    except Exception as e:
        log(f"  Telemetry retry error (non-fatal): {e}")


def format_time(hour: int, minute: int) -> str:
    """Format hour:minute as HH:MM string for schedule library."""
    return f"{hour:02d}:{minute:02d}"


def _register(name, schedule_str):
    """Log a scheduled task and add it to REGISTERED_TASKS."""
    log(f"  - {name}: {schedule_str}")
    REGISTERED_TASKS.append({'name': name, 'schedule': schedule_str})


def setup_schedule():
    """Configure all scheduled tasks."""
    log("=" * 60)
    log("FranklinWH Automation Scheduler v" + VERSION)
    log("=" * 60)
    
    # Get scheduling config
    buffer_minutes = config.PEAK_TRANSITION_BUFFER_MINUTES if CONFIG_LOADED else 10
    
    # Show enabled features
    if CONFIG_LOADED:
        log("Enabled features:")
        log(f"  - Solar: {config.SOLAR_ENABLED}")
        log(f"  - TOU: {config.TOU_ENABLED}")
        log(f"  - Dynamic Pricing: {config.DYNAMIC_PRICING_ENABLED}")
        log(f"  - Weather: {config.WEATHER_ENABLED}")
        log(f"  - PVOutput: {config.PVOUTPUT_ENABLED}")
        solar_arrays = getattr(config, 'SOLAR_ARRAYS', '')
        if solar_arrays:
            log(f"  - Solar Arrays: {solar_arrays}")
        elif getattr(config, 'ENPHASE_ENABLED', False):
            log(f"  - Enphase Solar: True (legacy mode)")
        log(f"  - Email: {getattr(config, 'EMAIL_ENABLED', False)}")
        se_panel = getattr(config, 'SOLAREDGE_PANEL_MONITORING', False)
        if se_panel:
            log(f"  - SolarEdge Panel Monitoring: site {getattr(config, 'SOLAREDGE_SITE_ID', 'N/A')}")
        adaptive = getattr(config, 'ADAPTIVE_ENGINE_ENABLED', False)
        if adaptive:
            log(f"  - V4.0 Adaptive Engine: ENABLED")
        log(f"  - Home Mode: {config.HOME_MODE}")
    
    # Three-mode strategy notice (v4 with TOU as default)
    if CONFIG_LOADED and getattr(config, 'ADAPTIVE_ENGINE_ENABLED', False):
        log("")
        log("=" * 60)
        log("  V4.0 THREE-MODE STRATEGY — CONFIGURATION REQUIRED")
        log("  " + "-" * 56)
        log("  The v4 engine uses three modes:")
        log("    TOU            = Default (solar -> battery, grid -> home)")
        log("    Self-Consumption = Peak hours (battery powers home)")
        log("    Emergency Backup = Gap-fill only (grid charges battery)")
        log("")
        log("  REQUIRED: In the FranklinWH app, configure your TOU")
        log("  tariff so ALL time periods use 'aPower charges from solar'.")
        log("  Without this, TOU mode will NOT route solar to the battery.")
        log("=" * 60)
        log("")
    
    log("-" * 60)
    log("Scheduling tasks:")
    global REGISTERED_TASKS
    REGISTERED_TASKS = []
    
    # Core automation - clock-aligned at :00 and :30 every hour
    # Fixed at 30-minute intervals to stay within Franklin API rate limits.
    # Clock-aligned ensures predictable timing regardless of container start.
    schedule.every().hour.at(":00").do(job_smart_decision)
    schedule.every().hour.at(":30").do(job_smart_decision)
    _register("Smart Decision (v4.0)", "Every 30 minutes (clock-aligned at :00 and :30)")
    
    # Peak transition checks - pinned to exact times
    if CONFIG_LOADED and config.TOU_ENABLED:
        # Pre-peak check: PEAK_START - buffer minutes
        pre_peak_hour = config.PEAK_START_HOUR
        pre_peak_minute = 60 - buffer_minutes  # e.g., 55 for 5-minute buffer
        if pre_peak_minute >= 60:
            pre_peak_minute -= 60
        else:
            pre_peak_hour -= 1
            if pre_peak_hour < 0:
                pre_peak_hour = 23
        
        pre_peak_time = format_time(pre_peak_hour, pre_peak_minute)
        schedule.every().day.at(pre_peak_time).do(job_smart_decision_peak_start)
        _register("Smart Decision [pre-peak]", f"Daily at {pre_peak_time} ({buffer_minutes}min before peak)")
        
        # Post-peak check: PEAK_END hour + 1 minute
        post_peak_time = format_time(config.PEAK_END_HOUR, 1)
        schedule.every().day.at(post_peak_time).do(job_smart_decision_peak_end)
        _register("Smart Decision [post-peak]", f"Daily at {post_peak_time} (1min after peak ends)")
        
        # Secondary peak period if configured
        if config.PEAK2_START_HOUR and config.PEAK2_END_HOUR:
            pre_peak2_hour = config.PEAK2_START_HOUR
            pre_peak2_minute = 60 - buffer_minutes
            if pre_peak2_minute >= 60:
                pre_peak2_minute -= 60
            else:
                pre_peak2_hour -= 1
                if pre_peak2_hour < 0:
                    pre_peak2_hour = 23
            
            pre_peak2_time = format_time(pre_peak2_hour, pre_peak2_minute)
            schedule.every().day.at(pre_peak2_time).do(job_smart_decision_peak_start)
            _register("Smart Decision [pre-peak2]", f"Daily at {pre_peak2_time}")
            
            post_peak2_time = format_time(config.PEAK2_END_HOUR, 1)
            schedule.every().day.at(post_peak2_time).do(job_smart_decision_peak_end)
            _register("Smart Decision [post-peak2]", f"Daily at {post_peak2_time}")
    
    # Dashboard data - every minute
    schedule.every(1).minutes.do(job_dashboard_data)
    _register("Dashboard Data", "Every 1 minute")
    
    # Weather — legacy collect_weather.py removed in Iteration 2
    # Weather data now comes exclusively from collect_weather_db.py (SQLite)
    # which is scheduled below in the SQLite collectors section
    
    # PVOutput - hourly (if enabled)
    if CONFIG_LOADED and config.PVOUTPUT_ENABLED:
        schedule.every().hour.at(":05").do(job_pvoutput)
        _register("PVOutput Collection", "Hourly at :05")
    
    # Solar arrays - every 5 minutes (if any configured)
    solar_arrays = getattr(config, 'SOLAR_ARRAYS', '') if CONFIG_LOADED else ''
    enphase_legacy = getattr(config, 'ENPHASE_ENABLED', False) if CONFIG_LOADED else False
    if solar_arrays or enphase_legacy:
        schedule.every(5).minutes.do(job_solar_arrays)
        if solar_arrays:
            _register("Solar [house]", f"Every 5 min")
        else:
            _register("Enphase Solar Collection", "Every 5 minutes (legacy)")
    
    # SolarEdge panel monitoring - every 15 minutes (if enabled)
    # Separate from the solar array collection above; this collects real
    # per-optimizer energy data for health monitoring / anomaly detection
    if CONFIG_LOADED and getattr(config, 'SOLAREDGE_PANEL_MONITORING', False):
        schedule.every(15).minutes.do(job_solaredge_panels)
        _register("SolarEdge Panel Monitoring", f"Every 15 minutes (site {config.SOLAREDGE_SITE_ID})")
    
    # Daily report - 4:30 PM (if email enabled)
    if CONFIG_LOADED and getattr(config, 'EMAIL_ENABLED', False):
        schedule.every().day.at("16:30").do(job_daily_report)
        _register("Daily Status Report", "Daily at 4:30 PM")
    
    # Daily savings - 00:05 AM (calculates previous day's savings with full data)
    schedule.every().day.at("00:05").do(job_daily_savings)
    _register("Daily Savings", "Daily at 00:05 AM (previous day)")
    
    # Solar health monitor - 20:30 (after sunset, full day's data available)
    # Runs for all solar arrays regardless of type (SolarEdge, Enphase, or both)
    solar_arrays = getattr(config, 'SOLAR_ARRAYS', '') if CONFIG_LOADED else ''
    enphase_legacy_health = getattr(config, 'ENPHASE_ENABLED', False) if CONFIG_LOADED else False
    se_panel_health = getattr(config, 'SOLAREDGE_PANEL_MONITORING', False) if CONFIG_LOADED else False
    if solar_arrays or enphase_legacy_health or se_panel_health:
        schedule.every().day.at("20:30").do(job_solar_health)
        _register("Solar Health Monitor", "Daily at 8:30 PM")
    
    # Weekly charts retired — replaced by interactive Analytics tab
    
    # SQLite data collectors
    # Modbus + Enphase run back-to-back in a single job for time-aligned data
    if Path(SCRIPT_DIR / "collect_modbus.py").exists() and Path(SCRIPT_DIR / "db.py").exists():
        schedule.every(5).minutes.do(job_collect_system_snapshot)
        _register("SQLite Modbus Collection", "Every 5 min")
    if Path(SCRIPT_DIR / "collect_weather_db.py").exists() and Path(SCRIPT_DIR / "db.py").exists():
        if CONFIG_LOADED and getattr(config, 'WEATHER_ENABLED', False):
            schedule.every(15).minutes.do(job_collect_weather_db)
            _register("SQLite Weather Collection", "Every 15 minutes")
    if Path(SCRIPT_DIR / "collect_device_inventory.py").exists() and Path(SCRIPT_DIR / "db.py").exists():
        schedule.every().day.at("03:00").do(job_collect_device_inventory)
        _register("Device Inventory Collection", "Daily at 3:00 AM")
    if Path(SCRIPT_DIR / "collect_franklin_cloud.py").exists() and Path(SCRIPT_DIR / "db.py").exists():
        schedule.every(15).minutes.do(job_collect_franklin_cloud)
        _register("Franklin Cloud Collection", "Every 15 minutes")
    if Path(SCRIPT_DIR / "collect_pv_output.py").exists() and Path(SCRIPT_DIR / "db.py").exists():
        if CONFIG_LOADED and getattr(config, 'PVOUTPUT_ENABLED', False):
            schedule.every().day.at("00:15").do(job_collect_pv_output)
            _register("PVOutput Daily Collection", "Daily at 00:15 AM")
    if Path(SCRIPT_DIR / "rollup_daily_energy.py").exists() and Path(SCRIPT_DIR / "db.py").exists():
        schedule.every().day.at("00:10").do(job_rollup_daily_energy)
        _register("Daily Energy Rollup", "Daily at 00:10 AM")
    if Path(SCRIPT_DIR / "db.py").exists():
        schedule.every().day.at("03:30").do(job_prune_logs)
        _register("Log Pruning", "Daily at 3:30 AM (30-day retention)")

    if Path(SCRIPT_DIR / "system_profile.py").exists() and Path(SCRIPT_DIR / "db.py").exists():
        schedule.every().sunday.at("03:00").do(job_rebuild_system_profile)
        _register("System Profile Rebuild", "Weekly Sunday at 3:00 AM (from DB)")
    
    # Anonymous telemetry — daily at 6:00 AM + retry at 7:00 AM
    # Only runs if user has opted in (consent file or .env)
    schedule.every().day.at("06:00").do(job_telemetry)
    schedule.every().day.at("07:00").do(job_telemetry_retry)
    telemetry_status = 'unknown'
    try:
        from telemetry_reporter import get_consent_status
        telemetry_status = get_consent_status().get('status', 'unknown')
    except ImportError:
        telemetry_status = 'not installed'
    _register("Telemetry", f"Daily at 6:00 AM (status: {telemetry_status})")
    
    log("-" * 60)
    log("Scheduler ready. Running initial tasks...")
    
    # Clean up startup grace flag so first smart_decision run observes only
    # This prevents aggressive mode switches on container restart
    try:
        grace_flag = Path(str(config.LOG_DIR)) / "startup_grace.flag" if CONFIG_LOADED else SCRIPT_DIR.parent / "logs" / "startup_grace.flag"
        if grace_flag.exists():
            grace_flag.unlink()
            log("Startup grace flag reset — first decision cycle will observe only")
    except Exception as e:
        log(f"Warning: Could not reset startup grace flag: {e}")
    
    # Run core tasks immediately on startup
    # Rebuild system profile FIRST so smart_decision loads fresh data
    if Path(SCRIPT_DIR / "system_profile.py").exists() and Path(SCRIPT_DIR / "db.py").exists():
        job_rebuild_system_profile()
    job_smart_decision()
    job_dashboard_data()
    
    log("-" * 60)
    log("Initial tasks complete. Entering scheduled loop...")
    log("=" * 60)


# ===== Internal API Server =====
API_PORT = int(os.getenv('API_PORT', '8101'))
DOCKER_START_TIME = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
REGISTERED_TASKS = []
DATA_DIR = SCRIPT_DIR.parent / (os.getenv('DATA_DIR', '') or 'data')
WEB_DIR_PATH = SCRIPT_DIR.parent / (os.getenv('WEB_DIR', '') or 'web')
# Handle absolute Docker paths
if os.getenv('DATA_DIR', '').startswith('/'):
    DATA_DIR = Path(os.getenv('DATA_DIR'))
if os.getenv('WEB_DIR', '').startswith('/'):
    WEB_DIR_PATH = Path(os.getenv('WEB_DIR'))


if CONFIG_LOADED:
    OVERRIDE_FILE = Path(str(config.LOG_DIR)) / "override.json"
else:
    OVERRIDE_FILE = SCRIPT_DIR.parent / "logs" / "override.json"


async def _do_mode_switch(mode_name: str) -> bool:
    """Switch the gateway to the specified mode using the franklinwh library.
    
    Args:
        mode_name: 'emergency_backup', 'self_consumption', or 'time_of_use'
    
    Returns: True if switch succeeded
    """
    from franklinwh import Client, TokenFetcher, Mode
    
    fetcher = TokenFetcher(config.FRANKLIN_USERNAME, config.FRANKLIN_PASSWORD)
    client = Client(fetcher, config.FRANKLIN_GATEWAY_ID)
    
    if mode_name == 'emergency_backup':
        mode_obj = Mode.emergency_backup()
        mode_obj.soc = config.RESERVE_SOC_BACKUP
    elif mode_name == 'self_consumption':
        mode_obj = Mode.self_consumption()
        mode_obj.soc = config.RESERVE_SOC_HOME
    elif mode_name == 'time_of_use':
        mode_obj = Mode.time_of_use()
        mode_obj.soc = config.RESERVE_SOC_HOME
    else:
        raise ValueError(f"Unknown mode: {mode_name}")
    
    await client.set_mode(mode_obj)
    return True


def switch_mode_sync(mode_name: str) -> bool:
    """Synchronous wrapper for mode switching."""
    try:
        return asyncio.run(_do_mode_switch(mode_name))
    except Exception as e:
        log(f"  Mode switch error: {e}")
        return False


def write_override(data: dict):
    """Write override state to file."""
    OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(OVERRIDE_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def read_override() -> dict:
    """Read current override state."""
    try:
        if OVERRIDE_FILE.exists():
            with open(OVERRIDE_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {'active': False}


def _read_latest_soc() -> float:
    """Read latest SOC from system_readings for override SOC checks.

    Returns SOC percentage or None if unavailable.
    """
    try:
        import sqlite3
        db_path = SCRIPT_DIR.parent / 'data' / 'franklin.db'
        if not db_path.exists():
            return None
        conn = sqlite3.connect(str(db_path), timeout=5)
        row = conn.execute(
            "SELECT soc_pct FROM system_readings ORDER BY id DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if row and row[0] is not None:
            return float(row[0])
    except Exception:
        pass
    return None


class APIHandler(BaseHTTPRequestHandler):
    """Handles dashboard save and override operations."""

    def log_message(self, format, *args):
        log(f"  API: {args[0]}")

    def _cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def _json_response(self, code, data):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self._cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/api/override':
            self._get_override()
        elif path == '/api/telemetry-consent':
            self._get_telemetry_consent()
        elif path.startswith('/api/system-readings'):
            self._get_system_readings()
        elif path == '/api/db-stats':
            self._get_db_stats()
        elif path == '/api/docker-start':
            self._json_response(200, {'docker_start': DOCKER_START_TIME})
        elif path == '/api/version':
            self._json_response(200, {'version': VERSION})
        elif path == '/api/scheduler-tasks':
            self._json_response(200, {'tasks': REGISTERED_TASKS})
        elif path.startswith('/api/scheduler-logs'):
            self._get_log_entries('scheduler')
        elif path.startswith('/api/intelligence-logs'):
            self._get_log_entries('intelligence')
        elif path.startswith('/api/chart-data'):
            self._get_chart_data()
        elif path.startswith('/api/decision-log'):
            self._get_decision_log()
        elif path.startswith('/api/chart-dates'):
            self._get_chart_dates()
        elif path.startswith('/api/peak-config'):
            self._get_peak_config()
        else:
            self._json_response(404, {'error': 'Not found'})

    def do_POST(self):
        if self.path == '/api/save-layout':
            self._save_layout()
        elif self.path == '/api/override':
            self._set_override()
        elif self.path == '/api/override/cancel':
            self._cancel_override()
        elif self.path == '/api/diagnostic-bundle':
            self._generate_diagnostic_bundle()
        elif self.path == '/api/telemetry-consent':
            self._set_telemetry_consent()
        else:
            self._json_response(404, {'error': 'Not found'})

    def do_DELETE(self):
        if self.path == '/api/override':
            self._cancel_override()
        else:
            self._json_response(404, {'error': 'Not found'})

    def _get_override(self):
        """Return current override status."""
        ov = read_override()
        # Check if expired by time
        if ov.get('active') and ov.get('expires_at'):
            expires = datetime.fromisoformat(ov['expires_at'])
            if datetime.now() >= expires:
                ov = {'active': False}
                write_override(ov)
        # Check if SOC exit condition met
        if ov.get('active') and ov.get('exit_soc_pct'):
            soc = _read_latest_soc()
            if soc is not None:
                exit_soc = ov['exit_soc_pct']
                mode = ov.get('mode', '')
                # EB charges up → exit when SOC reaches target
                # SC discharges down → exit when SOC drops to target
                if mode == 'emergency_backup' and soc >= exit_soc:
                    log(f"  Override SOC target reached: {soc:.1f}% >= {exit_soc:.0f}%")
                    ov = {'active': False}
                    write_override(ov)
                elif mode in ('self_consumption', 'time_of_use') and soc <= exit_soc:
                    log(f"  Override SOC target reached: {soc:.1f}% <= {exit_soc:.0f}%")
                    ov = {'active': False}
                    write_override(ov)
        self._json_response(200, ov)

    def _set_override(self):
        """Activate a manual override."""
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            payload = json.loads(body)

            mode = payload.get('mode')
            duration = payload.get('duration', '1h')
            exit_soc_pct = payload.get('exit_soc_pct')

            if mode not in ('emergency_backup', 'self_consumption', 'time_of_use'):
                self._json_response(400, {'error': f'Invalid mode: {mode}'})
                return

            # Calculate expiry
            dur_map = {'1h': 60, '2h': 120, '4h': 240, '8h': 480, 'until_cancel': None, 'until_soc': None}
            minutes = dur_map.get(duration)
            expires_at = None
            if minutes:
                expires_at = (datetime.now() + timedelta(minutes=minutes)).isoformat()

            # Write override state
            ov = {
                'active': True,
                'mode': mode,
                'duration': duration,
                'started_at': datetime.now().isoformat(),
                'expires_at': expires_at,
            }

            # SOC-based exit condition
            if exit_soc_pct is not None:
                try:
                    exit_soc_pct = float(exit_soc_pct)
                    if 0 < exit_soc_pct <= 100:
                        ov['exit_soc_pct'] = exit_soc_pct
                except (ValueError, TypeError):
                    pass

            write_override(ov)
            label = f"{mode} for {duration}"
            if ov.get('exit_soc_pct'):
                label += f" or until SOC {'≥' if mode == 'emergency_backup' else '≤'} {ov['exit_soc_pct']:.0f}%"
            log(f"  Override activated: {label}")

            # Switch mode immediately in background
            def do_switch():
                success = switch_mode_sync(mode)
                if success:
                    log(f"  Override mode switch OK: {mode}")
                else:
                    log(f"  Override mode switch FAILED: {mode}")

            threading.Thread(target=do_switch, daemon=True).start()

            self._json_response(200, {'status': 'ok', 'override': ov})

        except json.JSONDecodeError:
            self._json_response(400, {'error': 'Invalid JSON'})
        except Exception as e:
            log(f"  Override error: {e}")
            self._json_response(500, {'error': str(e)})

    def _cancel_override(self):
        """Cancel active override and resume automation."""
        try:
            write_override({'active': False})
            log(f"  Override cancelled")

            # Run a smart decision immediately to restore correct mode
            threading.Thread(
                target=run_script,
                args=("smart_decision.py", "Smart Decision [override cancelled]"),
                daemon=True
            ).start()

            self._json_response(200, {'status': 'ok', 'override': {'active': False}})
        except Exception as e:
            log(f"  Override cancel error: {e}")
            self._json_response(500, {'error': str(e)})

    def _generate_diagnostic_bundle(self):
        """Generate a sanitized diagnostic bundle for issue reporting."""
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length > 0 else b'{}'
            payload = json.loads(body) if body else {}

            hours = min(payload.get('hours', 24), 168)  # Cap at 1 week
            description = payload.get('description', '')[:500]  # Cap description

            # Import and configure the diagnostic bundle generator
            from diagnostic_bundle import (
                generate_bundle, generate_github_url, build_summary,
                BASE_DIR as DB_BASE, LOG_DIR as DB_LOG, DATA_DIR as DB_DATA,
                WEB_DIR as DB_WEB
            )
            import diagnostic_bundle as db

            # Point diagnostic_bundle at our actual paths
            db.BASE_DIR = SCRIPT_DIR.parent
            db.LOG_DIR = OVERRIDE_FILE.parent  # Same as config.LOG_DIR
            db.DATA_DIR = DATA_DIR
            db.WEB_DIR = WEB_DIR_PATH

            # Generate the bundle — output into the logs directory
            # nginx serves /logs/ from the container's /logs/ mount (= host logs dir)
            output_dir = OVERRIDE_FILE.parent  # Same as config.LOG_DIR
            output_dir.mkdir(parents=True, exist_ok=True)

            zip_path = generate_bundle(
                hours=hours,
                max_log_lines=500,
                output_dir=output_dir,
            )

            # Build summary for preview
            summary = build_summary(hours)

            # Build GitHub issue URL
            github_url = generate_github_url(
                user_description=description,
                hours=hours,
            )

            # Return download URL (relative to web root) + metadata
            filename = zip_path.name
            download_url = f"logs/{filename}"

            log(f"  Diagnostic bundle generated: {filename} ({zip_path.stat().st_size / 1024:.1f} KB)")

            self._json_response(200, {
                'status': 'ok',
                'filename': filename,
                'download_url': download_url,
                'github_url': github_url,
                'summary': summary,
                'size_kb': round(zip_path.stat().st_size / 1024, 1),
            })

        except json.JSONDecodeError:
            self._json_response(400, {'error': 'Invalid JSON'})
        except ImportError as e:
            log(f"  Diagnostic bundle import error: {e}")
            self._json_response(500, {'error': f'diagnostic_bundle.py not found: {e}'})
        except Exception as e:
            log(f"  Diagnostic bundle error: {e}")
            import traceback
            traceback.print_exc()
            self._json_response(500, {'error': str(e)})

    def _get_telemetry_consent(self):
        """Return current telemetry consent status.

        Includes grid_region from Modbus (if available) so the dashboard
        can pre-select the country dropdown on the consent modal.
        """
        try:
            from telemetry_reporter import get_consent_status, _read_modbus_telemetry, _infer_grid_region
            status = get_consent_status()

            # If status is unknown (first load), try to detect grid region
            # from Modbus so the dashboard can pre-select the country picker
            if status.get('status') == 'unknown':
                try:
                    modbus_data = _read_modbus_telemetry()
                    if modbus_data:
                        freq = modbus_data.get('grid_frequency_hz')
                        volt = modbus_data.get('grid_voltage')
                        if freq:
                            status['grid_region'] = _infer_grid_region(freq, volt)
                except Exception:
                    pass

            self._json_response(200, status)
        except ImportError:
            self._json_response(200, {
                'status': 'unavailable',
                'consented': None,
                'source': None,
                'install_uuid': None,
            })
        except Exception as e:
            log(f"  Telemetry consent GET error: {e}")
            self._json_response(500, {'error': str(e)})

    def _set_telemetry_consent(self):
        """Set telemetry consent from dashboard modal.

        Accepts: {consent: bool, region: str|null}
        Region is the country code from the dropdown (e.g., "US", "CA", "AU").
        """
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length) if length > 0 else b'{}'
            payload = json.loads(body) if body else {}

            consent = payload.get('consent')
            if consent is None:
                self._json_response(400, {'error': 'Missing "consent" field (true/false)'})
                return

            region = payload.get('region')  # Country code or None

            from telemetry_reporter import set_consent
            record = set_consent(consented=bool(consent), source='dashboard', region=region)

            log(f"  Telemetry consent set: {'opted in' if consent else 'opted out'}"
                f"{' (region=' + region + ')' if region else ''}")
            self._json_response(200, {
                'status': 'ok',
                'consented': record['consented'],
                'install_uuid': record['install_uuid'],
            })

        except json.JSONDecodeError:
            self._json_response(400, {'error': 'Invalid JSON'})
        except ImportError as e:
            log(f"  Telemetry consent import error: {e}")
            self._json_response(500, {'error': 'telemetry_reporter.py not found'})
        except Exception as e:
            log(f"  Telemetry consent error: {e}")
            self._json_response(500, {'error': str(e)})

    def _get_system_readings(self):
        """Return recent system_readings from SQLite as JSON.
        Query params: ?limit=N (default 50, max 500)
        """
        try:
            import db as db_mod

            limit = 50
            if '?' in self.path:
                from urllib.parse import urlparse, parse_qs
                params = parse_qs(urlparse(self.path).query)
                limit = min(int(params.get('limit', ['50'])[0]), 500)

            rows = db_mod.get_recent_readings(limit=limit)
            rows.reverse()
            self._json_response(200, {'readings': rows, 'count': len(rows)})
        except ImportError:
            self._json_response(503, {'error': 'Database not available'})
        except Exception as e:
            self._json_response(500, {'error': str(e)})

    def _get_db_stats(self):
        """Return database table row counts and size."""
        try:
            import db as db_mod
            stats = db_mod.db_stats()
            self._json_response(200, stats)
        except ImportError:
            self._json_response(503, {'error': 'Database not available'})
        except Exception as e:
            self._json_response(500, {'error': str(e)})

    def _get_log_entries(self, log_type: str):
        """Return recent log entries from DB as JSON.
        Query params: ?limit=N (default 100, max 5000)
        """
        try:
            import db as db_mod

            limit = 100
            if '?' in self.path:
                from urllib.parse import urlparse, parse_qs
                params = parse_qs(urlparse(self.path).query)
                limit = min(int(params.get('limit', ['100'])[0]), 5000)

            if log_type == 'scheduler':
                rows = db_mod.get_recent_scheduler_logs(limit=limit)
            elif log_type == 'intelligence':
                rows = db_mod.get_recent_intelligence_logs(limit=limit)
            else:
                self._json_response(400, {'error': f'Unknown log type: {log_type}'})
                return

            rows.reverse()
            self._json_response(200, {'entries': rows, 'count': len(rows), 'type': log_type})
        except ImportError:
            self._json_response(503, {'error': 'Database not available'})
        except Exception as e:
            self._json_response(500, {'error': str(e)})

    def _get_chart_data(self):
        """Return system_readings + enphase_readings for charting.
        Query params:
          ?date=YYYY-MM-DD           (single day)
          ?start=YYYY-MM-DD&end=YYYY-MM-DD (date range)
        """
        try:
            import db as db_mod
            from urllib.parse import urlparse, parse_qs

            params = parse_qs(urlparse(self.path).query)
            date_str = params.get('date', [None])[0]
            start_str = params.get('start', [None])[0]
            end_str = params.get('end', [None])[0]

            if start_str and end_str:
                date_clause = "date(timestamp) BETWEEN ? AND ?"
                date_params = (start_str, end_str)
                weather_clause = "date BETWEEN ? AND ?"
                resp_date = f"{start_str}..{end_str}"
            elif date_str:
                date_clause = "date(timestamp) = ?"
                date_params = (date_str,)
                weather_clause = "date = ?"
                resp_date = date_str
            else:
                self._json_response(400, {'error': 'Missing ?date= or ?start=&end='})
                return

            readings = db_mod.query(
                "SELECT timestamp, soc_pct, solar_kw, grid_kw, battery_kw, "
                "home_load_kw, mode "
                f"FROM system_readings WHERE {date_clause} ORDER BY timestamp",
                date_params
            )

            enphase = db_mod.query(
                "SELECT timestamp, inverter_sum_w, meter_w, curtailed_w "
                "FROM enphase_readings "
                f"WHERE {date_clause} AND array_id = 'house' ORDER BY timestamp",
                date_params
            )

            weather = db_mod.query(
                "SELECT timestamp, temp_f, humidity, solar_radiation_wm2 "
                f"FROM weather_observations WHERE {date_clause} ORDER BY timestamp",
                date_params
            )

            weather_daily = db_mod.query(
                f"SELECT date, temp_high, temp_low, temp_avg, humidity_avg, "
                "solar_radiation_high, precip_total, observation_count "
                f"FROM weather_daily WHERE {weather_clause} ORDER BY date",
                date_params
            )

            self._json_response(200, {
                'date': resp_date,
                'system_readings': [dict(r) for r in readings] if readings else [],
                'enphase_readings': [dict(r) for r in enphase] if enphase else [],
                'weather_observations': [dict(r) for r in weather] if weather else [],
                'weather_daily': [dict(r) for r in weather_daily] if weather_daily else [],
            })
        except ImportError:
            self._json_response(503, {'error': 'Database not available'})
        except Exception as e:
            self._json_response(500, {'error': str(e)})

    def _get_decision_log(self):
        """Return intelligence_log entries for charting.
        Query params:
          ?date=YYYY-MM-DD or ?start=YYYY-MM-DD&end=YYYY-MM-DD
          ?filter=decisions (default) | all
        """
        try:
            import db as db_mod
            from urllib.parse import urlparse, parse_qs

            params = parse_qs(urlparse(self.path).query)
            date_str = params.get('date', [None])[0]
            start_str = params.get('start', [None])[0]
            end_str = params.get('end', [None])[0]
            filt = params.get('filter', ['decisions'])[0]

            if start_str and end_str:
                date_clause = "date(timestamp) BETWEEN ? AND ?"
                date_params = (start_str, end_str)
            elif date_str:
                date_clause = "date(timestamp) = ?"
                date_params = (date_str,)
            else:
                self._json_response(400, {'error': 'Missing ?date= or ?start=&end='})
                return

            if filt == 'all':
                where = date_clause
            else:
                where = f"{date_clause} AND message LIKE '%Decision:%'"

            rows = db_mod.query(
                f"SELECT timestamp, message FROM intelligence_log "
                f"WHERE {where} ORDER BY timestamp",
                date_params
            )

            self._json_response(200, {
                'date': date_str or f"{start_str}..{end_str}",
                'decisions': [dict(r) for r in rows] if rows else [],
            })
        except ImportError:
            self._json_response(503, {'error': 'Database not available'})
        except Exception as e:
            self._json_response(500, {'error': str(e)})

    def _get_chart_dates(self):
        """Return list of dates that have system_readings data."""
        try:
            import db as db_mod

            rows = db_mod.query(
                "SELECT date(timestamp) as d, COUNT(*) as cnt "
                "FROM system_readings WHERE soc_pct IS NOT NULL "
                "GROUP BY d ORDER BY d DESC LIMIT 90"
            )

            self._json_response(200, {
                'dates': [{'date': r['d'], 'readings': r['cnt']} for r in rows] if rows else [],
            })
        except ImportError:
            self._json_response(503, {'error': 'Database not available'})
        except Exception as e:
            self._json_response(500, {'error': str(e)})

    def _get_peak_config(self):
        """Return TOU peak configuration for chart rendering."""
        try:
            tou_enabled = getattr(config, 'TOU_ENABLED', False) if CONFIG_LOADED else False
            peak_start = getattr(config, 'PEAK_START_HOUR', 17) if CONFIG_LOADED else 17
            peak_end = getattr(config, 'PEAK_END_HOUR', 20) if CONFIG_LOADED else 20
            peak_days = getattr(config, 'PEAK_DAYS', 'weekdays') if CONFIG_LOADED else 'weekdays'

            self._json_response(200, {
                'tou_enabled': tou_enabled,
                'peak_start_hour': peak_start,
                'peak_end_hour': peak_end,
                'peak_days': peak_days,
            })
        except Exception as e:
            self._json_response(500, {'error': str(e)})

    def _save_layout(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            payload = json.loads(body)

            array_id = payload.get('array_id', 'default')
            layout = payload.get('layout', {})

            # Validate
            if not array_id or not isinstance(layout, dict):
                self._json_response(400, {'error': 'Invalid payload'})
                return

            # Sanitize array_id (alphanumeric + underscore only)
            safe_id = ''.join(c for c in array_id if c.isalnum() or c == '_')
            if not safe_id:
                self._json_response(400, {'error': 'Invalid array_id'})
                return

            # Save to data directory
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            layout_file = DATA_DIR / f"enphase_array_layout_{safe_id}.json"
            with open(layout_file, 'w') as f:
                json.dump(layout, f, indent=2)

            log(f"  Layout saved: {layout_file.name} "
                f"({len(layout.get('arrays', [{}])[0].get('rows', []))} rows)")

            # Trigger a fresh collection so layout takes effect immediately
            solar_arrays = getattr(config, 'SOLAR_ARRAYS', '') if CONFIG_LOADED else ''
            if solar_arrays and safe_id in solar_arrays:
                array_type = os.getenv(
                    f'SOLAR_ARRAY_{safe_id.upper()}_TYPE', 'enphase')
                if array_type == 'enphase':
                    threading.Thread(
                        target=run_script_with_args,
                        args=("collect_solar_enphase.py",
                              ["--array-id", safe_id],
                              f"Solar [{safe_id}] (layout refresh)"),
                        daemon=True
                    ).start()

            self._json_response(200, {'status': 'ok', 'file': layout_file.name})

        except json.JSONDecodeError:
            self._json_response(400, {'error': 'Invalid JSON'})
        except Exception as e:
            log(f"  API error: {e}")
            self._json_response(500, {'error': str(e)})


def start_api_server():
    """Start the internal API server in a background thread."""
    try:
        server = HTTPServer(('0.0.0.0', API_PORT), APIHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        log(f"  API server listening on port {API_PORT}")
    except Exception as e:
        log(f"  WARNING: Could not start API server: {e}")


def main():
    """Main entry point."""
    if CONFIG_LOADED:
        configure_logging()
    try:
        import db as db_mod
        db_mod.init_db()
    except Exception:
        pass
    start_api_server()
    setup_schedule()
    
    # Run the scheduler loop
    while True:
        try:
            schedule.run_pending()
            time.sleep(10)  # Check every 10 seconds
        except KeyboardInterrupt:
            log("Scheduler stopped by user")
            break
        except Exception as e:
            log(f"Scheduler error: {e}")
            time.sleep(60)  # Wait a minute before retrying


if __name__ == "__main__":
    main()
