#!/usr/bin/env python3
"""
FranklinWH Modbus vs Cloud API Comparison Tool
================================================
Reads Model 701 (AC Measurement), Model 702 (DC Measurement),
Model 703 (Watt-Hours), Model 713 (DER Status), and Model 502 (Solar)
from Modbus, then pulls cloud API data at the same moment, and
attempts to correlate values.

Usage: python3 modbus_cloud_compare.py [--loops N] [--interval SEC]
"""

import sys
import os
import struct
import time
import json
import argparse
from datetime import datetime

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
    print("ERROR: franklinwh not installed.")
    sys.exit(1)

# =============================================================================
# CONFIGURATION - Update these for your system
# =============================================================================
AGATE_IP = os.environ.get("AGATE_IP", "192.168.5.149")
MODBUS_PORT = int(os.environ.get("MODBUS_PORT", "502"))

# --- Load .env file for credentials (same as smart_decision.py) ---
try:
    from dotenv import load_dotenv
    # Try the script's directory first, then common locations
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
    pass  # No dotenv, rely on environment variables

# Cloud API - reads from .env / environment
FRANKLIN_USER = os.environ.get("FRANKLIN_USERNAME", "")
FRANKLIN_PASS = os.environ.get("FRANKLIN_PASSWORD", "")
GATEWAY_ID = os.environ.get("FRANKLIN_GATEWAY_ID", os.environ.get("GATEWAY_ID", ""))

# =============================================================================
# SunSpec Model Register Map (from your discovery dump)
# =============================================================================
MODELS = {
    "Model 1 (Common)":       {"addr": 4,    "length": 66},
    "Model 701 (AC Measure)":  {"addr": 72,   "length": 153},
    "Model 702 (DC Measure)":  {"addr": 227,  "length": 50},
    "Model 703 (Watt-Hours)":  {"addr": 279,  "length": 17},
    "Model 713 (DER Status)":  {"addr": 1035, "length": 7},
    "Model 502 (Solar)":       {"addr": 1098, "length": 28},
}

# Known/suspected field mappings for Model 701
# Based on SunSpec DER AC Measurement spec (700-series)
# The 701 model uses scale factors at specific offsets to decode values
MODEL_701_FIELDS = {
    # offset: (name, type, notes)
    0:  ("ACType", "uint16", "1=single, 2=split, 7=?"),
    1:  ("St (State)", "uint16", "1=Off, 2=On, 3=Fault, 4=Error"),
    2:  ("InvSt", "uint16", "Inverter state"),
    3:  ("ConnSt", "uint16", "Connection state"),
    # Power values - typically int16 with scale factors
    8:  ("W (Active Power?)", "int16", "Watts - check scale factor"),
    9:  ("VA (Apparent Power?)", "int16", "VA"),
    10: ("Var (Reactive Power?)", "int16", "VAr"),
    11: ("PF (Power Factor?)", "int16", ""),
    12: ("A (Current?)", "int16", "Amps"),
    13: ("LLV (Line Voltage?)", "int16", "Voltage L-L"),
    14: ("LNV (Line-N Voltage?)", "int16", "Voltage L-N"),
    16: ("Hz (Frequency)", "uint16", "60000 = 60.000 Hz (sf=-3)"),
    # Energy accumulators (typically uint32, pairs of registers)
    19: ("TotWhInj_hi", "uint16", "Total Wh injected high word"),
    20: ("TotWhInj_lo", "uint16", "Total Wh injected low word"),
    23: ("TotWhAbs_hi", "uint16", "Total Wh absorbed high word"),
    24: ("TotWhAbs_lo", "uint16", "Total Wh absorbed low word"),
    # Per-phase data starts around offset 33+
    33: ("TmpAmb (Ambient Temp?)", "int16", ""),
    34: ("TmpCab (Cabinet Temp?)", "int16", ""),
    # Phase A power
    37: ("W_L1 (Power Phase A?)", "int16", ""),
    39: ("W_L2 (Power Phase B?)", "int16", ""),
    40: ("VA_L1?", "int16", ""),
    41: ("Var_L1?", "int16", ""),
    42: ("PF_L1?", "int16", ""),
    43: ("A_L1 (Current Phase A?)", "int16", ""),
    44: ("V_L1 (Voltage Phase A?)", "int16", ""),
    45: ("V_L1N?", "int16", ""),
    # Phase B
    62: ("W_L2 alt?", "int16", ""),
    63: ("VA_L2?", "int16", ""),
    64: ("Var_L2?", "int16", ""),
    65: ("PF_L2?", "int16", ""),
    66: ("A_L2?", "int16", ""),
    # Scale factors - usually near offset 108+ in 701
    108: ("SF block start?", "int16", ""),
    113: ("W_SF?", "int16", "Active power scale factor"),
    115: ("VA_SF?", "int16", ""),
    117: ("Var_SF?", "int16", ""),
    119: ("PF_SF?", "int16", ""),
}

