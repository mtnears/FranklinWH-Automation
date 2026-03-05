#!/usr/bin/env python3
"""
solar_forecast.py — Solar Production Forecasting for FranklinWH v4.0

Predicts how much solar energy will reach the battery today so the engine
can decide how much grid charging is needed (the "morning gap calculation").

IMPORTANT: Only the house array (Enphase, 6.96 kWp) feeds the battery.
The barn ground-mount (SolarEdge, 21.3 kWp) is on a separate meter and
does NOT contribute to battery charging. It only matters for aggregated
NEM2 billing, not for charging decisions.

Forecast sources (priority order):
  1. Forecast.Solar API (free, no key) — weather-aware hourly estimates
  2. Weather-calibrated clear-sky model — uses local weather history
     from SQLite (weather_daily + weather_observations tables) to
     estimate production quality
  3. Learned profile fallback — system_profile.py historical averages

The gap calculation:
  target_kwh = capacity * target_soc%
  forecast_solar_to_battery = mode-aware (see below)
  gap = target_kwh - current_kwh - forecast_solar_to_battery - tou_drift
  morning_ceiling = current_kwh + max(0, gap)

Mode-aware solar model:
  - Non-export (TOU mode): Grid powers home, solar goes to battery directly.
    net_solar ≈ 85% of forecast (small losses for inverter/transients).
  - Export (SC mode): Solar powers home first, surplus charges battery.
    net_solar = sum(max(0, solar_hour - consumption_hour))

If gap <= 0: solar alone will fill the battery. Do NOT grid charge.
If gap > 0:  grid charge only this much, leaving room for free solar.

Part of the v4.0 Adaptive Decision Engine.
"""

import json
import logging
import os
import math
from datetime import datetime, timedelta, date
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict
from pathlib import Path

try:
    import urllib.request
    import urllib.error
    HAS_URLLIB = True
except ImportError:
    HAS_URLLIB = False

logger = logging.getLogger('solar_forecast')

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FORECAST_SOLAR_BASE_URL = "https://api.forecast.solar"
FORECAST_SOLAR_MIN_INTERVAL_SEC = 360   # 6 min between API calls

FORECAST_CACHE_MAX_AGE_MIN = 60         # Re-fetch hourly
CALIBRATION_MIN_DAYS = 14               # Minimum paired data points

# House array defaults (battery-connected — the ONLY array that matters)
HOUSE_ARRAY_KWP = 6.96                  # 16x Hyundai 435W
HOUSE_ARRAY_TILT = 22                   # Roof pitch estimate
HOUSE_ARRAY_AZIMUTH = -65               # ~295° true → -65 in Forecast.Solar (0=S, -90=E, 90=W)

DEFAULT_LATITUDE = 38.91
DEFAULT_LONGITUDE = -120.84

# Sun schedule by month for ~39°N (PST/PDT hours)
SUN_SCHEDULE = {
    1: (7.3, 17.2), 2: (6.9, 17.8), 3: (7.2, 19.4),
    4: (6.4, 19.8), 5: (5.9, 20.3), 6: (5.7, 20.5),
    7: (5.9, 20.4), 8: (6.3, 19.9), 9: (6.7, 19.2),
    10: (7.1, 18.5), 11: (6.6, 17.1), 12: (7.1, 16.9),
}

