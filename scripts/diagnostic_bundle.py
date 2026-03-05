#!/usr/bin/env python3
"""
diagnostic_bundle.py — Generate sanitized diagnostic bundles for issue reporting.

Collects system state, recent decisions, configuration, and logs into a
downloadable zip file. Strips credentials, IPs, and personal info.

Can be run standalone (CLI) or called from the scheduler API endpoint.

Part of the FranklinWH Battery Automation v4.0.
"""

import csv
import io
import json
import os
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Base paths — use config if available, else Docker defaults
try:
    from config import config as _cfg
    BASE_DIR = _cfg.BASE_DIR
    LOG_DIR = _cfg.LOG_DIR
    DATA_DIR = _cfg.DATA_DIR
    WEB_DIR = _cfg.WEB_DIR
except ImportError:
    BASE_DIR = Path(os.getenv('BASE_DIR', '/app'))
    LOG_DIR = BASE_DIR / 'logs'
    DATA_DIR = BASE_DIR / 'data'
    WEB_DIR = BASE_DIR / 'web'


def get_cloud_mode_debug() -> str:
    """Query the Franklin cloud API for mode detection debug info.
    
    Returns the raw status fields needed to diagnose mode detection issues:
    run_status, name, mode, and other state fields.
    Masks gateway ID and credentials. Returns error message on failure.
    """
    try:
        import asyncio
        from franklinwh import Client, TokenFetcher
        
        username = os.environ.get('FRANKLIN_USERNAME', '')
        password = os.environ.get('FRANKLIN_PASSWORD', '')
        gateway = os.environ.get('FRANKLIN_GATEWAY_ID', '')
        
        if not all([username, password, gateway]):
            return "[Cloud API credentials not configured]"
        
        async def _query():
            fetcher = TokenFetcher(username, password)
            client = Client(fetcher, gateway)
            status = await client._status()
            
            # Extract mode-relevant fields
            lines = ["=== Cloud API Mode Detection Debug ==="]
            mode_keys = ['mode', 'name', 'run_status', 'workMode',
                         'bms_work', 'elecnet_state', 'slaver_stat',
                         'pe_stat', 'genStat', 'v2lModeEnable', 'v2lRunState',
                         'infi_status', 'bms_heat_state']
            
            for k in mode_keys:
                if k in status:
                    lines.append(f"  {k}: {status[k]}")
            
            # Also capture any other keys with 'mode', 'stat', 'run', 'work' in name
            for k, v in sorted(status.items()):
                if k not in mode_keys and any(w in k.lower() for w in ['mode', 'stat', 'run', 'work']):
                    lines.append(f"  {k}: {v}")
            
            return '\n'.join(lines)
        
        return asyncio.run(_query())
    except Exception as e:
        return f"[Cloud API mode query failed: {e}]"

# Files to include (relative to BASE_DIR)
# Log files retired — intelligence_log and scheduler_log now in SQLite DB
LOG_FILES = {}

DATA_FILES = {
    'override.json': LOG_DIR / 'override.json',
    'rate_schedule.json': DATA_DIR / 'rate_schedule.json',
    'power_dashboard_data.json': WEB_DIR / 'power_dashboard_data.json',
}