MODEL_713_FIELDS = {
    0: ("Field0 (Rated Power hi?)", "uint16", "30000"),
    1: ("Field1 (Rated Power lo?)", "uint16", "10089"),
    2: ("SOC? (÷10=%)", "uint16", "340 = 34.0%?"),
    3: ("Field3 (Voltage?)", "uint16", "993"),
    4: ("Field4", "uint16", ""),
    5: ("Field5", "uint16", ""),
    6: ("Field6", "uint16", "0xFFFF = not implemented"),
}

MODEL_703_FIELDS = {
    0:  ("Ena", "uint16", ""),
    1:  ("TotWhInj", "uint16", "Total Wh Injected"),
    2:  ("TotWhAbs", "uint16", "Total Wh Absorbed"),
    4:  ("TotWhInjL1", "uint16", "Wh Injected L1"),
    6:  ("TotWhAbsL1", "uint16", "Wh Absorbed L1"),
    8:  ("TotWhInjL2", "uint16", "Wh Injected L2"),
}


def read_modbus_model(client, addr, length, label=""):
    """Read registers for a SunSpec model, return as list of raw uint16 values."""
    # pymodbus can read max ~125 registers at once
    results = []
    remaining = length
    offset = 0
    while remaining > 0:
        count = min(remaining, 100)
        try:
            rr = client.read_holding_registers(addr + offset, count=count)
            if rr.isError():
                print(f"  ERROR reading {label} at {addr + offset}: {rr}")
                return None
            results.extend(rr.registers)
        except Exception as e:
            print(f"  EXCEPTION reading {label}: {e}")
            return None
        offset += count
        remaining -= count
    return results


def uint16_to_int16(val):
    """Convert unsigned 16-bit to signed 16-bit."""
    if val >= 0x8000:
        return val - 0x10000
    return val


def pair_to_uint32(hi, lo):
    """Combine two uint16 into uint32."""
    return (hi << 16) | lo


