#!/usr/bin/env python3
"""
SolarEdge Per-Panel Data Collector (Portal API)

Collects real per-optimizer energy data from the SolarEdge monitoring portal
using direct API endpoints. Requires pycognito for the /services/ energy API.

Data collected per optimizer per run:
  - Today's energy (Wh)
  - This week's energy (Wh)  
  - This month's energy (Wh)
  - Lifetime energy (Wh)
  - Inverter/string assignment
  - Real hardware serial number

Health Analysis per optimizer:
  - Efficiency vs nameplate rating (adjusted for degradation)
  - Relative performance vs array peers (same string, same inverter, whole array)
  - Health status classification (Excellent/Good/Fair/Watch/Alert)
  - Underperformer detection

Output files:
  data/solaredge_panel_current.json  - Latest snapshot (all optimizers + totals + health)
  data/solaredge_panel_peaks.json    - Per-panel lifetime peak daily production
  web/solaredge_panel_current.json   - Copy for dashboard access
  SQLite: solaredge_readings table   - Historical per-optimizer data

Portal API details:
  - layout/logical (legacy /solaredge-apigw/, HTTP basic auth) for inventory
  - /services/.../by-inverter (Cognito token auth) for energy + color health
  - One energy call per date range covers every optimizer
  - Portal refreshes every ~15 minutes; run on 15-min schedule

Usage:
    python3 collect_solaredge_panels.py              # Production mode
    python3 collect_solaredge_panels.py --test        # Print results only
    python3 collect_solaredge_panels.py --json        # Output JSON to stdout

Environment Variables:
    SOLAREDGE_SITE_ID       - Site ID (default: 1241660)
    SOLAREDGE_USERNAME      - Portal login email
    SOLAREDGE_PASSWORD      - Portal login password
"""

import os
import sys
import json
import time
import base64
import logging
from config import configure_logging
import argparse
import requests
from datetime import datetime, date, timedelta
from pathlib import Path

try:
    from db import store as db_store, init_db as db_init
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False

# Configure logging
configure_logging()
logger = logging.getLogger(__name__)

# Paths
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / "data"
WEB_DIR = SCRIPT_DIR.parent / "web"

# Output files
CURRENT_JSON = DATA_DIR / "solaredge_panel_current.json"
PEAKS_JSON = DATA_DIR / "solaredge_panel_peaks.json"
LAYOUT_CACHE = DATA_DIR / "solaredge_panel_layout.json"
DAILY_HISTORY = DATA_DIR / "solaredge_daily_history.json"
WEB_JSON = WEB_DIR / "solaredge_panel_current.json"
TOKEN_CACHE = DATA_DIR / "solaredge_se_token.json"

# ──────────────────────────────────────────────────────────────────────
# SolarEdge auth (2026): the legacy /solaredge-apigw/ endpoints still take
# HTTP basic auth (layout/logical works), but the newer /services/ energy
# API authenticates off an AWS Cognito access token presented as the
# se_monitoring_auth cookie plus an x-se-user-id header (the token's uuid
# claim) plus a real browser request-context header set.
# ──────────────────────────────────────────────────────────────────────
COGNITO_REGION = "eu-central-1"
COGNITO_POOL_ID = "eu-central-1_fVUTz39em"
COGNITO_CLIENT_ID = "ugfnsujd3384sshcjehaphlh3"
TOKEN_REFRESH_MARGIN_S = 600  # re-mint if within 10 min of expiry
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

# Color-based health overrides (SolarEdge's own period-relative metric,
# 1.0 = best performer in the period). Catches an acutely bad optimizer
# before multi-year lifetime ratios move enough to flag it.
COLOR_WATCH = 0.75
COLOR_ALERT = 0.60

# Layout cache duration (seconds) - re-fetch layout once per day
LAYOUT_CACHE_MAX_AGE = 86400

# Portal base URL
PORTAL_BASE = "https://monitoring.solaredge.com"

# ──────────────────────────────────────────────────────────────────────
# Panel specifications — LG LG355S2W-A5 (from installation plans)
# ──────────────────────────────────────────────────────────────────────
PANEL_SPEC = {
    "model": "LG LG355S2W-A5",
    "nameplate_w": 355,           # STC rated power (W)
    "ptc_w": 321.9,               # CEC PTC rating (W) - more realistic
    "module_efficiency_pct": 17.1, # Module efficiency (%)
    "optimizer_efficiency": 0.988, # SolarEdge P-400 optimizer (98.8%)
    "inverter_efficiency": 0.992,  # SolarEdge SE7600H/SE11400H (99.2%)
    "total_panels": 60,
    "install_date": "2019-05-07",  # From permit plans
    "tilt_deg": 10,                # Array tilt (degrees)
    "azimuth_deg": 230,            # Array azimuth (degrees, 180=south)
    # LG warranty degradation schedule
    "degradation_year1_pct": 2.0,  # -2% first year
    "degradation_annual_pct": 0.5, # -0.5%/yr after year 1
}

# Health status thresholds (percentage of peer average)
HEALTH_THRESHOLDS = {
    "excellent": 1.05,   # >105% of peer avg
    "good": 0.95,        # 95-105% of peer avg
    "fair": 0.85,        # 85-95% of peer avg
    "watch": 0.75,       # 75-85% of peer avg
    # Below 75% = "alert"
}


def load_env():
    """Load .env file if present."""
    env_path = SCRIPT_DIR.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key not in os.environ:
                        os.environ[key] = value


def get_config():
    """Get configuration from environment."""
    load_env()
    return {
        "site_id": os.environ.get("SOLAREDGE_SITE_ID",
                   os.environ.get("SOLAR_ARRAY_BARN_SITE_ID", "1241660")),
        "username": os.environ.get("SOLAREDGE_USERNAME", ""),
        "password": os.environ.get("SOLAREDGE_PASSWORD", ""),
        "api_key": os.environ.get("SOLAREDGE_API_KEY",
                   os.environ.get("SOLAR_ARRAY_BARN_API_KEY", "")),
        "modbus_enabled": os.environ.get("SOLAREDGE_MODBUS_ENABLED", "").strip().lower()
                          in ("1", "true", "yes", "on"),
    }


