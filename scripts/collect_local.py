#!/usr/bin/env python3
"""
Local connection collector (v4.7) — every 5 minutes.

  * Modbus not configured: the local connection is the only local data
    source, so each reading is written to system_readings (source='local').
  * Modbus configured: the Modbus collector keeps writing system_readings;
    this job takes one local reading and one Modbus reading back to back and
    records the comparison (SOC, grid power, mode) in channel_stats. That is
    what the reliability telemetry is built from.

Either way every attempt is counted per channel.
"""

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import settings  # noqa: E402
import channel_stats  # noqa: E402
import local_gateway as lg  # noqa: E402

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('collect_local')

EXT_MODE = {1: 'emergency_backup', 2: 'self_consumption', 3: 'time_of_use'}


def _bool(v, default=False):
    if v in (None, ''):
        return default
    return str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def modbus_quick(host: str, port: int):
    """SOC, grid W and mode from Modbus, same registers the engine uses."""
    from pymodbus.client import ModbusTcpClient
    t0 = time.time()
    c = ModbusTcpClient(host=host, port=port, timeout=5)
    try:
        if not c.connect():
            raise RuntimeError('could not connect')
        r1 = c.read_holding_registers(1035, count=7)
        r2 = c.read_holding_registers(72, count=10)
        r3 = c.read_holding_registers(15507, count=1)
        if r1.isError() or r2.isError():
            raise RuntimeError('register read error')
        soc = r1.registers[2] / 10.0
        g = r2.registers[8]
        grid_w = float(g - 0x10000 if g >= 0x8000 else g)
        mode = None if r3.isError() else EXT_MODE.get(r3.registers[0])
        channel_stats.record('modbus', 'read', True, (time.time() - t0) * 1000)
        return soc, grid_w, mode
    except Exception as e:
        channel_stats.record('modbus', 'read', False, None, str(e))
        raise
    finally:
        c.close()


def main():
    lg.ensure_host()
    gw = lg.LocalGateway()
    if not gw.enabled:
        log.info('Local connection not enabled or no aGate address — nothing to do')
        return 0
    try:
        flow = gw.read()
    except Exception as e:
        log.warning(f'Local reading failed: {gw.last_error or e}')
        g = channel_stats.degraded()
        if g:
            log.warning(f"Local connection degraded: {g['failures']} consecutive failures since "
                        f"{g['since'] or 'start'} ({g['error_class']}); mode changes fall back to the cloud")
        return 1

    modbus_on = _bool(settings.raw('MODBUS_ENABLED', 'false'))
    if modbus_on:
        try:
            host = settings.raw('MODBUS_HOST', '') or gw.host
            port = int(settings.raw('MODBUS_PORT', '502') or 502)
            soc, grid_w, mode = modbus_quick(host, port)
            channel_stats.record_crosscheck(flow.get('soc_pct'), soc, flow.get('grid_w'), grid_w,
                                            flow.get('mode'), mode)
            log.info(f"local SOC={flow.get('soc_pct'):.1f}% grid={flow.get('grid_w'):.0f}W "
                     f"mode={flow.get('mode')} [{flow.get('read_ms')}ms] | modbus SOC={soc:.1f}% "
                     f"grid={grid_w:.0f}W mode={mode}")
        except Exception as e:
            log.warning(f'Modbus comparison read failed: {e}')
        return 0

    # No Modbus: this reading is the system record.
    try:
        from db import store, init_db
        init_db()
        kw = lambda w: (w / 1000.0) if w is not None else None  # noqa: E731
        store.system_reading(
            soc_pct=flow.get('soc_pct'),
            grid_kw=kw(flow.get('grid_w')),
            solar_kw=kw(flow.get('solar_w')),
            battery_kw=kw(flow.get('battery_w')),
            battery_kw_direct=kw(flow.get('battery_w')),
            home_load_kw=kw(flow.get('load_w')),
            per_battery_soc_json=json.dumps(flow.get('per_battery_soc') or []),
            per_battery_power_json=json.dumps(flow.get('per_battery_kw') or []),
            mode=flow.get('mode'),
            mode_detail=flow.get('mode_name'),
            run_status=flow.get('run_status'),
            ambient_temp_c=flow.get('ambient_temp_c'),
            kwh_solar=flow.get('kwh_solar'),
            kwh_grid_import=flow.get('kwh_grid_import'),
            kwh_grid_export=flow.get('kwh_grid_export'),
            kwh_load=flow.get('kwh_load'),
            kwh_battery_charge=flow.get('kwh_battery_charge'),
            kwh_battery_discharge=flow.get('kwh_battery_discharge'),
            source='local',
            timestamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        )
        log.info(f"Stored local reading: SOC={flow.get('soc_pct'):.1f}% grid={flow.get('grid_w'):.0f}W "
                 f"mode={flow.get('mode')} [{flow.get('read_ms')}ms]")
    except Exception as e:
        log.error(f'Could not store local reading: {e}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
