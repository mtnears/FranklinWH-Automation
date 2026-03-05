#!/usr/bin/env python3
"""
collect_weather_db.py — Weather data collector for SQLite

Queries Weather Underground API every 15 minutes and stores:
  1. Raw observation → weather_observations table
  2. Updates daily aggregate → weather_daily table

This is the sole weather collector — collect_weather.py (CSV-based) is retired.
solar_forecast.py reads directly from these SQLite tables for calibration.

Requires: WEATHER_ENABLED=true, WEATHER_STATION_ID, WEATHER_API_KEY in env/config.
"""

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

logger = logging.getLogger('collect_weather_db')

DB_PATH = Path(os.getenv('DATA_DIR', '/app/data')) / 'franklin.db'

WEATHER_OBSERVATIONS_DDL = """
CREATE TABLE IF NOT EXISTS weather_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    station_id TEXT NOT NULL,
    obs_time_local TEXT,
    temp_f REAL,
    heat_index_f REAL,
    dewpoint_f REAL,
    wind_chill_f REAL,
    humidity REAL,
    pressure_inhg REAL,
    wind_speed_mph REAL,
    wind_gust_mph REAL,
    wind_dir_degrees INTEGER,
    precip_rate_in_hr REAL,
    precip_total_in REAL,
    solar_radiation_wm2 REAL,
    uv_index REAL,
    neighborhood TEXT,
    source TEXT DEFAULT 'wu_api',
    UNIQUE(timestamp, station_id)
);
CREATE INDEX IF NOT EXISTS idx_weather_obs_ts ON weather_observations(timestamp);
CREATE INDEX IF NOT EXISTS idx_weather_obs_station_ts ON weather_observations(station_id, timestamp);
"""

WEATHER_DAILY_DDL = """
CREATE TABLE IF NOT EXISTS weather_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    station_id TEXT NOT NULL,
    temp_high REAL,
    temp_low REAL,
    temp_avg REAL,
    dewpoint_high REAL,
    dewpoint_low REAL,
    dewpoint_avg REAL,
    heat_index_high REAL,
    windchill_low REAL,
    humidity_high REAL,
    humidity_low REAL,
    humidity_avg REAL,
    pressure_max REAL,
    pressure_min REAL,
    wind_speed_avg REAL,
    wind_speed_high REAL,
    wind_gust_high REAL,
    precip_total REAL,
    solar_radiation_high REAL,
    uv_index_high REAL,
    observation_count INTEGER DEFAULT 0,
    source TEXT DEFAULT 'aggregated',
    UNIQUE(date, station_id)
);
CREATE INDEX IF NOT EXISTS idx_weather_daily_date ON weather_daily(date);
CREATE INDEX IF NOT EXISTS idx_weather_daily_station ON weather_daily(station_id, date);
"""


def ensure_tables():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(WEATHER_OBSERVATIONS_DDL)
    conn.executescript(WEATHER_DAILY_DDL)
    conn.close()


def fetch_wu_observation():
    try:
        from config import config
    except ImportError:
        logger.warning("config module not available")
        return None

    if not config.WEATHER_ENABLED:
        return None
    if not config.WEATHER_STATION_ID or not config.WEATHER_API_KEY:
        logger.warning("Weather station ID or API key not configured")
        return None

    import urllib.request
    import urllib.error

    url = (
        f"https://api.weather.com/v2/pws/observations/current"
        f"?stationId={config.WEATHER_STATION_ID}&format=json&units=e"
        f"&apiKey={config.WEATHER_API_KEY}"
    )

    try:
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 'FranklinWH-Automation/4.0')
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        obs_list = data.get('observations', [])
        if not obs_list:
            logger.warning("No observation data in WU response")
            return None

        obs = obs_list[0]
        imp = obs.get('imperial', {})

        return {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'station_id': obs.get('stationID', config.WEATHER_STATION_ID),
            'obs_time_local': obs.get('obsTimeLocal', ''),
            'temp_f': imp.get('temp'),
            'heat_index_f': imp.get('heatIndex'),
            'dewpoint_f': imp.get('dewpt'),
            'wind_chill_f': imp.get('windChill'),
            'humidity': obs.get('humidity'),
            'pressure_inhg': imp.get('pressure'),
            'wind_speed_mph': imp.get('windSpeed'),
            'wind_gust_mph': imp.get('windGust'),
            'wind_dir_degrees': obs.get('winddir'),
            'precip_rate_in_hr': imp.get('precipRate'),
            'precip_total_in': imp.get('precipTotal'),
            'solar_radiation_wm2': obs.get('solarRadiation'),
            'uv_index': obs.get('uv'),
            'neighborhood': obs.get('neighborhood', ''),
        }
    except Exception as e:
        logger.error(f"WU API error: {e}")
        return None


