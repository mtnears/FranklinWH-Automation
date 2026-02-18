#!/usr/bin/env python3
"""
FranklinWH aGate Modbus SunSpec Discovery Script
=================================================
Queries the aGate via Modbus TCP to discover available SunSpec models
and read all accessible registers. This helps determine what data is
available locally vs. what requires the cloud API.

Usage:
    python3 modbus_discovery.py <AGATE_IP_ADDRESS>

The aGate typically listens on Modbus TCP port 502 (default).
You must have Modbus enabled on your aGate (via SPAN panel toggle in the app).

SunSpec reference:
- Base address starts at 40000 or 0 with SunSpec ID "SunS" (0x53756e53)
- Each model has: Model ID (1 reg), Model Length (1 reg), then data registers
- Model ID 0xFFFF marks end of model list
"""

import sys
import struct
import time
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

# Common SunSpec Model IDs and their descriptions
SUNSPEC_MODELS = {
    1: "Common (Manufacturer, Model, Serial, Version)",
    101: "Single Phase Inverter (AC Current, Voltage, Power, Frequency, Energy)",
    102: "Split Phase Inverter",
    103: "Three Phase Inverter",
    111: "Single Phase Inverter (float)",
    112: "Split Phase Inverter (float)",
    113: "Three Phase Inverter (float)",
    120: "Nameplate Ratings",
    121: "Basic Settings",
    122: "Measurements & Status",
    123: "Immediate Controls",
    124: "Basic Storage Controls",
    126: "Static Volt-VAR",
    127: "Freq-Watt Param",
    128: "Dynamic Reactive Current",
    131: "Watt-Power Factor",
    132: "Volt-Watt",
    133: "Basic Scheduling",
    134: "Freq-Watt Curve",
    135: "Low Freq Ride Through",
    136: "High Freq Ride Through",
    137: "Low Volt Ride Through",
    138: "High Volt Ride Through",
    139: "Low Volt Momentary Cessation",
    140: "High Volt Momentary Cessation",
    141: "Scheduling",
    142: "DER Capacity",
    143: "DER Connect/Disconnect",
    144: "DER Enter Service",
    145: "DER AC Measurement",
    160: "Multiple MPPT Inverter Extension",
    201: "Single Phase Meter (AC)",
    202: "Split Phase Meter (AC)",
    203: "Three Phase Meter (AC)",
    211: "Single Phase Meter (float)",
    212: "Split Phase Meter (float)",
    213: "Three Phase Meter (float)",
    302: "Irradiance Model",
    303: "Back of Module Temperature",
    304: "Inclinometer",
    305: "GPS",
    401: "String Combiner (Current)",
    402: "String Combiner (Advanced)",
    403: "String Combiner (Current, float)",
    404: "String Combiner (Advanced, float)",
    501: "Solar Module",
    502: "Solar Module (float)",
    601: "Tracker Controller",
    701: "DER AC Measurement",
    702: "DER DC Measurement",
    703: "DER Watt-Hours",
    704: "DER Capacity (new)",
    705: "DER Enter Service",
    706: "DER Volt-Var",
    707: "DER Trip (LV, HV, LF, HF)",
    708: "DER Frequency Droop",
    709: "DER Watt-Var",
    710: "DER Watt-Power Factor",
    711: "DER Volt-Watt",
    712: "DER Connect/Disconnect",
    713: "DER Status",
    714: "DER Current Limit",
    715: "DER Power Limit",
    # Battery/Storage related
    801: "Battery (Base Model)",
    802: "Battery (Extended)",
    803: "Lithium-Ion Battery Model",
    804: "Lithium-Ion String Model",
    805: "Lithium-Ion Module Model",
    64001: "Vendor Specific (Outback)",
    64110: "Vendor Specific (SolarEdge)",
    64111: "Vendor Specific (SolarEdge Battery)",
    64112: "Vendor Specific (SolarEdge Storage)",
}

