#!/usr/bin/env python3
"""
collect_modbus.py — Modbus TCP Data Collector for FranklinWH Agate

Reads all SunSpec + Franklin extension registers from the Agate gateway
via Modbus TCP every 5 minutes. Stores:
  1. Raw register blocks → modbus_raw_readings (every register, future-proof)
  2. Parsed system state → system_readings (SOC, power flows, temps, mode)

Supports multiple Agate devices via --device-id (default: agate_main).
Configuration from environment or config.py.

Usage:
    python3 collect_modbus.py                          # single read
    python3 collect_modbus.py --loop --interval 300    # continuous 5-min
    python3 collect_modbus.py --device-id agate_barn   # named device

Output:
    SQLite: /app/data/franklin.db (system_readings + modbus_raw_readings)
    CSV:    /app/logs/continuous_monitoring.csv (legacy compatibility)
"""

import argparse
import csv
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [modbus] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('collect_modbus')

try:
    from pymodbus.client import ModbusTcpClient
    MODBUS_AVAILABLE = True
except ImportError:
    MODBUS_AVAILABLE = False
    log.error("pymodbus not installed. Run: pip install pymodbus")

try:
    from config import config
    CONFIG_LOADED = True
except ImportError:
    CONFIG_LOADED = False

try:
    from db import store, init_db
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    log.warning("db.py not available — SQLite storage disabled")


# =============================================================================
# Register Map
# =============================================================================

REGISTER_BLOCKS = {
    'model_701a': {'addr': 72,    'count': 80,  'desc': 'AC Measurement (pt1: power, V, freq, temps)'},
    'model_701b': {'addr': 152,   'count': 73,  'desc': 'AC Measurement (pt2: per-phase, accumulators)'},
    'model_702': {'addr': 227,   'count': 50,  'desc': 'DC Measurement / Nameplate'},
    'model_703': {'addr': 279,   'count': 50,  'desc': 'Lifetime Watt-Hour Accumulators'},
    'model_713': {'addr': 1035,  'count': 7,   'desc': 'DER Status (SOC, battery V)'},
    'ext_15500': {'addr': 15500, 'count': 17,  'desc': 'Franklin Extension (solar, load, mode, dispatch)'},
    'conn_state': {'addr': 75,   'count': 1,   'desc': 'Grid connection state'},
}

EXT_MODE_MAP = {
    0: ('standby', 'Standby'),
    1: ('emergency_backup', 'Emergency Backup'),
    2: ('self_consumption', 'Self Consumption'),
    3: ('time_of_use', 'Time of Use'),
}

# Sanity bounds for Modbus register values (watts).
# Values near 0xFFFF (e.g. 65531) are not exact sentinel matches but are
# clearly erroneous for residential systems. Discard anything above these.
MAX_PLAUSIBLE_SOLAR_W = 25000   # 25 kW — well above any residential array
MAX_PLAUSIBLE_LOAD_W = 50000    # 50 kW — well above any residential load

TOU_DISPATCH_MAP = {
    0: 'Idle', 1: 'Home Loads', 2: 'Standby',
    3: 'Solar Charging', 4: 'Grid Charging', 5: 'Grid Discharge',
    6: 'Self Consumption', 7: 'Grid Export', 8: 'Grid Charge',
}


# =============================================================================
# Collector
# =============================================================================

