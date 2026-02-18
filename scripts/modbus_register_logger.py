#!/usr/bin/env python3
"""
FranklinWH Modbus Register Logger
===================================
Continuous polling of ALL interesting Modbus registers to capture
state transitions — especially grid disconnect/island events.

Logs every register read to a CSV with timestamps for post-analysis.
Also logs inferred system state based on known register meanings.

Usage:
    python3 modbus_register_logger.py                    # Default: 10s interval, continuous
    python3 modbus_register_logger.py --interval 5       # 5-second polling  
    python3 modbus_register_logger.py --interval 30 --duration 3600  # 30s for 1 hour
    python3 modbus_register_logger.py --quick             # 3-second polling, key registers only

Designed to run in background and capture grid outage events:
    nohup python3 modbus_register_logger.py --interval 10 >> /dev/null 2>&1 &

CSV output: ../logs/modbus_register_log.csv
"""

import sys
import os
import time
import csv
import signal
import argparse
from datetime import datetime
from pathlib import Path

try:
    from pymodbus.client import ModbusTcpClient
    from pymodbus.exceptions import ModbusException
except ImportError:
    print("ERROR: pymodbus not installed. Run: pip install pymodbus")
    sys.exit(1)

# ─── Configuration ────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

CSV_FILE = LOG_DIR / "modbus_register_log.csv"
CONSOLE_LOG = LOG_DIR / "modbus_register_logger.log"

# aGate connection
AGATE_IP = os.getenv("AGATE_IP", "192.168.5.149")
AGATE_PORT = int(os.getenv("AGATE_PORT", "502"))

# SunSpec model base addresses (discovered from aGate)
MODELS = {
    "common":  {"addr": 4,    "len": 66,  "name": "Common (ID/Serial)"},
    "m701":    {"addr": 72,   "len": 153, "name": "DER AC Measurement"},
    "m702":    {"addr": 227,  "len": 50,  "name": "DER DC Measurement"},
    "m703":    {"addr": 279,  "len": 17,  "name": "DER Watt-Hours"},
    "m713":    {"addr": 1035, "len": 7,   "name": "DER Status"},
    "m502":    {"addr": 1098, "len": 28,  "name": "Solar Module"},
}

# ─── Register Definitions ─────────────────────────────────────────────────────
# Each entry: (model_key, offset, csv_column, description, transform)
# transform: "raw", "signed", "div10", "div1000", "signed_div10"

