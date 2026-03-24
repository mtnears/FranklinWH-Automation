#!/usr/bin/env python3
"""
collect_device_inventory.py — Track serial numbers, firmware, and models across all systems

Checks Enphase, SolarEdge, and Franklin devices and logs to device_inventory table.
Only writes a new row when something changes (firmware update, new device, etc).

Designed to run once daily via scheduler (low overhead, no rate limit concerns).

Systems checked:
  - Enphase: /inventory.json from local gateway (microinverters + gateway)
  - SolarEdge: /site/{id}/inventory from cloud API (inverters + optimizers)
  - Franklin: Modbus discovery data + cloud API (gateway + batteries)

Usage:
    python3 collect_device_inventory.py
    python3 collect_device_inventory.py --force   # write all devices even if unchanged
    python3 collect_device_inventory.py --test    # print findings, don't write
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [inventory] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('collect_device_inventory')

SCRIPT_DIR = Path(__file__).parent
DATA_DIR = SCRIPT_DIR.parent / 'data'

try:
    from db import store, init_db, get_latest_device_firmware
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    log.warning("db.py not available")


def load_env():
    env_path = SCRIPT_DIR.parent / '.env'
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, _, value = line.partition('=')
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key not in os.environ:
                        os.environ[key] = value


def has_changed(system, serial_number, model, firmware, extra_json):
    """Check if device info differs from last stored entry."""
    last = get_latest_device_firmware(system, serial_number)
    if last is None:
        return True
    if last.get('firmware') != firmware:
        return True
    if last.get('model') != model:
        return True
    if last.get('extra_json') != extra_json:
        return True
    return False


def collect_enphase(test_mode=False, force=False):
    """Query Enphase gateway for microinverter and gateway inventory."""
    try:
        import requests
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    except ImportError:
        log.warning("requests not available, skipping Enphase")
        return []

    ip = os.environ.get('ENPHASE_ENVOY_IP', os.environ.get('SOLAR_ARRAY_HOUSE_IP', ''))
    if not ip:
        log.info("No Enphase IP configured, skipping")
        return []

    token_file = os.environ.get('ENPHASE_TOKEN_FILE',
                  os.environ.get('SOLAR_ARRAY_HOUSE_TOKEN_FILE', '/app/data/enphase_token.txt'))
    token_path = Path(token_file)
    if not token_path.exists():
        log.warning("No Enphase token file, skipping")
        return []
    token = token_path.read_text().strip()

    devices = []
    gateway_serial = os.environ.get('ENPHASE_ENVOY_SERIAL',
                     os.environ.get('SOLAR_ARRAY_HOUSE_SERIAL', ''))

    try:
        resp = requests.get(
            f'https://{ip}/inventory.json',
            headers={'Authorization': f'Bearer {token}'},
            verify=False, timeout=10,
        )
        if resp.status_code != 200:
            log.warning(f"Enphase inventory HTTP {resp.status_code}")
            return []

        for group in resp.json():
            dev_type = group.get('type', '')
            type_map = {'PCU': 'microinverter', 'ACB': 'battery', 'NSRB': 'relay'}
            device_type = type_map.get(dev_type, dev_type.lower())

            for dev in group.get('devices', []):
                sn = dev.get('serial_num', '')
                if not sn:
                    continue
                fw = dev.get('img_pnum_running', '')
                model = dev.get('part_num', '')
                extra = {}
                if dev.get('img_load_date'):
                    extra['fw_load_date'] = dev['img_load_date']
                if dev.get('device_status'):
                    extra['status_flags'] = dev['device_status']
                if dev.get('phase'):
                    extra['phase'] = dev['phase']

                devices.append({
                    'system': 'enphase',
                    'device_type': device_type,
                    'serial_number': sn,
                    'model': model,
                    'firmware': fw,
                    'parent_serial': gateway_serial,
                    'extra_json': json.dumps(extra) if extra else None,
                })

    except Exception as e:
        log.warning(f"Enphase inventory failed: {e}")

    if gateway_serial:
        try:
            resp = requests.get(
                f'https://{ip}/info.xml',
                headers={'Authorization': f'Bearer {token}'},
                verify=False, timeout=10,
            )
            if resp.status_code == 200:
                import re
                text = resp.text
                sw = re.search(r'<software>(.*?)</software>', text)
                devices.append({
                    'system': 'enphase',
                    'device_type': 'gateway',
                    'serial_number': gateway_serial,
                    'model': 'IQ Gateway',
                    'firmware': sw.group(1) if sw else None,
                    'parent_serial': None,
                    'extra_json': None,
                })
        except Exception as e:
            log.debug(f"Gateway info.xml failed: {e}")

    log.info(f"Enphase: found {len(devices)} devices")
    return devices


def collect_solaredge(test_mode=False, force=False):
    """Query SolarEdge cloud API for inverter inventory."""
    import urllib.request
    import urllib.parse
    import urllib.error

    site_id = os.environ.get('SOLAREDGE_SITE_ID',
               os.environ.get('SOLAR_ARRAY_BARN_SITE_ID', '1241660'))
    api_key = os.environ.get('SOLAREDGE_API_KEY',
               os.environ.get('SOLAR_ARRAY_BARN_API_KEY', ''))
    if not api_key:
        log.info("No SolarEdge API key configured, skipping")
        return []

    devices = []

    try:
        url = (f"https://monitoringapi.solaredge.com/site/{site_id}/inventory"
               f"?api_key={urllib.parse.quote(api_key)}")
        req = urllib.request.Request(url)
        req.add_header('Accept', 'application/json')
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        inv_data = data.get('Inventory', {})

        for inv in inv_data.get('inverters', []):
            sn = inv.get('SN', '')
            extra = {}
            for key in ['dsp1Version', 'dsp2Version', 'cpuVersion',
                        'communicationMethod', 'connectedOptimizers']:
                if inv.get(key):
                    extra[key] = inv[key]

            devices.append({
                'system': 'solaredge',
                'device_type': 'inverter',
                'serial_number': sn,
                'model': inv.get('model', inv.get('partNumber', '')),
                'firmware': inv.get('cpuVersion', ''),
                'parent_serial': None,
                'extra_json': json.dumps(extra) if extra else None,
            })

    except Exception as e:
        log.warning(f"SolarEdge inventory failed: {e}")

    log.info(f"SolarEdge: found {len(devices)} devices")
    return devices


def _read_modbus_string(client, start, count):
    """Read Modbus registers and decode as ASCII string."""
    result = client.read_holding_registers(start, count=count)
    if result.isError():
        return None
    chars = []
    for val in result.registers:
        chars.append(chr(val >> 8))
        chars.append(chr(val & 0xFF))
    return ''.join(c for c in chars if 32 <= ord(c) < 127).strip()


def collect_franklin(test_mode=False, force=False):
    """Get Franklin device info from Modbus (gateway) and cloud API (batteries)."""
    devices = []
    gateway_serial = None

    try:
        from pymodbus.client import ModbusTcpClient
        host = os.getenv('MODBUS_HOST', '192.168.4.59')
        port = int(os.getenv('MODBUS_PORT', '502'))

        client = ModbusTcpClient(host, port=port, timeout=5)
        if client.connect():
            manufacturer = _read_modbus_string(client, 4, 16) or 'FranklinWH'
            model = _read_modbus_string(client, 20, 16) or 'aGate'
            fw_raw = _read_modbus_string(client, 36, 16) or ''
            gateway_serial = _read_modbus_string(client, 52, 16)

            firmware = fw_raw
            if 'Option Name' in fw_raw:
                firmware = fw_raw.replace('Option Name', '').strip()

            client.close()

            if gateway_serial:
                devices.append({
                    'system': 'franklin',
                    'device_type': 'gateway',
                    'serial_number': gateway_serial,
                    'model': model,
                    'firmware': firmware,
                    'parent_serial': None,
                    'extra_json': json.dumps({'manufacturer': manufacturer}),
                })
                log.info(f"  Modbus: gateway {gateway_serial} model={model} fw={firmware}")
            else:
                log.warning("Modbus: could not read gateway serial")
        else:
            log.warning("Modbus: connection failed")
    except Exception as e:
        log.warning(f"Modbus gateway read failed: {e}")

    try:
        import asyncio
        import httpx
        from franklinwh import Client, TokenFetcher

        try:
            from config import config
            username = config.FRANKLIN_USERNAME
            password = config.FRANKLIN_PASSWORD
            gateway_id = config.FRANKLIN_GATEWAY_ID
        except ImportError:
            username = os.getenv('FRANKLIN_USERNAME', '')
            password = os.getenv('FRANKLIN_PASSWORD', '')
            gateway_id = os.getenv('FRANKLIN_GATEWAY_ID', '')

        if all([username, password, gateway_id]):
            async def get_cloud_info():
                fetcher = TokenFetcher(username, password)
                client = Client(fetcher, gateway_id)
                status = await client._status()
                # get_home_gateway_list for richer gateway info
                gw_list = None
                try:
                    token = await fetcher.fetch_token()
                    headers = {'loginToken': token, 'Content-Type': 'application/json'}
                    async with httpx.AsyncClient(timeout=30) as http:
                        resp = await http.post(
                            'https://energy.franklinwh.com/hes/api/host/getHomeGatewayList',
                            headers=headers,
                            json={'pageNum': 1, 'pageSize': 10}
                        )
                        if resp.status_code == 200:
                            body = resp.json()
                            gw_list = body.get('result', {}).get('records', [])
                except Exception as e:
                    log.debug(f"get_home_gateway_list failed: {e}")
                return status, gw_list

            status, gw_list = asyncio.run(get_cloud_info())

            # Enrich gateway record with cloud API data
            if gw_list:
                for gw in gw_list:
                    gw_sn = gw.get('gatewaySerialNum', '')
                    if gw_sn and (gw_sn == gateway_serial or not gateway_serial):
                        extra = {}
                        for key in ['realSysHdVersion', 'protocolVer', 'sysSdVersion',
                                    'sysVersion', 'gatewayVersion', 'fhpVersion']:
                            val = gw.get(key)
                            if val:
                                extra[key] = val
                        cloud_fw = gw.get('sysVersion', '')
                        cloud_model = gw.get('gatewayModel', 'aGate')
                        # Merge with existing gateway device if present
                        for dev in devices:
                            if dev['device_type'] == 'gateway' and dev['serial_number'] == gw_sn:
                                old_extra = json.loads(dev['extra_json']) if dev['extra_json'] else {}
                                old_extra.update(extra)
                                dev['extra_json'] = json.dumps(old_extra)
                                if cloud_fw:
                                    dev['firmware'] = cloud_fw
                                break
                        else:
                            # No Modbus gateway found — add from cloud
                            if gw_sn:
                                devices.append({
                                    'system': 'franklin',
                                    'device_type': 'gateway',
                                    'serial_number': gw_sn,
                                    'model': cloud_model,
                                    'firmware': cloud_fw,
                                    'parent_serial': None,
                                    'extra_json': json.dumps(extra) if extra else None,
                                })
                        log.info(f"  Cloud API: gateway enriched hw={extra.get('realSysHdVersion','?')} "
                                 f"proto={extra.get('protocolVer','?')}")

            if status and isinstance(status, dict):
                battery_sns = status.get('fhpSn', [])
                battery_socs = status.get('fhpSoc', [])
                battery_powers = status.get('fhpPower', [])

                for i, sn in enumerate(battery_sns):
                    if sn:
                        extra = {}
                        if i < len(battery_socs):
                            extra['current_soc'] = battery_socs[i]
                        if i < len(battery_powers):
                            extra['current_power_kw'] = battery_powers[i]
                        # Identify battery model from serial prefix
                        # Positions 4-8 of serial: '0015' = aPower 2
                        battery_model = 'aPower'
                        if len(sn) >= 8 and sn[3:7] == '0015':
                            battery_model = 'aPower 2'
                        devices.append({
                            'system': 'franklin',
                            'device_type': 'battery',
                            'serial_number': sn,
                            'model': battery_model,
                            'firmware': '',
                            'parent_serial': gateway_serial,
                            'extra_json': json.dumps(extra) if extra else None,
                        })
                        log.info(f"  Cloud API: battery {sn} ({battery_model})")
        else:
            log.warning("Franklin cloud credentials not configured")
    except Exception as e:
        log.warning(f"Franklin cloud API failed: {e}")

    log.info(f"Franklin: found {len(devices)} devices")
    return devices


def main():
    parser = argparse.ArgumentParser(description='Collect device inventory across all systems')
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()

    load_env()

    if DB_AVAILABLE and not args.test:
        init_db()

    all_devices = []
    all_devices.extend(collect_enphase(test_mode=args.test, force=args.force))
    all_devices.extend(collect_solaredge(test_mode=args.test, force=args.force))
    all_devices.extend(collect_franklin(test_mode=args.test, force=args.force))

    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    written = 0
    unchanged = 0

    for dev in all_devices:
        changed = args.force or has_changed(
            dev['system'], dev['serial_number'],
            dev.get('model'), dev.get('firmware'), dev.get('extra_json')
        )

        if args.test:
            status = "CHANGED" if changed else "unchanged"
            log.info(f"  [{dev['system']}] {dev['device_type']} "
                     f"{dev['serial_number']} fw={dev.get('firmware', '?')} "
                     f"model={dev.get('model', '?')} -> {status}")
        elif changed and DB_AVAILABLE:
            store.device_inventory(
                system=dev['system'],
                device_type=dev['device_type'],
                serial_number=dev['serial_number'],
                model=dev.get('model'),
                firmware=dev.get('firmware'),
                parent_serial=dev.get('parent_serial'),
                extra_json=dev.get('extra_json'),
                timestamp=ts,
            )
            written += 1
            log.info(f"  [{dev['system']}] {dev['device_type']} "
                     f"{dev['serial_number']} -> STORED (fw={dev.get('firmware', '?')})")
        else:
            unchanged += 1

    log.info(f"Total: {len(all_devices)} devices, {written} stored, {unchanged} unchanged")


if __name__ == '__main__':
    main()
