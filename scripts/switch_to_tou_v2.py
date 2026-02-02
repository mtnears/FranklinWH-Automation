#!/usr/bin/env python3
"""
Switch to Time-of-Use Mode
This stops grid charging and returns to TOU operation

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

        print("Switching to TOU mode...")
        await client.set_mode(Mode.time_of_use())

        print("✓ Successfully switched to TOU mode")
        print("✓ Battery charging stopped, using TOU schedule")
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
