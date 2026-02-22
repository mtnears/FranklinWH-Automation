#!/usr/bin/env python3
"""
Anonymous Telemetry Module for FranklinWH Battery Automation - v3.5.0

Provides opt-in anonymous usage statistics to help improve the project.
Follows privacy-first principles with full transparency.

What is collected:
- System configuration (battery capacity, features enabled)
- Performance metrics (polling frequency, data source type)
- Uptime and stability information
- Geographic region (state/country level only)

What is NOT collected:
- Personal information (names, emails, addresses)
- Credentials or authentication data
- Energy usage data or SOC levels
- IP addresses (beyond basic country detection)
- Any sensitive system information

Implementation:
- Completely opt-in via TELEMETRY_ENABLED=true
- Data sent via simple HTTPS POST to community endpoint
- Includes UUID for deduplication but no personal identification
- Can be disabled at any time by setting TELEMETRY_ENABLED=false

Benefits to Community:
- Understand most common configurations and use cases
- Identify performance bottlenecks and optimization opportunities
- Track adoption of new features (Modbus, dynamic pricing)
- Plan development priorities based on real usage patterns
"""

import json
import logging
import hashlib
import platform
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from pathlib import Path

import aiohttp
import asyncio

from config import config, configure_logging

logger = logging.getLogger(__name__)


class TelemetryCollector:
    """Collects and transmits anonymous usage statistics."""
    
    def __init__(self):
        self.enabled = config.TELEMETRY_ENABLED
        self.endpoint = config.TELEMETRY_ENDPOINT
        self.interval_hours = config.TELEMETRY_INTERVAL_HOURS
        self.telemetry_file = config.DATA_DIR / "telemetry_state.json"
        self.uuid = self._get_or_create_uuid()
        
    def _get_or_create_uuid(self) -> str:
        """Get existing UUID or create new one for deduplication."""
        try:
            if self.telemetry_file.exists():
                with open(self.telemetry_file, 'r') as f:
                    data = json.load(f)
                    if 'uuid' in data:
                        return data['uuid']
        except Exception:
            pass
        
        # Create new UUID
        new_uuid = str(uuid.uuid4())
        self._save_state({'uuid': new_uuid, 'created_at': datetime.now().isoformat()})
        return new_uuid
    
    def _save_state(self, data: Dict[str, Any]):
        """Save telemetry state to file."""
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(self.telemetry_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.debug(f"Could not save telemetry state: {e}")
    
    def _should_send_telemetry(self) -> bool:
        """Check if it's time to send telemetry data."""
        if not self.enabled:
            return False
            
        try:
            if self.telemetry_file.exists():
                with open(self.telemetry_file, 'r') as f:
                    data = json.load(f)
                    last_sent = data.get('last_sent')
                    if last_sent:
                        last_sent_dt = datetime.fromisoformat(last_sent)
                        if datetime.now() - last_sent_dt < timedelta(hours=self.interval_hours):
                            return False
        except Exception:
            pass
        
        return True
    
    def _get_system_hash(self) -> str:
        """Create anonymous system identifier."""
        # Create hash from system info (not personally identifiable)
        system_info = f"{platform.system()}{platform.release()}{config.BATTERY_CAPACITY_KWH}"
        return hashlib.sha256(system_info.encode()).hexdigest()[:16]
    
    def _collect_telemetry_data(self) -> Dict[str, Any]:
        """Collect anonymous usage statistics."""
        
        # Read uptime from logs if available
        uptime_hours = self._estimate_uptime()
        
        # Count recent data source performance
        connection_stats = self._get_connection_stats()
        
        # Determine geographic region (state/country level only)
        region = self._get_anonymous_region()
        
        payload = {
            # Metadata
            'timestamp': datetime.now().isoformat(),
            'version': '3.5.0',
            'uuid': self.uuid,
            'system_hash': self._get_system_hash(),
            'region': region,
            
            # System Configuration (anonymous)
            'config': {
                'battery_capacity_kwh': config.BATTERY_CAPACITY_KWH,
                'charge_rate_per_hour': config.CHARGE_RATE_PER_HOUR,
                'target_soc': config.TARGET_SOC,
                'charging_strategy': config.CHARGING_STRATEGY,
                'home_mode': config.HOME_MODE,
                'check_interval_minutes': config.CHECK_INTERVAL_MINUTES,
            },
            
            # Feature Usage
            'features': {
                'solar_enabled': config.SOLAR_ENABLED,
                'tou_enabled': config.TOU_ENABLED,
                'dynamic_pricing_enabled': config.DYNAMIC_PRICING_ENABLED,
                'modbus_enabled': config.MODBUS_ENABLED,
                'weather_enabled': config.WEATHER_ENABLED,
                'pvoutput_enabled': config.PVOUTPUT_ENABLED,
                'telemetry_enabled': config.TELEMETRY_ENABLED,
                'multi_array_solar': bool(config.SOLAR_ARRAYS),
            },
            
            # TOU Configuration (anonymous)
            'tou_config': {
                'peak_start_hour': config.PEAK_START_HOUR if config.TOU_ENABLED else None,
                'peak_end_hour': config.PEAK_END_HOUR if config.TOU_ENABLED else None,
                'midnight_crossing': (config.PEAK_START_HOUR > config.PEAK_END_HOUR) if config.TOU_ENABLED else False,
                'peak_days': config.PEAK_DAYS if config.TOU_ENABLED else None,
                'has_secondary_peak': bool(config.PEAK2_START_HOUR and config.PEAK2_END_HOUR),
            } if config.TOU_ENABLED else None,
            
            # Performance Metrics
            'performance': {
                'uptime_hours': uptime_hours,
                'preferred_data_source': 'modbus' if config.MODBUS_ENABLED else 'cloud_api',
                'connection_stats': connection_stats,
            },
            
            # Deployment Environment (anonymous)
            'environment': {
                'platform': platform.system(),
                'python_version': platform.python_version(),
                'deployment_type': 'docker',  # Assumption for this version
            }
        }
        
        return payload
    
    def _estimate_uptime(self) -> float:
        """Estimate system uptime from log files."""
        try:
            log_file = config.LOG_DIR / "continuous_monitoring.csv"
            if log_file.exists():
                # Check time span of recent log entries
                with open(log_file, 'r') as f:
                    lines = f.readlines()
                    if len(lines) > 1:
                        # Get last 100 lines to estimate recent uptime
                        recent_lines = lines[-100:]
                        if len(recent_lines) >= 2:
                            first_ts = recent_lines[0].split(',')[0]
                            last_ts = recent_lines[-1].split(',')[0] 
                            try:
                                start = datetime.strptime(first_ts, '%Y-%m-%d %H:%M:%S')
                                end = datetime.strptime(last_ts, '%Y-%m-%d %H:%M:%S')
                                return (end - start).total_seconds() / 3600
                            except ValueError:
                                pass
        except Exception:
            pass
        
        return 0.0
    
    def _get_connection_stats(self) -> Dict[str, Any]:
        """Get anonymous connection performance statistics."""
        try:
            stats_file = config.LOG_DIR / "data_source_health.json"
            if stats_file.exists():
                with open(stats_file, 'r') as f:
                    data = json.load(f)
                    
                    # Extract anonymous performance metrics
                    stats = {}
                    for source_name, source_data in data.get('sources', {}).items():
                        if source_data.get('enabled'):
                            stats[source_name] = {
                                'success_rate': round(source_data.get('success_rate', 0), 1),
                                'avg_response_time_ms': round(source_data.get('avg_response_time_ms', 0)),
                                'total_attempts': min(source_data.get('total_attempts', 0), 10000),  # Cap for privacy
                            }
                    
                    return stats
        except Exception:
            pass
        
        return {}
    
    def _get_anonymous_region(self) -> str:
        """Get anonymous geographic region (state/country only)."""
        # For privacy, only return very general location
        # This could be enhanced to use timezone or other anonymous indicators
        timezone = config.TZ
        
        if 'America/Los_Angeles' in timezone or 'America/Denver' in timezone:
            return 'US-West'
        elif 'America/Chicago' in timezone:
            return 'US-Central' 
        elif 'America/New_York' in timezone:
            return 'US-East'
        elif 'America/' in timezone:
            return 'Americas'
        elif 'Europe/' in timezone:
            return 'Europe'
        elif 'Asia/' in timezone:
            return 'Asia'
        else:
            return 'Unknown'
    
    async def send_telemetry(self) -> bool:
        """Send telemetry data if enabled and due."""
        if not self._should_send_telemetry():
            return False
        
        try:
            payload = self._collect_telemetry_data()
            
            logger.info("Sending anonymous telemetry data (opt-in)")
            logger.debug(f"Telemetry payload: {json.dumps(payload, indent=2)}")
            
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.endpoint,
                    json=payload,
                    headers={'Content-Type': 'application/json'}
                ) as response:
                    if response.status == 200:
                        logger.info("Telemetry data sent successfully")
                        
                        # Update last sent timestamp
                        state_data = {'uuid': self.uuid, 'last_sent': datetime.now().isoformat()}
                        self._save_state(state_data)
                        
                        return True
                    else:
                        logger.warning(f"Telemetry send failed: HTTP {response.status}")
                        return False
                        
        except Exception as e:
            logger.debug(f"Telemetry send error: {e}")
            return False
    
    def log_telemetry_sample(self):
        """Log what would be sent for transparency (debugging)."""
        if not self.enabled:
            return
            
        try:
            payload = self._collect_telemetry_data()
            logger.info("Sample telemetry payload (for transparency):")
            logger.info(json.dumps(payload, indent=2))
        except Exception as e:
            logger.debug(f"Could not generate telemetry sample: {e}")