def portal_session(username, password):
    """Create authenticated session with CSRF token."""
    s = requests.Session()
    s.auth = (username, password)

    # Login to get session cookies
    r = s.get(f"{PORTAL_BASE}/solaredge-web/p/login", timeout=30)
    r.raise_for_status()

    csrf = s.cookies.get("CSRF-TOKEN", "")
    s.headers.update({
        "x-csrf-token": csrf,
        "x-requested-with": "XMLHttpRequest",
        "content-type": "application/json",
        "accept": "application/json",
    })
    return s


def get_layout(session, site_id):
    """
    Get logical layout: inverter -> string -> optimizer with serial numbers.

    Returns:
        optimizer_map: dict of {optimizer_id: {serial_number, inverter, string, ...}}
        inverter_info: list of inverter dicts with string/optimizer hierarchy
    """
    url = f"{PORTAL_BASE}/solaredge-apigw/api/sites/{site_id}/layout/logical"
    r = session.get(url, timeout=30)
    r.raise_for_status()
    data = json.loads(r.text)

    optimizer_map = {}
    inverter_info = []

    for inv_node in data["logicalTree"]["children"]:
        inv = inv_node["data"]
        inv_entry = {
            "id": inv["id"],
            "serial": inv["serialNumber"],
            "name": inv["name"],
            "strings": [],
        }

        # Handle possible production meter nesting
        string_nodes = inv_node.get("children", [])
        if "PRODUCTION METER" in inv.get("name", "").upper():
            for sub in inv_node.get("children", []):
                string_nodes = sub.get("children", [])

        for str_node in string_nodes:
            str_data = str_node["data"]
            str_entry = {
                "name": str_data["name"],
                "optimizers": [],
            }

            for opt_node in str_node.get("children", []):
                opt = opt_node["data"]
                opt_id = str(opt["id"])
                optimizer_map[opt_id] = {
                    "serial_number": opt["serialNumber"],
                    "name": opt.get("displayName", opt["name"]),
                    "inverter": inv["name"],
                    "inverter_sn": inv["serialNumber"],
                    "string": str_data["name"],
                }
                str_entry["optimizers"].append({
                    "id": opt_id,
                    "serial": opt["serialNumber"],
                })

            inv_entry["strings"].append(str_entry)
        inverter_info.append(inv_entry)

    return optimizer_map, inverter_info


def get_cached_layout(session, site_id):
    """Get layout from cache or fetch fresh."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if LAYOUT_CACHE.exists():
        try:
            cache = json.loads(LAYOUT_CACHE.read_text())
            cache_age = time.time() - cache.get("cached_at", 0)
            if cache_age < LAYOUT_CACHE_MAX_AGE:
                logger.info(f"Using cached layout ({int(cache_age/3600)}h old, "
                            f"{len(cache['optimizer_map'])} optimizers)")
                return cache["optimizer_map"], cache["inverter_info"]
        except (json.JSONDecodeError, KeyError, IOError):
            pass

    # Fetch fresh
    optimizer_map, inverter_info = get_layout(session, site_id)

    # Cache it
    cache = {
        "cached_at": time.time(),
        "optimizer_map": optimizer_map,
        "inverter_info": inverter_info,
    }
    LAYOUT_CACHE.write_text(json.dumps(cache, indent=2))
    logger.info(f"Layout fetched and cached: {len(optimizer_map)} optimizers")

    return optimizer_map, inverter_info


def _jwt_claims(token):
    """Decode a JWT payload (no signature verification) to read claims."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _cache_token(token, refresh_token):
    """Persist the access token + refresh token + decoded claims."""
    try:
        claims = _jwt_claims(token)
        TOKEN_CACHE.write_text(json.dumps({
            "access_token": token,
            "refresh_token": refresh_token,
            "uuid": claims.get("uuid"),
            "exp": claims.get("exp", 0),
        }))
    except (IOError, ValueError, KeyError) as e:
        logger.warning(f"Token cache write failed (non-fatal): {e}")


def get_se_token(username, password):
    """
    Return (access_token, uuid) for the /services/ energy API.

    Mints an AWS Cognito access token via SRP (pycognito). The token lives
    ~24h and carries a refresh token, so we cache it and reuse/refresh rather
    than running the full SRP handshake every collection cycle.
    """
    # 1. Try a still-valid cached access token.
    if TOKEN_CACHE.exists():
        try:
            cache = json.loads(TOKEN_CACHE.read_text())
            if cache.get("access_token") and \
               cache.get("exp", 0) - time.time() > TOKEN_REFRESH_MARGIN_S:
                logger.info("Using cached SolarEdge token "
                            f"({int((cache['exp'] - time.time()) / 3600)}h to expiry)")
                return cache["access_token"], cache.get("uuid")
        except (json.JSONDecodeError, IOError):
            pass

    from pycognito import Cognito

    # 2. Try refreshing with a cached refresh token (avoids re-sending password).
    if TOKEN_CACHE.exists():
        try:
            cache = json.loads(TOKEN_CACHE.read_text())
            rt = cache.get("refresh_token")
            if rt:
                c = Cognito(COGNITO_POOL_ID, COGNITO_CLIENT_ID,
                            user_pool_region=COGNITO_REGION)
                c.refresh_token = rt
                c.renew_access_token()
                if c.access_token:
                    logger.info("Refreshed SolarEdge token via refresh_token")
                    _cache_token(c.access_token, rt)
                    return c.access_token, _jwt_claims(c.access_token).get("uuid")
        except Exception as e:
            logger.info(f"Token refresh failed, falling back to SRP login: {e}")

    # 3. Full SRP authentication.
    c = Cognito(COGNITO_POOL_ID, COGNITO_CLIENT_ID,
                username=username, user_pool_region=COGNITO_REGION)
    c.authenticate(password=password)
    token = c.access_token
    _cache_token(token, c.refresh_token)
    logger.info("Minted SolarEdge token via SRP login")
    return token, _jwt_claims(token).get("uuid")


def _services_headers(uuid):
    """Browser request-context headers the /services/ API requires."""
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "referer": f"{PORTAL_BASE}/one",
        "origin": PORTAL_BASE,
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "user-agent": BROWSER_UA,
        "x-se-user-id": uuid or "",
    }