def store_observation(obs):
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            INSERT OR REPLACE INTO weather_observations
            (timestamp, station_id, obs_time_local, temp_f, heat_index_f,
             dewpoint_f, wind_chill_f, humidity, pressure_inhg,
             wind_speed_mph, wind_gust_mph, wind_dir_degrees,
             precip_rate_in_hr, precip_total_in,
             solar_radiation_wm2, uv_index, neighborhood, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'wu_api')
        """, (
            obs['timestamp'], obs['station_id'], obs['obs_time_local'],
            obs['temp_f'], obs['heat_index_f'],
            obs['dewpoint_f'], obs['wind_chill_f'],
            obs['humidity'], obs['pressure_inhg'],
            obs['wind_speed_mph'], obs['wind_gust_mph'], obs['wind_dir_degrees'],
            obs['precip_rate_in_hr'], obs['precip_total_in'],
            obs['solar_radiation_wm2'], obs['uv_index'], obs['neighborhood'],
        ))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"DB write failed: {e}")
        return False


def update_daily_aggregate(obs):
    date_str = obs['timestamp'][:10]
    station_id = obs['station_id']

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

        row = conn.execute(
            "SELECT * FROM weather_daily WHERE date=? AND station_id=?",
            (date_str, station_id)
        ).fetchone()

        def safe_float(v):
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        temp = safe_float(obs.get('temp_f'))
        dewpoint = safe_float(obs.get('dewpoint_f'))
        heat_index = safe_float(obs.get('heat_index_f'))
        windchill = safe_float(obs.get('wind_chill_f'))
        humidity = safe_float(obs.get('humidity'))
        pressure = safe_float(obs.get('pressure_inhg'))
        wind_speed = safe_float(obs.get('wind_speed_mph'))
        wind_gust = safe_float(obs.get('wind_gust_mph'))
        precip_total = safe_float(obs.get('precip_total_in'))
        solar_rad = safe_float(obs.get('solar_radiation_wm2'))
        uv = safe_float(obs.get('uv_index'))

        def _max(a, b):
            if a is None: return b
            if b is None: return a
            return max(a, b)

        def _min(a, b):
            if a is None: return b
            if b is None: return a
            return min(a, b)

        if row is None:
            conn.execute("""
                INSERT INTO weather_daily
                (date, station_id, temp_high, temp_low, temp_avg,
                 dewpoint_high, dewpoint_low, dewpoint_avg,
                 heat_index_high, windchill_low,
                 humidity_high, humidity_low, humidity_avg,
                 pressure_max, pressure_min,
                 wind_speed_avg, wind_speed_high, wind_gust_high,
                 precip_total, solar_radiation_high, uv_index_high,
                 observation_count, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'aggregated')
            """, (
                date_str, station_id,
                temp, temp, temp,
                dewpoint, dewpoint, dewpoint,
                heat_index, windchill,
                humidity, humidity, humidity,
                pressure, pressure,
                wind_speed, wind_speed, wind_gust,
                precip_total, solar_rad, uv,
            ))
        else:
            cols = [desc[0] for desc in conn.execute("PRAGMA table_info(weather_daily)").fetchall()]
            old = dict(zip([c[1] for c in conn.execute("PRAGMA table_info(weather_daily)").fetchall()],
                          row))
            n = old.get('observation_count', 0) or 0

            new_temp_high = _max(old.get('temp_high'), temp)
            new_temp_low = _min(old.get('temp_low'), temp)
            old_avg = old.get('temp_avg') or 0
            new_temp_avg = (old_avg * n + (temp or 0)) / (n + 1) if temp is not None else old_avg

            new_dp_high = _max(old.get('dewpoint_high'), dewpoint)
            new_dp_low = _min(old.get('dewpoint_low'), dewpoint)
            old_dp_avg = old.get('dewpoint_avg') or 0
            new_dp_avg = (old_dp_avg * n + (dewpoint or 0)) / (n + 1) if dewpoint is not None else old_dp_avg

            new_hi_high = _max(old.get('heat_index_high'), heat_index)
            new_wc_low = _min(old.get('windchill_low'), windchill)

            new_hum_high = _max(old.get('humidity_high'), humidity)
            new_hum_low = _min(old.get('humidity_low'), humidity)
            old_hum_avg = old.get('humidity_avg') or 0
            new_hum_avg = (old_hum_avg * n + (humidity or 0)) / (n + 1) if humidity is not None else old_hum_avg

            new_pres_max = _max(old.get('pressure_max'), pressure)
            new_pres_min = _min(old.get('pressure_min'), pressure)

            old_ws_avg = old.get('wind_speed_avg') or 0
            new_ws_avg = (old_ws_avg * n + (wind_speed or 0)) / (n + 1) if wind_speed is not None else old_ws_avg
            new_ws_high = _max(old.get('wind_speed_high'), wind_speed)
            new_wg_high = _max(old.get('wind_gust_high'), wind_gust)

            new_precip = _max(old.get('precip_total'), precip_total)
            new_solar_high = _max(old.get('solar_radiation_high'), solar_rad)
            new_uv_high = _max(old.get('uv_index_high'), uv)

            conn.execute("""
                UPDATE weather_daily SET
                    temp_high=?, temp_low=?, temp_avg=?,
                    dewpoint_high=?, dewpoint_low=?, dewpoint_avg=?,
                    heat_index_high=?, windchill_low=?,
                    humidity_high=?, humidity_low=?, humidity_avg=?,
                    pressure_max=?, pressure_min=?,
                    wind_speed_avg=?, wind_speed_high=?, wind_gust_high=?,
                    precip_total=?, solar_radiation_high=?, uv_index_high=?,
                    observation_count=?, source='aggregated'
                WHERE date=? AND station_id=?
            """, (
                new_temp_high, new_temp_low, round(new_temp_avg, 1),
                new_dp_high, new_dp_low, round(new_dp_avg, 1),
                new_hi_high, new_wc_low,
                new_hum_high, new_hum_low, round(new_hum_avg, 1),
                new_pres_max, new_pres_min,
                round(new_ws_avg, 1), new_ws_high, new_wg_high,
                new_precip, new_solar_high, new_uv_high,
                n + 1,
                date_str, station_id,
            ))

        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Daily aggregate update failed: {e}")
        return False


def collect():
    ensure_tables()
    obs = fetch_wu_observation()
    if not obs:
        return False

    ok = store_observation(obs)
    if ok:
        update_daily_aggregate(obs)
        temp = obs.get('temp_f', '?')
        solar = obs.get('solar_radiation_wm2', '?')
        logger.info(f"Weather: {temp}F, solar_rad={solar}W/m², station={obs['station_id']}")
    return ok


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s')
    success = collect()
    sys.exit(0 if success else 1)
