#!/volume1/docker/franklin/venv311/bin/python3
"""
Switch to Emergency Backup Mode
This starts grid charging the battery
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
