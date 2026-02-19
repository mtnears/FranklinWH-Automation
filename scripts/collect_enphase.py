#!/usr/bin/env python3
"""
Enphase IQ Gateway - Solar Panel Data Collector (Multi-Array)

Collects per-microinverter production data from a local Enphase IQ Gateway
and writes it to a JSON file for the dashboard Solar tab.

Supports multiple arrays — each identified by an ARRAY_ID (e.g. "house").
Configuration is read from environment variables prefixed with the array ID:

    SOLAR_ARRAY_HOUSE_TYPE=enphase
    SOLAR_ARRAY_HOUSE_NAME=Rooftop
    SOLAR_ARRAY_HOUSE_IP=192.168.4.93
    SOLAR_ARRAY_HOUSE_SERIAL=482511049960
    SOLAR_ARRAY_HOUSE_EMAIL=user@example.com
    SOLAR_ARRAY_HOUSE_PASSWORD=secret

Usage:
    python3 collect_enphase.py --array-id house
    python3 collect_enphase.py                     # legacy: uses ENPHASE_* vars

Output:
    web/solar_house.json   (nginx-served)
    data/solar_house.json  (persistent)
"""

import json
import os
import sys
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path

# ── Panel Specifications (from install plans) ───────────────────
# Populated per array_id; add new arrays here as needed.
PANEL_SPECS = {
    "house": {
        "model": "Hyundai HiN-T435NF(BK)",
        "nameplate_w": 435,
        "ptc_w": 407.2,
        "install_date": "2025-09-05",
        "tilt_deg": 22,           # estimated roof pitch
        "azimuth_deg": 295,       # WNW from Google Earth
        "microinverter": "Enphase IQ8M-72-M-US",
        "degradation_year1_pct": 2.0,
        "degradation_annual_pct": 0.5,
        "circuits": 2,
        "panels_per_circuit": 8,
    },
}

# Health classification thresholds (ratio vs peer average)
HEALTH_THRESHOLDS = {
    "excellent": 1.05,   # ≥105% of peer avg
    "good": 0.95,        # 95-105%
    "fair": 0.85,        # 85-95%
    "watch": 0.75,       # 75-85%
    # below 75% = "alert"
}


def calc_panel_age_years(install_date_str):
    """Calculate panel age in years from install date."""
    install = datetime.strptime(install_date_str, "%Y-%m-%d")
    delta = datetime.now() - install
    return round(delta.days / 365.25, 2)


def calc_expected_degradation(spec):
    """Calculate expected degradation based on manufacturer warranty curve."""
    age = calc_panel_age_years(spec["install_date"])
    if age <= 0:
        return {"age_years": 0, "expected_pct_remaining": 100.0,
                "effective_ptc_w": spec["ptc_w"]}
    # Year 1: initial degradation
    year1_factor = 1.0 - (spec["degradation_year1_pct"] / 100)
    if age <= 1:
        factor = 1.0 - (spec["degradation_year1_pct"] / 100) * age
    else:
        additional_years = age - 1
        factor = year1_factor * (1 - spec["degradation_annual_pct"] / 100 * additional_years)
    return {
        "age_years": age,
        "expected_pct_remaining": round(factor * 100, 1),
        "effective_ptc_w": round(spec["ptc_w"] * factor, 1),
    }


def classify_health(ratio):
    """Classify panel health based on ratio vs peer average."""
    if ratio >= HEALTH_THRESHOLDS["excellent"]:
        return "excellent"
    elif ratio >= HEALTH_THRESHOLDS["good"]:
        return "good"
    elif ratio >= HEALTH_THRESHOLDS["fair"]:
        return "fair"
    elif ratio >= HEALTH_THRESHOLDS["watch"]:
        return "watch"
    return "alert"


