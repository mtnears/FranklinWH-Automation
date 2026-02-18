#!/usr/bin/env python3
"""
FranklinWH Modbus vs Cloud API - Data Collection Logger
=========================================================
Collects key register values from Modbus and cloud API simultaneously,
logs to CSV for analysis. Run multiple times throughout the day to build
a dataset for register mapping.

Usage:
  python3 modbus_collect.py                    # Single read, append to log
  python3 modbus_collect.py --loops 5 --interval 120  # 5 reads, 2 min apart
  python3 modbus_collect.py --modbus-only      # Skip cloud API

Output: modbus_collection.csv (appended each run)
"""

import sys
import os
import csv
import time
import argparse
from datetime import datetime

# --- Load .env file for credentials ---
try:
    from dotenv import load_dotenv
    import pathlib
    script_dir = pathlib.Path(__file__).resolve().parent
    for env_path in [
        script_dir / ".env",
        script_dir.parent / ".env",
        pathlib.Path("/volume1/docker/franklin-git/.env"),
        pathlib.Path("/volume1/docker/franklin/.env"),
    ]:
        if env_path.exists():
            load_dotenv(env_path)
            break
except ImportError:
    pass

# --- Modbus ---
try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    print("ERROR: pymodbus not installed. Run: pip install pymodbus")
    sys.exit(1)

# --- Cloud API ---
try:
    from franklinwh import Client, TokenFetcher
except ImportError:
    Client = None
    TokenFetcher = None

# =============================================================================
# CONFIGURATION
# =============================================================================
AGATE_IP = os.environ.get("AGATE_IP", "192.168.5.149")
MODBUS_PORT = int(os.environ.get("MODBUS_PORT", "502"))

FRANKLIN_USER = os.environ.get("FRANKLIN_USERNAME", "")
FRANKLIN_PASS = os.environ.get("FRANKLIN_PASSWORD", "")
GATEWAY_ID = os.environ.get("FRANKLIN_GATEWAY_ID", os.environ.get("GATEWAY_ID", ""))

# Log file location - same directory as script
LOG_DIR = pathlib.Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "modbus_collection.csv"

# SunSpec model addresses (from discovery)
MODELS = {
    "m701": {"addr": 72,   "length": 153},  # AC Measurement
    "m702": {"addr": 227,  "length": 50},   # DC Measurement / Nameplate
    "m703": {"addr": 279,  "length": 17},   # Watt-Hours
    "m713": {"addr": 1035, "length": 7},    # DER Status (SOC)
    "m502": {"addr": 1098, "length": 28},   # Solar
}

# Key register offsets we want to track (from tonight's successful read)
# Format: (model, offset, name, interpretation_notes)
KEY_REGISTERS = [
    # Model 713 - DER Status
    ("m713", 0, "m713_rated_pwr_hi",    "30000 = rated power high word"),
    ("m713", 1, "m713_rated_pwr_lo",    "rated power low word"),
    ("m713", 2, "m713_soc",             "SOC × 10 (340 = 34.0%)"),
    ("m713", 3, "m713_batt_voltage",    "Battery voltage × 10 (993 = 99.3V)"),
    # Model 701 - AC Measurement (candidates from tonight's mapping)
    ("m701", 0,  "m701_ac_type",        "AC type (0=?,1=single,2=split)"),
    ("m701", 1,  "m701_state",          "State (1=Off,2=On,3=Fault)"),
    ("m701", 2,  "m701_inv_state",      "Inverter state"),
    ("m701", 3,  "m701_conn_state",     "Connection state"),
    ("m701", 8,  "m701_off8_power",     "Active power? 2600=grid 2.6kW?"),
    ("m701", 9,  "m701_off9_va",        "Apparent power?"),
    ("m701", 10, "m701_off10_signed",   "Signed - reactive? or battery?"),
    ("m701", 11, "m701_off11",          "PF? or load?"),
    ("m701", 12, "m701_off12_current",  "Current? 218=21.8A?"),
    ("m701", 13, "m701_off13_voltage",  "Voltage L-L? 2489=248.9V"),
    ("m701", 14, "m701_off14_voltage",  "Voltage L-N? 2489=248.9V"),
    ("m701", 16, "m701_freq_hz",        "Frequency (60000=60.000Hz)"),
    # Phase A
    ("m701", 33, "m701_temp_amb",       "Ambient temp? ÷10 °C"),
    ("m701", 34, "m701_temp_cab",       "Cabinet temp? ÷10 °C"),
    ("m701", 37, "m701_w_l1",           "Power phase A?"),
    ("m701", 39, "m701_w_l2",           "Power phase B?"),
    ("m701", 43, "m701_a_l1",           "Current phase A?"),
    ("m701", 44, "m701_v_l1",           "Voltage phase A?"),
    # Phase B
    ("m701", 62, "m701_off62",          "Phase B power?"),
    ("m701", 63, "m701_off63",          "Phase B VA?"),
    ("m701", 66, "m701_off66_a_l2",     "Phase B current?"),
    # Energy accumulators (uint32 pairs)
    ("m701", 19, "m701_wh_inj_hi",      "Total Wh injected hi"),
    ("m701", 20, "m701_wh_inj_lo",      "Total Wh injected lo"),
    ("m701", 23, "m701_wh_abs_hi",      "Total Wh absorbed hi"),
    ("m701", 24, "m701_wh_abs_lo",      "Total Wh absorbed lo"),
    # Model 703 - Watt-Hours
    ("m703", 1,  "m703_tot_wh_inj",     "Total Wh injected"),
    ("m703", 2,  "m703_tot_wh_abs",     "Total Wh absorbed"),
    ("m703", 4,  "m703_wh_inj_l1",      "Wh injected L1"),
    ("m703", 6,  "m703_wh_abs_l1",      "Wh absorbed L1"),
    ("m703", 8,  "m703_wh_inj_l2",      "Wh injected L2"),
    # Model 502 - Solar
    ("m502", 19, "m502_off19",          "Solar field (12 at night)"),
    ("m502", 20, "m502_off20",          "Solar field (20532 at night)"),
]