REGISTERS = [
    # ══════════════════════════════════════════════════════════════════════════
    # Model 713: DER Status — SOC, battery voltage, rated power
    # Base addr 1035 (our offsets) = 41035 in 40000-based addressing
    # ══════════════════════════════════════════════════════════════════════════
    ("m713", 0, "der_rated_power_hi",   "Rated power high word",        "raw"),
    ("m713", 1, "der_rated_power_lo",   "Rated power low word",         "raw"),
    ("m713", 2, "soc_raw",             "SOC x10 (raw)",                 "raw"),
    ("m713", 3, "batt_dc_voltage_raw", "Battery DC voltage x10 (raw)",  "raw"),
    ("m713", 4, "der_off4",            "DER offset 4",                  "raw"),
    ("m713", 5, "der_off5",            "DER offset 5",                  "raw"),
    ("m713", 6, "der_off6",            "DER offset 6",                  "raw"),
    # Derived (same source registers, human-friendly)
    ("m713", 2, "soc_pct",             "SOC percent",                   "div10"),
    ("m713", 3, "batt_dc_voltage",     "Battery DC voltage V",          "div10"),

    # ══════════════════════════════════════════════════════════════════════════
    # Model 701: DER AC Measurement — THE MAIN MODEL
    # Base addr 72 (our offsets) = 40072 in 40000-based addressing
    # Labels confirmed from David's SunSpec2 Explorer (Feb 17, 2026)
    # ══════════════════════════════════════════════════════════════════════════

    # ── State Registers — CRITICAL for grid disconnect detection ──
    # Addr  Name       David's Label                    David's Value
    # 40072 ACType     AC Wiring Type                   0
    # 40073 St         Operating State                  1 (dashboard: "Off")
    # 40074 InvSt      Inverter State                   3 (dashboard: "Starting")
    # 40075 ConnSt     Grid Connection State             1 (dashboard: connected)
    # 40076 Alrm       Alarm Bitfield                   0
    # 40078 DERMode    DER Operational Character         1 (dashboard: Self-Consumption)
    ("m701", 0,  "ac_type",            "ACType: AC Wiring Type",        "raw"),
    ("m701", 1,  "operating_state",    "St: Operating State (1=Off,4=MPPT,8=Standby)", "raw"),
    ("m701", 2,  "inverter_state",     "InvSt: Inverter State (3=Starting)", "raw"),
    ("m701", 3,  "conn_state",         "ConnSt: Grid Connection State (0=Disconn,1=Conn)", "raw"),
    ("m701", 4,  "alarm_bitfield",     "Alrm: Alarm Bitfield",          "raw"),
    ("m701", 5,  "m701_off5",          "Offset 5 (between Alrm and DERMode)", "raw"),
    ("m701", 6,  "der_mode",           "DERMode: DER Operational Mode (1=SelfCon?)", "raw"),
    ("m701", 7,  "m701_off7",          "Offset 7",                      "raw"),

    # ── AC Power & Power Quality ──
    # Addr  Name  David's Label         David's Value  Units
    # 40080 W     Active Power          0              W     (signed)
    # 40081 VA    Apparent Power         885            VA    (signed)
    # 40082 Var   Reactive Power        -703            Var   (signed) — NOT battery!
    # 40083 PF    Power Factor           0                    (signed)
    # 40084 A     Total AC Current       3.6            A     (raw 36, /10)
    ("m701", 8,  "grid_power_w",       "W: Active Power (grid) W, signed", "signed"),
    ("m701", 9,  "apparent_power_va",  "VA: Apparent Power VA, signed",    "signed"),
    ("m701", 10, "reactive_power_var", "Var: Reactive Power VAr, signed (NOT battery!)", "signed"),
    ("m701", 11, "power_factor_raw",   "PF: Power Factor (raw)",           "signed"),
    ("m701", 12, "total_ac_current_raw", "A: Total AC Current (raw, /10=Amps)", "raw"),

    # ── Voltage ──
    # 40085 LLV   Voltage LL            242.3          V     (raw 2423, /10)
    # 40086 LNV   Voltage LN            242.3          V     (raw 2423, /10)
    ("m701", 13, "voltage_ll",         "LLV: Line-Line Voltage V",      "div10"),
    ("m701", 14, "voltage_ln",         "LNV: Line-Neutral Voltage V",   "div10"),
    ("m701", 13, "voltage_ll_raw",     "LLV raw",                       "raw"),

    # ── Frequency — key for island detection ──
    # 40087 Hz    Frequency             50             Hz    (raw 50000, /1000)
    # NOTE: David's system shows 50Hz (non-US). Ours should show 60Hz.
    ("m701", 15, "m701_off15",         "Offset 15",                     "raw"),
    ("m701", 16, "frequency_hz",       "Hz: AC Frequency",              "div1000"),
    ("m701", 16, "frequency_raw",      "Hz raw (60000=60.000Hz)",       "raw"),

    # ── Scale factors and gap ──
    ("m701", 17, "m701_off17",         "Offset 17 (scale factor?)",     "signed"),
    ("m701", 18, "m701_off18",         "Offset 18 (scale factor?)",     "signed"),

    # ── Energy Accumulators ──
    # 40089 TotWhInj    Total Energy Injected    4423382  Wh
    # 40093 TotWhAbs    Total Energy Absorbed    1202256  Wh
    # 40097 TotVarhInj  Total Reactive Inj       0        Varh
    # 40101 TotVarhAbs  Total Reactive Abs        0        Varh
    # These are uint32 pairs (hi/lo words) in our register map
    ("m701", 19, "wh_injected_hi",     "TotWhInj hi (uint32 pair)",     "raw"),
    ("m701", 20, "wh_injected_lo",     "TotWhInj lo",                   "raw"),
    ("m701", 21, "m701_off21",         "Offset 21 (TotWhInj cont?)",    "raw"),
    ("m701", 22, "m701_off22",         "Offset 22",                     "raw"),
    ("m701", 23, "wh_absorbed_hi",     "TotWhAbs hi (uint32 pair)",     "raw"),
    ("m701", 24, "wh_absorbed_lo",     "TotWhAbs lo",                   "raw"),
    ("m701", 25, "m701_off25",         "TotVarhInj?",                   "raw"),
    ("m701", 26, "m701_off26",         "TotVarhInj lo?",                "raw"),
    ("m701", 27, "m701_off27",         "Offset 27",                     "raw"),
    ("m701", 28, "m701_off28",         "Offset 28",                     "raw"),
    ("m701", 29, "m701_off29",         "TotVarhAbs?",                   "raw"),
    ("m701", 30, "m701_off30",         "TotVarhAbs lo?",                "raw"),
    ("m701", 31, "m701_off31",         "Offset 31",                     "raw"),
    ("m701", 32, "m701_off32",         "Offset 32",                     "raw"),

    # ── Temperatures ──
    # 40105 TmpAmb   Ambient Temperature    23.9  C  (raw 239, /10)
    # 40106 TmpCab   Cabinet Temperature    33.2  C  (raw 332, /10)  
    # 40107 TmpSnk   Heat Sink Temperature  0     C
    # 40108 TmpTrns  Transformer Temp       0     C
    # 40109 TmpSw    IGBT/MOSFET Temp       25    C  (raw 250, /10)
    # 40110 TmpOt    Other Temperature       0     C
    ("m701", 33, "temp_ambient_c",     "TmpAmb: Ambient Temperature C", "signed_div10"),
    ("m701", 34, "temp_cabinet_c",     "TmpCab: Cabinet Temperature C", "signed_div10"),
    ("m701", 35, "temp_heatsink_c",    "TmpSnk: Heat Sink Temp C",      "signed_div10"),
    ("m701", 36, "temp_transformer_c", "TmpTrns: Transformer Temp C",   "signed_div10"),
    ("m701", 37, "temp_igbt_c",        "TmpSw: IGBT/MOSFET Temp C",     "signed_div10"),
    ("m701", 38, "temp_other_c",       "TmpOt: Other Temperature C",    "signed_div10"),

    # ── Phase L1 (Phase A) ──
    # 40111 WL1    Watts L1              0     W     (signed)
    # 40112 VAL1   VA L1                 885   VA
    # 40113 VarL1  Var L1               -703   Var
    # 40114 PFL1   PF L1                 0
    # 40115 AL1    Amps L1               3.6   A     (raw 36, /10)
    # 40116 VL1L2  Phase Voltage L1-L2   242.3 V     (raw 2423, /10)
    # 40117 VL1    Phase Voltage L1-N    242.3 V
    ("m701", 39, "watts_l1",           "WL1: Watts Phase L1 W",         "signed"),
    ("m701", 40, "va_l1",             "VAL1: VA Phase L1",              "signed"),
    ("m701", 41, "var_l1",            "VarL1: Var Phase L1",            "signed"),
    ("m701", 42, "pf_l1",             "PFL1: PF Phase L1",              "signed"),
    ("m701", 43, "amps_l1_raw",       "AL1: Amps L1 (raw, /10=A)",     "raw"),
    ("m701", 44, "voltage_l1l2",      "VL1L2: Phase Voltage L1-L2 V",  "div10"),
    ("m701", 45, "voltage_l1n",       "VL1: Phase Voltage L1-N V",     "div10"),

    # ── Phase L2 (Phase B) — split-phase, expect similar to L1 ──
    ("m701", 46, "watts_l2",           "WL2: Watts Phase L2 W",         "signed"),
    ("m701", 47, "va_l2",             "VAL2: VA Phase L2",              "signed"),
    ("m701", 48, "var_l2",            "VarL2: Var Phase L2",            "signed"),
    ("m701", 49, "pf_l2",             "PFL2: PF Phase L2",              "signed"),
    ("m701", 50, "amps_l2_raw",       "AL2: Amps L2 (raw, /10=A)",     "raw"),
    ("m701", 51, "voltage_l2l3",      "VL2L3: Phase Voltage L2-L3 V",  "div10"),
    ("m701", 52, "voltage_l2n",       "VL2: Phase Voltage L2-N V",     "div10"),

    # ── Phase L3 (not used on split-phase, but log anyway) ──
    ("m701", 53, "watts_l3",           "WL3: Watts Phase L3 W",         "signed"),
    ("m701", 54, "va_l3",             "VAL3: VA Phase L3",              "signed"),
    ("m701", 55, "var_l3",            "VarL3: Var Phase L3",            "signed"),
    ("m701", 56, "pf_l3",             "PFL3: PF Phase L3",              "signed"),
    ("m701", 57, "amps_l3_raw",       "AL3: Amps L3 (raw, /10=A)",     "raw"),
    ("m701", 58, "voltage_l3l1",      "VL3L1: Phase Voltage L3-L1 V",  "div10"),
    ("m701", 59, "voltage_l3n",       "VL3: Phase Voltage L3-N V",     "div10"),

    # ── Remaining offsets 60-152: scan for hidden registers ──
    # David's tool shows data through addr ~40117 (offset 45) for phase data
    # but Model 701 has length 153, so offsets 60-152 may have more data
    ("m701", 60, "m701_off60",         "Offset 60",                     "raw"),
    ("m701", 61, "m701_off61",         "Offset 61",                     "raw"),
    ("m701", 62, "m701_off62",         "Offset 62",                     "raw"),
    ("m701", 63, "m701_off63",         "Offset 63",                     "raw"),
    ("m701", 64, "m701_off64",         "Offset 64",                     "raw"),
    ("m701", 65, "m701_off65",         "Offset 65",                     "raw"),
    ("m701", 66, "m701_off66",         "Offset 66",                     "raw"),
    ("m701", 67, "m701_off67",         "Offset 67",                     "raw"),
    ("m701", 68, "m701_off68",         "Offset 68",                     "raw"),
    ("m701", 69, "m701_off69",         "Offset 69",                     "raw"),
    ("m701", 70, "m701_off70",         "Offset 70",                     "raw"),
    ("m701", 75, "m701_off75",         "Offset 75",                     "raw"),
    ("m701", 80, "m701_off80",         "Offset 80",                     "raw"),
    ("m701", 85, "m701_off85",         "Offset 85",                     "raw"),
    ("m701", 90, "m701_off90",         "Offset 90",                     "raw"),
    ("m701", 95, "m701_off95",         "Offset 95",                     "raw"),
    ("m701", 100, "m701_off100",       "Offset 100",                    "raw"),
    ("m701", 105, "m701_off105",       "Offset 105 (NOT temp-dup)",     "raw"),
    ("m701", 110, "m701_off110",       "Offset 110",                    "raw"),
    ("m701", 115, "m701_off115",       "Offset 115",                    "raw"),
    ("m701", 120, "m701_off120",       "Offset 120 (mfr error string?)", "raw"),
    ("m701", 125, "m701_off125",       "Offset 125",                    "raw"),
    ("m701", 130, "m701_off130",       "Offset 130",                    "raw"),
    ("m701", 135, "m701_off135",       "Offset 135",                    "raw"),
    ("m701", 140, "m701_off140",       "Offset 140",                    "raw"),
    ("m701", 145, "m701_off145",       "Offset 145",                    "raw"),
    ("m701", 150, "m701_off150",       "Offset 150",                    "raw"),
    ("m701", 152, "m701_off152",       "Offset 152 (last in model)",    "raw"),

    # ══════════════════════════════════════════════════════════════════════════
    # Model 703: DER Watt-Hours — Energy accumulators
    # ══════════════════════════════════════════════════════════════════════════
    ("m703", 0,  "m703_off0",          "WH model header",               "raw"),
    ("m703", 1,  "wh_total_inj",       "Total Wh injected",             "raw"),
    ("m703", 2,  "wh_total_abs",       "Total Wh absorbed",             "raw"),
    ("m703", 3,  "m703_off3",          "WH offset 3 (SF?)",             "raw"),
    ("m703", 4,  "wh_inj_l1",         "Wh injected L1",                "raw"),
    ("m703", 5,  "m703_off5",          "WH offset 5",                   "raw"),
    ("m703", 6,  "wh_abs_l1",         "Wh absorbed L1",                "raw"),
    ("m703", 7,  "m703_off7",          "WH offset 7",                   "raw"),
    ("m703", 8,  "wh_inj_l2",         "Wh injected L2",                "raw"),
    ("m703", 9,  "m703_off9",          "WH offset 9",                   "raw"),
    ("m703", 10, "wh_abs_l2",         "Wh absorbed L2",                "raw"),
    ("m703", 11, "m703_off11",         "WH offset 11",                  "raw"),
    ("m703", 12, "m703_off12",         "WH offset 12",                  "raw"),
    ("m703", 13, "m703_off13",         "WH offset 13",                  "raw"),
    ("m703", 14, "m703_off14",         "WH offset 14",                  "raw"),
    ("m703", 15, "m703_off15",         "WH offset 15",                  "raw"),
    ("m703", 16, "m703_off16",         "WH offset 16",                  "raw"),

    # ══════════════════════════════════════════════════════════════════════════
    # Model 702: DER DC Measurement — mostly static nameplate
    # ══════════════════════════════════════════════════════════════════════════
    ("m702", 0,  "dc_rated_w",         "DC rated power W",              "raw"),
    ("m702", 1,  "dc_rated_va",        "DC rated VA",                   "raw"),
    ("m702", 8,  "dc_voltage_max",     "DC max voltage",                "raw"),
    ("m702", 10, "dc_current_rated",   "DC rated current",              "raw"),

    # ══════════════════════════════════════════════════════════════════════════
    # Model 502: Solar Module — all zeros at night, check daytime
    # ══════════════════════════════════════════════════════════════════════════
    ("m502", 0,  "solar_off0",         "Solar model header",            "raw"),
    ("m502", 1,  "solar_off1",         "Solar offset 1",                "raw"),
    ("m502", 2,  "solar_off2",         "Solar offset 2",                "raw"),
    ("m502", 3,  "solar_off3",         "Solar offset 3",                "raw"),
    ("m502", 4,  "solar_off4",         "Solar offset 4",                "raw"),
    ("m502", 5,  "solar_off5",         "Solar offset 5",                "raw"),
    ("m502", 6,  "solar_off6",         "Solar offset 6",                "raw"),
    ("m502", 7,  "solar_off7",         "Solar offset 7",                "raw"),
    ("m502", 8,  "solar_off8",         "Solar offset 8",                "raw"),
    ("m502", 9,  "solar_off9",         "Solar offset 9",                "raw"),
    ("m502", 10, "solar_off10",        "Solar offset 10",               "raw"),
    ("m502", 11, "solar_off11",        "Solar offset 11",               "raw"),
    ("m502", 12, "solar_off12",        "Solar offset 12",               "raw"),
    ("m502", 13, "solar_off13",        "Solar offset 13",               "raw"),
    ("m502", 14, "solar_off14",        "Solar offset 14",               "raw"),
    ("m502", 15, "solar_off15",        "Solar offset 15",               "raw"),
    ("m502", 16, "solar_off16",        "Solar offset 16",               "raw"),
    ("m502", 17, "solar_off17",        "Solar offset 17",               "raw"),
    ("m502", 18, "solar_off18",        "Solar offset 18",               "raw"),
    ("m502", 19, "solar_off19",        "Solar counter? (12 at night)",  "raw"),
    ("m502", 20, "solar_off20",        "Solar counter? (20532 night)",  "raw"),
    ("m502", 21, "solar_off21",        "Solar offset 21",               "raw"),
    ("m502", 22, "solar_off22",        "Solar offset 22",               "raw"),
    ("m502", 23, "solar_off23",        "Solar offset 23",               "raw"),
    ("m502", 24, "solar_off24",        "Solar offset 24",               "raw"),
    ("m502", 25, "solar_off25",        "Solar offset 25",               "raw"),
    ("m502", 26, "solar_off26",        "Solar offset 26",               "raw"),
    ("m502", 27, "solar_off27",        "Solar offset 27",               "raw"),
]