def get_energy(token, uuid, site_id, inverter_serials, start_date, end_date):
    """
    Get per-optimizer energy + color for a date range from the by-inverter
    endpoint. One call covers every optimizer. Returns the parsed JSON.
    """
    url = (f"{PORTAL_BASE}/services/layout/energy/site/{site_id}/by-inverter"
           f"?start-date={start_date}&end-date={end_date}"
           f"&inverter-serials={inverter_serials}"
           f"&include-max-temperature=false&include-color=true")
    r = requests.get(url, headers=_services_headers(uuid),
                     cookies={"se_monitoring_auth": token}, timeout=30)
    r.raise_for_status()
    return r.json()


def _energy_by_serial(resp):
    """
    Flatten a by-inverter response into {serial: energy_wh} and
    {serial: color}. Optimizers are nested under inverters[].optimizers[]
    and keyed by hardware serial directly.
    """
    energy = {}
    color = {}
    for inv in resp.get("inverters", []):
        for opt in inv.get("optimizers", []):
            sn = opt.get("serial")
            if not sn:
                continue
            energy[sn] = (opt.get("energy") or {}).get("value", 0) or 0
            c = opt.get("color")
            if c is not None:
                color[sn] = c
    return energy, color


def get_current_site_power(site_id, api_key):
    """
    Get current site power from SolarEdge cloud API (1 lightweight request).

    This supplements the portal energy data with real-time power.
    The cloud API overview endpoint returns currentPower and today's energy.

    Returns dict with current_power_w and today_energy_wh, or None on failure.
    """
    if not api_key:
        return None

    import urllib.request
    import urllib.parse
    import urllib.error

    url = (f"https://monitoringapi.solaredge.com/site/{site_id}/overview"
           f"?api_key={urllib.parse.quote(api_key)}")

    try:
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        overview = data.get("overview", {})
        return {
            "current_power_w": overview.get("currentPower", {}).get("power", 0),
            "today_energy_wh": overview.get("lastDayData", {}).get("energy", 0),
            "lifetime_energy_wh": overview.get("lifeTimeData", {}).get("energy", 0),
        }
    except Exception as e:
        logger.warning(f"Cloud API power fetch failed (non-fatal): {e}")
        return None


# ──────────────────────────────────────────────────────────────────────
# Health & Efficiency Analysis
# ──────────────────────────────────────────────────────────────────────

def calc_panel_age_years():
    """Calculate panel age in fractional years from install date."""
    install = datetime.strptime(PANEL_SPEC["install_date"], "%Y-%m-%d").date()
    today = date.today()
    delta = today - install
    return delta.days / 365.25


def calc_expected_degradation_pct():
    """
    Calculate expected cumulative degradation based on LG warranty schedule.
    Year 1: -2%, then -0.5%/yr thereafter.
    Returns degradation as decimal (e.g., 0.956 means 4.4% degraded).
    """
    age_years = calc_panel_age_years()

    if age_years <= 0:
        return 1.0
    elif age_years <= 1.0:
        # Proportional year-1 degradation
        return 1.0 - (PANEL_SPEC["degradation_year1_pct"] / 100.0 * age_years)
    else:
        year1_factor = 1.0 - (PANEL_SPEC["degradation_year1_pct"] / 100.0)
        additional_years = age_years - 1.0
        annual_factor = 1.0 - (PANEL_SPEC["degradation_annual_pct"] / 100.0 * additional_years)
        return year1_factor * annual_factor


def calc_expected_daily_wh(peak_sun_hours):
    """
    Calculate expected daily energy (Wh) per panel given peak sun hours.

    Uses PTC rating (more realistic than STC) adjusted for:
      - Panel age degradation
      - Optimizer efficiency (98.8%)
      - Does NOT include inverter efficiency (that's AC-side)
    """
    degradation = calc_expected_degradation_pct()
    # PTC watts, adjusted for degradation and optimizer efficiency
    effective_w = (PANEL_SPEC["ptc_w"]
                   * degradation
                   * PANEL_SPEC["optimizer_efficiency"])
    return effective_w * peak_sun_hours


def classify_health(ratio_to_peers):
    """
    Classify panel health based on ratio to peer average.

    Args:
        ratio_to_peers: panel's energy / peer group average energy

    Returns:
        str: health status (excellent/good/fair/watch/alert)
    """
    if ratio_to_peers >= HEALTH_THRESHOLDS["excellent"]:
        return "excellent"
    elif ratio_to_peers >= HEALTH_THRESHOLDS["good"]:
        return "good"
    elif ratio_to_peers >= HEALTH_THRESHOLDS["fair"]:
        return "fair"
    elif ratio_to_peers >= HEALTH_THRESHOLDS["watch"]:
        return "watch"
    else:
        return "alert"


