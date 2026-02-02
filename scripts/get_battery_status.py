#!/usr/bin/env python3
"""
Get Current Battery Status
Uses Franklin Cloud API with retry logic for reliability

Configuration loaded from .env file via config module.
"""
import asyncio
from franklinwh import Client, TokenFetcher

# Import configuration
from config import config


async def get_stats_with_retry(max_retries=3, delay=5):
    """Get stats with retry logic for cloud API timeouts"""
    fetcher = TokenFetcher(config.FRANKLIN_USERNAME, config.FRANKLIN_PASSWORD)
    client = Client(fetcher, config.FRANKLIN_GATEWAY_ID)
    
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
        print(f"Error: {e}")
        return None


if __name__ == "__main__":
    soc = asyncio.run(main())
