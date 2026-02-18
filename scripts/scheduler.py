#!/usr/bin/env python3
"""
FranklinWH Automation Scheduler - v3.5.0

Master scheduler that runs all automation tasks on their configured intervals.
This allows the Docker container to be fully self-contained - no external
cron or Task Scheduler needed.

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
- collect_weather.py: Every 15 minutes (if WEATHER_ENABLED)
- collect_pvoutput.py: Every hour (if PVOUTPUT_ENABLED)
- collect_enphase.py: Every 5 minutes per array (if SOLAR_ARRAYS configured)
- daily_status_report.py: Daily at 4:30 PM (if EMAIL_ENABLED)
- generate_weekly_charts.py: Weekly on Sunday at 2:00 AM
- calculate_daily_savings.py: Daily at 00:05 AM (previous day)

API:
- Internal HTTP server on port 8101 for dashboard operations
- POST /api/save-layout: Save solar array layout JSON
- POST /api/override: Activate manual mode override
- DELETE /api/override: Cancel active override
- GET /api/override: Get current override status
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
    from config import config
    CONFIG_LOADED = True
except ImportError:
    print("Warning: Could not load config, using defaults")
    CONFIG_LOADED = False


def log(message: str):
    """Print timestamped log message and write to log file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    print(log_line, flush=True)
    
    # Also write to log file
    try:
        log_file = SCRIPT_DIR.parent / "logs" / "scheduler.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, 'a') as f:
            f.write(log_line + "\n")
    except Exception:
        pass  # Don't fail if we can't write to log file


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
    """Weather collection - runs every 15 minutes if enabled."""
    if CONFIG_LOADED and config.WEATHER_ENABLED:
        run_script("collect_weather.py", "Weather Collection")
    else:
        pass  # Silently skip if not enabled


def job_pvoutput():
    """PVOutput collection - runs hourly if enabled."""
    if CONFIG_LOADED and config.PVOUTPUT_ENABLED:
        run_script("collect_pvoutput.py", "PVOutput Collection")
    else:
        pass  # Silently skip if not enabled


def job_solar_arrays():
    """Solar array data collection - runs every 5 minutes if any arrays configured."""
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
                    "collect_enphase.py",
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
        run_script("collect_enphase.py", "Enphase Solar Collection")


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


def format_time(hour: int, minute: int) -> str:
    """Format hour:minute as HH:MM string for schedule library."""
    return f"{hour:02d}:{minute:02d}"