# Detailed register maps for models we care about most
MODEL_101_REGISTERS = {
    # offset: (name, length_in_regs, type, unit, scale_factor_offset)
    0: ("A (AC Current Total)", 1, "uint16", "A", 4),
    1: ("AphA (Phase A Current)", 1, "uint16", "A", 4),
    2: ("AphB (Phase B Current)", 1, "uint16", "A", 4),
    3: ("AphC (Phase C Current)", 1, "uint16", "A", 4),
    4: ("A_SF (Current Scale Factor)", 1, "int16", "SF", None),
    5: ("PPVphAB (Phase AB Voltage)", 1, "uint16", "V", 9),
    6: ("PPVphBC (Phase BC Voltage)", 1, "uint16", "V", 9),
    7: ("PPVphCA (Phase CA Voltage)", 1, "uint16", "V", 9),
    8: ("PhVphA (Phase A Voltage)", 1, "uint16", "V", 9),
    9: ("V_SF (Voltage Scale Factor)", 1, "int16", "SF", None),
    10: ("W (AC Power)", 1, "int16", "W", 12),
    11: ("Hz (Frequency)", 1, "uint16", "Hz", 13),
    12: ("W_SF (Power Scale Factor)", 1, "int16", "SF", None),
    13: ("Hz_SF (Frequency Scale Factor)", 1, "int16", "SF", None),
    14: ("VA (Apparent Power)", 1, "int16", "VA", 17),
    15: ("VAR (Reactive Power)", 1, "int16", "var", 18),
    16: ("PF (Power Factor)", 1, "int16", "%", 19),
    17: ("VA_SF (Apparent Power SF)", 1, "int16", "SF", None),
    18: ("VAR_SF (Reactive Power SF)", 1, "int16", "SF", None),
    19: ("PF_SF (Power Factor SF)", 1, "int16", "SF", None),
    20: ("WH (AC Energy)", 2, "acc32", "Wh", 22),
    22: ("WH_SF (Energy Scale Factor)", 1, "int16", "SF", None),
    23: ("DCA (DC Current)", 1, "uint16", "A", 25),
    24: ("DCV (DC Voltage)", 1, "uint16", "V", 25),
    25: ("DCW_SF (DC SF)", 1, "int16", "SF", None),
    26: ("DCW (DC Power)", 1, "int16", "W", 25),
    27: ("TmpCab (Cabinet Temp)", 1, "int16", "°C", 30),
    28: ("TmpSnk (Heat Sink Temp)", 1, "int16", "°C", 30),
    29: ("TmpTrns (Transformer Temp)", 1, "int16", "°C", 30),
    30: ("Tmp_SF (Temp Scale Factor)", 1, "int16", "SF", None),
    31: ("St (Operating State)", 1, "enum16", "", None),
    32: ("StVnd (Vendor State)", 1, "enum16", "", None),
}

OPERATING_STATES = {
    1: "Off",
    2: "Sleeping",
    3: "Starting",
    4: "MPPT (Running)",
    5: "Throttled",
    6: "Shutting Down",
    7: "Fault",
    8: "Standby",
}


def read_sunspec_id(client, base_addr):
    """Check for SunSpec 'SunS' identifier at given base address."""
    result = client.read_holding_registers(base_addr, count=2)
    if result.isError():
        return False
    # SunSpec ID is "SunS" = 0x53756e53
    val = (result.registers[0] << 16) | result.registers[1]
    return val == 0x53756E53


def read_registers_safe(client, address, count):
    """Read registers with error handling."""
    try:
        result = client.read_holding_registers(address, count=count)
        if result.isError():
            return None
        return result.registers
    except Exception as e:
        print(f"  Error reading address {address}: {e}")
        return None


def decode_string(registers):
    """Decode SunSpec string from register values."""
    raw = b""
    for reg in registers:
        raw += struct.pack(">H", reg)
    return raw.decode("ascii", errors="replace").rstrip("\x00").strip()


def decode_int16(registers, offset=0):
    """Decode signed 16-bit integer."""
    val = registers[offset]
    if val >= 0x8000:
        val -= 0x10000
    # Check for "not implemented" values
    if val == -32768:  # 0x8000
        return None
    return val