def get_cloud_data():
    """Fetch current data from Franklin cloud API."""
    import asyncio
    
    async def _fetch():
        try:
            fetcher = TokenFetcher(FRANKLIN_USER, FRANKLIN_PASS)
            client = Client(fetcher, GATEWAY_ID)
            stats = await client.get_stats()
            
            data = {
                "soc": stats.current.battery_soc,
                "battery_power_kw": stats.current.battery_use,
                "grid_power_kw": stats.current.grid_use,
                "solar_power_kw": stats.current.solar_production,
                "home_load_kw": stats.current.home_load,
            }
            
            # Try to get per-battery SOC and status fields
            try:
                status = await client._status()
                if isinstance(status, dict):
                    data["run_status"] = status.get("run_status")
                    data["mode"] = status.get("mode")
                    data["ambient_temp"] = status.get("tmp")
                    data["grid_to_bat"] = status.get("soChGrid", 0)
                    data["solar_to_bat"] = status.get("soChBat", 0)
                    # Per-battery SOC
                    bat_list = status.get("batteries", [])
                    data["per_battery_soc"] = []
                    for b in bat_list:
                        if isinstance(b, dict):
                            data["per_battery_soc"].append(b.get("soc"))
                        else:
                            data["per_battery_soc"].append(getattr(b, 'soc', None))
            except Exception as e2:
                print(f"    (status fetch failed: {e2}, continuing with stats only)")

            return data

        except Exception as e:
            print(f"  Cloud API error: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            return asyncio.ensure_future(_fetch())
        else:
            return loop.run_until_complete(_fetch())
    except RuntimeError:
        return asyncio.run(_fetch())


def run_comparison():
    """Run one comparison cycle."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*70}")
    print(f"  MODBUS vs CLOUD API COMPARISON - {timestamp}")
    print(f"{'='*70}")

    # --- Read Modbus ---
    print(f"\n  Connecting to Modbus at {AGATE_IP}:{MODBUS_PORT}...")
    mb_client = ModbusTcpClient(AGATE_IP, port=MODBUS_PORT, timeout=10)
    
    if not mb_client.connect():
        print("  FAILED to connect to Modbus!")
        return
    
    modbus_data = {}
    for label, info in MODELS.items():
        regs = read_modbus_model(mb_client, info["addr"], info["length"], label)
        if regs:
            modbus_data[label] = regs
            
    mb_client.close()
    modbus_ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"  Modbus read complete at {modbus_ts}")

    # --- Read Cloud API ---
    print(f"\n  Fetching Cloud API data...")
    cloud = get_cloud_data()
    cloud_ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    print(f"  Cloud API read complete at {cloud_ts}")

    # =========================================================================
    # DECODE AND COMPARE
    # =========================================================================

    # --- Model 713 (DER Status) - Our SOC candidate ---
    print(f"\n{'─'*70}")
    print(f"  MODEL 713 (DER Status) - SOC CANDIDATE")
    print(f"{'─'*70}")
    
    m713 = modbus_data.get("Model 713 (DER Status)")
    if m713:
        for i, val in enumerate(m713):
            signed = uint16_to_int16(val)
            marker = ""
            if i == 2:
                marker = f"  ← SOC? ({val/10:.1f}%)"
            elif i == 3:
                marker = f"  ← Battery voltage? ({val/10:.1f}V)"
            print(f"    [{i}] addr={1035+i:5d}  raw=0x{val:04X} ({val:6d}) signed=({signed:6d}){marker}")
        
        if cloud:
            cloud_soc = cloud.get("soc")
            modbus_soc = m713[2] / 10.0 if len(m713) > 2 else None
            
            print(f"\n    COMPARISON:")
            print(f"      Modbus offset 2:  {modbus_soc:.1f}%" if modbus_soc else "      Modbus offset 2:  N/A")
            print(f"      Cloud SOC:        {cloud_soc:.1f}%" if cloud_soc else "      Cloud SOC:        N/A")
            per_batt = cloud.get("per_battery_soc", [])
            for idx, s in enumerate(per_batt):
                if s is not None:
                    print(f"      Cloud Batt {idx+1}:    {s:.1f}%")
            if modbus_soc and cloud_soc:
                diff = abs(modbus_soc - cloud_soc)
                match = "✓ MATCH" if diff < 2.0 else "✗ MISMATCH"
                print(f"      Difference:       {diff:.1f}%  {match}")

    # --- Model 701 (AC Measurement) ---
    print(f"\n{'─'*70}")
    print(f"  MODEL 701 (AC Measurement) - Key Fields")
    print(f"{'─'*70}")
    
    m701 = modbus_data.get("Model 701 (AC Measure)")
    if m701:
        # Frequency - offset 16 is typically Hz with sf=-3
        hz_raw = m701[16] if len(m701) > 16 else 0
        hz = hz_raw / 1000.0
        print(f"    Frequency:    {hz:.3f} Hz  (raw={hz_raw})")
        
        # ACType
        ac_type = m701[0]
        print(f"    AC Type:      {ac_type}  (0=?, 1=single, 2=split, 3=3ph-delta, 4=3ph-wye, 7=?)")
        
        # State
        state = m701[1]
        state_map = {0: "Unknown", 1: "Off", 2: "On", 3: "Fault", 4: "Error"}
        print(f"    State:        {state} ({state_map.get(state, '?')})")
        
        # Try to identify voltages and power
        # Look for values in plausible ranges
        print(f"\n    --- Scanning for recognizable values ---")
        for i in range(min(90, len(m701))):
            val = m701[i]
            signed = uint16_to_int16(val)
            if val == 0 or val == 0xFFFF:
                continue
            
            notes = []
            # Voltage candidates (looking for ~2400-2500 which /10 = 240-250V)
            if 2300 <= val <= 2600:
                notes.append(f"voltage? {val/10:.1f}V")
            # Frequency
            if 59000 <= val <= 61000:
                notes.append(f"freq? {val/1000:.3f}Hz")
            # Power candidates (signed, could be negative for import)
            if abs(signed) > 0 and abs(signed) < 20000:
                # With sf=-1: value/10 = kW? Or raw watts?
                if 100 <= abs(signed) <= 15000:
                    notes.append(f"power? {signed}W or {signed/10:.1f}×10W")
            # Temperature
            if 0 < val < 500:
                notes.append(f"temp? {val/10:.1f}°C or {val}raw")
            # Energy (larger values)
            if 5000 <= val <= 50000:
                notes.append(f"energy? {val}Wh or {val/10:.1f}×10Wh")
            
            if notes:
                note_str = " | ".join(notes)
                print(f"    [{i:3d}] addr={72+i:5d}  raw={val:6d}  signed={signed:6d}  → {note_str}")

        # Print cloud comparison values
        if cloud:
            print(f"\n    CLOUD API REFERENCE VALUES:")
            print(f"      Battery Power:  {cloud.get('battery_power_kw', 'N/A')} kW")
            print(f"      Grid Power:     {cloud.get('grid_power_kw', 'N/A')} kW")
            print(f"      Solar Power:    {cloud.get('solar_power_kw', 'N/A')} kW")
            print(f"      Home Load:      {cloud.get('home_load_kw', 'N/A')} kW")
            print(f"      Grid→Batt:      {cloud.get('grid_to_bat', 'N/A')}")
            print(f"      Solar→Batt:     {cloud.get('solar_to_bat', 'N/A')}")
            print(f"      Ambient Temp:   {cloud.get('ambient_temp', 'N/A')}")
            print(f"      Run Status:     {cloud.get('run_status', 'N/A')}")
            print(f"      Mode:           {cloud.get('mode', 'N/A')}")

    # --- Model 703 (Watt-Hours) ---
    print(f"\n{'─'*70}")
    print(f"  MODEL 703 (Watt-Hours) - Energy Accumulators")
    print(f"{'─'*70}")
    
    m703 = modbus_data.get("Model 703 (Watt-Hours)")
    if m703:
        for i, val in enumerate(m703):
            signed = uint16_to_int16(val)
            if val != 0 and val != 0xFFFF and val != 0xFFFE:
                print(f"    [{i:2d}] addr={279+i:5d}  raw={val:6d}  signed={signed:6d}")
        
        if cloud:
            print(f"\n    CLOUD LIFETIME TOTALS (for reference):")
            print(f"      (Lifetime values from cloud would need separate API call)")

    # --- Model 502 (Solar) ---
    print(f"\n{'─'*70}")
    print(f"  MODEL 502 (Solar Module) - PV Data")
    print(f"{'─'*70}")
    
    m502 = modbus_data.get("Model 502 (Solar)")
    if m502:
        non_zero = [(i, v) for i, v in enumerate(m502) if v != 0 and v != 0xFFFF and v != 0x8000]
        if non_zero:
            for i, val in non_zero:
                signed = uint16_to_int16(val)
                print(f"    [{i:2d}] addr={1098+i:5d}  raw={val:6d}  signed={signed:6d}")
        else:
            print(f"    All zeros/unimplemented (expected at night)")
        
        if cloud:
            print(f"    Cloud Solar Power: {cloud.get('solar_power_kw', 'N/A')} kW")

    # --- Model 702 (DC Measurement) ---
    print(f"\n{'─'*70}")
    print(f"  MODEL 702 (DC Measurement) - Nameplate/Capacity")
    print(f"{'─'*70}")
    
    m702 = modbus_data.get("Model 702 (DC Measure)")
    if m702:
        for i, val in enumerate(m702):
            signed = uint16_to_int16(val)
            if val != 0 and val != 0xFFFF:
                notes = ""
                if val == 20000:
                    notes = "  ← 20kW or 20000W rating?"
                elif val == 23000:
                    notes = "  ← 23kVA rating?"
                elif val == 16000:
                    notes = "  ← 16kW?"
                elif val == 11358:
                    notes = "  ← 11.358kW?"
                elif val == 870:
                    notes = "  ← 870V DC max?"
                elif val == 7450:
                    notes = "  ← 7450W or 74.50?"
                print(f"    [{i:2d}] addr={227+i:5d}  raw={val:6d}  signed={signed:6d}{notes}")

    # --- Summary ---
    print(f"\n{'='*70}")
    print(f"  SUMMARY")
    print(f"{'='*70}")
    print(f"  Modbus read at:  {modbus_ts}")
    print(f"  Cloud read at:   {cloud_ts}")
    
    if m713 and cloud:
        modbus_soc = m713[2] / 10.0 if len(m713) > 2 else None
        cloud_soc = cloud.get("soc")
        
        print(f"\n  KEY FINDINGS:")
        print(f"    SOC (Modbus 713[2]):   {modbus_soc:.1f}%" if modbus_soc else "    SOC: N/A")
        print(f"    SOC (Cloud combined):  {cloud_soc:.1f}%" if cloud_soc else "    SOC (Cloud): N/A")
        if modbus_soc and cloud_soc:
            diff = abs(modbus_soc - cloud_soc)
            if diff < 2.0:
                print(f"    → SOC CONFIRMED via Modbus! (diff={diff:.1f}%)")
            else:
                print(f"    → SOC mismatch (diff={diff:.1f}%) - may not be SOC")
    
    print()
    return modbus_data, cloud


def main():
    global AGATE_IP

    parser = argparse.ArgumentParser(description="Compare Modbus vs Cloud API data")
    parser.add_argument("--loops", type=int, default=1, help="Number of comparison cycles")
    parser.add_argument("--interval", type=int, default=60, help="Seconds between cycles")
    parser.add_argument("--agate-ip", type=str, default=AGATE_IP, help="aGate IP address")
    args = parser.parse_args()

    AGATE_IP = args.agate_ip

    print(f"FranklinWH Modbus vs Cloud API Comparison")
    print(f"  aGate IP: {AGATE_IP}:{MODBUS_PORT}")
    print(f"  Loops: {args.loops}, Interval: {args.interval}s")
    
    if not FRANKLIN_USER or not FRANKLIN_PASS or not GATEWAY_ID:
        print(f"\n  WARNING: Cloud API credentials not found.")
        print(f"  Looked for .env file and FRANKLIN_USERNAME/FRANKLIN_PASSWORD/FRANKLIN_GATEWAY_ID env vars.")
        print(f"  Run from the franklin-git/scripts/ directory, or set env vars.")
        print(f"  Will attempt Modbus-only mode.\n")

    for i in range(args.loops):
        if i > 0:
            print(f"\n  Waiting {args.interval}s before next read...")
            time.sleep(args.interval)
        
        print(f"\n  === Cycle {i+1}/{args.loops} ===")
        run_comparison()
    
    print("Done!")


if __name__ == "__main__":
    main()
