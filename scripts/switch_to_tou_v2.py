#!/volume1/docker/franklin/venv311/bin/python3
"""
Switch to Time-of-Use Mode
This stops grid charging and returns to TOU operation
"""
import asyncio
from franklinwh import Client, TokenFetcher, Mode

# ⚠️ REPLACE WITH YOUR FRANKLIN WH CREDENTIALS
USERNAME = "YOUR_EMAIL@example.com"
PASSWORD = "YOUR_PASSWORD"
GATEWAY_ID = "YOUR_GATEWAY_ID"

async def main():
    try:
        print("Authenticating with Franklin WH...")
        fetcher = TokenFetcher(USERNAME, PASSWORD)

        print("Creating client...")
        client = Client(fetcher, GATEWAY_ID)

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