def analyze_health(inverter_data, array_id="house"):
    """
    Analyze panel health using peer comparison.

    For Enphase, we have real-time watts + daily averages for every panel,
    all on the same roof face. This makes the entire array one peer group.

    Returns health dict to merge into output JSON.
    """
    spec = PANEL_SPECS.get(array_id)
    if not spec:
        return {}

    panels = list(inverter_data.values())
    if not panels:
        return {}

    # ── Degradation info ──
    degradation = calc_expected_degradation(spec)

    # ── Real-time health (current watts) ──
    current_watts = [p.get("current_watts", 0) for p in panels]
    producing = [w for w in current_watts if w > 0]
    realtime_analysis = {}
    if len(producing) >= 4:  # Need enough producing panels for meaningful comparison
        rt_avg = sum(producing) / len(producing)
        for serial, p in inverter_data.items():
            w = p.get("current_watts", 0)
            if w > 0:
                ratio = w / rt_avg if rt_avg > 0 else 1.0
                realtime_analysis[serial] = {
                    "ratio_vs_array": round(ratio, 3),
                    "status": classify_health(ratio),
                }

    # ── Daily average health ──
    daily_avgs = {s: p.get("avg_today_watts", 0) for s, p in inverter_data.items()}
    active_avgs = {s: v for s, v in daily_avgs.items() if v > 0}
    daily_analysis = {}
    if len(active_avgs) >= 4:
        day_mean = sum(active_avgs.values()) / len(active_avgs)
        for serial, avg in active_avgs.items():
            ratio = avg / day_mean if day_mean > 0 else 1.0
            daily_analysis[serial] = {
                "ratio_vs_array_today": round(ratio, 3),
                "status_today": classify_health(ratio),
            }

    # ── Max-ever health (lifetime proxy) ──
    max_evers = {s: p.get("max_ever_watts", 0) for s, p in inverter_data.items()}
    active_maxes = {s: v for s, v in max_evers.items() if v > 0}
    lifetime_analysis = {}
    if active_maxes:
        max_avg = sum(active_maxes.values()) / len(active_maxes)
        for serial, mx in active_maxes.items():
            ratio = mx / max_avg if max_avg > 0 else 1.0
            lifetime_analysis[serial] = {
                "ratio_max_ever": round(ratio, 3),
                "status_lifetime": classify_health(ratio),
            }

    # ── Merge health into per-panel data ──
    for serial, p in inverter_data.items():
        rt = realtime_analysis.get(serial, {})
        da = daily_analysis.get(serial, {})
        lt = lifetime_analysis.get(serial, {})

        # Primary status: use realtime if producing, else daily, else lifetime
        if rt:
            status = rt["status"]
            primary_ratio = rt["ratio_vs_array"]
        elif da:
            status = da["status_today"]
            primary_ratio = da["ratio_vs_array_today"]
        elif lt:
            status = lt["status_lifetime"]
            primary_ratio = lt["ratio_max_ever"]
        else:
            status = "good"  # No data yet
            primary_ratio = 1.0

        p["health"] = {
            "status": status,
            "ratio_vs_array": primary_ratio,
            "ratio_realtime": rt.get("ratio_vs_array", 0),
            "ratio_today_avg": da.get("ratio_vs_array_today", 0),
            "ratio_max_ever": lt.get("ratio_max_ever", 0),
        }

    # ── Array-wide health summary ──
    statuses = [p.get("health", {}).get("status", "good") for p in inverter_data.values()]
    health_counts = {s: statuses.count(s) for s in ["excellent", "good", "fair", "watch", "alert"]}

    underperformers = []
    for serial, p in inverter_data.items():
        h = p.get("health", {})
        if h.get("status") in ("watch", "alert"):
            underperformers.append({
                "serial_number": serial,
                "status": h["status"],
                "ratio_vs_array": h.get("ratio_vs_array", 0),
                "current_watts": p.get("current_watts", 0),
                "avg_today_watts": p.get("avg_today_watts", 0),
            })

    max_ever_vals = [v for v in max_evers.values() if v > 0]
    spread_pct = round((max(max_ever_vals) - min(max_ever_vals)) / (sum(max_ever_vals) / len(max_ever_vals)) * 100, 1) if max_ever_vals else 0

    return {
        "panel_spec": {
            "model": spec["model"],
            "nameplate_w": spec["nameplate_w"],
            "ptc_w": spec["ptc_w"],
            "install_date": spec["install_date"],
            "age_years": degradation["age_years"],
            "microinverter": spec["microinverter"],
        },
        "degradation": degradation,
        "array_stats": {
            "panel_count": len(panels),
            "max_ever_avg": round(sum(max_evers.values()) / len(max_evers), 1) if max_evers else 0,
            "max_ever_spread_pct": spread_pct,
            "max_ever_min": min(max_ever_vals) if max_ever_vals else 0,
            "max_ever_max": max(max_ever_vals) if max_ever_vals else 0,
        },
        "health_counts": health_counts,
        "underperformers": underperformers,
    }