# Clear-sky efficiency by month (fraction of nameplate kWp)
CLEAR_SKY_EFFICIENCY = {
    1: 0.48, 2: 0.55, 3: 0.65, 4: 0.75, 5: 0.82, 6: 0.85,
    7: 0.84, 8: 0.80, 9: 0.72, 10: 0.60, 11: 0.50, 12: 0.44,
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ArrayConfig:
    """Solar array physical parameters."""
    name: str
    latitude: float
    longitude: float
    declination: int        # Tilt degrees
    azimuth: int            # Forecast.Solar: 0=south, -90=east, 90=west
    kwp: float
    feeds_battery: bool = True

    def api_path(self) -> str:
        return f"{self.declination}/{self.azimuth}/{self.kwp}"


@dataclass
class HourlyForecast:
    """Single hour forecast."""
    hour: int
    watts: float
    watt_hours: float
    source: str = 'api'


@dataclass
class DayForecast:
    """Complete day forecast."""
    date: date
    hourly: List[HourlyForecast] = field(default_factory=list)
    total_kwh: float = 0.0
    source: str = 'unknown'
    calibration_factor: float = 1.0
    weather_score: float = -1.0
    raw_api_kwh: float = 0.0
    fetched_at: Optional[str] = None

    def kwh_between(self, from_hour: int, until_hour: int) -> float:
        return sum(h.watt_hours / 1000.0 for h in self.hourly
                   if from_hour <= h.hour < until_hour)

    def remaining_kwh(self, until_hour: int = 17) -> float:
        return self.kwh_between(datetime.now().hour, until_hour)

    def to_dict(self) -> dict:
        return {
            'date': self.date.isoformat(),
            'total_kwh': round(self.total_kwh, 2),
            'raw_api_kwh': round(self.raw_api_kwh, 2),
            'source': self.source,
            'calibration_factor': round(self.calibration_factor, 3),
            'weather_score': round(self.weather_score, 3) if self.weather_score >= 0 else None,
            'fetched_at': self.fetched_at,
            'hourly': [{'hour': h.hour, 'watts': round(h.watts, 1),
                        'watt_hours': round(h.watt_hours, 1), 'source': h.source}
                       for h in self.hourly],
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'DayForecast':
        hourly = [HourlyForecast(h['hour'], h['watts'], h['watt_hours'],
                                 h.get('source', 'cached'))
                  for h in data.get('hourly', [])]
        return cls(
            date=date.fromisoformat(data['date']), hourly=hourly,
            total_kwh=data.get('total_kwh', 0),
            raw_api_kwh=data.get('raw_api_kwh', 0),
            source=data.get('source', 'cached'),
            calibration_factor=data.get('calibration_factor', 1.0),
            weather_score=data.get('weather_score', -1),
            fetched_at=data.get('fetched_at'),
        )


@dataclass
class MorningPlan:
    """The morning gap calculation result — primary output for the decision engine."""
    timestamp: str
    current_soc_pct: float
    target_soc_pct: float
    battery_capacity_kwh: float
    current_kwh: float
    target_kwh: float

    forecast_total_kwh: float
    forecast_remaining_kwh: float
    forecast_to_battery_kwh: float
    expected_consumption_kwh: float

    gap_kwh: float
    morning_ceiling_pct: float
    morning_ceiling_kwh: float

    forecast_source: str
    weather_score: float
    recommendation: str

    def to_dict(self) -> dict:
        return asdict(self)

    def to_log_str(self) -> str:
        return (f"Plan: solar_rem={self.forecast_remaining_kwh:.1f}kWh, "
                f"consumption={self.expected_consumption_kwh:.1f}kWh, "
                f"net_to_bat={self.forecast_to_battery_kwh:.1f}kWh, "
                f"gap={self.gap_kwh:.1f}kWh, "
                f"ceiling={self.morning_ceiling_pct:.0f}% "
                f"[{self.forecast_source}]")


# ---------------------------------------------------------------------------
# Default array configuration
# ---------------------------------------------------------------------------

def get_default_arrays() -> List[ArrayConfig]:
    """Build array config from environment or defaults. House array only."""
    lat = float(os.getenv('FORECAST_LATITUDE', str(DEFAULT_LATITUDE)))
    lon = float(os.getenv('FORECAST_LONGITUDE', str(DEFAULT_LONGITUDE)))
    return [ArrayConfig(
        name='house',
        latitude=lat, longitude=lon,
        declination=int(os.getenv('FORECAST_HOUSE_TILT', str(HOUSE_ARRAY_TILT))),
        azimuth=int(os.getenv('FORECAST_HOUSE_AZIMUTH', str(HOUSE_ARRAY_AZIMUTH))),
        kwp=float(os.getenv('FORECAST_HOUSE_KWP', str(HOUSE_ARRAY_KWP))),
        feeds_battery=True,
    )]


# ---------------------------------------------------------------------------
# Forecast.Solar API
# ---------------------------------------------------------------------------

class ForecastSolarAPI:
    """Free Forecast.Solar API — no key needed, 12 req/hour."""

    def __init__(self, arrays: List[ArrayConfig], api_key: str = None):
        self.arrays = [a for a in arrays if a.feeds_battery]
        self.api_key = api_key or os.getenv('FORECAST_SOLAR_API_KEY', '')
        self._last_fetch: Optional[datetime] = None

    def _build_url(self, array: ArrayConfig) -> str:
        base = FORECAST_SOLAR_BASE_URL
        if self.api_key:
            return f"{base}/{self.api_key}/estimate/{array.latitude}/{array.longitude}/{array.api_path()}"
        return f"{base}/estimate/{array.latitude}/{array.longitude}/{array.api_path()}"

    def fetch(self) -> Optional[Dict[str, DayForecast]]:
        if not HAS_URLLIB or not self.arrays:
            return None

        # Rate limit
        if self._last_fetch:
            elapsed = (datetime.now() - self._last_fetch).total_seconds()
            if elapsed < FORECAST_SOLAR_MIN_INTERVAL_SEC:
                logger.debug(f"Rate limit: {elapsed:.0f}s < {FORECAST_SOLAR_MIN_INTERVAL_SEC}s")
                return None

        combined_watts: Dict[str, Dict[int, list]] = {}
        combined_daily: Dict[str, float] = {}
        any_success = False

        for array in self.arrays:
            url = self._build_url(array)
            logger.info(f"Forecast.Solar: {array.name} → {url}")

            try:
                req = urllib.request.Request(url)
                req.add_header('Accept', 'application/json')
                req.add_header('User-Agent', 'FranklinWH-Automation/4.0')

                with urllib.request.urlopen(req, timeout=30) as resp:
                    self._last_fetch = datetime.now()
                    data = json.loads(resp.read().decode())

                if not data or 'result' not in data:
                    continue

                msg = data.get('message', {})
                if msg.get('type') == 'error':
                    logger.error(f"API error: {msg.get('text', '?')}")
                    continue

                any_success = True
                result = data['result']

                for ts_str, watts in result.get('watts', {}).items():
                    try:
                        ts = datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
                        dk = ts.strftime('%Y-%m-%d')
                        h = ts.hour
                        combined_watts.setdefault(dk, {}).setdefault(h, []).append(float(watts))
                    except (ValueError, TypeError):
                        continue

                for dk, wh in result.get('watt_hours_day', {}).items():
                    combined_daily[dk] = combined_daily.get(dk, 0) + float(wh)

            except urllib.error.HTTPError as e:
                logger.warning(f"Forecast.Solar HTTP {e.code}" +
                               (" (rate limited)" if e.code == 429 else f": {e.reason}"))
            except Exception as e:
                logger.error(f"Forecast.Solar error: {e}")

        if not any_success:
            return None

        forecasts = {}
        now_str = datetime.now().isoformat()
        for dk in sorted(combined_watts.keys()):
            hourly = []
            for hour in range(24):
                raw = combined_watts[dk].get(hour, [])
                avg_w = sum(raw) / len(raw) if raw else 0.0
                hourly.append(HourlyForecast(hour=hour, watts=avg_w, watt_hours=avg_w, source='api'))

            dt = datetime.strptime(dk, '%Y-%m-%d').date()
            total = combined_daily.get(dk, sum(h.watt_hours for h in hourly)) / 1000.0
            forecasts[dk] = DayForecast(
                date=dt, hourly=hourly, total_kwh=round(total, 2),
                raw_api_kwh=round(total, 2), source='forecast_solar',
                fetched_at=now_str,
            )

        today_kwh = forecasts.get(date.today().isoformat(), DayForecast(date=date.today())).total_kwh
        logger.info(f"Forecast.Solar: {len(forecasts)} days, today={today_kwh:.1f} kWh")
        return forecasts


# ---------------------------------------------------------------------------
# Weather Calibration
# ---------------------------------------------------------------------------

class WeatherCalibration:
    """Adjusts forecasts using local weather observations.

    Weather data sources (all from SQLite via db module):
      1. weather_daily table — long-term daily aggregates (imported history + live)
      2. weather_observations table — 15-minute readings from collect_weather_db.py,
         aggregated into daily summaries for recent days
      3. Weather Underground API — live current conditions for "today" (fallback)

    Any user with WEATHER_ENABLED=true and a WU API key will start building
    history immediately via collect_weather_db.py. After 14 days of paired
    weather + solar data, the calibration model activates automatically.

    Users without WU still get Forecast.Solar API (which has its own weather
    model) — the calibration just adds local fine-tuning on top.
    """

    def __init__(self, house_kwp: float = HOUSE_ARRAY_KWP,
                 wu_station_id: str = None, wu_api_key: str = None,
                 db_module=None):
        self._db = db_module                        # db.py module (required source)
        self.wu_station_id = wu_station_id or os.getenv('WEATHER_STATION_ID', '')
        self.wu_api_key = wu_api_key or os.getenv('WEATHER_API_KEY', '')
        self.house_kwp = house_kwp
        self._weather: Dict[str, dict] = {}
        self._model: Optional[dict] = None

    def load_weather(self) -> int:
        """Load weather from SQLite database.

        Sources: weather_daily table (long-term history),
        then weather_observations for recent days to fill gaps.
        Requires db module — fails visibly if unavailable.
        """
        if not self._db:
            logger.warning("Weather: no db module — calibration disabled")
            return 0

        count = 0

        # Source 1: weather_daily table (all historical data)
        count += self._load_daily_from_db()

        # Source 2: weather_observations (recent, overrides stale daily rows)
        count_obs = self._load_observations_from_db()
        if count_obs > 0:
            logger.info(f"Weather observations (DB): {count_obs} days aggregated")

        logger.info(f"Weather: {len(self._weather)} total days loaded")
        return len(self._weather)

    def _load_daily_from_db(self) -> int:
        """Load from weather_daily SQLite table."""
        if not self._db:
            return 0
        try:
            rows = self._db.get_weather_daily_all()
            count = 0
            for row in rows:
                d = row.get('date', '').strip()
                if not d:
                    continue
                self._weather[d] = {
                    'temp_high': row.get('temp_high') or 0,
                    'temp_low': row.get('temp_low') or 0,
                    'humidity_avg': row.get('humidity_avg') or 0,
                    'precip_total': row.get('precip_total') or 0,
                    'pressure_max': row.get('pressure_max') or 0,
                    'source': 'weather_daily_db',
                }
                count += 1
            if count > 0:
                logger.info(f"weather_daily (DB): {count} days")
            return count
        except Exception as e:
            logger.error(f"weather_daily DB error: {e}")
            return 0

    def _load_observations_from_db(self) -> int:
        """Load recent weather_observations from DB, aggregate to daily.

        Only overrides dates not already covered by weather_daily,
        or recent dates where observations may be more current.
        """
        if not self._db:
            return 0
        try:
            recent_start = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
            today = datetime.now().strftime('%Y-%m-%d')
            new_days = 0
            d = datetime.strptime(recent_start, '%Y-%m-%d')
            end = datetime.strptime(today, '%Y-%m-%d')
            while d <= end:
                date_str = d.strftime('%Y-%m-%d')
                agg = self._db.get_weather_observations_daily_agg(date_str)
                if agg and agg.get('observation_count', 0) > 0:
                    daily = {
                        'temp_high': agg.get('temp_high') or 0,
                        'temp_low': agg.get('temp_low') or 0,
                        'humidity_avg': agg.get('humidity_avg') or 0,
                        'precip_total': agg.get('precip_total') or 0,
                        'pressure_max': agg.get('pressure_max') or 0,
                        'source': 'weather_observations_db',
                    }
                    if date_str not in self._weather or self._weather[date_str].get('source') != 'weather_daily_db':
                        self._weather[date_str] = daily
                        new_days += 1
                d += timedelta(days=1)
            return new_days
        except Exception as e:
            logger.error(f"weather_observations DB error: {e}")
            return 0

    def weather_score(self, weather: dict = None, date_str: str = None) -> float:
        """Weather conditions → 0.0-1.0 solar quality score."""
        if weather is None and date_str:
            weather = self._weather.get(date_str)
        if weather is None:
            return 0.5

        humidity = weather.get('humidity_avg', 50)
        precip = weather.get('precip_total', 0)
        temp_high = weather.get('temp_high', 60)
        temp_low = weather.get('temp_low', 40)
        pressure = weather.get('pressure_max', 30.0)

        # Humidity: low=clear, high=cloudy
        f_hum = max(0.15, 1.0 - 0.85 * max(0, humidity - 40) / 55.0) if humidity > 40 else 1.0

        # Precipitation: any rain = significant clouds
        if precip <= 0:
            f_precip = 1.0
        elif precip < 0.05:
            f_precip = 0.75
        elif precip < 0.2:
            f_precip = 0.5
        elif precip < 0.5:
            f_precip = 0.3
        else:
            f_precip = 0.15

        # Temp range: wide=clear (sun heats day, radiative cooling at night)
        temp_range = temp_high - temp_low
        if temp_range >= 25:
            f_temp = 1.0
        elif temp_range >= 18:
            f_temp = 0.85
        elif temp_range >= 12:
            f_temp = 0.6
        elif temp_range >= 6:
            f_temp = 0.4
        else:
            f_temp = 0.2

        # Barometric pressure: high=clear, low=storm
        if pressure >= 30.20:
            f_pres = 1.0
        elif pressure >= 30.00:
            f_pres = 0.85
        elif pressure >= 29.80:
            f_pres = 0.55
        else:
            f_pres = 0.25

        score = 0.35 * f_hum + 0.30 * f_precip + 0.20 * f_temp + 0.15 * f_pres
        return max(0.05, min(1.0, score))

    def build_model(self, solar_daily: List[dict]) -> Optional[dict]:
        """Build calibration from paired weather + house solar production data."""
        pairs = []
        for entry in solar_daily:
            d = entry.get('date', '')
            kwh = entry.get('kwh', 0)
            if d not in self._weather or kwh < 0.3:
                continue
            dt = datetime.strptime(d, '%Y-%m-%d').date()
            clear_sky = self._clear_sky_kwh(dt)
            if clear_sky < 0.5:
                continue
            ws = self.weather_score(self._weather[d])
            pairs.append({
                'weather_score': ws,
                'production_ratio': min(kwh / clear_sky, 1.5),
            })

        if len(pairs) < CALIBRATION_MIN_DAYS:
            logger.info(f"Calibration: {len(pairs)}/{CALIBRATION_MIN_DAYS} paired days (insufficient)")
            return None

        # Linear regression: production_ratio = slope * weather_score + intercept
        n = len(pairs)
        sx = sum(p['weather_score'] for p in pairs)
        sy = sum(p['production_ratio'] for p in pairs)
        sxy = sum(p['weather_score'] * p['production_ratio'] for p in pairs)
        sx2 = sum(p['weather_score'] ** 2 for p in pairs)
        denom = n * sx2 - sx ** 2

        if abs(denom) < 1e-10:
            slope, intercept = 0.0, sy / n
        else:
            slope = (n * sxy - sx * sy) / denom
            intercept = (sy - slope * sx) / n

        y_mean = sy / n
        ss_tot = sum((p['production_ratio'] - y_mean) ** 2 for p in pairs)
        ss_res = sum((p['production_ratio'] - (slope * p['weather_score'] + intercept)) ** 2 for p in pairs)
        r2 = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0

        self._model = {'slope': round(slope, 4), 'intercept': round(intercept, 4),
                       'r_squared': round(r2, 4), 'samples': n}
        logger.info(f"Calibration: ratio = {slope:.3f}*score + {intercept:.3f} (R²={r2:.3f}, n={n})")
        return self._model

    def correction_factor(self, weather: dict = None, date_str: str = None) -> float:
        """Get production correction factor for given weather conditions."""
        ws = self.weather_score(weather, date_str)
        if self._model:
            factor = self._model['slope'] * ws + self._model['intercept']
        else:
            factor = ws  # No model — raw weather score as rough proxy
        return max(0.05, min(1.5, factor))

    def _clear_sky_kwh(self, dt: date) -> float:
        """Clear-sky kWh for the house array. WNW azimuth reduces ~18%."""
        month = dt.month
        eff = CLEAR_SKY_EFFICIENCY.get(month, 0.65)
        sunrise, sunset = SUN_SCHEDULE.get(month, (7.0, 18.0))
        return self.house_kwp * eff * (sunset - sunrise) * 0.82  # 0.82 = WNW penalty

    def get_today_weather(self) -> Optional[dict]:
        """Get today's weather — from loaded data, DB observations, or live WU API."""
        today = datetime.now().strftime('%Y-%m-%d')

        # Check loaded data first
        if today in self._weather:
            return self._weather[today]

        # Try DB observations for today (partial day aggregate)
        if self._db:
            try:
                agg = self._db.get_weather_observations_daily_agg(today)
                if agg and agg.get('observation_count', 0) > 0:
                    daily = {
                        'temp_high': agg.get('temp_high') or 60,
                        'temp_low': agg.get('temp_low') or 40,
                        'humidity_avg': agg.get('humidity_avg') or 50,
                        'precip_total': agg.get('precip_total') or 0,
                        'pressure_max': agg.get('pressure_max') or 30.0,
                        'source': 'weather_observations_db',
                    }
                    self._weather[today] = daily
                    return daily
            except Exception as e:
                logger.debug(f"DB today weather: {e}")

        # Try live WU API if configured
        if self.wu_station_id and self.wu_api_key and HAS_URLLIB:
            live = self._fetch_wu_current()
            if live:
                self._weather[today] = live
                return live

        return None

    def _fetch_wu_current(self) -> Optional[dict]:
        """Fetch current conditions from Weather Underground API.

        Uses the same API endpoint as collect_weather_db.py.
        Returns a daily-summary-format dict for weather_score().
        """
        try:
            url = (f"https://api.weather.com/v2/pws/observations/current"
                   f"?stationId={self.wu_station_id}&format=json&units=e"
                   f"&apiKey={self.wu_api_key}")
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'FranklinWH-Automation/4.0')

            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            obs_list = data.get('observations', [])
            if not obs_list:
                return None

            obs = obs_list[0]
            imp = obs.get('imperial', {})

            # Build a partial daily summary from current conditions
            # Not as good as end-of-day aggregates, but usable for scoring
            temp = imp.get('temp', 60)
            return {
                'temp_high': temp,              # Current temp as proxy for high
                'temp_low': temp - 10,          # Rough estimate
                'humidity_avg': obs.get('humidity', 50),
                'precip_total': imp.get('precipTotal', 0) or 0,
                'pressure_max': imp.get('pressure', 30.0) or 30.0,
                'source': 'wu_live',
            }
        except Exception as e:
            logger.debug(f"WU API fetch: {e}")
            return None


# ---------------------------------------------------------------------------
# Clear-Sky Fallback
# ---------------------------------------------------------------------------

class ClearSkyFallback:
    """Hourly clear-sky estimate for when the API is unavailable."""

    def __init__(self, kwp: float = HOUSE_ARRAY_KWP):
        self.kwp = kwp

    def estimate(self, dt: date = None) -> DayForecast:
        if dt is None:
            dt = date.today()
        month = dt.month
        eff = CLEAR_SKY_EFFICIENCY.get(month, 0.65)
        sunrise, sunset = SUN_SCHEDULE.get(month, (7.0, 18.0))
        solar_noon = (sunrise + sunset) / 2.0 + 1.0  # +1h for WNW shift
        half_day = (sunset - sunrise) / 2.0

        hourly = []
        for hour in range(24):
            h_mid = hour + 0.5
            if h_mid < sunrise or h_mid > sunset or half_day <= 0:
                watts = 0.0
            else:
                x = (h_mid - solar_noon) / half_day
                watts = self.kwp * 1000.0 * eff * max(0, math.cos(x * math.pi / 2.0)) if abs(x) < 1.0 else 0.0

            hourly.append(HourlyForecast(hour=hour, watts=max(0, watts),
                                         watt_hours=max(0, watts), source='clear_sky'))

        total = sum(h.watt_hours for h in hourly) / 1000.0
        return DayForecast(date=dt, hourly=hourly, total_kwh=round(total, 2),
                           raw_api_kwh=round(total, 2), source='clear_sky',
                           fetched_at=datetime.now().isoformat())


# ---------------------------------------------------------------------------
# Main Engine
# ---------------------------------------------------------------------------

class SolarForecastEngine:
    """Combines all forecast sources. Main interface for adaptive_engine.py.

    All forecasts are for the HOUSE ARRAY ONLY (battery-connected).
    """

    def __init__(self, arrays: List[ArrayConfig] = None,
                 cache_dir: str = None, solar_profile=None,
                 db_module=None):
        self.arrays = arrays or get_default_arrays()
        self.cache_dir = cache_dir or os.getenv('DATA_DIR', '/app/data')
        self.solar_profile = solar_profile

        self._api = ForecastSolarAPI(self.arrays)
        self._calibration = WeatherCalibration(
            house_kwp=self.arrays[0].kwp if self.arrays else HOUSE_ARRAY_KWP,
            db_module=db_module,
        )
        self._clear_sky = ClearSkyFallback(self.arrays[0].kwp if self.arrays else HOUSE_ARRAY_KWP)
        self._cache: Dict[str, DayForecast] = {}
        self._initialized = False

    def initialize(self):
        if self._initialized:
            return
        if self._calibration._db:
            self._calibration.load_weather()
        if self.solar_profile and hasattr(self.solar_profile, 'solar'):
            recent = getattr(self.solar_profile.solar, 'recent_daily_kwh', [])
            if recent:
                self._calibration.build_model(recent)
        self._load_cache()
        self._initialized = True

    def get_today_forecast(self, force_refresh: bool = False) -> DayForecast:
        if not self._initialized:
            self.initialize()

        today_str = date.today().isoformat()

        # Check cache
        cached = self._cache.get(today_str)
        if cached and cached.fetched_at and not force_refresh:
            try:
                age = (datetime.now() - datetime.fromisoformat(cached.fetched_at)).total_seconds() / 60
                if age < FORECAST_CACHE_MAX_AGE_MIN and cached.source.startswith('forecast_solar'):
                    return cached
            except (ValueError, TypeError):
                pass

        # Try API
        api_result = self._api.fetch()
        if api_result and today_str in api_result:
            forecast = api_result[today_str]
            self._apply_calibration(forecast)
            self._cache.update(api_result)
            self._save_cache()
            return forecast

        # Return cached even if stale
        if cached:
            return cached

        # Fallback: calibrated clear-sky
        forecast = self._clear_sky.estimate()
        self._apply_calibration(forecast)
        if forecast.source == 'clear_sky':
            forecast.source = 'clear_sky+calibrated' if forecast.weather_score >= 0 else 'clear_sky'

        # Last resort: profile
        if forecast.weather_score < 0 and self.solar_profile:
            sp = getattr(self.solar_profile, 'solar', None)
            if sp and hasattr(sp, 'forecast_remaining_kwh'):
                rem = sp.forecast_remaining_kwh()
                if rem > 0:
                    forecast.total_kwh = rem
                    forecast.source = 'profile_fallback'

        self._cache[today_str] = forecast
        return forecast

    def _apply_calibration(self, forecast: DayForecast):
        """Apply weather calibration to a forecast."""
        weather = self._calibration.get_today_weather()
        if not weather:
            return

        ws = self._calibration.weather_score(weather)
        correction = self._calibration.correction_factor(weather)
        forecast.weather_score = ws
        forecast.calibration_factor = correction

        for h in forecast.hourly:
            h.watts *= correction
            h.watt_hours *= correction
            h.source = 'calibrated'
        forecast.total_kwh = round(forecast.raw_api_kwh * correction, 2)

    def morning_plan(self, current_soc_pct: float, target_soc_pct: float,
                     battery_capacity_kwh: float, peak_start_hour: int = 17,
                     consumption_profile=None, solar_export: bool = True,
                     tou_drift_kwh: float = 0.0) -> MorningPlan:
        """THE CORE ALGORITHM — calculate how much grid charging is needed.

        1. Get solar forecast remaining until peak
        2. Estimate how much solar reaches the battery (mode-aware)
        3. Account for TOU drift (phantom grid→battery charging)
        4. Gap = target − current − net solar − drift
        5. Ceiling = current + gap (leave room for solar)

        Mode-aware solar model:
        - Export systems (solar_export=True): Solar powers home first,
          only surplus above consumption reaches the battery.
        - Non-export systems (solar_export=False): In TOU mode the grid
          powers the home and solar goes directly to the battery. Only a
          small fraction is lost to inverter overhead / momentary loads.
          This dramatically increases net_solar_to_battery vs the old
          surplus-only model that assumed Self-Consumption behavior.
        """
        now = datetime.now()
        forecast = self.get_today_forecast()

        current_kwh = battery_capacity_kwh * current_soc_pct / 100.0
        target_kwh = battery_capacity_kwh * target_soc_pct / 100.0
        forecast_remaining = forecast.remaining_kwh(until_hour=peak_start_hour)

        # Consumption estimate (used for both models, different purposes)
        if consumption_profile and hasattr(consumption_profile, 'expected_kwh'):
            peak_dt = now.replace(hour=peak_start_hour, minute=0, second=0)
            expected_consumption = consumption_profile.expected_kwh(now, peak_dt) if now < peak_dt else 0.0
        else:
            hours_to_peak = max(0, peak_start_hour - now.hour - now.minute / 60.0)
            expected_consumption = 1.2 * hours_to_peak

        current_hour = now.hour
        net_solar_to_battery = 0.0

        if not solar_export:
            # --- NON-EXPORT TOU MODEL ---
            # In TOU mode, the grid powers the home. Solar goes to the battery
            # with only minor losses. The system spends most pre-peak hours in
            # TOU, so nearly all forecast solar reaches the battery.
            #
            # We apply a 15% haircut for:
            #   - Inverter conversion losses (~3-5%)
            #   - Momentary load spikes that pull from solar before grid responds
            #   - Periods when engine switches to SC (e.g., small gap deferral)
            #   - Ramp-up/ramp-down periods at dawn/dusk with low output
            TOU_SOLAR_EFFICIENCY = 0.85

            for h in forecast.hourly:
                if h.hour < current_hour or h.hour >= peak_start_hour:
                    continue
                solar_kwh = h.watt_hours / 1000.0
                if h.hour == current_hour:
                    remaining_frac = 1.0 - (now.minute / 60.0)
                    solar_kwh *= remaining_frac
                net_solar_to_battery += solar_kwh * TOU_SOLAR_EFFICIENCY

        else:
            # --- EXPORT / SELF-CONSUMPTION MODEL (original) ---
            # Solar powers the home first, only surplus charges the battery.
            consumption_per_hour = expected_consumption / max(1, peak_start_hour - current_hour) if peak_start_hour > current_hour else 0.0
            for h in forecast.hourly:
                if h.hour < current_hour or h.hour >= peak_start_hour:
                    continue
                solar_kwh = h.watt_hours / 1000.0
                if h.hour == current_hour:
                    remaining_frac = 1.0 - (now.minute / 60.0)
                    solar_kwh *= remaining_frac
                    hour_consumption = consumption_per_hour * remaining_frac
                else:
                    hour_consumption = consumption_per_hour
                net_solar_to_battery += max(0.0, solar_kwh - hour_consumption)

        # TOU drift: phantom grid→battery charging in TOU mode.
        # The adaptive engine tracks this and passes the estimated
        # drift-kWh from now until peak. Reduces the gap further.
        drift_credit = max(0.0, tou_drift_kwh)

        # The gap (accounting for solar AND drift)
        gap = target_kwh - current_kwh - net_solar_to_battery - drift_credit

        # Set ceiling
        if gap <= 0:
            ceiling_kwh = current_kwh
            ceiling_pct = current_soc_pct
            rec = (f"Solar surplus of {abs(gap):.1f} kWh — skip grid charging. "
                   f"Forecast {forecast_remaining:.1f} kWh solar, "
                   f"{expected_consumption:.1f} kWh consumption, "
                   f"{net_solar_to_battery:.1f} kWh free to battery"
                   f"{f', drift +{drift_credit:.1f} kWh' if drift_credit > 0.1 else ''}.")
        else:
            ceiling_kwh = current_kwh + gap
            ceiling_pct = min(target_soc_pct, ceiling_kwh / battery_capacity_kwh * 100.0)
            ceiling_pct = max(ceiling_pct, current_soc_pct + 5.0)  # Never below current+5%
            ceiling_kwh = battery_capacity_kwh * ceiling_pct / 100.0
            rec = (f"Grid charge {gap:.1f} kWh to {ceiling_pct:.0f}%, "
                   f"then solar fills {net_solar_to_battery:.1f} kWh. "
                   f"Forecast {forecast_remaining:.1f} kWh solar, "
                   f"{expected_consumption:.1f} kWh consumption"
                   f"{f', drift +{drift_credit:.1f} kWh' if drift_credit > 0.1 else ''}.")

        # Low-confidence safety buffer: charge 10% extra
        if forecast.source in ('clear_sky', 'profile_fallback') and gap > 0:
            safety = battery_capacity_kwh * 0.10
            ceiling_kwh = min(target_kwh, ceiling_kwh + safety)
            ceiling_pct = ceiling_kwh / battery_capacity_kwh * 100.0
            rec += " [+10% safety: low forecast confidence]"

        return MorningPlan(
            timestamp=now.isoformat(),
            current_soc_pct=round(current_soc_pct, 1),
            target_soc_pct=round(target_soc_pct, 1),
            battery_capacity_kwh=battery_capacity_kwh,
            current_kwh=round(current_kwh, 2),
            target_kwh=round(target_kwh, 2),
            forecast_total_kwh=round(forecast.total_kwh, 2),
            forecast_remaining_kwh=round(forecast_remaining, 2),
            forecast_to_battery_kwh=round(net_solar_to_battery, 2),
            expected_consumption_kwh=round(expected_consumption, 2),
            gap_kwh=round(gap, 2),
            morning_ceiling_pct=round(ceiling_pct, 1),
            morning_ceiling_kwh=round(ceiling_kwh, 2),
            forecast_source=forecast.source,
            weather_score=round(forecast.weather_score, 3) if forecast.weather_score >= 0 else -1,
            recommendation=rec,
        )

    # --- Cache ---

    def _cache_path(self) -> str:
        return os.path.join(self.cache_dir, 'solar_forecast_cache.json')

    def _save_cache(self):
        try:
            data = {k: v.to_dict() for k, v in self._cache.items()}
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(self._cache_path(), 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Cache save: {e}")

    def _load_cache(self):
        try:
            p = self._cache_path()
            if os.path.exists(p):
                with open(p) as f:
                    for k, v in json.load(f).items():
                        self._cache[k] = DayForecast.from_dict(v)
        except Exception as e:
            logger.warning(f"Cache load: {e}")

    def get_status(self) -> dict:
        today = self._cache.get(date.today().isoformat())
        return {
            'initialized': self._initialized,
            'cached_days': len(self._cache),
            'today': today.to_dict() if today else None,
            'calibration': self._calibration._model,
            'arrays': [{'name': a.name, 'kwp': a.kwp} for a in self.arrays],
        }


# ---------------------------------------------------------------------------
# Global accessor
# ---------------------------------------------------------------------------

_engine: Optional[SolarForecastEngine] = None


def get_forecast_engine(config=None, solar_profile=None) -> SolarForecastEngine:
    """Get or create the singleton forecast engine.

    Weather data comes from SQLite tables (weather_daily, weather_observations)
    populated by collect_weather_db.py every 15 minutes.

    The db module is required — if unavailable, weather calibration is disabled
    but the engine still works via Forecast.Solar API and clear-sky fallback.
    """
    global _engine
    if _engine is not None:
        return _engine

    # Load db module for SQLite weather access
    db_module = None
    try:
        import db as db_mod
        db_mod.init_db()
        db_module = db_mod
    except ImportError:
        logger.warning("db module not available — weather calibration disabled")

    _engine = SolarForecastEngine(
        arrays=get_default_arrays(),
        cache_dir=os.getenv('DATA_DIR', '/app/data'),
        solar_profile=solar_profile,
        db_module=db_module,
    )
    _engine.initialize()
    return _engine


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s', datefmt='%H:%M:%S')

    current_soc = float(sys.argv[1]) if len(sys.argv) > 1 else 40.0

    print("=" * 60)
    print("SOLAR FORECAST ENGINE — Test Run")
    print("=" * 60)

    arrays = get_default_arrays()
    for a in arrays:
        print(f"\nArray: {a.name} ({a.kwp} kWp, tilt={a.declination}°, az={a.azimuth}°)")
        print(f"  API: {FORECAST_SOLAR_BASE_URL}/estimate/{a.latitude}/{a.longitude}/{a.api_path()}")

    db_module = None
    try:
        import db as db_mod
        db_mod.init_db()
        db_module = db_mod
    except ImportError:
        print("WARNING: db module not available — weather calibration disabled")

    engine = SolarForecastEngine(arrays=arrays, db_module=db_module)
    engine.initialize()

    # Recent weather scores
    if engine._calibration._weather:
        print(f"\nWeather scores (last 7 days):")
        for i in range(7, 0, -1):
            d = (date.today() - timedelta(days=i)).isoformat()
            w = engine._calibration._weather.get(d)
            if w:
                ws = engine._calibration.weather_score(w)
                print(f"  {d}: score={ws:.2f}  humid={w['humidity_avg']:.0f}%  "
                      f"precip={w['precip_total']:.2f}\"  "
                      f"temp={w['temp_high']:.0f}/{w['temp_low']:.0f}F  "
                      f"pres={w['pressure_max']:.2f}")

    # Today forecast
    print(f"\n--- Today's Forecast ---")
    fc = engine.get_today_forecast()
    print(f"Source: {fc.source}")
    print(f"Total: {fc.total_kwh:.1f} kWh")
    if fc.weather_score >= 0:
        print(f"Weather score: {fc.weather_score:.2f}, Calibration: {fc.calibration_factor:.3f}")
    print(f"\nHourly:")
    for h in fc.hourly:
        if h.watts > 10:
            print(f"  {h.hour:2d}:00  {h.watts:7.1f}W  ({h.watt_hours/1000:.2f} kWh)  [{h.source}]")

    # Morning plan
    print(f"\n--- Morning Plan (SOC={current_soc:.0f}%) ---")
    plan = engine.morning_plan(current_soc, 95.0, 30.0, 17)
    print(f"  Solar → peak: {plan.forecast_remaining_kwh:.1f} kWh")
    print(f"  Consumption: {plan.expected_consumption_kwh:.1f} kWh")
    print(f"  Net → battery: {plan.forecast_to_battery_kwh:.1f} kWh")
    print(f"  Gap: {plan.gap_kwh:.1f} kWh")
    print(f"  Ceiling: {plan.morning_ceiling_pct:.0f}% ({plan.morning_ceiling_kwh:.1f} kWh)")
    print(f"  >>> {plan.recommendation}")
