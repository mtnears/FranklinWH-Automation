#!/usr/bin/env python3
"""
collect_franklin_cloud.py — Franklin Cloud API Data Collector

Queries the Franklin cloud API every 15 minutes and enriches system_readings
rows with data only available from the cloud:
  - Per-battery SOC breakdown (fhpSoc array)
  - Cumulative daily energy totals (solar, grid import/export, load, battery charge/discharge)
  - Cell/WiFi signal strength
  - Ambient temperature (cloud source)
  - Solar-to-battery and grid-to-battery charging breakdown
  - Run status and mode name

Strategy: finds the nearest Modbus row (within 5 min) and UPDATEs the NULL
cloud columns. If no Modbus row exists, INSERTs a standalone cloud row.

This runs independently of smart_decision.py and collect_modbus.py.

Usage:
    python3 collect_franklin_cloud.py
    python3 collect_franklin_cloud.py --test    # print data, don't write
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [franklin-cloud] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger('collect_franklin_cloud')

try:
    from db import store, init_db
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    log.warning("db.py not available — will print only")

try:
    from config import config
    CONFIG_LOADED = True
except ImportError:
    CONFIG_LOADED = False


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


async def collect_cloud_data(test_mode=False):
    """Query Franklin cloud API and store enrichment data."""
    from franklinwh import Client, TokenFetcher

    username = config.FRANKLIN_USERNAME if CONFIG_LOADED else os.getenv('FRANKLIN_USERNAME', '')
    password = config.FRANKLIN_PASSWORD if CONFIG_LOADED else os.getenv('FRANKLIN_PASSWORD', '')
    gateway_id = config.FRANKLIN_GATEWAY_ID if CONFIG_LOADED else os.getenv('FRANKLIN_GATEWAY_ID', '')

    if not all([username, password, gateway_id]):
        log.error("Franklin credentials not configured")
        return False

    start_time = time.time()

    try:
        fetcher = TokenFetcher(username, password)
        client = Client(fetcher, gateway_id)

        stats = await client.get_stats()
        if not stats:
            log.error("No stats returned from cloud API")
            return False

        status = None
        try:
            status = await client._status()
        except Exception as e:
            log.debug(f"Could not get detailed status: {e}")

        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        response_ms = (time.time() - start_time) * 1000

        per_battery_soc = None
        per_battery_power = None
        cell_signal = None
        wifi_signal = None
        ambient_temp = None
        run_status_val = None
        mode_name = None
        mode_detail = None
        solar_to_bat = None
        grid_to_bat = None

        if status and isinstance(status, dict):
            if 'fhpSoc' in status:
                per_battery_soc = json.dumps(status['fhpSoc'])
            if 'fhpPower' in status:
                per_battery_power = json.dumps(status.get('fhpPower'))
            if 'signal' in status:
                cell_signal = status['signal']
            if 'wifiSignal' in status:
                wifi_signal = status['wifiSignal']
            if 't_amb' in status:
                ambient_temp = status['t_amb']
            run_status_val = status.get('run_status')
            mode_name = status.get('name')
            if 'gridChBat' in status:
                grid_to_bat = status['gridChBat']
            if 'soChBat' in status:
                solar_to_bat = status['soChBat']

            battery_power = getattr(stats.current, 'battery_use', 0.0)
            if battery_power and battery_power > 0.1:
                grid_to_bat = 0.0
                solar_to_bat = 0.0

        kwh_solar = getattr(stats.totals, 'solar', None)
        kwh_grid_import = getattr(stats.totals, 'grid_import', None)
        kwh_grid_export = getattr(stats.totals, 'grid_export', None)
        kwh_load = getattr(stats.totals, 'load', None)
        kwh_battery_charge = getattr(stats.totals, 'battery_charge', None)
        kwh_battery_discharge = getattr(stats.totals, 'battery_discharge', None)
        kwh_generator = getattr(stats.totals, 'generator', None)

        if test_mode:
            log.info(f"Cloud API response in {response_ms:.0f}ms")
            log.info(f"  SOC: {stats.current.battery_soc:.1f}%")
            log.info(f"  Per-battery SOC: {per_battery_soc}")
            log.info(f"  Energy totals: solar={kwh_solar}, grid_in={kwh_grid_import}, "
                     f"grid_out={kwh_grid_export}, load={kwh_load}, "
                     f"bat_chg={kwh_battery_charge}, bat_dis={kwh_battery_discharge}")
            log.info(f"  Cell signal: {cell_signal}, WiFi: {wifi_signal}")
            log.info(f"  Ambient temp: {ambient_temp}")
            log.info(f"  Run status: {run_status_val}, Mode: {mode_name}")
            log.info(f"  Charging: solar_to_bat={solar_to_bat}, grid_to_bat={grid_to_bat}")
            return True

        if DB_AVAILABLE:
            init_db()
            store.system_reading_update_cloud(
                timestamp=ts,
                per_battery_soc_json=per_battery_soc,
                per_battery_power_json=per_battery_power,
                kwh_solar=kwh_solar,
                kwh_grid_import=kwh_grid_import,
                kwh_grid_export=kwh_grid_export,
                kwh_load=kwh_load,
                kwh_battery_charge=kwh_battery_charge,
                kwh_battery_discharge=kwh_battery_discharge,
                kwh_generator=kwh_generator,
                cell_signal=cell_signal,
                wifi_signal=wifi_signal,
                solar_to_battery_kw=solar_to_bat,
                grid_to_battery_kw=grid_to_bat,
            )
            log.info(f"Cloud data stored ({response_ms:.0f}ms): "
                     f"per_bat_soc={per_battery_soc}, "
                     f"kwh_solar={kwh_solar}, cell={cell_signal}")
        else:
            log.warning("DB not available, data not stored")

        return True

    except Exception as e:
        log.error(f"Cloud API collection failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description='Collect Franklin cloud API data')
    parser.add_argument('--test', action='store_true', help='Print data without writing to DB')
    args = parser.parse_args()

    load_env()

    success = asyncio.run(collect_cloud_data(test_mode=args.test))
    return 0 if success else 1


if __name__ == '__main__':
    sys.exit(main())