# ── Path Setup ──────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"

# Web directory
_web_env = os.getenv("WEB_DIR", "")
if _web_env and os.path.isabs(_web_env):
    WEB_DIR = Path(_web_env)
else:
    WEB_DIR = PROJECT_ROOT / (_web_env or "web")

# Try loading .env
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

# ── Logging ─────────────────────────────────────────────────────
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "enphase_collector.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ── Configuration Loader ────────────────────────────────────────
def load_array_config(array_id: str) -> dict:
    """
    Load configuration for a specific solar array.

    When array_id is provided, reads SOLAR_ARRAY_{ID}_* env vars.
    Falls back to legacy ENPHASE_* vars for backward compatibility.
    """
    if array_id:
        prefix = f"SOLAR_ARRAY_{array_id.upper()}_"
        cfg = {
            "array_id": array_id.lower(),
            "name": os.getenv(f"{prefix}NAME", array_id.title()),
            "type": os.getenv(f"{prefix}TYPE", "enphase"),
            "ip": os.getenv(f"{prefix}IP", ""),
            "serial": os.getenv(f"{prefix}SERIAL", ""),
            "email": os.getenv(f"{prefix}EMAIL", ""),
            "password": os.getenv(f"{prefix}PASSWORD", ""),
            "token_file": os.getenv(
                f"{prefix}TOKEN_FILE",
                str(DATA_DIR / f"enphase_token.txt"),
            ),
            "layout_file": os.getenv(
                f"{prefix}LAYOUT_FILE",
                str(DATA_DIR / f"enphase_array_layout_{array_id.lower()}.json"),
            ),
            "model": os.getenv(f"{prefix}MODEL", "IQ8"),
        }
    else:
        # Legacy single-array mode
        cfg = {
            "array_id": "default",
            "name": os.getenv("ENPHASE_ARRAY_NAME", "Solar Array"),
            "type": "enphase",
            "ip": os.getenv("ENPHASE_ENVOY_IP", ""),
            "serial": os.getenv("ENPHASE_ENVOY_SERIAL", ""),
            "email": os.getenv("ENPHASE_EMAIL", ""),
            "password": os.getenv("ENPHASE_PASSWORD", ""),
            "token_file": os.getenv(
                "ENPHASE_TOKEN_FILE",
                str(DATA_DIR / "enphase_token.txt"),
            ),
            "layout_file": os.getenv(
                "ENPHASE_ARRAY_LAYOUT",
                str(DATA_DIR / "enphase_array_layout.json"),
            ),
            "model": "IQ8",
        }
    return cfg


# ── Token Management ────────────────────────────────────────────
def get_or_refresh_token(cfg: dict) -> str:
    """Get a valid Enphase token, fetching a new one if needed."""
    token_path = Path(cfg["token_file"])
    if token_path.exists():
        token = token_path.read_text().strip()
        if token and len(token) > 50:
            logger.debug("Using saved token from %s", token_path)
            return token

    if not cfg["email"] or not cfg["password"] or not cfg["serial"]:
        logger.error("Cannot fetch token: email, password, and serial required")
        return None

    try:
        import requests
    except ImportError:
        logger.error("'requests' module required")
        return None

    logger.info("Fetching new Enphase token for array '%s'...", cfg["array_id"])

    try:
        # Step 1: Login to Enphase cloud
        login_resp = requests.post(
            "https://enlighten.enphaseenergy.com/login/login.json?",
            data={
                "user[email]": cfg["email"],
                "user[password]": cfg["password"],
            },
            timeout=30,
        )
        if login_resp.status_code != 200:
            logger.error("Enphase login failed (HTTP %d)", login_resp.status_code)
            return None

        session_id = login_resp.json().get("session_id")
        if not session_id:
            logger.error("No session_id in login response")
            return None

        # Step 2: Exchange for gateway token
        token_resp = requests.post(
            "https://entrez.enphaseenergy.com/tokens",
            json={
                "session_id": session_id,
                "serial_num": cfg["serial"],
                "username": cfg["email"],
            },
            timeout=30,
        )
        if token_resp.status_code != 200:
            logger.error("Token request failed (HTTP %d)", token_resp.status_code)
            return None

        token = token_resp.text.strip()
        if not token or len(token) < 50:
            logger.error("Invalid token received (length=%d)", len(token))
            return None

        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(token)
        logger.info("New token saved to %s", token_path)
        return token

    except requests.RequestException as e:
        logger.error("Token fetch error: %s", e)
        return None


