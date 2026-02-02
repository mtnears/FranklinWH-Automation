#!/usr/bin/env python3
"""
Switch to Emergency Backup Mode
This starts grid charging the battery

Configuration loaded from .env file via config module.
"""
import asyncio
from franklinwh import Client, TokenFetcher, Mode

# Import configuration
from config import config


async def main():
    try:
        print("Authenticating with Franklin WH...")
        fetcher = TokenFetcher(config.FRANKLIN_USERNAME, config.FRANKLIN_PASSWORD)

        print("Creating client...")
        client = Client(fetcher, config.FRANKLIN_GATEWAY_ID)

        print("Switching to Emergency Backup mode...")
        await client.set_mode(Mode.emergency_backup())

        print("✓ Successfully switched to Emergency Backup mode")
        print("✓ Battery is now charging from grid")
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
