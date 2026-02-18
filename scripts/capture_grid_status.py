#!/usr/bin/env python3
"""
Quick grid status capture - run during power outage to record register values.
Usage: python3 capture_grid_status.py

Run this IMMEDIATELY when grid disconnects to capture what registers
look like during islanding. Results append to grid_status_captures.log
"""

import sys
import json
from datetime import datetime

try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    print("ERROR: pip install pymodbus")
    sys.exit(1)

AGATE_IP = "192.168.5.149"
MODBUS_PORT = 502
LOG_FILE = "/volume1/docker/franklin/logs/grid_status_captures.log"

def capture():
    client = ModbusTcpClient(AGATE_IP, port=MODBUS_PORT)
    if not client.connect():
        print("ERROR: Cannot connect to aGate")
        return

    timestamp = datetime.now().isoformat()
    data = {"timestamp": timestamp}

    try:
        # Model 701 - AC Measurement (base addr 72)
        r701 = client.read_holding_registers(72, count=20, slave=1)
        if not r701.isError():
            data["reg_72_ac_type"]   = r701.registers[0]
            data["reg_73_state"]     = r701.registers[1]
            data["reg_74_inv_state"] = r701.registers[2]
            data["reg_75_conn_st"]   = r701.registers[3]
            # Grid power (signed)
            raw_80 = r701.registers[8]
            data["reg_80_grid_w"] = raw_80 if raw_80 < 32768 else raw_80 - 65536
        else:
            data["model_701_error"] = str(r701)

        # Voltage and frequency (registers 85-88)
        r_vf = client.read_holding_registers(85, count=4, slave=1)
        if not r_vf.isError():
            data["reg_85_voltage"]  = r_vf.registers[0]
            data["reg_86_voltage2"] = r_vf.registers[1]
            data["reg_88_freq_hz"]  = r_vf.registers[3]

        # Model 713 - DER Status (base addr 1035)
        r713 = client.read_holding_registers(1035, count=7, slave=1)
        if not r713.isError():
            data["reg_1037_soc"]      = r713.registers[2]
            data["reg_1038_batt_v"]   = r713.registers[3]

        # Temperatures
        r_temp = client.read_holding_registers(105, count=2, slave=1)
        if not r_temp.isError():
            data["reg_105_temp_amb"] = r_temp.registers[0]
            data["reg_106_temp_cab"] = r_temp.registers[1]

    finally:
        client.close()

    # Print to console
    print(f"\n{'='*60}")
    print(f"  GRID STATUS CAPTURE - {timestamp}")
    print(f"{'='*60}")
    print(f"  State (reg 73):      {data.get('reg_73_state', '?')}")
    print(f"  ConnSt (reg 75):     {data.get('reg_75_conn_st', '?')}")
    print(f"  InvSt (reg 74):      {data.get('reg_74_inv_state', '?')}")
    print(f"  Grid Power (reg 80): {data.get('reg_80_grid_w', '?')} W")
    print(f"  Voltage (reg 85):    {data.get('reg_85_voltage', '?')}")
    print(f"  Frequency (reg 88):  {data.get('reg_88_freq_hz', '?')}")
    print(f"  SOC (reg 1037):      {data.get('reg_1037_soc', '?')}")
    print(f"  Batt V (reg 1038):   {data.get('reg_1038_batt_v', '?')}")
    print(f"{'='*60}\n")

    # Append to log file
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(data) + "\n")
        print(f"Saved to {LOG_FILE}")
    except Exception as e:
        print(f"Could not write log: {e}")
        print(f"JSON: {json.dumps(data)}")

    return data


if __name__ == "__main__":
    # Run twice with a note about grid state
    import sys
    note = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
    if note:
        print(f"Note: {note}")

    print("Capturing grid status registers...")
    data = capture()

    print("\nTIP: Run with a note to tag the capture:")
    print("  python3 capture_grid_status.py grid-down")
    print("  python3 capture_grid_status.py grid-restored")
    print("\nCompare grid-down vs grid-up captures to identify which registers change.")
