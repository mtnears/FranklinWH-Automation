#!/usr/bin/env python3
"""
SolarEdge Solar Array Data Collector

Collects per-optimizer (panel-level) production data from SolarEdge cloud API
and writes a JSON file compatible with the dashboard's solar array renderer.

Usage:
    # Multi-array mode (reads SOLAR_ARRAY_{ID}_* env vars):
    python3 collect_solaredge.py --array-id barn

    # The output file is written to both data/ and web/ directories:
    #   data/solar_barn.json   (persistent)
    #   web/solar_barn.json    (served by dashboard)

SolarEdge API endpoints used:
    - /site/{id}/overview      → current power, today/lifetime energy
    - /site/{id}/inventory     → list of optimizers (serial numbers)
    - /site/{id}/power         → 15-min resolution power data (site total)
    - /equipment/{id}/{sn}/data → per-inverter telemetry (includes optimizer data)

Rate limits: 300 requests/day per API key, 3 concurrent max.
Collection every 5 minutes = 288/day, well within limits.

Output format matches collect_enphase.py for dashboard compatibility.
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path

# ===== Logging Setup =====
# ===== Paths =====
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DATA_DIR = PROJECT_ROOT / "data"
WEB_DIR = PROJECT_ROOT / "web"
LOG_DIR = PROJECT_ROOT / "logs"

# Try loading .env if python-dotenv available
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_DIR / "solaredge_collector.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# SolarEdge API base
SE_API_BASE = "https://monitoringapi.solaredge.com"


def se_api_get(endpoint: str, api_key: str, params: dict = None) -> dict:
    """Make a GET request to the SolarEdge API."""
    url = f"{SE_API_BASE}{endpoint}"
    # Build query string
    query_params = {"api_key": api_key}
    if params:
        query_params.update(params)
    query_string = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in query_params.items())
    full_url = f"{url}?{query_string}"

    req = urllib.request.Request(full_url)
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        logger.error("SolarEdge API error: HTTP %d for %s", e.code, endpoint)
        if e.code == 429:
            logger.error("Rate limit exceeded - reduce collection frequency")
        elif e.code == 403:
            logger.error("Forbidden - check API key and site permissions")
        raise
    except urllib.error.URLError as e:
        logger.error("SolarEdge API connection error: %s", e.reason)
        raise


import urllib.parse


def get_site_overview(site_id: str, api_key: str) -> dict:
    """Get current power and energy overview for the site."""
    return se_api_get(f"/site/{site_id}/overview", api_key)


def get_site_inventory(site_id: str, api_key: str) -> dict:
    """Get equipment inventory including all optimizers."""
    return se_api_get(f"/site/{site_id}/inventory", api_key)


def get_site_power(site_id: str, api_key: str, start_time: str, end_time: str) -> dict:
    """Get site power measurements in 15-min resolution."""
    return se_api_get(f"/site/{site_id}/power", api_key, {
        "startTime": start_time,
        "endTime": end_time,
    })


def get_inverter_data(site_id: str, api_key: str, serial: str,
                      start_time: str, end_time: str) -> dict:
    """Get technical data for a specific inverter."""
    return se_api_get(f"/equipment/{site_id}/{serial}/data", api_key, {
        "startTime": start_time,
        "endTime": end_time,
    })


def collect_solaredge(array_id: str, config: dict) -> dict:
    """
    Collect SolarEdge data and return dashboard-compatible output.

    Rate limit strategy (300 requests/day):
      - Overview: 1 request per collection (every 5 min = 288/day)
      - Inventory: cached, refreshed once per hour (24/day)
      - Inverter data: skipped to stay within limits
      Total: ~312/day — tight but viable at 5-min interval.
      At 6-min interval: ~240+24 = 264/day — comfortable.

    Args:
        array_id: Array identifier (e.g., 'barn')
        config: Dict with keys: site_id, api_key, name, model (optional)
    """
    site_id = config["site_id"]
    api_key = config["api_key"]
    array_name = config.get("name", array_id)
    model = config.get("model", "SolarEdge")

    logger.info("Starting SolarEdge collection: %s (%s) site=%s",
                array_name, array_id, site_id)

    now = datetime.now()

    # ===== 1. Site Overview (current power, today's energy) =====
    # This is 1 request and gives us the most important data
    overview_data = {}
    try:
        overview = get_site_overview(site_id, api_key)
        overview_data = overview.get("overview", {})
        logger.info("Site overview: %.1f W current, %.1f Wh today",
                     overview_data.get("currentPower", {}).get("power", 0),
                     overview_data.get("lastDayData", {}).get("energy", 0))
    except Exception as e:
        logger.warning("Could not get site overview: %s", e)

    # ===== 2. Inventory (optimizer serial numbers) — CACHED =====
    # Only refresh once per hour to conserve API calls
    inventory_cache_file = DATA_DIR / f"solaredge_inventory_{array_id}.json"
    optimizers = []
    inverters_info = []
    cache_stale = True

    if inventory_cache_file.exists():
        try:
            with open(inventory_cache_file) as f:
                cached = json.load(f)
            cache_age = now.timestamp() - cached.get("cached_at", 0)
            if cache_age < 3600:  # 1 hour
                optimizers = cached.get("optimizers", [])
                inverters_info = cached.get("inverters", [])
                cache_stale = False
                logger.info("Using cached inventory: %d optimizers, %d inverters (%.0fm old)",
                            len(optimizers), len(inverters_info), cache_age / 60)
        except Exception:
            pass

    if cache_stale:
        try:
            inventory = get_site_inventory(site_id, api_key)
            inv_data = inventory.get("Inventory", {})
            inverters_info = inv_data.get("inverters", [])
            optimizers = inv_data.get("optimizers", [])
            logger.info("Refreshed inventory: %d inverter(s), %d optimizer(s)",
                         len(inverters_info), len(optimizers))
            # Cache it
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(inventory_cache_file, "w") as f:
                json.dump({
                    "cached_at": now.timestamp(),
                    "inverters": inverters_info,
                    "optimizers": optimizers,
                }, f, indent=2)
        except Exception as e:
            logger.warning("Could not get inventory: %s", e)

    # ===== 3. Load historical max watts from tracking file =====
    max_watts_file = DATA_DIR / f"solaredge_max_watts_{array_id}.json"
    max_watts_data = {}
    if max_watts_file.exists():
        try:
            with open(max_watts_file) as f:
                max_watts_data = json.load(f)
        except Exception:
            pass

    # ===== Build output in dashboard-compatible format =====
    current_power = overview_data.get("currentPower", {}).get("power", 0)
    today_energy = overview_data.get("lastDayData", {}).get("energy", 0)
    lifetime_energy = overview_data.get("lifeTimeData", {}).get("energy", 0)

    panel_count = len(optimizers) if optimizers else 0

    # Build per-optimizer entries
    # SolarEdge API doesn't provide per-optimizer real-time watts.
    # We distribute current site power evenly across all optimizers.
    # This gives a reasonable visual representation on the dashboard.
    inverters_dict = {}
    avg_watts = current_power / panel_count if panel_count > 0 else 0

    for opt in optimizers:
        serial = opt.get("SN", opt.get("serialNumber", ""))
        if not serial:
            continue

        # Track max ever watts per optimizer
        prev_max = max_watts_data.get(serial, 0)
        est_watts = round(avg_watts)
        new_max = max(prev_max, est_watts)
        max_watts_data[serial] = new_max

        inverters_dict[serial] = {
            "serial": serial,
            "current_watts": est_watts,
            "max_ever_watts": new_max,
            "last_report_time": int(now.timestamp()),
            "last_report_human": now.strftime("%H:%M:%S"),
            "dev_type": 0,
            "max_today_watts": 0,
            "samples_today": 0,
            "avg_today_watts": 0,
            "manufacturer": opt.get("manufacturer", "SolarEdge"),
            "model": opt.get("model", model),
            "optimizer": True,
        }

    # If no optimizer inventory (API doesn't return individual optimizers),
    # generate panel entries from each inverter's connectedOptimizers count.
    # This gives us the correct number of draggable tiles on the dashboard.
    if not inverters_dict and inverters_info:
        # Calculate total connected optimizers for power distribution
        total_optimizers = sum(inv.get("connectedOptimizers", 0) for inv in inverters_info)
        if total_optimizers == 0:
            # Fallback: just use inverters as panels
            total_optimizers = len(inverters_info)

        for inv in inverters_info:
            inv_serial = inv.get("SN", "")
            inv_name = inv.get("name", "Inverter")
            connected = inv.get("connectedOptimizers", 1)
            inv_model = inv.get("model", model)

            # Distribute site power proportionally by optimizer count
            inv_share = current_power * (connected / total_optimizers) if total_optimizers > 0 else 0
            per_panel_watts = round(inv_share / connected) if connected > 0 else 0

            for i in range(1, connected + 1):
                # Generate a synthetic serial: INV_SERIAL-OPT_NUM
                # e.g., "731ED2B5-18-P01" through "731ED2B5-18-P36"
                opt_serial = f"{inv_serial}-P{i:02d}"

                prev_max = max_watts_data.get(opt_serial, 0)
                new_max = max(prev_max, per_panel_watts)
                max_watts_data[opt_serial] = new_max

                inverters_dict[opt_serial] = {
                    "serial": opt_serial,
                    "current_watts": per_panel_watts,
                    "max_ever_watts": new_max,
                    "last_report_time": int(now.timestamp()),
                    "last_report_human": now.strftime("%H:%M:%S"),
                    "dev_type": 0,
                    "max_today_watts": 0,
                    "samples_today": 0,
                    "avg_today_watts": 0,
                    "manufacturer": inv.get("manufacturer", "SolarEdge"),
                    "model": inv_model,
                    "optimizer": True,
                    "parent_inverter": inv_serial,
                    "parent_name": inv_name,
                }

        panel_count = len(inverters_dict)
        logger.info("Generated %d panel entries from %d inverters (%s)",
                     panel_count,
                     len(inverters_info),
                     ", ".join(f"{inv.get('name','?')}={inv.get('connectedOptimizers',0)}"
                              for inv in inverters_info))

    # Save max watts tracking
    if max_watts_data:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(max_watts_file, "w") as f:
                json.dump(max_watts_data, f, indent=2)
        except Exception:
            pass

    # Determine active count
    active_count = sum(1 for v in inverters_dict.values()
                       if v.get("current_watts", 0) > 0)

    # Find max/min for spread
    watts_list = [v.get("current_watts", 0) for v in inverters_dict.values()]
    max_w = max(watts_list) if watts_list else 0
    min_w = min(watts_list) if watts_list else 0
    spread = max_w - min_w if active_count > 0 else 0

    output = {
        "timestamp": now.isoformat(),
        "timestamp_epoch": int(now.timestamp()),
        "array_id": array_id,
        "array_name": array_name,
        "array_type": "solaredge",
        "gateway": {
            "ip": "cloud",
            "serial": site_id,
            "model": model,
        },
        "summary": {
            "total_watts": round(current_power),
            "total_kw": round(current_power / 1000, 2),
            "panel_count": panel_count,
            "active_count": active_count,
            "average_watts": round(avg_watts),
            "min_watts": min_w,
            "max_watts": max_w,
            "total_max_ever": 0,
            "spread": spread,
        },
        "production": {
            "inverters_wh_lifetime": round(lifetime_energy),
            "inverters_wh_today": round(today_energy),
            "inverters_active": active_count,
            "meter_w_now": round(current_power, 1),
            "meter_wh_today": round(today_energy, 3),
            "meter_wh_lifetime": round(lifetime_energy, 3),
        },
        "inverters": inverters_dict,
        "underperformers": [],
        "layout": {},
        "collection_status": "ok",
    }

    return output


def write_output(output: dict, array_id: str):
    """Write output JSON to data/ and web/ directories."""
    filename = f"solar_{array_id}.json"

    # Write to data dir
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data_file = DATA_DIR / filename
    with open(data_file, "w") as f:
        json.dump(output, f, indent=2)

    # Write to web dir
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    web_file = WEB_DIR / filename
    with open(web_file, "w") as f:
        json.dump(output, f, indent=2)

    logger.info("Data written: %d panels, %d W total, %d active → %s",
                output["summary"]["panel_count"],
                output["summary"]["total_watts"],
                output["summary"]["active_count"],
                filename)


def main():
    parser = argparse.ArgumentParser(
        description="Collect SolarEdge per-panel solar data")
    parser.add_argument("--array-id", required=True,
                        help="Array identifier (matches SOLAR_ARRAY_{ID}_* env vars)")
    args = parser.parse_args()

    array_id = args.array_id.lower()
    prefix = f"SOLAR_ARRAY_{array_id.upper()}_"

    # Read config from environment
    site_id = os.getenv(f"{prefix}SITE_ID", "")
    api_key = os.getenv(f"{prefix}API_KEY", "")
    name = os.getenv(f"{prefix}NAME", array_id)
    model = os.getenv(f"{prefix}MODEL", "SolarEdge")

    if not site_id:
        logger.error("Missing %sSITE_ID", prefix)
        sys.exit(1)
    if not api_key:
        logger.error("Missing %sAPI_KEY", prefix)
        sys.exit(1)

    config = {
        "site_id": site_id,
        "api_key": api_key,
        "name": name,
        "model": model,
    }

    try:
        output = collect_solaredge(array_id, config)
        write_output(output, array_id)
    except Exception as e:
        logger.error("Collection failed: %s", e)
        # Write error status so dashboard shows something
        error_output = {
            "timestamp": datetime.now().isoformat(),
            "timestamp_epoch": int(time.time()),
            "array_id": array_id,
            "array_name": name,
            "array_type": "solaredge",
            "gateway": {"ip": "cloud", "serial": site_id, "model": model},
            "summary": {
                "total_watts": 0, "total_kw": 0, "panel_count": 0,
                "active_count": 0, "average_watts": 0, "min_watts": 0,
                "max_watts": 0, "total_max_ever": 0, "spread": 0,
            },
            "production": {
                "inverters_wh_lifetime": 0, "inverters_wh_today": 0,
                "inverters_active": 0, "meter_w_now": 0,
                "meter_wh_today": 0, "meter_wh_lifetime": 0,
            },
            "inverters": {},
            "underperformers": [],
            "layout": {},
            "collection_status": "error",
            "error": str(e),
        }
        write_output(error_output, array_id)
        sys.exit(1)


if __name__ == "__main__":
    main()