# Unique CSV column names (some registers produce two columns with different transforms)
def get_csv_columns():
    seen = set()
    cols = ["timestamp", "read_ms", "read_ok"]
    for _, _, col, _, _ in REGISTERS:
        if col not in seen:
            cols.append(col)
            seen.add(col)
    # Derived columns
    cols.extend([
        "inferred_grid_status",     # connected/disconnected/unknown
        "inferred_charging",        # charging/discharging/idle
        "inferred_solar",           # producing/dark
        "freq_deviation_mhz",       # deviation from 60.000Hz in millihertz
        "notes",
    ])
    return cols


# ─── Modbus Read ──────────────────────────────────────────────────────────────

def uint16_to_int16(val):
    """Convert unsigned 16-bit to signed."""
    return val - 0x10000 if val >= 0x8000 else val


def read_all_models(client):
    """Read all SunSpec models, return dict of model_key -> list of uint16 values."""
    data = {}
    for key, info in MODELS.items():
        if key == "common":
            continue  # Skip common block for speed
        try:
            # Read in chunks if needed (Modbus TCP max ~125 registers per read)
            length = info["len"]
            addr = info["addr"]
            if length <= 125:
                result = client.read_holding_registers(addr, count=length)
                if result and not result.isError():
                    data[key] = result.registers
                else:
                    data[key] = None
            else:
                # Split into two reads
                first_len = 125
                result1 = client.read_holding_registers(addr, count=first_len)
                result2 = client.read_holding_registers(addr + first_len, count=length - first_len)
                if result1 and not result1.isError() and result2 and not result2.isError():
                    data[key] = result1.registers + result2.registers
                elif result1 and not result1.isError():
                    data[key] = result1.registers  # Partial read — use what we got
                else:
                    data[key] = None
        except Exception as e:
            data[key] = None
    return data