def setup_schedule():
    """Configure all scheduled tasks."""
    log("=" * 60)
    log("FranklinWH Automation Scheduler v3.5.0")
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
        log(f"  - Home Mode: {config.HOME_MODE}")
    
    log("-" * 60)
    log("Scheduling tasks:")
    
    # Core automation - clock-aligned at :00 and :30 every hour
    # Fixed at 30-minute intervals to stay within Franklin API rate limits.
    # Clock-aligned ensures predictable timing regardless of container start.
    schedule.every().hour.at(":00").do(job_smart_decision)
    schedule.every().hour.at(":30").do(job_smart_decision)
    log(f"  - Smart Decision: Every 30 minutes (clock-aligned at :00 and :30)")
    
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
        log(f"  - Pre-peak check: Daily at {pre_peak_time} ({buffer_minutes}min before peak)")
        
        # Post-peak check: PEAK_END hour + 1 minute
        post_peak_time = format_time(config.PEAK_END_HOUR, 1)
        schedule.every().day.at(post_peak_time).do(job_smart_decision_peak_end)
        log(f"  - Post-peak check: Daily at {post_peak_time} (1min after peak ends)")
        
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
            log(f"  - Pre-peak2 check: Daily at {pre_peak2_time}")
            
            post_peak2_time = format_time(config.PEAK2_END_HOUR, 1)
            schedule.every().day.at(post_peak2_time).do(job_smart_decision_peak_end)
            log(f"  - Post-peak2 check: Daily at {post_peak2_time}")
    
    # Dashboard data - every minute
    schedule.every(1).minutes.do(job_dashboard_data)
    log("  - Dashboard Data: Every 1 minute")
    
    # Weather - every 15 minutes (if enabled)
    if CONFIG_LOADED and config.WEATHER_ENABLED:
        schedule.every(15).minutes.do(job_weather)
        log("  - Weather Collection: Every 15 minutes")
    
    # PVOutput - hourly (if enabled)
    if CONFIG_LOADED and config.PVOUTPUT_ENABLED:
        schedule.every().hour.at(":05").do(job_pvoutput)
        log("  - PVOutput Collection: Hourly at :05")
    
    # Solar arrays - every 5 minutes (if any configured)
    solar_arrays = getattr(config, 'SOLAR_ARRAYS', '') if CONFIG_LOADED else ''
    enphase_legacy = getattr(config, 'ENPHASE_ENABLED', False) if CONFIG_LOADED else False
    if solar_arrays or enphase_legacy:
        schedule.every(5).minutes.do(job_solar_arrays)
        if solar_arrays:
            log(f"  - Solar Array Collection: Every 5 minutes ({solar_arrays})")
        else:
            log("  - Enphase Solar Collection: Every 5 minutes (legacy)")
    
    # SolarEdge panel monitoring - every 15 minutes (if enabled)
    # Separate from the solar array collection above; this collects real
    # per-optimizer energy data for health monitoring / anomaly detection
    if CONFIG_LOADED and getattr(config, 'SOLAREDGE_PANEL_MONITORING', False):
        schedule.every(15).minutes.do(job_solaredge_panels)
        log(f"  - SolarEdge Panel Monitoring: Every 15 minutes (site {config.SOLAREDGE_SITE_ID})")
    
    # Daily report - 4:30 PM (if email enabled)
    if CONFIG_LOADED and getattr(config, 'EMAIL_ENABLED', False):
        schedule.every().day.at("16:30").do(job_daily_report)
        log("  - Daily Status Report: Daily at 4:30 PM")
    
    # Daily savings - 00:05 AM (calculates previous day's savings with full data)
    schedule.every().day.at("00:05").do(job_daily_savings)
    log("  - Daily Savings: Daily at 00:05 AM (previous day)")
    
    # Solar health monitor - 20:30 (after sunset, full day's data available)
    # Runs for all solar arrays regardless of type (SolarEdge, Enphase, or both)
    solar_arrays = getattr(config, 'SOLAR_ARRAYS', '') if CONFIG_LOADED else ''
    enphase_legacy_health = getattr(config, 'ENPHASE_ENABLED', False) if CONFIG_LOADED else False
    se_panel_health = getattr(config, 'SOLAREDGE_PANEL_MONITORING', False) if CONFIG_LOADED else False
    if solar_arrays or enphase_legacy_health or se_panel_health:
        schedule.every().day.at("20:30").do(job_solar_health)
        log("  - Solar Health Monitor: Daily at 8:30 PM")
    
    # Weekly charts - Sunday 2:00 AM
    schedule.every().sunday.at("02:00").do(job_weekly_charts)
    log("  - Weekly Charts: Sunday at 2:00 AM")
    
    log("-" * 60)
    log("Scheduler ready. Running initial tasks...")
    
    # Run core tasks immediately on startup
    job_smart_decision()
    job_dashboard_data()
    
    log("-" * 60)
    log("Initial tasks complete. Entering scheduled loop...")
    log("=" * 60)


# ===== Internal API Server =====
API_PORT = int(os.getenv('API_PORT', '8101'))
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
        mode_obj.soc = 100
    elif mode_name == 'self_consumption':
        mode_obj = Mode.self_consumption()
        mode_obj.soc = 20
    elif mode_name == 'time_of_use':
        mode_obj = Mode.time_of_use()
        mode_obj.soc = 20
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
        if self.path == '/api/override':
            self._get_override()
        else:
            self._json_response(404, {'error': 'Not found'})

    def do_POST(self):
        if self.path == '/api/save-layout':
            self._save_layout()
        elif self.path == '/api/override':
            self._set_override()
        elif self.path == '/api/override/cancel':
            self._cancel_override()
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
        # Check if expired
        if ov.get('active') and ov.get('expires_at'):
            expires = datetime.fromisoformat(ov['expires_at'])
            if datetime.now() >= expires:
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

            if mode not in ('emergency_backup', 'self_consumption', 'time_of_use'):
                self._json_response(400, {'error': f'Invalid mode: {mode}'})
                return

            # Calculate expiry
            dur_map = {'1h': 60, '2h': 120, '4h': 240, '8h': 480, 'until_cancel': None}
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
            write_override(ov)
            log(f"  Override activated: {mode} for {duration}")

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
                        args=("collect_enphase.py",
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