# Patterns to sanitize
SANITIZE_PATTERNS = [
    # API keys, passwords, tokens
    (re.compile(r'(password|passwd|api_key|token|secret|apikey)\s*[=:]\s*\S+', re.IGNORECASE), r'\1=***REDACTED***'),
    # Email addresses
    (re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+'), '[EMAIL_REDACTED]'),
    # IP addresses (preserve localhost/LAN structure but mask last octets for WAN)
    (re.compile(r'\b(\d{1,3}\.\d{1,3}\.)\d{1,3}\.\d{1,3}\b'), r'\1*.*'),
    # Franklin gateway IDs (preserve first/last 4 chars)
    (re.compile(r'(gateway[_-]?id\s*[=:]\s*)([A-Za-z0-9]{4})[A-Za-z0-9]+([A-Za-z0-9]{4})', re.IGNORECASE), r'\1\2****\3'),
    # Franklin username
    (re.compile(r'(FRANKLIN_USERNAME\s*=\s*)\S+', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(FRANKLIN_PASSWORD\s*=\s*)\S+', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(FRANKLIN_GATEWAY_ID\s*=\s*)([A-Za-z0-9]{4})[A-Za-z0-9]+([A-Za-z0-9]{4})', re.IGNORECASE), r'\1\2****\3'),
]


def sanitize_text(text: str) -> str:
    """Apply all sanitization patterns to text."""
    for pattern, replacement in SANITIZE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def tail_file(filepath: Path, max_lines: int = 200) -> str:
    """Read the last N lines of a file."""
    try:
        with open(filepath, 'r', errors='replace') as f:
            lines = f.readlines()
            return ''.join(lines[-max_lines:])
    except (FileNotFoundError, PermissionError) as e:
        return f"[Could not read: {e}]"


def tail_csv(filepath: Path, max_rows: int = 50) -> str:
    """Read the last N rows of a CSV file, preserving header."""
    try:
        with open(filepath, 'r', errors='replace') as f:
            lines = f.readlines()
            if len(lines) <= 1:
                return ''.join(lines)
            header = lines[0]
            data_lines = lines[-max_rows:]
            return header + ''.join(data_lines)
    except (FileNotFoundError, PermissionError) as e:
        return f"[Could not read: {e}]"


def read_json_safe(filepath: Path) -> str:
    """Read a JSON file and return formatted content."""
    try:
        with open(filepath, 'r') as f:
            data = json.load(f)
            return json.dumps(data, indent=2)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError) as e:
        return f"[Could not read: {e}]"


def get_env_structure(filepath: Path = None) -> str:
    """Read .env file and return config with safe values exposed.
    
    Sensitive keys (passwords, emails, API keys, tokens) are masked.
    Non-sensitive config keys show actual values for diagnostics.
    """
    if filepath is None:
        filepath = BASE_DIR / '.env'
    
    # Keys whose VALUES should be masked (contain credentials or PII)
    sensitive_patterns = [
        'password', 'passwd', 'secret', 'token', 'key', 'api_key',
        'email', 'username', 'user', 'sender', 'recipient',
        'gateway_id', 'site_id',
    ]
    
    def is_sensitive(key: str) -> bool:
        key_lower = key.lower()
        return any(pat in key_lower for pat in sensitive_patterns)
    
    try:
        lines = []
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    lines.append(line)
                    continue
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    if is_sensitive(key):
                        lines.append(f"{key}=***")
                    else:
                        lines.append(f"{key}={value}")
                else:
                    lines.append(line)
        return '\n'.join(lines)
    except (FileNotFoundError, PermissionError) as e:
        return f"[Could not read: {e}]"


def get_system_info() -> dict:
    """Collect non-sensitive system information."""
    info = {
        'timestamp': datetime.now().isoformat(),
        'python_version': None,
        'os_info': None,
        'uptime': None,
        'disk_usage': None,
    }
    try:
        import sys
        info['python_version'] = sys.version
    except Exception:
        pass

    try:
        import platform
        info['os_info'] = f"{platform.system()} {platform.release()} ({platform.machine()})"
    except Exception:
        pass

    try:
        import shutil
        usage = shutil.disk_usage(str(BASE_DIR))
        info['disk_usage'] = {
            'total_gb': round(usage.total / (1024**3), 1),
            'free_gb': round(usage.free / (1024**3), 1),
            'used_pct': round((usage.used / usage.total) * 100, 1),
        }
    except Exception:
        pass

    return info


def extract_recent_decisions(log_path: Path, max_decisions: int = 20) -> str:
    """Extract the most recent decision cycles from the intelligence log."""
    try:
        with open(log_path, 'r', errors='replace') as f:
            lines = f.readlines()

        # Find decision lines (contain "Decision:" or "[v4")
        decision_blocks = []
        current_block = []
        in_block = False

        for line in lines:
            if '=' * 20 in line:
                if current_block:
                    decision_blocks.append(''.join(current_block))
                current_block = [line]
                in_block = True
            elif in_block:
                current_block.append(line)

        if current_block:
            decision_blocks.append(''.join(current_block))

        # Return last N blocks
        recent = decision_blocks[-max_decisions:]
        return ''.join(recent)
    except (FileNotFoundError, PermissionError) as e:
        return f"[Could not read: {e}]"


