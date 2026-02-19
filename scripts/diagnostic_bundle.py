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

# Base paths — adjusted at runtime if needed
BASE_DIR = Path('/volume1/docker/franklin')
LOG_DIR = BASE_DIR / 'logs'
DATA_DIR = BASE_DIR / 'data'
WEB_DIR = BASE_DIR / 'web'

# Files to include (relative to BASE_DIR)
LOG_FILES = {
    'solar_intelligence.log': LOG_DIR / 'solar_intelligence.log',
    'scheduler.log': LOG_DIR / 'scheduler.log',
}

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
    """Read .env file and return keys only (no values)."""
    if filepath is None:
        filepath = BASE_DIR / '.env'
    try:
        lines = []
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    lines.append(line)
                    continue
                if '=' in line:
                    key = line.split('=', 1)[0].strip()
                    lines.append(f"{key}=***")
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

    # Recent errors
    intel_path = LOG_DIR / 'solar_intelligence.log'
    error_text = extract_errors(intel_path, hours)
    if '[No errors' in error_text:
        summary_parts.append("**Errors (last {}h):** None".format(hours))
    else:
        error_count = error_text.count('\n')
        summary_parts.append(f"**Errors (last {hours}h):** {error_count} (see attached bundle)")

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

        # 3. Intelligence log (last N lines)
        intel_path = LOG_DIR / 'solar_intelligence.log'
        if intel_path.exists():
            content = tail_file(intel_path, max_log_lines)
            zf.writestr('logs/solar_intelligence.log', sanitize_text(content))

        # 4. Recent decisions (structured extraction)
        if intel_path.exists():
            decisions = extract_recent_decisions(intel_path, max_decisions=30)
            zf.writestr('logs/recent_decisions.txt', sanitize_text(decisions))

        # 5. Errors only
        if intel_path.exists():
            errors = extract_errors(intel_path, hours)
            zf.writestr('logs/errors.txt', sanitize_text(errors))

        # 6. Scheduler log
        sched_path = LOG_DIR / 'scheduler.log'
        if sched_path.exists():
            content = tail_file(sched_path, max_log_lines)
            zf.writestr('logs/scheduler.log', sanitize_text(content))

        # 7. Recent monitoring CSV (last 50 rows with header)
        csv_path = LOG_DIR / 'continuous_monitoring.csv'
        if csv_path.exists():
            content = tail_csv(csv_path, max_rows=100)
            zf.writestr('logs/continuous_monitoring_recent.csv', sanitize_text(content))

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