def analyze_health(optimizers):
    """
    Perform health analysis on all optimizers.

    Calculates:
      - Relative performance vs string peers, inverter peers, and whole array
      - Lifetime efficiency vs expected (degradation-adjusted)
      - Health classification
      - Array-wide statistics

    Modifies optimizer dicts in-place to add 'health' sub-dict.
    Returns array-wide health summary.
    """
    age_years = calc_panel_age_years()
    degradation_factor = calc_expected_degradation_pct()
    degraded_ptc_w = PANEL_SPEC["ptc_w"] * degradation_factor

    # ── Group optimizers by string and inverter ──
    by_string = {}   # "Inverter 1 / String 1.0" -> [opt, ...]
    by_inverter = {} # "Inverter 1" -> [opt, ...]

    for opt in optimizers:
        key_str = f"{opt['inverter']} / {opt['string']}"
        key_inv = opt["inverter"]
        by_string.setdefault(key_str, []).append(opt)
        by_inverter.setdefault(key_inv, []).append(opt)

    # ── Array-wide averages ──
    all_today = [o["today_wh"] for o in optimizers if o["today_wh"] > 0]
    all_lifetime = [o["lifetime_wh"] for o in optimizers if o["lifetime_wh"] > 0]

    array_avg_today = sum(all_today) / len(all_today) if all_today else 0
    array_avg_lifetime = sum(all_lifetime) / len(all_lifetime) if all_lifetime else 0

    # ── Per-string and per-inverter averages ──
    string_avgs_today = {}
    string_avgs_lifetime = {}
    for key, opts in by_string.items():
        vals_today = [o["today_wh"] for o in opts if o["today_wh"] > 0]
        vals_life = [o["lifetime_wh"] for o in opts if o["lifetime_wh"] > 0]
        string_avgs_today[key] = sum(vals_today) / len(vals_today) if vals_today else 0
        string_avgs_lifetime[key] = sum(vals_life) / len(vals_life) if vals_life else 0

    inverter_avgs_today = {}
    inverter_avgs_lifetime = {}
    for key, opts in by_inverter.items():
        vals_today = [o["today_wh"] for o in opts if o["today_wh"] > 0]
        vals_life = [o["lifetime_wh"] for o in opts if o["lifetime_wh"] > 0]
        inverter_avgs_today[key] = sum(vals_today) / len(vals_today) if vals_today else 0
        inverter_avgs_lifetime[key] = sum(vals_life) / len(vals_life) if vals_life else 0

    # ── Per-optimizer health analysis ──
    health_counts = {"excellent": 0, "good": 0, "fair": 0, "watch": 0, "alert": 0}
    underperformers = []

    for opt in optimizers:
        str_key = f"{opt['inverter']} / {opt['string']}"
        inv_key = opt["inverter"]

        # Lifetime performance ratio
        # This is the key metric: how does this panel compare to expected?
        lifetime_daily_avg_wh = 0
        if age_years > 0 and opt["lifetime_wh"] > 0:
            lifetime_daily_avg_wh = opt["lifetime_wh"] / (age_years * 365.25)

        # Ratios vs peers (using lifetime as most stable metric)
        ratio_vs_string = (opt["lifetime_wh"] / string_avgs_lifetime[str_key]
                           if string_avgs_lifetime.get(str_key, 0) > 0 else 1.0)
        ratio_vs_inverter = (opt["lifetime_wh"] / inverter_avgs_lifetime[inv_key]
                             if inverter_avgs_lifetime.get(inv_key, 0) > 0 else 1.0)
        ratio_vs_array = (opt["lifetime_wh"] / array_avg_lifetime
                          if array_avg_lifetime > 0 else 1.0)

        # Today ratios (more volatile but catches acute issues)
        today_ratio_vs_string = 0
        today_ratio_vs_array = 0
        if opt["today_wh"] > 0:
            today_ratio_vs_string = (opt["today_wh"] / string_avgs_today.get(str_key, 1)
                                     if string_avgs_today.get(str_key, 0) > 0 else 1.0)
            today_ratio_vs_array = (opt["today_wh"] / array_avg_today
                                    if array_avg_today > 0 else 1.0)

        # Use string peers as primary comparison (same conditions)
        # Fall back to array average if string data unavailable
        primary_ratio = ratio_vs_string if string_avgs_lifetime.get(str_key, 0) > 0 else ratio_vs_array
        status = classify_health(primary_ratio)
        health_counts[status] += 1

        # Flag underperformers
        if status in ("watch", "alert"):
            underperformers.append({
                "serial_number": opt["serial_number"],
                "inverter": opt["inverter"],
                "string": opt["string"],
                "status": status,
                "ratio_vs_string": round(ratio_vs_string, 3),
                "ratio_vs_array": round(ratio_vs_array, 3),
                "lifetime_wh": opt["lifetime_wh"],
                "today_wh": opt["today_wh"],
            })

        # Attach health data to optimizer
        opt["health"] = {
            "status": status,
            "lifetime_daily_avg_wh": round(lifetime_daily_avg_wh, 1),
            "ratio_vs_string": round(ratio_vs_string, 3),
            "ratio_vs_inverter": round(ratio_vs_inverter, 3),
            "ratio_vs_array": round(ratio_vs_array, 3),
            "today_ratio_vs_string": round(today_ratio_vs_string, 3),
            "today_ratio_vs_array": round(today_ratio_vs_array, 3),
        }

    # ── Array-wide summary ──
    lifetime_min = min(o["lifetime_wh"] for o in optimizers) if optimizers else 0
    lifetime_max = max(o["lifetime_wh"] for o in optimizers) if optimizers else 0
    lifetime_spread_pct = ((lifetime_max - lifetime_min) / array_avg_lifetime * 100
                           if array_avg_lifetime > 0 else 0)

    summary = {
        "panel_spec": {
            "model": PANEL_SPEC["model"],
            "nameplate_w": PANEL_SPEC["nameplate_w"],
            "ptc_w": PANEL_SPEC["ptc_w"],
            "install_date": PANEL_SPEC["install_date"],
            "age_years": round(age_years, 2),
            "tilt_deg": PANEL_SPEC["tilt_deg"],
            "azimuth_deg": PANEL_SPEC["azimuth_deg"],
        },
        "degradation": {
            "expected_factor": round(degradation_factor, 4),
            "expected_pct_remaining": round(degradation_factor * 100, 1),
            "degraded_ptc_w": round(degraded_ptc_w, 1),
        },
        "array_stats": {
            "avg_today_wh": round(array_avg_today, 1),
            "avg_lifetime_wh": round(array_avg_lifetime, 1),
            "avg_lifetime_kwh": round(array_avg_lifetime / 1000, 1),
            "avg_lifetime_daily_wh": round(
                array_avg_lifetime / (age_years * 365.25), 1
            ) if age_years > 0 else 0,
            "lifetime_spread_pct": round(lifetime_spread_pct, 1),
            "lifetime_min_wh": lifetime_min,
            "lifetime_max_wh": lifetime_max,
        },
        "health_counts": health_counts,
        "underperformers": underperformers,
        "string_averages": {
            k: {
                "avg_today_wh": round(string_avgs_today.get(k, 0), 1),
                "avg_lifetime_wh": round(v, 1),
                "panel_count": len(by_string[k]),
            }
            for k, v in string_avgs_lifetime.items()
        },
        "inverter_averages": {
            k: {
                "avg_today_wh": round(inverter_avgs_today.get(k, 0), 1),
                "avg_lifetime_wh": round(v, 1),
                "panel_count": len(by_inverter[k]),
            }
            for k, v in inverter_avgs_lifetime.items()
        },
    }

    return summary