def extract_register(model_data, model_key, offset, transform):
    """Extract a register value with the given transform."""
    regs = model_data.get(model_key)
    if regs is None or offset >= len(regs):
        return None
    
    raw = regs[offset]
    
    # Only filter 0xFFFF as "not implemented" — 0x8000 is valid signed value (-32768)
    # Also don't filter for "raw" transforms since 0xFFFF could be a valid bitfield
    if raw == 0xFFFF and transform != "raw":
        return None
    
    if transform == "raw":
        return raw
    elif transform == "signed":
        return uint16_to_int16(raw)
    elif transform == "div10":
        return raw / 10.0
    elif transform == "div1000":
        return raw / 1000.0
    elif transform == "signed_div10":
        return uint16_to_int16(raw) / 10.0
    else:
        return raw


def infer_state(values):
    """Infer system state from register values."""
    grid_w = values.get("grid_power_w")
    soc = values.get("soc_pct")
    freq = values.get("frequency_hz")
    op_state = values.get("operating_state")
    inv_state = values.get("inverter_state")
    conn_state = values.get("conn_state")
    der_mode = values.get("der_mode")
    voltage = values.get("voltage_ll")
    alarm = values.get("alarm_bitfield")
    
    # Grid status inference
    grid_status = "unknown"
    if conn_state == 0:
        grid_status = "DISCONNECTED"
    elif conn_state == 1:
        grid_status = "connected"
    # Additional checks for abnormal conditions
    if voltage is not None and (voltage < 220 or voltage > 260):
        grid_status = f"abnormal_voltage_{voltage:.1f}V"
    
    # Charging inference from grid power
    charging = "unknown"
    if grid_w is not None:
        if grid_w < -100:
            charging = f"importing_{abs(grid_w)}W"
        elif grid_w > 100:
            charging = f"exporting_{grid_w}W"
        else:
            charging = "idle"
    
    # Solar inference (from time of day — Model 502 may also help)
    hour = datetime.now().hour
    solar = "dark" if hour < 6 or hour > 20 else "possible"
    
    # Frequency deviation (important for island detection)
    # US grid = 60Hz, David's system = 50Hz. Detect which.
    freq_dev = None
    if freq is not None:
        nominal = 60.0 if freq > 55 else 50.0
        freq_dev = round((freq - nominal) * 1000, 1)  # millihertz deviation
    
    # Build notes for anything interesting
    notes = []
    if op_state is not None and op_state != 1:
        notes.append(f"OpState={op_state}")
    if inv_state is not None and inv_state not in (1, 3):
        notes.append(f"InvState={inv_state}")
    if conn_state is not None and conn_state != 1:
        notes.append(f"ConnState={conn_state}!")
    if der_mode is not None:
        notes.append(f"DERMode={der_mode}")
    if alarm is not None and alarm != 0:
        notes.append(f"ALARM={alarm}")
    if freq_dev is not None and abs(freq_dev) > 50:
        notes.append(f"freq_dev={freq_dev}mHz")
    if voltage is not None and (voltage < 235 or voltage > 255):
        notes.append(f"voltage={voltage:.1f}V")
    
    return grid_status, charging, solar, freq_dev, "; ".join(notes)


