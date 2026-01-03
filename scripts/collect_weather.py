#!/volume1/docker/franklin/venv311/bin/python3
"""
Weather Underground Data Collector
Pulls weather data from your PWS and stores to CSV
"""
import requests
import csv
from datetime import datetime
from pathlib import Path

# ⚠️ REPLACE WITH YOUR WEATHER UNDERGROUND CREDENTIALS
PWS_ID = "YOUR_WEATHER_STATION_ID"
API_KEY = "YOUR_WEATHER_UNDERGROUND_API_KEY"

# File path
LOG_DIR = Path("/volume1/docker/franklin/logs")
WEATHER_LOG = LOG_DIR / "weather_data.csv"

def get_current_conditions():
    """Get current weather conditions from Weather Underground"""
    url = "https://api.weather.com/v2/pws/observations/current"

    params = {
        "stationId": PWS_ID,
        "format": "json",
        "units": "e",  # English units
        "apiKey": API_KEY
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
                'station_id': obs.get('stationID', PWS_ID),
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
            print(f"No observation data available")
            return None

    except requests.exceptions.RequestException as e:
        print(f"Error fetching weather data: {e}")
        return None
    except Exception as e:
        print(f"Error parsing weather data: {e}")
        return None

def save_to_csv(weather_data):
    """Save weather data to CSV file"""
    if not weather_data:
        return

    WEATHER_LOG.parent.mkdir(parents=True, exist_ok=True)
    file_exists = WEATHER_LOG.exists()

    fieldnames = [
        'timestamp', 'obs_time_local', 'station_id', 'neighborhood',
        'temp_f', 'heat_index_f', 'dewpoint_f', 'wind_chill_f',
        'humidity', 'pressure_inhg',
        'wind_speed_mph', 'wind_gust_mph', 'wind_dir_degrees',
        'precip_rate_in_hr', 'precip_total_in',
        'solar_radiation_wm2', 'uv_index'
    ]

    try:
        with open(WEATHER_LOG, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow(weather_data)

        print(f"✓ Weather data saved: {weather_data['temp_f']}°F, {weather_data['solar_radiation_wm2']} W/m² solar")

    except Exception as e:
        print(f"Error saving weather data: {e}")

def collect_weather():
    """Main collection function"""
    weather_data = get_current_conditions()
    if weather_data:
        save_to_csv(weather_data)
        return True
    return False

if __name__ == "__main__":
    import sys
    success = collect_weather()
    sys.exit(0 if success else 1)