def apply_color_health(optimizers, summary):
    """
    Overlay SolarEdge's per-optimizer 'color' metric onto the health status.

    'color' (0.0-1.0, period-relative, 1.0 = best in the period) is the
    portal's own relative-performance signal and is what the Layout view
    shows by eye. It catches an acutely bad optimizer faster than the
    lifetime-energy ratios, which are dominated by years of prior good
    production. We only let color *escalate* severity (never downgrade a
    panel the ratio math already flagged), then recompute the array-wide
    counts and underperformer list from the final per-optimizer statuses.
    """
    rank = {"excellent": 0, "good": 1, "fair": 2, "watch": 3, "alert": 4}

    for opt in optimizers:
        c = opt.get("color")
        health = opt.setdefault("health", {})
        if c is None:
            continue
        health["color"] = round(c, 4)
        color_status = None
        if c < COLOR_ALERT:
            color_status = "alert"
        elif c < COLOR_WATCH:
            color_status = "watch"
        if color_status and rank[color_status] > rank.get(health.get("status", "good"), 1):
            health["status"] = color_status

    # Recompute counts + underperformers from final statuses.
    counts = {"excellent": 0, "good": 0, "fair": 0, "watch": 0, "alert": 0}
    underperformers = []
    for opt in optimizers:
        status = opt.get("health", {}).get("status", "good")
        if status in counts:
            counts[status] += 1
        if status in ("watch", "alert"):
            h = opt["health"]
            underperformers.append({
                "serial_number": opt["serial_number"],
                "inverter": opt["inverter"],
                "string": opt["string"],
                "status": status,
                "color": h.get("color"),
                "ratio_vs_string": h.get("ratio_vs_string"),
                "ratio_vs_array": h.get("ratio_vs_array"),
                "lifetime_wh": opt["lifetime_wh"],
                "today_wh": opt["today_wh"],
            })

    summary["health_counts"] = counts
    summary["underperformers"] = underperformers
    return summary


def collect(config):
    """
    Main collection: authenticate, get layout, fetch energy data.

    Returns dict with all optimizer data or None on failure.
    """
    start_time = time.time()

    try:
        session = portal_session(config["username"], config["password"])
    except Exception as e:
        logger.error(f"Portal authentication failed: {e}")
        return None

    try:
        opt_map, inverter_info = get_cached_layout(session, config["site_id"])
    except Exception as e:
        logger.error(f"Layout fetch failed: {e}")
        return None

    # Mint the Cognito token for the /services/ energy API.
    try:
        token, uuid = get_se_token(config["username"], config["password"])
    except Exception as e:
        logger.error(f"SolarEdge token mint failed: {e}")
        return None

    inverter_serials = ",".join(
        inv["serial"] for inv in inverter_info if inv.get("serial"))

    # Fetch energy by explicit date range. DAY is fatal; the wider ranges are
    # best-effort so a slow/failed lifetime call never blocks the live grid.
    today = date.today()
    install = datetime.strptime(PANEL_SPEC["install_date"], "%Y-%m-%d").date()
    ranges = {
        "DAY":   (today, today),
        "WEEK":  (today - timedelta(days=6), today),
        "MONTH": (today.replace(day=1), today),
        "ALL":   (install, today),
    }

    energy = {}
    colors = {}
    for label, (start, end) in ranges.items():
        try:
            resp = get_energy(token, uuid, config["site_id"], inverter_serials,
                              start.isoformat(), end.isoformat())
            wh, col = _energy_by_serial(resp)
            energy[label] = wh
            if label == "DAY":
                colors = col
        except Exception as e:
            if label == "DAY":
                logger.error(f"DAY energy fetch failed (fatal): {e}")
                return None
            logger.warning(f"{label} energy fetch failed (non-fatal): {e}")
            energy[label] = {}

    # Build optimizer results, joining layout to energy on hardware serial.
    now = datetime.now()
    optimizers = []
    totals = {"today_wh": 0, "week_wh": 0, "month_wh": 0, "lifetime_wh": 0}

    for opt_id, info in sorted(opt_map.items(),
                                key=lambda x: (x[1]["inverter"], x[1]["string"], x[1]["serial_number"])):
        sn = info["serial_number"]
        today_wh = energy.get("DAY", {}).get(sn, 0)
        week_wh = energy.get("WEEK", {}).get(sn, 0)
        month_wh = energy.get("MONTH", {}).get(sn, 0)
        lifetime_wh = energy.get("ALL", {}).get(sn, 0)
        col = colors.get(sn)

        optimizers.append({
            "id": opt_id,
            "serial_number": sn,
            "inverter": info["inverter"],
            "inverter_sn": info["inverter_sn"],
            "string": info["string"],
            "name": info["name"],
            "today_wh": today_wh,
            "week_wh": week_wh,
            "month_wh": month_wh,
            "lifetime_wh": lifetime_wh,
            "color": round(col, 4) if col is not None else None,
        })

        totals["today_wh"] += today_wh
        totals["week_wh"] += week_wh
        totals["month_wh"] += month_wh
        totals["lifetime_wh"] += lifetime_wh

    active = sum(1 for o in optimizers if o["today_wh"] > 0)

    # ── Run health analysis ──
    health_summary = analyze_health(optimizers)
    apply_color_health(optimizers, health_summary)

    # ── Fetch current site power from cloud API ──
    # This is a single lightweight API call that gives us real-time watts
    # to distribute across panels (portal only has energy totals, not live power)
    site_power = get_current_site_power(config["site_id"], config.get("api_key", ""))
    current_power_w = 0
    if site_power:
        current_power_w = site_power.get("current_power_w", 0)
        logger.info(f"Cloud API: {current_power_w:.0f}W current site power")

    # Distribute current power proportionally based on today_wh
    # Panels that produced more energy today are likely producing more right now
    total_today = sum(o["today_wh"] for o in optimizers)
    for opt in optimizers:
        if current_power_w > 0 and total_today > 0:
            # Proportional distribution based on today's energy share
            share = opt["today_wh"] / total_today if opt["today_wh"] > 0 else 0
            opt["current_watts"] = round(current_power_w * share, 1)
        elif current_power_w > 0:
            # Even distribution as fallback (early morning, all panels equal)
            opt["current_watts"] = round(current_power_w / len(optimizers), 1)
        else:
            opt["current_watts"] = 0

    elapsed = time.time() - start_time

    result = {
        "timestamp": now.isoformat(),
        "collection_time_s": round(elapsed, 1),
        "site_id": config["site_id"],
        "total_optimizers": len(optimizers),
        "active_today": active,
        "current_power_w": current_power_w,
        "totals": totals,
        "health": health_summary,
        "inverters": inverter_info,
        "optimizers": optimizers,
    }

    # Log summary
    hc = health_summary["health_counts"]
    logger.info(f"Collected {len(optimizers)} optimizers in {elapsed:.1f}s: "
                f"{totals['today_wh']/1000:.1f} kWh today, {active} active")
    logger.info(f"Health: {hc['excellent']} excellent, {hc['good']} good, "
                f"{hc['fair']} fair, {hc['watch']} watch, {hc['alert']} alert")

    if health_summary["underperformers"]:
        for up in health_summary["underperformers"]:
            logger.warning(f"Underperformer: {up['serial_number']} on {up['string']} — "
                           f"{up['status'].upper()}, {up['ratio_vs_string']:.0%} of string avg")

    return result