def decode_uint16(registers, offset=0):
    """Decode unsigned 16-bit integer."""
    val = registers[offset]
    if val == 0xFFFF:  # not implemented
        return None
    return val


def decode_uint32(registers, offset=0):
    """Decode unsigned 32-bit integer from 2 registers."""
    val = (registers[offset] << 16) | registers[offset + 1]
    if val == 0xFFFFFFFF:
        return None
    return val


def discover_models(client, base_addr=40000):
    """Walk the SunSpec model chain starting from base address."""
    models = []
    addr = base_addr + 2  # Skip past the SunS identifier

    max_iterations = 50  # Safety limit
    iteration = 0

    while iteration < max_iterations:
        iteration += 1
        regs = read_registers_safe(client, addr, 2)
        if regs is None:
            print(f"  Failed to read at address {addr}, stopping discovery")
            break

        model_id = regs[0]
        model_len = regs[1]

        # End marker
        if model_id == 0xFFFF:
            print(f"\n  End of model list at address {addr}")
            break

        # Sanity check
        if model_len == 0 or model_len > 2000:
            print(f"  Suspicious model length {model_len} at address {addr}, stopping")
            break

        model_name = SUNSPEC_MODELS.get(model_id, f"Unknown Model")
        data_addr = addr + 2  # Data starts after model_id and length

        models.append({
            "model_id": model_id,
            "model_name": model_name,
            "address": addr,
            "data_address": data_addr,
            "length": model_len,
        })

        print(f"  Model {model_id}: {model_name}")
        print(f"    Address: {addr}, Data starts: {data_addr}, Length: {model_len} registers")

        # Move to next model
        addr = data_addr + model_len

    return models


def read_common_model(client, model):
    """Read and display Model 1 (Common) data."""
    addr = model["data_address"]
    length = model["length"]

    regs = read_registers_safe(client, addr, min(length, 66))
    if regs is None:
        print("  Could not read Common model data")
        return

    print("\n  === Common Model (Model 1) ===")

    # Manufacturer: 16 registers (32 chars) starting at offset 0
    if len(regs) >= 16:
        manufacturer = decode_string(regs[0:16])
        print(f"  Manufacturer:  {manufacturer}")

    # Model: 16 registers starting at offset 16
    if len(regs) >= 32:
        model_str = decode_string(regs[16:32])
        print(f"  Model:         {model_str}")

    # Options: 8 registers starting at offset 32
    if len(regs) >= 40:
        options = decode_string(regs[32:40])
        print(f"  Options:       {options}")

    # Version: 8 registers starting at offset 40
    if len(regs) >= 48:
        version = decode_string(regs[40:48])
        print(f"  Version:       {version}")

    # Serial Number: 16 registers starting at offset 48
    if len(regs) >= 64:
        serial = decode_string(regs[48:64])
        print(f"  Serial Number: {serial}")

    # Device Address: offset 64
    if len(regs) >= 65:
        dev_addr = regs[64]
        print(f"  Device Addr:   {dev_addr}")

    return {
        "manufacturer": manufacturer if len(regs) >= 16 else "N/A",
        "model": model_str if len(regs) >= 32 else "N/A",
    }


