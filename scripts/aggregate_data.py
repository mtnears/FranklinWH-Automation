#!/usr/bin/env python3
"""
Energy Data Aggregator
Simple utility to verify data collection is working across all sources

Runs daily at 6:00 AM to provide a quick inventory of collected data.
Useful for monitoring that all data collection tasks are functioning properly.
"""
import sys
from pathlib import Path

# Data source paths
LOG_DIR = Path("/volume1/docker/franklin/logs")
FRANKLIN_LOG = LOG_DIR / "continuous_monitoring.csv"
GROUND_SOLAR_LOG = LOG_DIR / "pvoutput_ground_mount.csv"
HOUSE_SOLAR_LOG = LOG_DIR / "pvoutput_house.csv"
WEATHER_LOG = LOG_DIR / "weather_data.csv"

def aggregate_data():
    """Simple aggregation - just report what we have"""
    print("\n" + "="*60)
    print("ENERGY DATA SUMMARY")
    print("="*60 + "\n")

    # Count records in each file
    files = {
        "Franklin Battery": FRANKLIN_LOG,
        "Ground Mount Solar": GROUND_SOLAR_LOG,
        "House Solar": HOUSE_SOLAR_LOG,
        "Weather": WEATHER_LOG
    }

    for name, filepath in files.items():
        if filepath.exists():
            with open(filepath, 'r') as f:
                lines = sum(1 for line in f) - 1  # Subtract header
                print(f"✓ {name}: {lines} records")
        else:
            print(f"⚠ {name}: File not found")

    print("\n" + "="*60 + "\n")
    print("✓ Data summary complete")
    return True

if __name__ == "__main__":
    try:
        result = aggregate_data()
        sys.exit(0 if result else 1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