def update_peaks(data):
    """
    Track per-panel peak daily production for health monitoring.

    Tracks:
      - peak_today_wh: highest single-day production we've recorded
      - peak_today_date: when that peak occurred
      - lifetime_wh: latest lifetime total (for degradation tracking)
      - first_seen / last_seen: tracking window
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    peaks = {}
    if PEAKS_JSON.exists():
        try:
            peaks = json.loads(PEAKS_JSON.read_text())
        except (json.JSONDecodeError, IOError):
            peaks = {}

    ts = data["timestamp"]
    today_date = ts[:10]  # YYYY-MM-DD
    updated_count = 0

    for opt in data["optimizers"]:
        sn = opt["serial_number"]
        today_wh = opt["today_wh"]
        lifetime_wh = opt["lifetime_wh"]

        if sn not in peaks:
            peaks[sn] = {
                "serial_number": sn,
                "inverter": opt["inverter"],
                "inverter_sn": opt["inverter_sn"],
                "string": opt["string"],
                "peak_today_wh": 0,
                "peak_today_date": None,
                "first_seen": ts,
                "lifetime_wh_at_first_seen": lifetime_wh,
            }

        # Update daily peak if current reading is higher
        if today_wh > peaks[sn].get("peak_today_wh", 0):
            peaks[sn]["peak_today_wh"] = today_wh
            peaks[sn]["peak_today_date"] = today_date
            updated_count += 1

        # Track peak instantaneous watts (persistent across restarts)
        current_w = opt.get("current_watts", 0)
        if current_w > peaks[sn].get("peak_watts", 0):
            peaks[sn]["peak_watts"] = current_w
            peaks[sn]["peak_watts_date"] = today_date

        # Always update lifetime and last_seen
        peaks[sn]["lifetime_wh"] = lifetime_wh
        peaks[sn]["last_seen"] = ts

        # Add health status from latest analysis
        if "health" in opt:
            peaks[sn]["health_status"] = opt["health"]["status"]
            peaks[sn]["ratio_vs_string"] = opt["health"]["ratio_vs_string"]

    PEAKS_JSON.write_text(json.dumps(peaks, indent=2))

    if updated_count > 0:
        logger.info(f"Peaks: {updated_count} new daily peaks recorded")


def save_current(data):
    """Save current snapshot for dashboard and inspection."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WEB_DIR.mkdir(parents=True, exist_ok=True)

    CURRENT_JSON.write_text(json.dumps(data, indent=2))
    WEB_JSON.write_text(json.dumps(data, indent=2))

    logger.info(f"Snapshot saved: {data['totals']['today_wh']/1000:.1f} kWh today, "
                f"{data['active_today']}/{data['total_optimizers']} active")