# CSV column headers
CSV_COLUMNS = (
    ["timestamp", "modbus_ms", "cloud_ms"]
    + [r[2] for r in KEY_REGISTERS]
    + [
        "cloud_soc", "cloud_battery_kw", "cloud_grid_kw",
        "cloud_solar_kw", "cloud_home_load_kw",
        "cloud_grid_to_bat", "cloud_solar_to_bat",
        "cloud_mode", "cloud_run_status",
    ]
)


def uint16_to_int16(val):
    return val - 0x10000 if val >= 0x8000 else val


def read_modbus_all(ip, port):
    """Read all models, return dict of model_key -> list of uint16 values."""
    client = ModbusTcpClient(ip, port=port, timeout=10)
    if not client.connect():
        print("  FAILED to connect to Modbus!")
        return None, 0

    t0 = time.monotonic()
    data = {}
    for key, info in MODELS.items():
        results = []
        remaining = info["length"]
        offset = 0
        while remaining > 0:
            count = min(remaining, 100)
            try:
                rr = client.read_holding_registers(info["addr"] + offset, count=count)
                if rr.isError():
                    print(f"  ERROR reading {key} at {info['addr'] + offset}: {rr}")
                    break
                results.extend(rr.registers)
            except Exception as e:
                print(f"  EXCEPTION reading {key}: {e}")
                break
            offset += count
            remaining -= count
        if results:
            data[key] = results

    client.close()
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return data, elapsed_ms


def read_cloud():
    """Fetch cloud API data, return dict."""
    if not Client or not FRANKLIN_USER:
        return None, 0

    import asyncio

    async def _fetch():
        fetcher = TokenFetcher(FRANKLIN_USER, FRANKLIN_PASS)
        client = Client(fetcher, GATEWAY_ID)
        stats = await client.get_stats()

        data = {
            "soc": stats.current.battery_soc,
            "battery_kw": stats.current.battery_use,
            "grid_kw": stats.current.grid_use,
            "solar_kw": stats.current.solar_production,
            "home_load_kw": stats.current.home_load,
        }

        try:
            status = await client._status()
            if isinstance(status, dict):
                data["grid_to_bat"] = status.get("soChGrid", 0)
                data["solar_to_bat"] = status.get("soChBat", 0)
                data["mode"] = status.get("mode")
                data["run_status"] = status.get("run_status")
        except Exception:
            pass

        return data

    t0 = time.monotonic()
    try:
        result = asyncio.run(_fetch())
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return result, elapsed_ms
    except Exception as e:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        print(f"  Cloud API error: {e}")
        return None, elapsed_ms