# ─── Console Output ───────────────────────────────────────────────────────────

def print_status(values, read_ms, read_ok, poll_num):
    """Print compact real-time status to console."""
    ts = datetime.now().strftime("%H:%M:%S")
    soc = values.get("soc_pct", "?")
    grid = values.get("grid_power_w", "?")
    freq = values.get("frequency_hz", "?")
    volt = values.get("voltage_ll", "?")
    temp_a = values.get("temp_ambient_c", "?")
    temp_c = values.get("temp_cabinet_c", "?")
    op_st = values.get("operating_state", "?")
    inv_st = values.get("inverter_state", "?")
    conn_st = values.get("conn_state", "?")
    der_mode = values.get("der_mode", "?")
    batt_v = values.get("batt_dc_voltage", "?")
    react_var = values.get("reactive_power_var", "?")
    apparent = values.get("apparent_power_va", "?")
    alarm = values.get("alarm_bitfield", "?")
    
    # Format values
    soc_s = f"{soc:.1f}%" if isinstance(soc, (int, float)) else "?"
    grid_s = f"{grid:+d}W" if isinstance(grid, (int, float)) else "?"
    freq_s = f"{freq:.3f}Hz" if isinstance(freq, (int, float)) else "?"
    volt_s = f"{volt:.1f}V" if isinstance(volt, (int, float)) else "?"
    
    status = "OK" if read_ok else "FAIL"
    
    # Compact one-line output
    print(f"[{ts}] #{poll_num:>5} {status} | "
          f"SOC:{soc_s:>6} Grid:{grid_s:>8} Freq:{freq_s:>9} V:{volt_s:>6} | "
          f"Op:{op_st} Inv:{inv_st} Conn:{conn_st} Mode:{der_mode} Alrm:{alarm} | "
          f"BattV:{batt_v} VA:{apparent} VAr:{react_var} | "
          f"Temp:{temp_a}/{temp_c}C | {read_ms:.0f}ms")