def update_daily_history(data):
    """
    Track per-panel daily max/avg watts across collection cycles.

    Maintains a JSON file keyed by date, then by serial number.
    Each panel entry tracks:
      - max_watts_today: highest current_watts seen today
      - cumulative_watts: sum of all current_watts readings (for avg calc)
      - samples: number of readings with current_watts > 0
      - first_seen_watts: first non-zero reading of the day
      - last_watts: most recent reading

    Resets automatically when the date changes.
    Similar to Enphase's enphase_daily_history_house.json format.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")

    # Load existing history
    history = {}
    if DAILY_HISTORY.exists():
        try:
            history = json.loads(DAILY_HISTORY.read_text())
        except (json.JSONDecodeError, IOError):
            history = {}

    # Get or create today's entry
    today_data = history.get(today, {})

    # Prune history older than 30 days (matches Enphase daily history retention)
    dates = sorted(history.keys())
    if len(dates) > 30:
        for old_date in dates[:-30]:
            del history[old_date]

    # Update each panel
    updated = 0
    for opt in data.get("optimizers", []):
        sn = opt["serial_number"]
        watts = opt.get("current_watts", 0)

        if sn not in today_data:
            today_data[sn] = {
                "first_seen_watts": 0,
                "max_watts_today": 0,
                "cumulative_watts": 0,
                "samples": 0,
                "last_watts": 0,
                "peak_max": 0,  # all-time max across days
            }

        panel = today_data[sn]

        # Carry forward peak_max from previous state
        # (also check yesterday's data for peak_max)
        if panel["peak_max"] == 0:
            for d in history:
                if d != today and sn in history[d]:
                    old_peak = history[d][sn].get("peak_max", 0)
                    old_max = history[d][sn].get("max_watts_today", 0)
                    panel["peak_max"] = max(panel["peak_max"], old_peak, old_max)

        if watts > 0:
            if panel["first_seen_watts"] == 0:
                panel["first_seen_watts"] = watts
            panel["max_watts_today"] = max(panel["max_watts_today"], watts)
            panel["cumulative_watts"] += watts
            panel["samples"] += 1
            panel["peak_max"] = max(panel["peak_max"], watts)
            updated += 1

        panel["last_watts"] = watts

    history[today] = today_data
    DAILY_HISTORY.write_text(json.dumps(history, indent=2))

    if updated > 0:
        logger.info(f"Daily history: {updated} panels updated "
                    f"(samples range: {min(p['samples'] for p in today_data.values())}-"
                    f"{max(p['samples'] for p in today_data.values())})")

    return today_data


def save_dashboard_json(data, config, daily_history=None):
    """
    Write solar_barn.json in the format the power dashboard expects.

    This replaces the old collect_solaredge.py output, using REAL serial
    numbers from the portal instead of synthetic ones. The dashboard reads
    this file for the Solar Arrays tab.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WEB_DIR.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    epoch = int(now.timestamp())
    optimizers = data.get("optimizers", [])
    health_summary = data.get("health", {})
    daily = daily_history or {}

    # Load peaks for persistent max_ever_watts (survives restarts)
    peaks = {}
    if PEAKS_JSON.exists():
        try:
            peaks = json.loads(PEAKS_JSON.read_text())
        except (json.JSONDecodeError, IOError):
            peaks = {}

    # Load full daily history for max_ever scanning across all retained days
    all_daily_history = {}
    if DAILY_HISTORY.exists():
        try:
            all_daily_history = json.loads(DAILY_HISTORY.read_text())
        except (json.JSONDecodeError, IOError):
            all_daily_history = {}

    # Build the inverters dict keyed by real serial number
    inverters = {}
    for opt in optimizers:
        sn = opt["serial_number"]
        h = opt.get("health", {})
        current_w = opt.get("current_watts", 0)
        dh = daily.get(sn, {})
        samples = dh.get("samples", 0)
        avg_today = round(dh["cumulative_watts"] / samples, 1) if samples > 0 else 0

        # max_ever_watts: highest instantaneous watts ever observed
        # Priority: peaks JSON peak_watts (persistent) > scan daily history > current reading
        peak_data = peaks.get(sn, {})
        best_watts = peak_data.get("peak_watts", 0)

        # If peaks JSON has no peak_watts yet, scan all daily history days
        if best_watts == 0:
            for d_date, d_panels in all_daily_history.items():
                if sn in d_panels:
                    day_max = d_panels[sn].get("max_watts_today", 0)
                    day_peak = d_panels[sn].get("peak_max", 0)
                    best_watts = max(best_watts, day_max, day_peak)

        # Last resort: use current reading as floor
        max_ever = best_watts or current_w

        inverters[sn] = {
            "serial": sn,
            "current_watts": current_w,
            "max_ever_watts": max_ever,
            "last_report_time": epoch,
            "last_report_human": now.strftime("%H:%M:%S"),
            "dev_type": 0,
            "max_today_watts": dh.get("max_watts_today", 0),
            "samples_today": samples,
            "avg_today_watts": avg_today,
            "manufacturer": "LG Solar",
            "model": PANEL_SPEC["model"],
            "optimizer": True,
            "parent_inverter": opt.get("inverter_sn", ""),
            "parent_name": opt.get("inverter", ""),
            "string": opt.get("string", ""),
            # Health data for dashboard overlay
            "health_status": h.get("status", ""),
            "health_ratio_vs_string": h.get("ratio_vs_string", 1.0),
            "health_ratio_vs_array": h.get("ratio_vs_array", 1.0),
            "lifetime_daily_avg_wh": h.get("lifetime_daily_avg_wh", 0),
            # Energy totals from portal
            "today_wh": opt.get("today_wh", 0),
            "week_wh": opt.get("week_wh", 0),
            "month_wh": opt.get("month_wh", 0),
            "lifetime_wh": opt.get("lifetime_wh", 0),
        }

    # Build underperformers list
    underperformers = []
    for up in health_summary.get("underperformers", []):
        underperformers.append({
            "serial": up["serial_number"],
            "pct_of_avg": round(up.get("ratio_vs_string", 1.0) * 100),
            "status": up.get("status", ""),
        })

    # Calculate summary stats
    today_wh_values = [o.get("today_wh", 0) for o in optimizers]
    active = sum(1 for v in today_wh_values if v > 0)
    total_today_wh = sum(today_wh_values)

    # Current power stats from distributed watts
    current_power_w = data.get("current_power_w", 0)
    watts_list = [inverters[sn]["current_watts"] for sn in inverters]
    active_now = sum(1 for w in watts_list if w > 0)
    avg_watts = current_power_w / len(optimizers) if optimizers else 0
    max_w = max(watts_list) if watts_list else 0
    min_w = min(w for w in watts_list if w > 0) if active_now > 0 else 0
    spread = max_w - min_w if active_now > 0 else 0

    dashboard_data = {
        "timestamp": now.isoformat(),
        "timestamp_epoch": epoch,
        "array_id": "barn",
        "array_name": "Barn",
        "array_type": "solaredge",
        "gateway": {
            "ip": "cloud",
            "serial": config["site_id"],
            "model": "SolarEdge",
        },
        "summary": {
            "total_watts": round(current_power_w),
            "total_kw": round(current_power_w / 1000, 2),
            "panel_count": len(optimizers),
            "active_count": active_now if current_power_w > 0 else active,
            "average_watts": round(avg_watts),
            "min_watts": round(min_w),
            "max_watts": round(max_w),
            "total_max_ever": 0,
            "spread": round(spread),
            # Extended stats from health analysis
            "today_kwh": round(total_today_wh / 1000, 2),
            "lifetime_spread_pct": health_summary.get("array_stats", {}).get("lifetime_spread_pct", 0),
        },
        "production": {
            "inverters_wh_lifetime": data.get("totals", {}).get("lifetime_wh", 0),
            "inverters_wh_today": total_today_wh,
            "inverters_active": active_now if current_power_w > 0 else active,
            "meter_w_now": round(current_power_w, 1),
            "meter_wh_today": float(total_today_wh),
            "meter_wh_lifetime": float(data.get("totals", {}).get("lifetime_wh", 0)),
        },
        "inverters": inverters,
        "underperformers": underperformers,
        "layout": {},  # User arranges via layout editor
        "collection_status": "ok",
    }

    # Write to both data/ and web/
    barn_data = DATA_DIR / "solar_barn.json"
    barn_web = WEB_DIR / "solar_barn.json"

    barn_data.write_text(json.dumps(dashboard_data, indent=2))
    barn_web.write_text(json.dumps(dashboard_data, indent=2))

    logger.info(f"Dashboard JSON: solar_barn.json updated ({len(inverters)} panels, "
                f"real serial numbers)")