class ModbusCollector:
    """Reads all Modbus registers and stores to SQLite."""

    def __init__(self, host: str, port: int, device_id: str = 'agate_main',
                 timeout: float = 5.0):
        self.host = host
        self.port = port
        self.device_id = device_id
        self.timeout = timeout
        self.client = None
        self.consecutive_failures = 0
        self.total_reads = 0
        self.successful_reads = 0

    def connect(self) -> bool:
        try:
            if self.client and self.client.connected:
                return True
            self.client = ModbusTcpClient(
                host=self.host, port=self.port, timeout=self.timeout
            )
            if self.client.connect():
                log.debug(f"Connected to {self.host}:{self.port}")
                return True
            log.warning(f"Connection failed to {self.host}:{self.port}")
            return False
        except Exception as e:
            log.error(f"Connection error: {e}")
            return False

    def disconnect(self):
        if self.client and self.client.connected:
            self.client.close()

    def _read_registers(self, addr: int, count: int) -> list:
        """Read holding registers, returns list of uint16 or None."""
        try:
            resp = self.client.read_holding_registers(addr, count=count)
            if resp.isError():
                return None
            return resp.registers
        except Exception as e:
            log.debug(f"Read error at {addr}: {e}")
            return None

    @staticmethod
    def _int16(val: int) -> int:
        return val - 0x10000 if val >= 0x8000 else val

    def collect_once(self) -> dict:
        """Read all register blocks and store to SQLite. Returns parsed data."""
        self.total_reads += 1
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        if not self.connect():
            self.consecutive_failures += 1
            return None

        start = time.time()
        raw_blocks = {}

        for block_name, info in REGISTER_BLOCKS.items():
            regs = self._read_registers(info['addr'], info['count'])
            if regs is not None:
                raw_blocks[block_name] = regs
            else:
                log.debug(f"Failed to read {block_name} ({info['desc']})")

        elapsed_ms = (time.time() - start) * 1000

        if not raw_blocks.get('model_713'):
            log.warning("Model 713 (SOC) unavailable — skipping cycle")
            self.consecutive_failures += 1
            return None

        self.successful_reads += 1
        self.consecutive_failures = 0

        if DB_AVAILABLE:
            for block_name, values in raw_blocks.items():
                info = REGISTER_BLOCKS[block_name]
                store.modbus_raw(
                    block_name=block_name,
                    register_base=info['addr'],
                    register_count=info['count'],
                    values=values,
                    device_id=self.device_id,
                    timestamp=ts,
                )

        parsed = self._parse(raw_blocks, ts, elapsed_ms)

        if DB_AVAILABLE:
            store.system_reading(
                soc_pct=parsed.get('soc_pct'),
                grid_kw=parsed.get('grid_kw'),
                solar_kw=parsed.get('solar_kw'),
                battery_kw=parsed.get('battery_kw'),
                home_load_kw=parsed.get('home_load_kw'),
                mode=parsed.get('mode'),
                mode_detail=parsed.get('mode_detail'),
                grid_voltage_v=parsed.get('grid_voltage_v'),
                grid_frequency_hz=parsed.get('grid_frequency_hz'),
                ambient_temp_c=parsed.get('ambient_temp_c'),
                cabinet_temp_c=parsed.get('cabinet_temp_c'),
                batt_dc_voltage_v=parsed.get('batt_dc_voltage_v'),
                grid_status=parsed.get('grid_status'),
                grid_connected=1 if parsed.get('grid_status') == 'Connected' else
                               0 if parsed.get('grid_status') == 'Disconnected' else None,
                conn_state=parsed.get('conn_state'),
                self_reserve_pct=parsed.get('self_reserve_pct'),
                tou_reserve_pct=parsed.get('tou_reserve_pct'),
                source='modbus',
                device_id=self.device_id,
                timestamp=ts,
            )

        log.info(
            f"SOC={parsed.get('soc_pct', '?')}% "
            f"Grid={parsed.get('grid_kw') or 0:.3f}kW "
            f"Solar={parsed.get('solar_kw') or 0:.3f}kW "
            f"Load={parsed.get('home_load_kw') or 0:.3f}kW "
            f"Mode={parsed.get('mode_detail', '?')} "
            f"[{elapsed_ms:.0f}ms] "
            f"Blocks={len(raw_blocks)}/{len(REGISTER_BLOCKS)}"
        )

        return parsed

    def _parse(self, blocks: dict, ts: str, elapsed_ms: float) -> dict:
        """Parse raw register blocks into usable values."""
        d = {'timestamp': ts, 'device_id': self.device_id, 'read_ms': elapsed_ms}

        m713 = blocks.get('model_713')
        if m713 and len(m713) > 2:
            d['soc_pct'] = m713[2] / 10.0
        if m713 and len(m713) > 3:
            d['batt_dc_voltage_v'] = m713[3] / 10.0

        m701a = blocks.get('model_701a')
        m701b = blocks.get('model_701b')
        m701 = (m701a + m701b) if m701a and m701b else m701a
        if m701:
            if len(m701) > 8:
                d['grid_kw'] = self._int16(m701[8]) / 1000.0
            if len(m701) > 16:
                freq = m701[16]
                if freq > 0:
                    d['grid_frequency_hz'] = freq / 1000.0
            if len(m701) > 33:
                v = m701[33]
                if v not in (0xFFFF, 0x8000):
                    d['ambient_temp_c'] = self._int16(v) / 10.0
            if len(m701) > 34:
                v = m701[34]
                if v not in (0xFFFF, 0x8000):
                    d['cabinet_temp_c'] = self._int16(v) / 10.0
            if len(m701) > 44:
                v = m701[44]
                if v > 0:
                    d['grid_voltage_v'] = v / 10.0
            if len(m701) > 3:
                d['grid_status'] = 'Connected' if m701[3] == 1 else 'Disconnected'

        ext = blocks.get('ext_15500')
        if ext and len(ext) >= 10:
            pv_w = ext[2]
            load_w = ext[6]
            pv_valid = pv_w != 0xFFFF and pv_w < MAX_PLAUSIBLE_SOLAR_W
            load_valid = load_w != 0xFFFF and load_w < MAX_PLAUSIBLE_LOAD_W
            if pv_valid:
                d['solar_kw'] = pv_w / 1000.0
            if load_valid:
                d['home_load_kw'] = load_w / 1000.0

            if load_valid:
                ext_solar_w = pv_w if pv_valid else 0
                grid_w = d.get('grid_kw', 0) * 1000
                d['battery_kw'] = (load_w - ext_solar_w - grid_w) / 1000.0

            mode_raw = ext[7]
            if mode_raw in EXT_MODE_MAP:
                mode_id, mode_label = EXT_MODE_MAP[mode_raw]
                d['mode'] = mode_id
                d['mode_detail'] = mode_label
                if mode_raw == 3 and len(ext) > 16:
                    dispatch = ext[16]
                    dispatch_text = TOU_DISPATCH_MAP.get(dispatch, f'Unknown({dispatch})')
                    d['mode_detail'] = f'TOU-{dispatch_text}'

            d['self_reserve_pct'] = ext[8]
            d['tou_reserve_pct'] = ext[9]

        conn = blocks.get('conn_state')
        if conn:
            d['conn_state'] = conn[0]

        return d