# ─── Main Loop ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FranklinWH Modbus Register Logger")
    parser.add_argument("--interval", type=int, default=10, help="Seconds between polls (default: 10)")
    parser.add_argument("--duration", type=int, default=0, help="Total seconds to run (0=forever)")
    parser.add_argument("--quick", action="store_true", help="Quick mode: 3s interval")
    parser.add_argument("--ip", default=AGATE_IP, help=f"aGate IP (default: {AGATE_IP})")
    parser.add_argument("--port", type=int, default=AGATE_PORT, help=f"Modbus port (default: {AGATE_PORT})")
    parser.add_argument("--no-csv", action="store_true", help="Skip CSV logging (console only)")
    parser.add_argument("--debug", action="store_true", help="Dump raw registers on first read")
    args = parser.parse_args()
    
    if args.quick:
        args.interval = 3
    
    print(f"=" * 80)
    print(f"FranklinWH Modbus Register Logger")
    print(f"=" * 80)
    print(f"  aGate:     {args.ip}:{args.port}")
    print(f"  Interval:  {args.interval}s")
    print(f"  Duration:  {'forever' if args.duration == 0 else f'{args.duration}s'}")
    print(f"  CSV:       {CSV_FILE if not args.no_csv else 'disabled'}")
    print(f"  Registers: {len(REGISTERS)} across {len(MODELS)-1} models")
    print(f"=" * 80)
    
    # CSV setup
    csv_columns = get_csv_columns()
    write_header = not CSV_FILE.exists() or CSV_FILE.stat().st_size == 0
    csv_file = None
    csv_writer = None
    
    if not args.no_csv:
        csv_file = open(CSV_FILE, "a", newline="")
        csv_writer = csv.DictWriter(csv_file, fieldnames=csv_columns, extrasaction="ignore")
        if write_header:
            csv_writer.writeheader()
            csv_file.flush()
    
    # Graceful shutdown
    running = True
    def signal_handler(sig, frame):
        nonlocal running
        print("\n\nShutting down gracefully...")
        running = False
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Connect
    client = ModbusTcpClient(args.ip, port=args.port, timeout=10)
    if not client.connect():
        print(f"ERROR: Failed to connect to {args.ip}:{args.port}")
        sys.exit(1)
    print(f"Connected to aGate at {args.ip}:{args.port}")
    
    start_time = time.time()
    poll_num = 0
    consecutive_failures = 0
    
    try:
        while running:
            if args.duration > 0 and (time.time() - start_time) > args.duration:
                print(f"\nDuration reached ({args.duration}s). Stopping.")
                break
            
            poll_num += 1
            t0 = time.time()
            
            # Read all models
            try:
                model_data = read_all_models(client)
                read_ok = any(v is not None for v in model_data.values())
                read_ms = (time.time() - t0) * 1000
                
                # Debug dump on first read
                if args.debug and poll_num == 1:
                    print(f"\n{'='*80}")
                    print(f"DEBUG: Raw register dump (first read)")
                    print(f"{'='*80}")
                    for key, regs in model_data.items():
                        info = MODELS.get(key, {})
                        print(f"\n  {key} ({info.get('name','?')}) — addr {info.get('addr','?')}, "
                              f"requested {info.get('len','?')} regs, got {len(regs) if regs else 0}")
                        if regs:
                            # Show first 70 registers with offset numbers
                            for i, val in enumerate(regs[:70]):
                                signed = uint16_to_int16(val)
                                flag = ""
                                if val == 0xFFFF:
                                    flag = " [0xFFFF]"
                                elif val == 0x8000:
                                    flag = " [0x8000]"
                                elif val == 0:
                                    flag = ""
                                else:
                                    # Try to annotate interesting values
                                    if 2300 <= val <= 2600:
                                        flag = f" [voltage? {val/10:.1f}V]"
                                    elif 58000 <= val <= 62000:
                                        flag = f" [freq? {val/1000:.3f}Hz]"
                                    elif 100 <= val <= 500 and i in (33,34,35,36,37,38):
                                        flag = f" [temp? {val/10:.1f}C]"
                                print(f"    [{i:>3}] addr {info.get('addr',0)+i:>5}: "
                                      f"raw={val:>6} signed={signed:>7} hex=0x{val:04X}{flag}")
                            if len(regs) > 70:
                                print(f"    ... ({len(regs)-70} more registers)")
                        else:
                            print(f"    (no data returned)")
                    print(f"{'='*80}\n")
                
                if not read_ok:
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        print(f"  Reconnecting after {consecutive_failures} failures...")
                        client.close()
                        time.sleep(2)
                        if not client.connect():
                            print(f"  Reconnect failed. Waiting...")
                            time.sleep(args.interval)
                            continue
                        consecutive_failures = 0
                else:
                    consecutive_failures = 0
                    
            except Exception as e:
                read_ok = False
                read_ms = (time.time() - t0) * 1000
                model_data = {}
                consecutive_failures += 1
                print(f"  Read error: {e}")
            
            # Extract all register values
            values = {}
            seen_cols = set()
            for model_key, offset, col, desc, transform in REGISTERS:
                if col in seen_cols:
                    # Duplicate column (same register, different transform) — skip raw
                    continue
                val = extract_register(model_data, model_key, offset, transform)
                values[col] = val
                seen_cols.add(col)
            
            # Wait, we need BOTH raw and transformed for some registers
            # Re-extract with all entries
            values = {}
            for model_key, offset, col, desc, transform in REGISTERS:
                val = extract_register(model_data, model_key, offset, transform)
                values[col] = val
            
            # Infer system state
            grid_status, charging, solar, freq_dev, notes = infer_state(values)
            
            # Print to console
            print_status(values, read_ms, read_ok, poll_num)
            
            # Write to CSV
            if csv_writer:
                row = {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                    "read_ms": round(read_ms, 1),
                    "read_ok": 1 if read_ok else 0,
                    "inferred_grid_status": grid_status,
                    "inferred_charging": charging,
                    "inferred_solar": solar,
                    "freq_deviation_mhz": freq_dev,
                    "notes": notes,
                }
                for model_key, offset, col, desc, transform in REGISTERS:
                    val = extract_register(model_data, model_key, offset, transform)
                    row[col] = val
                
                csv_writer.writerow(row)
                csv_file.flush()
            
            # Sleep
            elapsed = time.time() - t0
            sleep_time = max(0, args.interval - elapsed)
            if sleep_time > 0 and running:
                time.sleep(sleep_time)
    
    finally:
        client.close()
        if csv_file:
            csv_file.close()
        print(f"\nDone. {poll_num} polls logged to {CSV_FILE}")
        print(f"Analyze with: python3 -c \"import pandas as pd; df=pd.read_csv('{CSV_FILE}'); print(df.describe())\"")


if __name__ == "__main__":
    main()
