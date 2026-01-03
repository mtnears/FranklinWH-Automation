#!/volume1/docker/franklin/venv311/bin/python3
"""
Get Current Battery Status
Uses Franklin Cloud API with retry logic for reliability
"""
import asyncio
import time
from franklinwh import Client, TokenFetcher

# ⚠️ REPLACE WITH YOUR FRANKLIN WH CREDENTIALS
USERNAME = "YOUR_EMAIL@example.com"
PASSWORD = "YOUR_PASSWORD"
GATEWAY_ID = "YOUR_GATEWAY_ID"

async def get_stats_with_retry(max_retries=3, delay=5):
    """Get stats with retry logic for cloud API timeouts"""
    fetcher = TokenFetcher(USERNAME, PASSWORD)
    client = Client(fetcher, GATEWAY_ID)
    
    for attempt in range(max_retries):
        try:
            stats = await client.get_stats()
            return stats
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"Attempt {attempt + 1} failed: {e}")
                print(f"Retrying in {delay} seconds...")
                await asyncio.sleep(delay)
            else:
                raise

async def main():
    try:
        stats = await get_stats_with_retry(max_retries=3, delay=5)

        print("=" * 50)
        print("FRANKLIN BATTERY STATUS")
        print("=" * 50)
        print(f"Battery SOC:        {stats.current.battery_soc:.1f}%")
        print(f"Solar Production:   {stats.current.solar_production:.3f} kW")
        print(f"Grid Use:           {stats.current.grid_use:.3f} kW")
        print(f"Battery Use:        {stats.current.battery_use:.3f} kW")
        print(f"Home Load:          {stats.current.home_load:.3f} kW")
        print(f"Grid Status:        {stats.current.grid_status.name}")
        print("=" * 50)

        # Return SOC for use in automation
        return stats.current.battery_soc

    except Exception as e:
        print(f"Error: Device response timed out")
        return None

if __name__ == "__main__":
    soc = asyncio.run(main())