# ── API Queries ─────────────────────────────────────────────────
def query_inverters(cfg: dict, token: str) -> list:
    """Query per-microinverter data from the local gateway."""
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        logger.error("'requests' module required")
        return None

    url = f"https://{cfg['ip']}/api/v1/production/inverters"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.get(url, headers=headers, verify=False, timeout=10)

        if resp.status_code == 401:
            token_path = Path(cfg["token_file"])
            if token_path.exists():
                token_path.unlink()
                logger.warning("Token expired, deleted saved token.")
            return None

        if resp.status_code != 200:
            logger.error("Inverter query failed (HTTP %d): %s",
                         resp.status_code, resp.text[:200])
            return None

        inverters = resp.json()
        logger.info("Retrieved data for %d microinverters", len(inverters))
        return inverters

    except requests.RequestException as e:
        logger.error("Inverter query error: %s", e)
        return None


def query_production_summary(cfg: dict, token: str) -> dict:
    """Query overall production summary from the gateway."""
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        return None

    url = f"https://{cfg['ip']}/production.json"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.get(url, headers=headers, verify=False, timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        logger.debug("Production summary query failed: %s", e)
    return None


# ── Layout & History ────────────────────────────────────────────
def load_array_layout(cfg: dict) -> dict:
    """Load the array layout configuration if it exists."""
    layout_path = Path(cfg["layout_file"])
    if layout_path.exists():
        try:
            with open(layout_path, "r") as f:
                layout = json.load(f)
            logger.debug("Loaded array layout from %s", layout_path)
            return layout
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("Failed to load array layout: %s", e)
    return {}


def update_daily_history(cfg: dict, inverters: list) -> dict:
    """Track daily per-inverter production history."""
    today = datetime.now().strftime("%Y-%m-%d")
    history_file = DATA_DIR / f"enphase_daily_history_{cfg['array_id']}.json"
    history = {}

    if history_file.exists():
        try:
            with open(history_file, "r") as f:
                history = json.load(f)
        except (json.JSONDecodeError, IOError):
            history = {}

    if today not in history:
        history[today] = {}

    for inv in inverters:
        serial = inv.get("serialNumber", "")
        watts = inv.get("lastReportWatts", 0)
        max_watts = inv.get("maxReportWatts", 0)

        if serial not in history[today]:
            history[today][serial] = {
                "first_seen_watts": watts,
                "max_watts_today": watts,
                "peak_max": max_watts,
                "samples": 0,
                "cumulative_watts": 0,
            }

        entry = history[today][serial]
        entry["max_watts_today"] = max(entry["max_watts_today"], watts)
        entry["samples"] += 1
        entry["cumulative_watts"] += watts
        entry["last_watts"] = watts

    # Prune history older than 30 days
    dates = sorted(history.keys())
    if len(dates) > 30:
        for old_date in dates[:-30]:
            del history[old_date]

    try:
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(history_file, "w") as f:
            json.dump(history, f, indent=2)
    except IOError as e:
        logger.warning("Failed to save daily history: %s", e)

    return history.get(today, {})


# ── Output Builder ──────────────────────────────────────────────
def build_output(cfg: dict, inverters: list, layout: dict,
                 daily_data: dict, production: dict) -> dict:
    """Build the output JSON consumed by the dashboard."""
    now = datetime.now()

    # Load full daily history for max_ever scanning across all retained days
    history_file = DATA_DIR / f"enphase_daily_history_{cfg['array_id']}.json"
    all_daily_history = {}
    if history_file.exists():
        try:
            with open(history_file, "r") as f:
                all_daily_history = json.load(f)
        except (json.JSONDecodeError, IOError):
            all_daily_history = {}

    inverter_data = {}
    total_watts = 0
    total_max_ever = 0
    active_count = 0

    for inv in inverters:
        serial = inv.get("serialNumber", "")
        current_watts = inv.get("lastReportWatts", 0)
        firmware_max = inv.get("maxReportWatts", 0)
        last_report = inv.get("lastReportDate", 0)
        dev_type = inv.get("devType", 0)

        # max_ever_watts: scan daily history for highest observed watts
        # This is consistent with how SolarEdge computes it
        best_watts = firmware_max  # firmware value as starting point
        for d_date, d_panels in all_daily_history.items():
            if serial in d_panels:
                day_max = d_panels[serial].get("max_watts_today", 0)
                day_peak = d_panels[serial].get("peak_max", 0)
                best_watts = max(best_watts, day_max, day_peak)
        max_ever = best_watts or current_watts

        total_watts += current_watts
        total_max_ever += max_ever
        if current_watts > 0:
            active_count += 1

        daily = daily_data.get(serial, {})

        inverter_data[serial] = {
            "serial": serial,
            "current_watts": current_watts,
            "max_ever_watts": max_ever,
            "last_report_time": last_report,
            "last_report_human": (
                datetime.fromtimestamp(last_report).strftime("%H:%M:%S")
                if last_report else "N/A"
            ),
            "dev_type": dev_type,
            "max_today_watts": daily.get("max_watts_today", 0),
            "samples_today": daily.get("samples", 0),
            "avg_today_watts": (
                round(daily["cumulative_watts"] / daily["samples"], 1)
                if daily.get("samples", 0) > 0 else 0
            ),
        }

    watt_values = [inv.get("lastReportWatts", 0) for inv in inverters]
    avg_watts = sum(watt_values) / len(watt_values) if watt_values else 0

    underperformers = []
    if avg_watts > 5:
        for inv in inverters:
            if inv.get("lastReportWatts", 0) < avg_watts * 0.7:
                underperformers.append({
                    "serial": inv["serialNumber"],
                    "watts": inv["lastReportWatts"],
                    "pct_of_avg": round(
                        inv["lastReportWatts"] / avg_watts * 100, 1
                    ),
                })

    prod_summary = {}
    if production:
        for p in production.get("production", []):
            if p.get("type") == "inverters":
                prod_summary["inverters_wh_lifetime"] = p.get("whLifetime", 0)
                prod_summary["inverters_wh_today"] = p.get("whToday", 0)
                prod_summary["inverters_active"] = p.get("activeCount", 0)
            elif (p.get("type") == "eim"
                  and p.get("measurementType") == "production"):
                prod_summary["meter_w_now"] = p.get("wNow", 0)
                prod_summary["meter_wh_today"] = p.get("whToday", 0)
                prod_summary["meter_wh_lifetime"] = p.get("whLifetime", 0)

    return {
        "timestamp": now.isoformat(),
        "timestamp_epoch": int(now.timestamp()),
        "array_id": cfg["array_id"],
        "array_name": cfg["name"],
        "array_type": cfg["type"],
        "gateway": {
            "ip": cfg["ip"],
            "serial": cfg["serial"],
            "model": cfg.get("model", ""),
        },
        "summary": {
            "total_watts": total_watts,
            "total_kw": round(total_watts / 1000, 2),
            "panel_count": len(inverters),
            "active_count": active_count,
            "average_watts": round(avg_watts, 1),
            "min_watts": min(watt_values) if watt_values else 0,
            "max_watts": max(watt_values) if watt_values else 0,
            "total_max_ever": total_max_ever,
            "spread": (max(watt_values) - min(watt_values))
                      if watt_values else 0,
        },
        "production": prod_summary,
        "inverters": inverter_data,
        "underperformers": underperformers,
        "layout": layout,
        "collection_status": "ok",
        "health": analyze_health(inverter_data, cfg["array_id"]),
    }


# ── File Output ─────────────────────────────────────────────────
def write_output(cfg: dict, output: dict):
    """Write output JSON to data/ and web/ directories."""
    aid = cfg["array_id"]
    fname = f"solar_{aid}.json"

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WEB_DIR.mkdir(parents=True, exist_ok=True)

    for path in [DATA_DIR / fname, WEB_DIR / fname]:
        try:
            with open(path, "w") as f:
                json.dump(output, f, indent=2)
        except IOError as e:
            logger.error("Failed to write %s: %s", path, e)

    logger.info(
        "Data written: %d panels, %d W total, %d active → %s",
        output["summary"]["panel_count"],
        output["summary"]["total_watts"],
        output["summary"]["active_count"],
        fname,
    )


def write_error_output(cfg: dict, error_msg: str):
    """Write error status so the dashboard can display it."""
    aid = cfg["array_id"]
    fname = f"solar_{aid}.json"
    output = {
        "timestamp": datetime.now().isoformat(),
        "array_id": aid,
        "array_name": cfg.get("name", aid),
        "array_type": cfg.get("type", "enphase"),
        "collection_status": "error",
        "error": error_msg,
        "summary": {},
        "inverters": {},
        "layout": {},
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    for path in [DATA_DIR / fname, WEB_DIR / fname]:
        try:
            with open(path, "w") as f:
                json.dump(output, f, indent=2)
        except IOError:
            pass


# ── Main ────────────────────────────────────────────────────────
def collect_array(array_id: str = None):
    """Run collection for one array."""
    cfg = load_array_config(array_id)

    if not cfg["ip"]:
        logger.error("No IP configured for array '%s'", cfg["array_id"])
        write_error_output(cfg, "No IP configured")
        return

    logger.info("Starting Enphase collection: %s (%s) @ %s",
                cfg["name"], cfg["array_id"], cfg["ip"])

    # Get token
    token = get_or_refresh_token(cfg)
    if not token:
        logger.error("Failed to obtain token for %s", cfg["array_id"])
        write_error_output(cfg, "Failed to obtain token")
        return

    # Query inverters
    inverters = query_inverters(cfg, token)
    if not inverters:
        logger.info("Retrying with fresh token...")
        # Delete saved token and try again
        token_path = Path(cfg["token_file"])
        if token_path.exists():
            token_path.unlink()
        token = get_or_refresh_token(cfg)
        if token:
            inverters = query_inverters(cfg, token)

    if not inverters:
        logger.error("Failed to query inverters for %s", cfg["array_id"])
        write_error_output(cfg, "Failed to query inverters")
        return

    # Production summary (optional)
    production = query_production_summary(cfg, token)

    # Layout + history
    layout = load_array_layout(cfg)
    daily_data = update_daily_history(cfg, inverters)

    # Build & write
    output = build_output(cfg, inverters, layout, daily_data, production)
    write_output(cfg, output)


def main():
    """Entry point — supports --array-id for multi-array."""
    parser = argparse.ArgumentParser(
        description="Collect Enphase microinverter data"
    )
    parser.add_argument(
        "--array-id",
        default=None,
        help="Array identifier (e.g. 'house'). "
             "Reads SOLAR_ARRAY_{ID}_* env vars. "
             "Omit for legacy ENPHASE_* mode."
    )
    args = parser.parse_args()

    # Check enabled — multi-array mode uses SOLAR_ARRAYS list
    if args.array_id:
        # Multi-array: trust that scheduler already checked enabled
        collect_array(args.array_id)
    else:
        # Legacy single-array mode
        if os.getenv("ENPHASE_ENABLED", "false").lower() != "true":
            logger.info("Enphase collection disabled (ENPHASE_ENABLED=false)")
            return
        collect_array(None)


if __name__ == "__main__":
    main()
