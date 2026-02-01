#!/usr/bin/env python3
"""
Weather Underground Data Collector - v3.0

Pulls weather data from configured PWS and stores to CSV.
Configuration loaded from environment variables.

Only runs if WEATHER_ENABLED=true in configuration.
"""
import requests
import csv
from datetime import datetime
import sys

# Import configuration
from config import config


def get_current_conditions():
    """Get current weather conditions from Weather Underground."""
    if not config.WEATHER_ENABLED:
        print("Weather collection is disabled (WEATHER_ENABLED=false)")
        return None
    
    if not config.WEATHER_STATION_ID or not config.WEATHER_API_KEY:
        print("Weather station ID or API key not configured")
        return None
    
    url = "https://api.weather.com/v2/pws/observations/current"
    params = {
        "stationId": config.WEATHER_STATION_ID,
        "format": "json",
        "units": "e",  # English units
        "apiKey": config.WEATHER_API_KEY
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if "observations" in data and len(data["observations"]) > 0:
            obs = data["observations"][0]
            imperial = obs.get("imperial", {})
            
            weather_data = {
                'timestamp': datetime.now().isoformat(),
                'obs_time_local': obs.get('obsTimeLocal', ''),
                'station_id': obs.get('stationID', config.WEATHER_STATION_ID),
                'neighborhood': obs.get('neighborhood', ''),
                'temp_f': imperial.get('temp'),
                'heat_index_f': imperial.get('heatIndex'),
                'dewpoint_f': imperial.get('dewpt'),
                'wind_chill_f': imperial.get('windChill'),
                'humidity': obs.get('humidity'),
                'pressure_inhg': imperial.get('pressure'),
                'wind_speed_mph': imperial.get('windSpeed'),
                'wind_gust_mph': imperial.get('windGust'),
                'wind_dir_degrees': obs.get('winddir'),
                'precip_rate_in_hr': imperial.get('precipRate'),
                'precip_total_in': imperial.get('precipTotal'),
                'solar_radiation_wm2': obs.get('solarRadiation'),
                'uv_index': obs.get('uv'),
            }
            return weather_data
        else:
            print("No observation data available")
            return None
            
    except requests.exceptions.RequestException as e:
        print(f"Error fetching weather data: {e}")
        return None
    except Exception as e:
        print(f"Error parsing weather data: {e}")
        return None


def save_to_csv(weather_data):
    """Save weather data to CSV file."""
    if not weather_data:
        return
    
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    weather_log = config.WEATHER_LOG
    file_exists = weather_log.exists()
    
    fieldnames = [
        'timestamp', 'obs_time_local', 'station_id', 'neighborhood',
        'temp_f', 'heat_index_f', 'dewpoint_f', 'wind_chill_f',
        'humidity', 'pressure_inhg',
        'wind_speed_mph', 'wind_gust_mph', 'wind_dir_degrees',
        'precip_rate_in_hr', 'precip_total_in',
        'solar_radiation_wm2', 'uv_index'
    ]
    
    try:
        with open(weather_log, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(weather_data)
        print(f"Weather data saved: {weather_data['temp_f']}F, {weather_data['solar_radiation_wm2']} W/m2 solar")
    except Exception as e:
        print(f"Error saving weather data: {e}")


def get_solar_forecast() -> float:
    """
    Estimate tomorrow's solar production based on weather forecast.
    
    Returns estimated kWh of solar production.
    This is a simplified model - can be enhanced with historical data.
    """
    if not config.WEATHER_ENABLED or not config.SOLAR_ENABLED:
        return 0.0
    
    # For now, use current solar radiation as a proxy
    # A more sophisticated version would fetch forecast data
    weather = get_current_conditions()
    if not weather:
        return 0.0
    
    solar_radiation = weather.get('solar_radiation_wm2', 0) or 0
    
    # Very rough estimate:
    # Peak solar radiation ~1000 W/m2
    # Assume 6 good hours of production
    # Scale by system size
    
    if solar_radiation > 800:
        production_factor = 0.9  # Clear day
    elif solar_radiation > 500:
        production_factor = 0.6  # Partly cloudy
    elif solar_radiation > 200:
        production_factor = 0.3  # Mostly cloudy
    else:
        production_factor = 0.1  # Overcast
    
    # Estimate: system_kw * 5 hours * production_factor
    estimated_kwh = config.SOLAR_CAPACITY_KW * 5 * production_factor
    
    return estimated_kwh


def collect_weather():
    """Main collection function."""
    weather_data = get_current_conditions()
    if weather_data:
        save_to_csv(weather_data)
        return True
    return False


if __name__ == "__main__":
    if not config.WEATHER_ENABLED:
        print("Weather collection is disabled in configuration.")
        print("Set WEATHER_ENABLED=true in .env to enable.")
        sys.exit(0)
    
    success = collect_weather()
    sys.exit(0 if success else 1)