def read_inverter_model(client, model):
    """Read and display Model 101/102/103 (Inverter) data."""
    addr = model["data_address"]
    length = model["length"]

    regs = read_registers_safe(client, addr, min(length, 40))
    if regs is None:
        print("  Could not read Inverter model data")
        return

    print(f"\n  === Inverter Model (Model {model['model_id']}) ===")

    results = {}

    # Read scale factors first
    sf_map = {}
    for offset, (name, reg_len, dtype, unit, sf_ref) in MODEL_101_REGISTERS.items():
        if "SF" in name and offset < len(regs):
            sf_map[offset] = decode_int16(regs, offset)

    # Now read all values
    for offset in sorted(MODEL_101_REGISTERS.keys()):
        if offset >= len(regs):
            break
        name, reg_len, dtype, unit, sf_offset = MODEL_101_REGISTERS[offset]

        if "SF" in unit:
            continue  # Skip scale factors in display

        if dtype == "uint16":
            raw = decode_uint16(regs, offset)
        elif dtype == "int16":
            raw = decode_int16(regs, offset)
        elif dtype == "enum16":
            raw = decode_uint16(regs, offset)
        elif dtype == "acc32" and offset + 1 < len(regs):
            raw = decode_uint32(regs, offset)
        else:
            raw = regs[offset]

        if raw is None:
            print(f"  {name}: Not Implemented")
            results[name] = None
            continue

        # Apply scale factor
        scaled = raw
        if sf_offset is not None and sf_offset in sf_map and sf_map[sf_offset] is not None:
            sf = sf_map[sf_offset]
            scaled = raw * (10 ** sf)

        # Special handling for operating state
        if "Operating State" in name and dtype == "enum16":
            state_name = OPERATING_STATES.get(raw, f"Unknown ({raw})")
            print(f"  {name}: {state_name} (raw: {raw})")
        elif dtype == "acc32":
            print(f"  {name}: {scaled:.1f} {unit}  (raw: {raw})")
        else:
            if isinstance(scaled, float):
                print(f"  {name}: {scaled:.2f} {unit}  (raw: {raw})")
            else:
                print(f"  {name}: {scaled} {unit}  (raw: {raw})")

        results[name] = scaled

    return results


def read_raw_registers(client, model):
    """Read and dump raw register values for any model."""
    addr = model["data_address"]
    length = model["length"]

    print(f"\n  === Raw Data: Model {model['model_id']} ({model['model_name']}) ===")
    print(f"  Reading {length} registers starting at address {addr}")

    # Read in chunks of 50 registers
    all_regs = []
    remaining = length
    current_addr = addr

    while remaining > 0:
        chunk = min(remaining, 50)
        regs = read_registers_safe(client, current_addr, chunk)
        if regs is None:
            print(f"  Failed at address {current_addr}")
            break
        all_regs.extend(regs)
        current_addr += chunk
        remaining -= chunk

    if not all_regs:
        return

    # Display in a readable format
    for i, val in enumerate(all_regs):
        signed = val - 0x10000 if val >= 0x8000 else val
        # Try to decode as ASCII (2 chars per register)
        try:
            chars = struct.pack(">H", val).decode("ascii", errors="replace")
            char_display = f'  "{chars}"' if chars.isprintable() else ""
        except:
            char_display = ""

        offset_addr = addr + i
        print(f"    [{i:3d}] addr={offset_addr:5d}  raw=0x{val:04X} ({val:6d}) signed=({signed:6d}){char_display}")