def extract_errors(log_path: Path, hours: int = 24) -> str:
    """Extract error lines from the last N hours."""
    try:
        cutoff = datetime.now() - timedelta(hours=hours)
        errors = []

        with open(log_path, 'r', errors='replace') as f:
            for line in f:
                if 'ERROR' in line or 'FAIL' in line or 'Exception' in line:
                    # Try to parse timestamp
                    try:
                        ts_str = line[:19]
                        ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                        if ts >= cutoff:
                            errors.append(line)
                    except (ValueError, IndexError):
                        errors.append(line)

        if not errors:
            return "[No errors found in the last {} hours]".format(hours)
        return ''.join(errors[-100:])  # Cap at 100 error lines
    except (FileNotFoundError, PermissionError) as e:
        return f"[Could not read: {e}]"


def build_summary(hours: int = 24) -> str:
    """Build a human-readable summary for the GitHub issue body."""
    now = datetime.now()
    summary_parts = [
        f"**Diagnostic Bundle** — {now.strftime('%Y-%m-%d %H:%M')}",
        f"**Time Range:** Last {hours} hours",
        "",
    ]

    # System info
    info = get_system_info()
    summary_parts.append("**System:**")
    summary_parts.append(f"- Python: {info.get('python_version', 'unknown')}")
    summary_parts.append(f"- OS: {info.get('os_info', 'unknown')}")
    if info.get('disk_usage'):
        du = info['disk_usage']
        summary_parts.append(f"- Disk: {du['used_pct']}% used ({du['free_gb']} GB free)")
    summary_parts.append("")

    # Current state from dashboard data
    dash_path = WEB_DIR / 'power_dashboard_data.json'
    try:
        with open(dash_path, 'r') as f:
            dash = json.load(f)
        summary_parts.append("**Current State:**")
        summary_parts.append(f"- SOC: {dash.get('soc_percent', '?')}%")
        summary_parts.append(f"- Mode: {dash.get('mode_name', '?')}")
        summary_parts.append(f"- Solar: {dash.get('solar_kw', '?')} kW")
        summary_parts.append(f"- Grid: {dash.get('grid_kw', '?')} kW")
        summary_parts.append(f"- Data Source: {dash.get('data_source', '?')}")
        if dash.get('engine_version'):
            summary_parts.append(f"- Engine: {dash.get('engine_version')}")
    except Exception:
        summary_parts.append("**Current State:** [Dashboard data unavailable]")

    summary_parts.append("")

    # Recent errors from DB
    try:
        import db as db_mod
        cutoff = (now - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
        err_rows = db_mod.query(
            "SELECT COUNT(*) as cnt FROM intelligence_log "
            "WHERE timestamp >= ? AND (level = 'ERROR' OR message LIKE '%ERROR%' "
            "OR message LIKE '%FAIL%' OR message LIKE '%Exception%')", (cutoff,))
        error_count = err_rows[0]['cnt'] if err_rows else 0
        if error_count == 0:
            summary_parts.append("**Errors (last {}h):** None".format(hours))
        else:
            summary_parts.append(f"**Errors (last {hours}h):** {error_count} (see attached bundle)")
    except Exception:
        summary_parts.append("**Errors (last {}h):** Unable to query DB".format(hours))

    summary_parts.append("")
    summary_parts.append("**Full diagnostic data is in the attached zip file.**")
    summary_parts.append("*(Bundle was auto-generated by the FranklinWH diagnostic tool. "
                         "Credentials and personal info have been stripped.)*")

    return '\n'.join(summary_parts)


def generate_bundle(hours: int = 24, max_log_lines: int = 500,
                    output_dir: Path = None) -> Path:
    """Generate a complete diagnostic bundle as a zip file.

    Args:
        hours: How many hours of history to include
        max_log_lines: Maximum lines from each log file
        output_dir: Where to save the zip (defaults to LOG_DIR)

    Returns:
        Path to the generated zip file
    """
    if output_dir is None:
        output_dir = LOG_DIR

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    zip_filename = f"diagnostic_bundle_{timestamp}.zip"
    zip_path = output_dir / zip_filename

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:

        # 1. Summary
        summary = build_summary(hours)
        zf.writestr('SUMMARY.md', sanitize_text(summary))

        # 2. System info
        sys_info = get_system_info()
        zf.writestr('system_info.json', json.dumps(sys_info, indent=2))

        # 3. Intelligence log from DB (last N entries)
        try:
            import db as db_mod
            db_mod.init_db()
            intel_rows = db_mod.get_recent_intelligence_logs(limit=max_log_lines)
            if intel_rows:
                intel_content = '\n'.join(
                    f"{r['timestamp']} - {r['message']}" for r in reversed(intel_rows))
                zf.writestr('logs/intelligence_log.txt', sanitize_text(intel_content))
        except Exception:
            pass

        # 4. Recent decisions from DB
        try:
            import db as db_mod
            rows = db_mod.query(
                "SELECT timestamp, message FROM intelligence_log "
                "WHERE message LIKE 'Decision:%' ORDER BY id DESC LIMIT 30")
            if rows:
                decisions_text = '\n'.join(
                    f"{r['timestamp']} - {r['message']}" for r in reversed(rows))
                zf.writestr('logs/recent_decisions.txt', sanitize_text(decisions_text))
        except Exception:
            pass

        # 5. Errors from DB
        try:
            import db as db_mod
            cutoff = (datetime.now() - timedelta(hours=hours)).strftime('%Y-%m-%d %H:%M:%S')
            rows = db_mod.query(
                "SELECT timestamp, level, message FROM intelligence_log "
                "WHERE timestamp >= ? AND (level = 'ERROR' OR message LIKE '%ERROR%' "
                "OR message LIKE '%FAIL%' OR message LIKE '%Exception%') "
                "ORDER BY id DESC LIMIT 100", (cutoff,))
            if rows:
                error_text = '\n'.join(
                    f"{r['timestamp']} [{r.get('level','')}] {r['message']}" for r in reversed(rows))
                zf.writestr('logs/errors.txt', sanitize_text(error_text))
        except Exception:
            pass

        # 6. Scheduler log from DB
        try:
            import db as db_mod
            sched_rows = db_mod.get_recent_scheduler_logs(limit=max_log_lines)
            if sched_rows:
                sched_content = '\n'.join(
                    f"[{r['timestamp']}] {r['message']}" for r in reversed(sched_rows))
                zf.writestr('logs/scheduler_log.txt', sanitize_text(sched_content))
        except Exception:
            pass

        # 7. Recent system readings from DB (last 100 rows)
        try:
            import db as db_mod
            readings = db_mod.get_readings_since(hours_ago=24)
            if readings:
                cols = ['timestamp', 'soc_pct', 'solar_kw', 'grid_kw', 'battery_kw', 'home_load_kw', 'mode']
                header = ','.join(cols)
                data_lines = []
                for r in readings[-100:]:
                    data_lines.append(','.join(str(r.get(c, '')) for c in cols))
                zf.writestr('logs/system_readings_recent.csv', header + '\n' + '\n'.join(data_lines))
        except Exception:
            pass

        # 8. Configuration (.env structure — keys only)
        env_structure = get_env_structure()
        zf.writestr('config/env_structure.txt', env_structure)

        # 9. Rate schedule
        rate_path = DATA_DIR / 'rate_schedule.json'
        if rate_path.exists():
            content = read_json_safe(rate_path)
            zf.writestr('config/rate_schedule.json', content)

        # 10. Override state
        override_path = LOG_DIR / 'override.json'
        if override_path.exists():
            content = read_json_safe(override_path)
            zf.writestr('state/override.json', sanitize_text(content))

        # 11. Dashboard data snapshot
        dash_path = WEB_DIR / 'power_dashboard_data.json'
        if dash_path.exists():
            content = read_json_safe(dash_path)
            zf.writestr('state/dashboard_snapshot.json', sanitize_text(content))

        # 12. Data source health
        health_path = LOG_DIR / 'data_source_health.json'
        if health_path.exists():
            content = read_json_safe(health_path)
            zf.writestr('state/data_source_health.json', sanitize_text(content))

        # 13. Last mode switch record
        switch_path = LOG_DIR / 'last_mode_switch.txt'
        if switch_path.exists():
            try:
                with open(switch_path, 'r') as f:
                    content = f.read()
                zf.writestr('state/last_mode_switch.txt', content)
            except Exception:
                pass

        # 14. Cloud API mode detection debug
        # Captures raw run_status, name, mode fields for diagnosing mode detection issues
        try:
            mode_debug = get_cloud_mode_debug()
            zf.writestr('state/cloud_mode_debug.txt', sanitize_text(mode_debug))
        except Exception as e:
            zf.writestr('state/cloud_mode_debug.txt', f'[Error collecting: {e}]')

    # Write to disk
    with open(zip_path, 'wb') as f:
        f.write(buf.getvalue())

    return zip_path


def generate_github_url(user_description: str = "", hours: int = 24,
                        repo: str = "mtnears/FranklinWH-Automation") -> str:
    """Generate a pre-filled GitHub issue URL.

    The summary is kept short to fit in URL length limits.
    User attaches the zip file manually.
    """
    import urllib.parse

    now = datetime.now()
    title = f"[Bug Report] {user_description[:80]}" if user_description else f"[Bug Report] Issue reported {now.strftime('%Y-%m-%d %H:%M')}"

    # Build concise body for URL (keep under ~4KB)
    body_parts = [
        "## Bug Report",
        "",
        f"**Description:** {user_description}" if user_description else "**Description:** [Please describe the issue]",
        "",
        f"**Reported:** {now.strftime('%Y-%m-%d %H:%M')}",
        "",
    ]

    # Add current state if available
    dash_path = WEB_DIR / 'power_dashboard_data.json'
    try:
        with open(dash_path, 'r') as f:
            dash = json.load(f)
        body_parts.extend([
            "**System State at Report Time:**",
            f"- SOC: {dash.get('soc_percent', '?')}%",
            f"- Mode: {dash.get('mode_name', '?')}",
            f"- Solar: {dash.get('solar_kw', '?')} kW | Grid: {dash.get('grid_kw', '?')} kW",
            f"- Data Source: {dash.get('data_source', '?')}",
            f"- Engine: {dash.get('engine_version', 'unknown')}",
            "",
        ])
    except Exception:
        pass

    body_parts.extend([
        "---",
        "**📎 Please attach the diagnostic bundle zip file to this issue.**",
        "*(Drag and drop the downloaded zip file into this text area)*",
        "",
        "---",
        "*Auto-generated by FranklinWH Diagnostic Tool*",
    ])

    body = '\n'.join(body_parts)

    params = urllib.parse.urlencode({
        'title': title,
        'body': body,
        'labels': 'bug,v4-beta',
    })

    return f"https://github.com/{repo}/issues/new?{params}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Generate FranklinWH diagnostic bundle')
    parser.add_argument('--hours', type=int, default=24, help='Hours of history to include')
    parser.add_argument('--lines', type=int, default=500, help='Max log lines per file')
    parser.add_argument('--output', type=str, default=None, help='Output directory')
    parser.add_argument('--base-dir', type=str, default=None, help='Override base directory')
    args = parser.parse_args()

    if args.base_dir:
        BASE_DIR = Path(args.base_dir)
        LOG_DIR = BASE_DIR / 'logs'
        DATA_DIR = BASE_DIR / 'data'
        WEB_DIR = BASE_DIR / 'web'

    output_dir = Path(args.output) if args.output else LOG_DIR

    print(f"Generating diagnostic bundle...")
    print(f"  Base: {BASE_DIR}")
    print(f"  Hours: {args.hours}")
    print(f"  Max lines: {args.lines}")

    zip_path = generate_bundle(
        hours=args.hours,
        max_log_lines=args.lines,
        output_dir=output_dir,
    )

    print(f"\n✅ Bundle saved: {zip_path}")
    print(f"   Size: {zip_path.stat().st_size / 1024:.1f} KB")

    # Also print a GitHub URL
    url = generate_github_url("", args.hours)
    print(f"\n📋 GitHub issue URL (without description):")
    print(f"   {url[:120]}...")