def collect_one(modbus_only=False):
    """Do one collection cycle, return a dict for CSV row."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"  [{timestamp}] Reading Modbus...", end="", flush=True)

    modbus_data, modbus_ms = read_modbus_all(AGATE_IP, MODBUS_PORT)
    print(f" {modbus_ms}ms", end="")

    cloud_data = None
    cloud_ms = 0
    if not modbus_only:
        print(f"  Cloud...", end="", flush=True)
        cloud_data, cloud_ms = read_cloud()
        print(f" {cloud_ms}ms", end="")

    print()  # newline

    # Build row
    row = {
        "timestamp": timestamp,
        "modbus_ms": modbus_ms,
        "cloud_ms": cloud_ms,
    }

    # Extract key registers
    if modbus_data:
        for model_key, offset, col_name, _notes in KEY_REGISTERS:
            regs = modbus_data.get(model_key)
            if regs and offset < len(regs):
                row[col_name] = regs[offset]
            else:
                row[col_name] = ""
    else:
        for _, _, col_name, _ in KEY_REGISTERS:
            row[col_name] = ""

    # Cloud values
    if cloud_data:
        row["cloud_soc"] = cloud_data.get("soc", "")
        row["cloud_battery_kw"] = cloud_data.get("battery_kw", "")
        row["cloud_grid_kw"] = cloud_data.get("grid_kw", "")
        row["cloud_solar_kw"] = cloud_data.get("solar_kw", "")
        row["cloud_home_load_kw"] = cloud_data.get("home_load_kw", "")
        row["cloud_grid_to_bat"] = cloud_data.get("grid_to_bat", "")
        row["cloud_solar_to_bat"] = cloud_data.get("solar_to_bat", "")
        row["cloud_mode"] = cloud_data.get("mode", "")
        row["cloud_run_status"] = cloud_data.get("run_status", "")
    else:
        for col in CSV_COLUMNS:
            if col.startswith("cloud_") and col not in row:
                row[col] = ""

    # Quick SOC comparison to console
    if modbus_data and "m713" in modbus_data:
        m_soc = modbus_data["m713"][2] / 10.0 if len(modbus_data["m713"]) > 2 else None
        c_soc = cloud_data.get("soc") if cloud_data else None
        if m_soc is not None:
            soc_str = f"SOC: {m_soc:.1f}% (Modbus)"
            if c_soc is not None:
                diff = abs(m_soc - c_soc)
                match = "✓" if diff < 2.0 else "✗"
                soc_str += f" vs {c_soc:.1f}% (Cloud) {match}"
            print(f"    {soc_str}")

    # Quick power summary
    if cloud_data:
        print(f"    Grid: {cloud_data.get('grid_kw', '?')} kW | "
              f"Solar: {cloud_data.get('solar_kw', '?')} kW | "
              f"Batt: {cloud_data.get('battery_kw', '?')} kW | "
              f"Load: {cloud_data.get('home_load_kw', '?')} kW")

    return row


def main():
    parser = argparse.ArgumentParser(
        description="Collect Modbus vs Cloud API data to CSV"
    )
    parser.add_argument("--loops", type=int, default=1,
                        help="Number of collection cycles (default: 1)")
    parser.add_argument("--interval", type=int, default=300,
                        help="Seconds between cycles (default: 300 = 5 min)")
    parser.add_argument("--modbus-only", action="store_true",
                        help="Skip cloud API calls")
    parser.add_argument("--log-file", type=str, default=None,
                        help="Override log file path")
    args = parser.parse_args()

    log_file = pathlib.Path(args.log_file) if args.log_file else LOG_FILE

    # Ensure log directory exists
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Check if file exists (to know whether to write header)
    write_header = not log_file.exists() or log_file.stat().st_size == 0

    print(f"Modbus Data Collector")
    print(f"  aGate: {AGATE_IP}:{MODBUS_PORT}")
    print(f"  Cloud API: {'disabled' if args.modbus_only else 'enabled'}")
    print(f"  Log file: {log_file}")
    print(f"  Loops: {args.loops}, Interval: {args.interval}s")
    if not FRANKLIN_USER and not args.modbus_only:
        print(f"  WARNING: No cloud credentials found, running Modbus-only")
        args.modbus_only = True
    print()

    with open(log_file, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()

        for i in range(args.loops):
            if i > 0:
                print(f"\n  Waiting {args.interval}s...")
                time.sleep(args.interval)

            row = collect_one(modbus_only=args.modbus_only)
            writer.writerow(row)
            f.flush()  # Write immediately so we don't lose data

    print(f"\nDone! {args.loops} reading(s) appended to {log_file}")


if __name__ == "__main__":
    main()