# =============================================================================
# Legacy CSV fallback (writes same format as continuous_monitoring.csv)
# =============================================================================

LEGACY_CSV_HEADERS = [
    'timestamp', 'soc_pct', 'grid_kw', 'solar_kw', 'battery_kw',
    'home_load_kw', 'mode', 'mode_detail', 'grid_voltage_v',
    'grid_frequency_hz', 'ambient_temp_c', 'cabinet_temp_c',
    'grid_status', 'read_ms',
]


def write_legacy_csv(parsed: dict, csv_path: Path):
    """Append to legacy CSV for backward compatibility."""
    try:
        exists = csv_path.exists()
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(csv_path, 'a', newline='') as f:
            w = csv.DictWriter(f, fieldnames=LEGACY_CSV_HEADERS, extrasaction='ignore')
            if not exists:
                w.writeheader()
            w.writerow(parsed)
    except Exception as e:
        log.warning(f"Legacy CSV write failed: {e}")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='FranklinWH Modbus Collector')
    parser.add_argument('--device-id', default='agate_main',
                        help='Device identifier for multi-agate setups')
    parser.add_argument('--host', default=None, help='Modbus host (default from config)')
    parser.add_argument('--port', type=int, default=None, help='Modbus port')
    parser.add_argument('--loop', action='store_true', help='Run continuously')
    parser.add_argument('--interval', type=int, default=300,
                        help='Seconds between reads in loop mode (default 300)')
    parser.add_argument('--csv', default=None,
                        help='Also write legacy CSV (path or "auto")')
    args = parser.parse_args()

    if not MODBUS_AVAILABLE:
        log.error("pymodbus not installed")
        sys.exit(1)

    host = args.host
    port = args.port
    if not host:
        if CONFIG_LOADED:
            host = config.MODBUS_HOST
            port = port or config.MODBUS_PORT
        else:
            host = os.getenv('MODBUS_HOST', '192.168.5.149')
            port = port or int(os.getenv('MODBUS_PORT', '502'))

    if DB_AVAILABLE:
        init_db()

    csv_path = None
    if args.csv:
        if args.csv == 'auto':
            log_dir = Path(os.getenv('LOG_DIR', '/app/logs'))
            csv_path = log_dir / 'modbus_collector.csv'
        else:
            csv_path = Path(args.csv)

    collector = ModbusCollector(host=host, port=port, device_id=args.device_id)

    running = True
    def handle_signal(sig, frame):
        nonlocal running
        running = False
        log.info("Shutdown signal received")
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    if args.loop:
        log.info(f"Starting continuous collection: {host}:{port} every {args.interval}s "
                 f"device={args.device_id}")
        while running:
            parsed = collector.collect_once()
            if parsed and csv_path:
                write_legacy_csv(parsed, csv_path)
            if running:
                time.sleep(args.interval)
    else:
        parsed = collector.collect_once()
        if parsed:
            if csv_path:
                write_legacy_csv(parsed, csv_path)
            print(json.dumps(parsed, indent=2))
        else:
            log.error("Collection failed")
            sys.exit(1)

    collector.disconnect()
    log.info(f"Done. {collector.successful_reads}/{collector.total_reads} successful reads.")


if __name__ == '__main__':
    main()