def print_health_report(data):
    """Print a formatted health report for --test mode."""
    h = data["health"]
    ps = h["panel_spec"]
    deg = h["degradation"]
    stats = h["array_stats"]
    hc = h["health_counts"]

    print(f"\n{'=' * 80}")
    print(f"  SOLAREDGE PANEL HEALTH REPORT")
    print(f"  {data['timestamp'][:19]}")
    print(f"{'=' * 80}")

    print(f"\n  Panel: {ps['model']}  |  {ps['nameplate_w']}W nameplate  |  {ps['ptc_w']}W PTC")
    print(f"  Install: {ps['install_date']}  |  Age: {ps['age_years']:.1f} years")
    print(f"  Array: {ps['tilt_deg']}deg tilt, {ps['azimuth_deg']}deg azimuth (ground mount)")
    print(f"  Degradation: {deg['expected_pct_remaining']}% remaining -> {deg['degraded_ptc_w']}W effective PTC")

    print(f"\n  {'-' * 76}")
    print(f"  ARRAY STATISTICS")
    print(f"  {'-' * 76}")
    print(f"  Today:    {stats['avg_today_wh']:.0f} Wh avg/panel  |  "
          f"{data['totals']['today_wh']/1000:.1f} kWh total  |  "
          f"{data['active_today']}/{data['total_optimizers']} active")
    print(f"  Lifetime: {stats['avg_lifetime_kwh']:.1f} kWh avg/panel  |  "
          f"{data['totals']['lifetime_wh']/1000000:.1f} MWh total")
    print(f"  Daily avg over lifetime: {stats['avg_lifetime_daily_wh']:.0f} Wh/panel")
    print(f"  Panel spread: {stats['lifetime_spread_pct']:.1f}% "
          f"({stats['lifetime_min_wh']/1000000:.2f} - {stats['lifetime_max_wh']/1000000:.2f} MWh)")

    print(f"\n  {'-' * 76}")
    print(f"  HEALTH DISTRIBUTION")
    print(f"  {'-' * 76}")
    icons = {"excellent": "+", "good": "=", "fair": "~", "watch": "?", "alert": "!"}
    for status in ["excellent", "good", "fair", "watch", "alert"]:
        count = hc[status]
        bar = "#" * count
        icon = icons[status]
        pct = count / data["total_optimizers"] * 100 if data["total_optimizers"] > 0 else 0
        print(f"  {icon} {status:<10} {count:>3} ({pct:>5.1f}%)  {bar}")

    # String comparison
    if h["string_averages"]:
        print(f"\n  {'-' * 76}")
        print(f"  STRING COMPARISON (Lifetime Avg)")
        print(f"  {'-' * 76}")
        for key, sa in sorted(h["string_averages"].items()):
            print(f"  {key:<35} {sa['panel_count']:>2} panels  "
                  f"{sa['avg_lifetime_wh']/1000000:>6.2f} MWh avg  "
                  f"{sa['avg_today_wh']:>6.0f} Wh today")

    # Per-panel detail
    print(f"\n  {'-' * 76}")
    print(f"  {'SN':<16} {'String':<12} {'Today':>8} {'Lifetime':>10} "
          f"{'vs Str':>7} {'vs Arr':>7} {'Status':<10}")
    print(f"  {'-'*16} {'-'*12} {'-'*8} {'-'*10} {'-'*7} {'-'*7} {'-'*10}")

    for opt in data["optimizers"]:
        ht = opt.get("health", {})
        status = ht.get("status", "?")
        marker = " "
        if status == "alert":
            marker = "!"
        elif status == "watch":
            marker = "?"
        elif status == "excellent":
            marker = "*"

        ratio_str = f"{ht.get('ratio_vs_string', 0)*100:>5.1f}%"
        ratio_arr = f"{ht.get('ratio_vs_array', 0)*100:>5.1f}%"

        print(f"{marker} {opt['serial_number']:<16} {opt['string']:<12} "
              f"{opt['today_wh']:>7.0f}W {opt['lifetime_wh']/1000000:>9.2f}M "
              f"{ratio_str} {ratio_arr} "
              f"{status:<10}")

    # Underperformers
    if h["underperformers"]:
        print(f"\n  {'-' * 76}")
        print(f"  ! UNDERPERFORMERS REQUIRING ATTENTION")
        print(f"  {'-' * 76}")
        for up in h["underperformers"]:
            deficit = (1.0 - up["ratio_vs_string"]) * 100
            print(f"  {up['serial_number']} on {up['string']}: "
                  f"{up['status'].upper()} -- {deficit:.1f}% below string avg "
                  f"(lifetime: {up['lifetime_wh']/1000000:.2f} MWh)")
    else:
        print(f"\n  All panels performing within normal range")

    print(f"\n{'=' * 80}\n")


def store_to_db(data):
    """Write optimizer readings to SQLite solaredge_readings table."""
    if not DB_AVAILABLE:
        return

    db_init()
    ts = data["timestamp"][:19].replace("T", " ")
    count = 0

    for opt in data["optimizers"]:
        health = opt.get("health", {})
        db_store.solaredge_reading(
            timestamp=ts,
            serial_number=opt["serial_number"],
            site_id=data.get("site_id", "1241660"),
            inverter=opt.get("inverter"),
            inverter_sn=opt.get("inverter_sn"),
            string=opt.get("string"),
            today_wh=opt.get("today_wh"),
            week_wh=opt.get("week_wh"),
            month_wh=opt.get("month_wh"),
            lifetime_wh=opt.get("lifetime_wh"),
            current_power_w=opt.get("current_watts"),
            health_status=health.get("status"),
            health_ratio_vs_string=health.get("ratio_vs_string"),
            health_ratio_vs_array=health.get("ratio_vs_array"),
        )
        count += 1

    logger.info(f"SQLite: {count} optimizer readings stored")


def main():
    parser = argparse.ArgumentParser(description="SolarEdge per-panel data collector")
    parser.add_argument("--test", action="store_true", help="Print results only, don't save")
    parser.add_argument("--json", action="store_true", help="Output JSON to stdout")
    args = parser.parse_args()

    config = get_config()

    if not config["username"] or not config["password"]:
        logger.error("Portal credentials not configured.")
        logger.error("Set SOLAREDGE_USERNAME and SOLAREDGE_PASSWORD in .env or environment")
        sys.exit(1)

    data = collect(config)
    if data is None:
        logger.error("Collection failed")
        sys.exit(1)

    if args.json:
        print(json.dumps(data, indent=2))
        return

    if args.test:
        print_health_report(data)
        return

    # Production mode: save everything
    update_peaks(data)
    save_current(data)
    daily_history = update_daily_history(data)
    if config.get("modbus_enabled"):
        logger.info("Modbus owns solar_barn.json; writing health overlay only")
    else:
        save_dashboard_json(data, config, daily_history)
    store_to_db(data)

    logger.info("Collection complete")


if __name__ == "__main__":
    main()