def scan_for_battery_data(client, models):
    """Look for battery-specific data in any available model."""
    print("\n" + "=" * 60)
    print("BATTERY / STORAGE DATA SCAN")
    print("=" * 60)

    # Check for standard battery models
    battery_models = [m for m in models if m["model_id"] in (801, 802, 803, 804, 805, 124)]
    if battery_models:
        print(f"\n  Found {len(battery_models)} battery-related model(s)!")
        for m in battery_models:
            read_raw_registers(client, m)
    else:
        print("\n  No standard SunSpec battery models found (801-805, 124)")
        print("  Battery SOC may be in vendor-specific registers or not exposed via Modbus")


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 modbus_discovery.py <AGATE_IP_ADDRESS> [port]")
        print("\nExample: python3 modbus_discovery.py 192.168.1.100")
        print("\nTo find your aGate IP:")
        print("  - Check your router's DHCP client list")
        print("  - Look for device named 'agate' or 'franklin'")
        print("  - Or check the FranklinWH app for network info")
        sys.exit(1)

    host = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 502

    print(f"FranklinWH aGate Modbus SunSpec Discovery")
    print(f"=" * 50)
    print(f"Target: {host}:{port}")
    print()

    client = ModbusTcpClient(host, port=port, timeout=10)

    if not client.connect():
        print(f"ERROR: Could not connect to {host}:{port}")
        print("Make sure:")
        print("  1. The aGate IP address is correct")
        print("  2. Modbus is enabled (SPAN panel toggle in Franklin app)")
        print("  3. You're on the same network as the aGate")
        sys.exit(1)

    print(f"Connected to {host}:{port}")
    print()

    # Try standard SunSpec base addresses
    base_addr = None
    for try_addr in [40000, 0, 50000]:
        print(f"Checking for SunSpec ID at base address {try_addr}...")
        if read_sunspec_id(client, try_addr):
            base_addr = try_addr
            print(f"  Found SunSpec identifier at address {base_addr}!")
            break
        else:
            print(f"  Not found at {try_addr}")

    if base_addr is None:
        print("\nERROR: No SunSpec identifier found at standard addresses")
        print("The aGate may not have SunSpec Modbus enabled")
        print("\nTrying raw register scan of common areas...")

        # Try reading some registers anyway
        for test_addr in [0, 100, 1000, 40000]:
            regs = read_registers_safe(client, test_addr, 10)
            if regs and any(r != 0 for r in regs):
                print(f"  Found data at address {test_addr}: {regs}")

        client.close()
        sys.exit(1)

    # Discover all models
    print(f"\nDiscovering SunSpec models starting at {base_addr}...")
    print("-" * 50)
    models = discover_models(client, base_addr)

    if not models:
        print("No models found!")
        client.close()
        sys.exit(1)

    print(f"\nFound {len(models)} model(s)")
    print()

    # Read Common Model (Model 1)
    common_models = [m for m in models if m["model_id"] == 1]
    for m in common_models:
        read_common_model(client, m)

    # Read Inverter Models
    inverter_models = [m for m in models if m["model_id"] in (101, 102, 103, 111, 112, 113)]
    for m in inverter_models:
        read_inverter_model(client, m)

    # Read all other models (raw dump)
    other_models = [m for m in models if m["model_id"] not in (1, 101, 102, 103, 111, 112, 113)]
    for m in other_models:
        read_raw_registers(client, m)

    # Scan for battery data specifically
    scan_for_battery_data(client, models)

    # Summary comparison
    print("\n" + "=" * 60)
    print("SUMMARY: Modbus vs Cloud API Data Availability")
    print("=" * 60)
    print()
    print("Data Point               Modbus  Cloud API  Notes")
    print("-" * 60)

    found_models = set(m["model_id"] for m in models)

    checks = [
        ("SOC (%)",               801 in found_models or 802 in found_models,  True,  "Critical for automation"),
        ("Battery Power (W)",     any(m in found_models for m in (101,102,103,111,112,113)), True, "p_fhp equivalent"),
        ("Grid Power (W)",        201 in found_models or 202 in found_models,  True,  "p_uti equivalent"),
        ("Solar Power (W)",       160 in found_models,                         True,  "p_sun equivalent"),
        ("Home Load (W)",         False,                                        True,  "p_load - likely cloud only"),
        ("AC Voltage/Current",    any(m in found_models for m in (101,102,103)), True, "Inverter model"),
        ("Frequency",             any(m in found_models for m in (101,102,103)), True, "Inverter model"),
        ("Cabinet/Ambient Temp",  any(m in found_models for m in (101,102,103)), True, "Inverter model"),
        ("Operating State",       any(m in found_models for m in (101,102,103)), True, "Inverter model"),
        ("Operating Mode",        False,                                        True,  "Franklin-specific, cloud only"),
        ("Mode Switching",        False,                                        True,  "WRITE required, cloud only"),
        ("Lifetime Energy",       any(m in found_models for m in (101,102,103)), True, "WH accumulator"),
        ("Per-Battery Stats",     False,                                        True,  "Franklin-specific"),
        ("Smart Circuit Data",    False,                                        True,  "Franklin-specific"),
        ("run_status/mode codes", False,                                        True,  "Franklin-specific"),
    ]

    for name, modbus, cloud, notes in checks:
        mb = "  ✓  " if modbus else "  ✗  "
        cl = "    ✓    " if cloud else "    ✗    "
        print(f"  {name:<24}{mb}{cl}  {notes}")

    print()
    print("NOTE: Modbus data rounds to nearest 100W for power values")
    print("      Cloud API provides finer granularity")

    client.close()
    print("\nDone!")


if __name__ == "__main__":
    main()