# Global telemetry collector
telemetry = TelemetryCollector()


async def send_telemetry_if_due() -> bool:
    """Convenience function to send telemetry if due."""
    return await telemetry.send_telemetry()


def log_telemetry_transparency():
    """Log transparency information about telemetry."""
    if config.TELEMETRY_ENABLED:
        logger.info("Anonymous telemetry enabled - helping improve FranklinWH automation")
        logger.info("Data sent: system config, performance metrics, feature usage (NO personal data)")
        logger.info("Disable anytime: set TELEMETRY_ENABLED=false in .env")
    telemetry.log_telemetry_sample()


if __name__ == "__main__":
    """Test telemetry collection."""
    configure_logging()
    async def test():
        print("FranklinWH Battery Automation - Telemetry Test")
        print(f"Telemetry enabled: {config.TELEMETRY_ENABLED}")
        
        if config.TELEMETRY_ENABLED:
            print("\nGenerating sample telemetry payload:")
            payload = telemetry._collect_telemetry_data()
            print(json.dumps(payload, indent=2))
            
            print(f"\nWould send to: {config.TELEMETRY_ENDPOINT}")
            print(f"Next send due: {telemetry._should_send_telemetry()}")
            
            # Test send (if due)
            if telemetry._should_send_telemetry():
                print("\nSending telemetry data...")
                success = await telemetry.send_telemetry()
                print(f"Send result: {'Success' if success else 'Failed'}")
        else:
            print("Telemetry disabled - no data collection")
    
    asyncio.run(test())
