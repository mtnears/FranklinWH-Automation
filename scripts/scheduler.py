#!/usr/bin/env python3
"""
FranklinWH Automation Scheduler

Master scheduler that runs all automation tasks on their configured intervals.
This allows the Docker container to be fully self-contained - no external
cron or Task Scheduler needed.

Tasks:
- smart_decision.py: Every 15 minutes (core automation)
- generate_dashboard_data.py: Every 1 minute (dashboard updates)
- collect_weather.py: Every 15 minutes (if WEATHER_ENABLED)
- collect_pvoutput.py: Every hour (if PVOUTPUT_ENABLED)
- calculate_daily_savings.py: Daily at 12:30 AM (savings tracking)
- daily_status_report.py: Daily at 4:30 PM (if EMAIL_ENABLED)
- generate_weekly_charts.py: Weekly on Sunday at 2:00 AM
"""
import schedule
import time
import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path

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


def run_script(script_name: str, description: str, extra_args: list = None):
    """Run a Python script and log the result."""
    script_path = SCRIPT_DIR / script_name
    
    if not script_path.exists():
        log(f"⚠ {description}: Script not found ({script_name})")
        return False
    
    log(f"▶ {description}: Starting...")
    
    try:
        cmd = [sys.executable, str(script_path)]
        if extra_args:
            cmd.extend(extra_args)

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
            cwd=str(SCRIPT_DIR)
        )
        
        if result.returncode == 0:
            log(f"✓ {description}: Completed successfully")
            if result.stdout.strip():
                # Print last line of output as summary
                last_line = result.stdout.strip().split('\n')[-1]
                log(f"  └─ {last_line}")
            return True
        else:
            log(f"✗ {description}: Failed (exit code {result.returncode})")
            if result.stderr.strip():
                log(f"  └─ Error: {result.stderr.strip()[:200]}")
            return False
            
    except subprocess.TimeoutExpired:
        log(f"✗ {description}: Timed out after 5 minutes")
        return False
    except Exception as e:
        log(f"✗ {description}: Exception - {e}")
        return False


def job_smart_decision():
    """Core automation - runs every 15 minutes."""
    run_script("smart_decision.py", "Smart Decision")


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


def job_daily_savings():
    """Daily savings calculation - runs at 12:30 AM for yesterday's data."""
    run_script("calculate_daily_savings.py", "Daily Savings", ["--yesterday", "--quiet"])


def job_daily_report():
    """Daily status report - runs at 4:30 PM if email enabled."""
    if CONFIG_LOADED and getattr(config, 'EMAIL_ENABLED', False):
        run_script("daily_status_report.py", "Daily Status Report")
    else:
        pass  # Silently skip if not enabled


def job_weekly_charts():
    """Weekly charts - runs Sunday at 2:00 AM."""
    run_script("generate_weekly_charts.py", "Weekly Charts")


def setup_schedule():
    """Configure all scheduled tasks."""
    log("=" * 60)
    log("FranklinWH Automation Scheduler Starting")
    log("=" * 60)
    
    # Show enabled features
    if CONFIG_LOADED:
        log("Enabled features:")
        log(f"  • Solar: {config.SOLAR_ENABLED}")
        log(f"  • TOU: {config.TOU_ENABLED}")
        log(f"  • Dynamic Pricing: {config.DYNAMIC_PRICING_ENABLED}")
        log(f"  • Weather: {config.WEATHER_ENABLED}")
        log(f"  • PVOutput: {config.PVOUTPUT_ENABLED}")
        log(f"  • Email: {getattr(config, 'EMAIL_ENABLED', False)}")
    
    log("-" * 60)
    log("Scheduling tasks:")
    
    # Core automation - every 15 minutes
    schedule.every(15).minutes.do(job_smart_decision)
    log("  • Smart Decision: Every 15 minutes")
    
    # Dashboard data - every minute
    schedule.every(1).minutes.do(job_dashboard_data)
    log("  • Dashboard Data: Every 1 minute")
    
    # Weather - every 15 minutes (if enabled)
    if CONFIG_LOADED and config.WEATHER_ENABLED:
        schedule.every(15).minutes.do(job_weather)
        log("  • Weather Collection: Every 15 minutes")
    
    # PVOutput - hourly (if enabled)
    if CONFIG_LOADED and config.PVOUTPUT_ENABLED:
        schedule.every().hour.at(":05").do(job_pvoutput)
        log("  • PVOutput Collection: Hourly at :05")
    
    # Daily savings - 12:30 AM (after midnight so yesterday's data is complete)
    schedule.every().day.at("00:30").do(job_daily_savings)
    log("  • Daily Savings: Daily at 12:30 AM")
    
    # Daily report - 4:30 PM (if email enabled)
    if CONFIG_LOADED and getattr(config, 'EMAIL_ENABLED', False):
        schedule.every().day.at("16:30").do(job_daily_report)
        log("  • Daily Status Report: Daily at 4:30 PM")
    
    # Weekly charts - Sunday 2:00 AM
    schedule.every().sunday.at("02:00").do(job_weekly_charts)
    log("  • Weekly Charts: Sunday at 2:00 AM")
    
    log("-" * 60)
    log("Scheduler ready. Running initial tasks...")
    
    # Run core tasks immediately on startup
    job_smart_decision()
    job_dashboard_data()
    
    log("-" * 60)
    log("Initial tasks complete. Entering scheduled loop...")
    log("=" * 60)


def main():
    """Main entry point."""
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
