#!/volume1/docker/franklin/venv311/bin/python3
"""
Get Current Battery Status
Quick utility to check battery SOC and power flows
"""
import asyncio
from franklinwh import Client, TokenFetcher

# ⚠️ REPLACE WITH YOUR FRANKLIN WH CREDENTIALS
USERNAME = "YOUR_EMAIL@example.com"
PASSWORD = "YOUR_PASSWORD"
GATEWAY_ID = "YOUR_GATEWAY_ID"

async def main():
    try:
        fetcher = TokenFetcher(USERNAME, PASSWORD)
        client = Client(fetcher, GATEWAY_ID)

        stats = await client.get_stats()

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
