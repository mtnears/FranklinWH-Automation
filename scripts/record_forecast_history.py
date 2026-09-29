#!/usr/bin/env python3
"""
record_forecast_history.py — keep a durable record of solar forecasts vs actuals

The engine's forecast reasoning lives in intelligence_log, which is pruned
after 30 days. This table keeps, per target day:

    stage 'evening'  forecast for TOMORROW made the evening before (20:40)
    stage 'morning'  calibrated forecast for TODAY made before production (06:40)

plus the day's actual battery-connected solar (filled the next morning from
daily_energy_summary) and the solar-day window used (sunrise/sunset, learned
production start/end). Enough to judge any day's prediction after the fact,
including the days around clock changes.

    python3 record_forecast_history.py evening
    python3 record_forecast_history.py morning      # also fills yesterday's actual
    python3 record_forecast_history.py report [days]
"""

import sys
from datetime import date, datetime, timedelta

import db
import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_history (
    target_date TEXT NOT NULL,
    stage TEXT NOT NULL,                 -- evening | morning
    made_at TEXT NOT NULL,
    forecast_kwh REAL,                   -- what the engine would use
    raw_api_kwh REAL,                    -- uncalibrated provider value
    calibration_factor REAL,
    yesterday_correction REAL,
    weather_score REAL,
    source TEXT,
    sunrise REAL, sunset REAL,           -- local decimal hours (astronomical)
    prod_start REAL, prod_end REAL,      -- learned production window
    actual_kwh REAL,                     -- battery-connected solar, filled next day
    PRIMARY KEY (target_date, stage)
);
"""


def _init():
    db.init_db()
    with db._safe_write() as conn:
        conn.executescript(SCHEMA)


def _engine():
    import solar_forecast
    eng = solar_forecast.get_forecast_engine()
    return eng


def _solar_day(d: date) -> dict:
    try:
        import solar_day
        sd = solar_day.solar_day(d)
        return {'sunrise': sd.sunrise, 'sunset': sd.sunset,
                'prod_start': sd.start, 'prod_end': sd.end}
    except Exception:
        return {'sunrise': None, 'sunset': None, 'prod_start': None, 'prod_end': None}


def _upsert(target: date, stage: str, fc, yc=None):
    sd = _solar_day(target)
    with db._safe_write() as conn:
        conn.execute("""
            INSERT INTO forecast_history (target_date, stage, made_at, forecast_kwh, raw_api_kwh,
                calibration_factor, yesterday_correction, weather_score, source,
                sunrise, sunset, prod_start, prod_end)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(target_date, stage) DO UPDATE SET
                made_at=excluded.made_at, forecast_kwh=excluded.forecast_kwh,
                raw_api_kwh=excluded.raw_api_kwh, calibration_factor=excluded.calibration_factor,
                yesterday_correction=excluded.yesterday_correction,
                weather_score=excluded.weather_score, source=excluded.source,
                sunrise=excluded.sunrise, sunset=excluded.sunset,
                prod_start=excluded.prod_start, prod_end=excluded.prod_end
        """, (target.isoformat(), stage, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
              getattr(fc, 'total_kwh', None), getattr(fc, 'raw_api_kwh', None),
              getattr(fc, 'calibration_factor', None), yc,
              getattr(fc, 'weather_score', None), getattr(fc, 'source', None),
              sd['sunrise'], sd['sunset'], sd['prod_start'], sd['prod_end']))


def fill_actual(day: date) -> bool:
    rows = db.query("SELECT SUM(solar_kwh) AS kwh FROM daily_energy_summary WHERE date = ?",
                    (day.isoformat(),))
    kwh = rows[0]['kwh'] if rows else None
    if kwh is None:
        return False
    with db._safe_write() as conn:
        conn.execute("UPDATE forecast_history SET actual_kwh = ? WHERE target_date = ?",
                     (round(kwh, 2), day.isoformat()))
    return True


def evening():
    eng = _engine()
    eng.get_today_forecast()                      # refreshes the provider fetch (today+tomorrow)
    tomorrow = date.today() + timedelta(days=1)
    fc = eng._cache.get(tomorrow.isoformat())
    if fc is None:
        print(f'no forecast cached for {tomorrow}')
        return 1
    _upsert(tomorrow, 'evening', fc)
    print(f'evening forecast for {tomorrow}: {fc.total_kwh} kWh ({fc.source})')
    return 0


def morning():
    eng = _engine()
    fc = eng.get_today_forecast(force_refresh=True)
    try:
        yc = round(eng._yesterday_correction_factor(), 3)
    except Exception:
        yc = None
    _upsert(date.today(), 'morning', fc, yc)
    y = date.today() - timedelta(days=1)
    ok = fill_actual(y)
    print(f'morning forecast for {date.today()}: {fc.total_kwh} kWh ({fc.source}); '
          f'actual for {y}: {"filled" if ok else "not available yet"}')
    return 0


def report(days: int = 14):
    rows = db.query("""
        SELECT target_date, stage, forecast_kwh, raw_api_kwh, actual_kwh, prod_start, prod_end
        FROM forecast_history WHERE target_date >= ? ORDER BY target_date, stage""",
                    ((date.today() - timedelta(days=days)).isoformat(),))
    for r in rows:
        err = ''
        if r['actual_kwh'] and r['forecast_kwh']:
            err = f"{(r['forecast_kwh'] - r['actual_kwh']) / r['actual_kwh'] * 100:+.0f}%"
        print(f"{r['target_date']} {r['stage']:8} fc={r['forecast_kwh']} raw={r['raw_api_kwh']} "
              f"actual={r['actual_kwh']} {err}")
    return 0


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'morning'
    _init()
    if cmd == 'evening':
        sys.exit(evening())
    if cmd == 'morning':
        sys.exit(morning())
    if cmd == 'report':
        sys.exit(report(int(sys.argv[2]) if len(sys.argv) > 2 else 14))
    print(__doc__)
    sys.exit(2)
