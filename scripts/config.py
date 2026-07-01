#!/usr/bin/env python3
"""
Configuration Management for FranklinWH Battery Automation

Loads settings from environment variables (.env file) with sensible defaults.
Provides a centralized configuration object used by all scripts.
Version is read from the VERSION file in the repo root.

Usage:
    from config import config
    
    if config.TOU_ENABLED:
        # Do TOU-specific logic
    
    print(f"Peak hours: {config.PEAK_START_HOUR}-{config.PEAK_END_HOUR}")
"""
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List
import json

# Try to load dotenv if available
try:
    from dotenv import load_dotenv
    # Load .env from script directory or parent
    env_paths = [
        Path(__file__).parent.parent / '.env',
        Path(__file__).parent / '.env',
        Path.cwd() / '.env'
    ]
    for env_path in env_paths:
        if env_path.exists():
            load_dotenv(env_path)
            break
except ImportError:
    pass  # dotenv not installed, rely on environment variables


def get_version() -> str:
    """Read version from VERSION file in repo root."""
    for candidate in [
        Path(__file__).parent.parent / 'VERSION',
        Path(__file__).parent / 'VERSION',
    ]:
        try:
            if candidate.exists():
                return candidate.read_text().strip()
        except Exception:
            pass
    return '0.0.0'


# Module-level version constant
VERSION = get_version()


def get_bool(key: str, default: bool = False) -> bool:
    """Parse boolean from environment variable."""
    value = os.getenv(key, str(default)).lower()
    return value in ('true', '1', 'yes', 'on')


def get_int(key: str, default: int) -> int:
    """Parse integer from environment variable."""
    try:
        return int(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def get_float(key: str, default: float) -> float:
    """Parse float from environment variable."""
    try:
        return float(os.getenv(key, str(default)))
    except (ValueError, TypeError):
        return default


def get_list(key: str, default: str = '') -> List[str]:
    """Parse comma-separated list from environment variable."""
    value = os.getenv(key, default)
    if not value:
        return []
    return [item.strip() for item in value.split(',') if item.strip()]


def get_optional_float(key: str) -> Optional[float]:
    """Parse optional float from environment variable.
    
    Returns None if the variable is not set or empty.
    This distinguishes between "disabled" (not set) and "set to 0"
    which is important for thresholds that accept negative values.
    """
    value = os.getenv(key)
    if value is None or value.strip() == '':
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def validate_peak_hours(start_hour: int, end_hour: int) -> bool:
    """Validate peak hour configuration, handling midnight-crossing periods."""
    # Both must be valid hours 0-23
    if not (0 <= start_hour <= 23) or not (0 <= end_hour <= 23):
        return False
    
    # Allow midnight-crossing periods (e.g., 22-6 or 17-0)
    # The only invalid case is start == end (zero duration)
    if start_hour == end_hour:
        return False
    
    return True


def is_peak_period(current_hour: int, start_hour: int, end_hour: int) -> bool:
    """Check if current hour is in peak period, handling midnight-crossing."""
    if start_hour < end_hour:
        # Normal period (e.g., 17-20): 17 <= hour < 20
        return start_hour <= current_hour < end_hour
    elif start_hour > end_hour:
        # Midnight-crossing period (e.g., 22-6): hour >= 22 OR hour < 6
        return current_hour >= start_hour or current_hour < end_hour
    else:
        # start_hour == end_hour: invalid period
        return False


@dataclass
class Config:
    """
    Configuration container for FranklinWH Battery Automation.
    
    All settings are loaded from environment variables with sensible defaults.
    """
    
    # ===== Required: Franklin WH Credentials =====
    FRANKLIN_USERNAME: str = field(default_factory=lambda: os.getenv('FRANKLIN_USERNAME', ''))
    FRANKLIN_PASSWORD: str = field(default_factory=lambda: os.getenv('FRANKLIN_PASSWORD', ''))
    FRANKLIN_GATEWAY_ID: str = field(default_factory=lambda: os.getenv('FRANKLIN_GATEWAY_ID', ''))
    
    # ===== Required: Battery Configuration =====
    BATTERY_CAPACITY_KWH: float = field(default_factory=lambda: get_float('BATTERY_CAPACITY_KWH', 30.0))
    CHARGE_RATE_PER_HOUR: float = field(default_factory=lambda: get_float('CHARGE_RATE_PER_HOUR', 32.0))
    
    # ===== Feature Toggles =====
    SOLAR_ENABLED: bool = field(default_factory=lambda: get_bool('SOLAR_ENABLED', True))
    TOU_ENABLED: bool = field(default_factory=lambda: get_bool('TOU_ENABLED', True))
    DYNAMIC_PRICING_ENABLED: bool = field(default_factory=lambda: get_bool('DYNAMIC_PRICING_ENABLED', False))
    WEATHER_ENABLED: bool = field(default_factory=lambda: get_bool('WEATHER_ENABLED', False))
    PVOUTPUT_ENABLED: bool = field(default_factory=lambda: get_bool('PVOUTPUT_ENABLED', False))
    ENPHASE_ENABLED: bool = field(default_factory=lambda: get_bool('ENPHASE_ENABLED', False))
    SOLAR_ARRAYS: str = field(default_factory=lambda: os.getenv('SOLAR_ARRAYS', ''))

    # ===== NEW: Modbus Integration =====
    MODBUS_ENABLED: bool = field(default_factory=lambda: get_bool('MODBUS_ENABLED', False))
    MODBUS_HOST: str = field(default_factory=lambda: os.getenv('MODBUS_HOST', '192.168.5.149'))
    MODBUS_PORT: int = field(default_factory=lambda: get_int('MODBUS_PORT', 502))
    MODBUS_TIMEOUT: float = field(default_factory=lambda: get_float('MODBUS_TIMEOUT', 5.0))
    MODBUS_RETRY_ATTEMPTS: int = field(default_factory=lambda: get_int('MODBUS_RETRY_ATTEMPTS', 3))
    
    # ===== NEW: Telemetry Options =====
    TELEMETRY_ENABLED: bool = field(default_factory=lambda: get_bool('TELEMETRY_ENABLED', False))
    TELEMETRY_ENDPOINT: str = field(default_factory=lambda: os.getenv('TELEMETRY_ENDPOINT', 'https://telemetry.example.com/franklin-automation'))
    TELEMETRY_INTERVAL_HOURS: int = field(default_factory=lambda: get_int('TELEMETRY_INTERVAL_HOURS', 24))
    ENGINE_VERSION: str = field(default_factory=lambda: os.getenv('ENGINE_VERSION', VERSION))
    MULTI_METER: bool = field(default_factory=lambda: get_bool('MULTI_METER', False))
    FORECAST_ENABLED: bool = field(default_factory=lambda: get_bool('FORECAST_ENABLED', False))

    # ===== NEW: V4.0 Adaptive Decision Engine =====
    # When enabled, smart_decision.py delegates to adaptive_engine.py
    # which uses forecast-aware charging, learned system profiles, and
    # full rate schedule awareness. Falls back to v3.5 logic on error.
    ADAPTIVE_ENGINE_ENABLED: bool = field(default_factory=lambda: get_bool('ADAPTIVE_ENGINE_ENABLED', False))
    DECISION_INTERVAL_MINUTES: int = field(default_factory=lambda: get_int('DECISION_INTERVAL_MINUTES', 15))
    BATTERY_COUNT: int = field(default_factory=lambda: get_int('BATTERY_COUNT', 2))
    BACKUP_RESERVE_PCT: float = field(default_factory=lambda: get_float('BACKUP_RESERVE_PCT', 20.0))

    # Solar export: does the system export surplus solar to the grid?
    # false = non-export (surplus is curtailed when battery full — engine drains to create headroom)
    # true  = net-metered export (surplus earns credits — headroom management skipped)
    SOLAR_EXPORT: bool = field(default_factory=lambda: get_bool('SOLAR_EXPORT', False))

    # ===== NEW: SolarEdge Panel-Level Monitoring =====
    # Optional: Scrapes SolarEdge portal for real per-optimizer energy data
    # Requires portal login credentials (username/password, not API key)
    # Provides per-panel health monitoring, degradation tracking, anomaly detection
    SOLAREDGE_PANEL_MONITORING: bool = field(default_factory=lambda: get_bool('SOLAREDGE_PANEL_MONITORING', False))
    # Local SunSpec Modbus TCP collection of the barn inverters. When true, the
    # Modbus collector owns solar_barn.json (live production) and the portal panel
    # collector writes only the health overlay. Consumed by scheduler setup.
    SOLAREDGE_MODBUS_ENABLED: bool = field(default_factory=lambda: get_bool('SOLAREDGE_MODBUS_ENABLED', False))
    SOLAREDGE_SITE_ID: str = field(default_factory=lambda: os.getenv('SOLAREDGE_SITE_ID',
                                   os.getenv('SOLAR_ARRAY_BARN_SITE_ID', '')))
    SOLAREDGE_USERNAME: str = field(default_factory=lambda: os.getenv('SOLAREDGE_USERNAME', ''))
    SOLAREDGE_PASSWORD: str = field(default_factory=lambda: os.getenv('SOLAREDGE_PASSWORD', ''))

    # ===== TOU Settings =====
    PEAK_START_HOUR: int = field(default_factory=lambda: get_int('PEAK_START_HOUR', 17))
    PEAK_END_HOUR: int = field(default_factory=lambda: get_int('PEAK_END_HOUR', 20))
    PEAK_DAYS: str = field(default_factory=lambda: os.getenv('PEAK_DAYS', 'weekdays'))
    
    # Optional second peak period
    PEAK2_START_HOUR: Optional[int] = field(default_factory=lambda: get_int('PEAK2_START_HOUR', 0) or None)
    PEAK2_END_HOUR: Optional[int] = field(default_factory=lambda: get_int('PEAK2_END_HOUR', 0) or None)
    PEAK2_DAYS: str = field(default_factory=lambda: os.getenv('PEAK2_DAYS', 'weekdays'))
    
    # ===== Scheduling Settings =====
    # Dynamic polling frequency based on data source and features
    CHECK_INTERVAL_MINUTES: int = 30  # Will be calculated in __post_init__
    PEAK_TRANSITION_BUFFER_MINUTES: int = field(default_factory=lambda: get_int('PEAK_TRANSITION_BUFFER_MINUTES', 10))
    HOME_MODE: str = field(default_factory=lambda: os.getenv('HOME_MODE', 'tou'))
    
    # ===== Dynamic Pricing Settings =====
    PRICING_PROVIDER: str = field(default_factory=lambda: os.getenv('PRICING_PROVIDER', 'comed'))
    PRICE_THRESHOLD_CENTS: float = field(default_factory=lambda: get_float('PRICE_THRESHOLD_CENTS', 4.0))
    PRICE_CEILING_CENTS: float = field(default_factory=lambda: get_float('PRICE_CEILING_CENTS', 10.0))
    
    # Solar override: when grid price is at or below this, charge from grid
    # even when solar is producing. Captures negative pricing credits.
    # None = disabled (default), 0 = grab free/negative, -2 = only below -2c
    SOLAR_OVERRIDE_PRICE_CENTS: Optional[float] = field(
        default_factory=lambda: get_optional_float('SOLAR_OVERRIDE_PRICE_CENTS'))
    
    # ===== Solar Settings =====
    SOLAR_CAPACITY_KW: float = field(default_factory=lambda: get_float('SOLAR_CAPACITY_KW', 0.0))
    MIN_SOLAR_FOR_WAIT: float = field(default_factory=lambda: get_float('MIN_SOLAR_FOR_WAIT', 0.5))
    
    # ===== Weather Settings =====
    WEATHER_PROVIDER: str = field(default_factory=lambda: os.getenv('WEATHER_PROVIDER', 'wunderground'))
    WEATHER_STATION_ID: str = field(default_factory=lambda: os.getenv('WEATHER_STATION_ID', ''))
    WEATHER_API_KEY: str = field(default_factory=lambda: os.getenv('WEATHER_API_KEY', ''))
    CLOUDY_THRESHOLD_PERCENT: int = field(default_factory=lambda: get_int('CLOUDY_THRESHOLD_PERCENT', 50))
    
    # ===== Solar Forecast Settings (v4.0 forecast-aware charging) =====
    # Array parameters for Forecast.Solar API — house array only (battery-connected)
    # Defaults are Ken's Georgetown setup; override in .env for other installations
    FORECAST_LATITUDE: float = field(default_factory=lambda: get_float('FORECAST_LATITUDE', 38.91))
    FORECAST_LONGITUDE: float = field(default_factory=lambda: get_float('FORECAST_LONGITUDE', -120.84))
    FORECAST_HOUSE_TILT: int = field(default_factory=lambda: get_int('FORECAST_HOUSE_TILT', 22))
    FORECAST_HOUSE_AZIMUTH: int = field(default_factory=lambda: get_int('FORECAST_HOUSE_AZIMUTH', -65))
    FORECAST_HOUSE_KWP: float = field(default_factory=lambda: get_float('FORECAST_HOUSE_KWP', 6.96))
    FORECAST_SOLAR_API_KEY: str = field(default_factory=lambda: os.getenv('FORECAST_SOLAR_API_KEY', ''))
    
    # ===== PVOutput Settings =====
    PVOUTPUT_API_KEY: str = field(default_factory=lambda: os.getenv('PVOUTPUT_API_KEY', ''))
    PVOUTPUT_SYSTEM_IDS: List[str] = field(default_factory=lambda: get_list('PVOUTPUT_SYSTEM_IDS'))
    
    # ===== Decision Tuning =====
    TARGET_SOC: float = field(default_factory=lambda: get_float('TARGET_SOC', 95.0))
    SAFETY_MARGIN_HOURS: float = field(default_factory=lambda: get_float('SAFETY_MARGIN_HOURS', 0.75))
    CHARGING_STRATEGY: str = field(default_factory=lambda: os.getenv('CHARGING_STRATEGY', 'balanced'))
    
    # Reserve SOC for mode switching (passed to franklinwh library)
    # BACKUP reserve: SOC target when in Emergency Backup / grid-charging mode
    # HOME reserve: minimum SOC to maintain in normal TOU/Self-Consumption mode
    RESERVE_SOC_BACKUP: int = field(default_factory=lambda: get_int('RESERVE_SOC_BACKUP', 100))
    RESERVE_SOC_HOME: int = field(default_factory=lambda: get_int('RESERVE_SOC_HOME', 20))
    
    # ===== System Paths =====
    BASE_DIR: Path = field(default_factory=lambda: Path(os.getenv('BASE_DIR', '/app')))
    LOG_DIR: Path = field(default_factory=lambda: Path(os.getenv('LOG_DIR', '/app/logs')))
    DATA_DIR: Path = field(default_factory=lambda: Path(os.getenv('DATA_DIR', '/app/data')))
    WEB_DIR: Path = field(default_factory=lambda: Path(os.getenv('WEB_DIR', '/app/web')))
    
    # ===== Notifications =====
    EMAIL_ENABLED: bool = field(default_factory=lambda: get_bool('EMAIL_ENABLED', False))
    SMTP_SERVER: str = field(default_factory=lambda: os.getenv('SMTP_SERVER', 'smtp.gmail.com'))
    SMTP_PORT: int = field(default_factory=lambda: get_int('SMTP_PORT', 587))
    SENDER_EMAIL: str = field(default_factory=lambda: os.getenv('SENDER_EMAIL', ''))
    SENDER_PASSWORD: str = field(default_factory=lambda: os.getenv('SENDER_PASSWORD', ''))
    RECIPIENT_EMAIL: str = field(default_factory=lambda: os.getenv('RECIPIENT_EMAIL', ''))
    
    # ===== Advanced =====
    DEBUG_MODE: bool = field(default_factory=lambda: get_bool('DEBUG_MODE', False))
    API_MAX_RETRIES: int = field(default_factory=lambda: get_int('API_MAX_RETRIES', 5))
    API_RETRY_DELAY: int = field(default_factory=lambda: get_int('API_RETRY_DELAY', 10))
    TZ: str = field(default_factory=lambda: os.getenv('TZ', 'America/Los_Angeles'))
    
    def __post_init__(self):
        """Ensure paths are Path objects and create directories if needed."""
        # Convert paths to Path objects
        for attr in ['BASE_DIR', 'LOG_DIR', 'DATA_DIR', 'WEB_DIR']:
            path_val = getattr(self, attr)
            if isinstance(path_val, str):
                setattr(self, attr, Path(path_val))
        
        # Calculate optimal CHECK_INTERVAL_MINUTES based on configuration
        self._calculate_polling_interval()
        
        # Create directories
        for directory in [self.LOG_DIR, self.DATA_DIR]:
            directory.mkdir(parents=True, exist_ok=True)
        
        # Set up derived file paths
        self.STATE_FILE = self.LOG_DIR / "battery_mode.txt"
        self.PEAK_STATE_FILE = self.LOG_DIR / "peak_state.txt"

    def _calculate_polling_interval(self):
        """Calculate optimal polling interval based on features and data source."""
        user_interval = get_int('CHECK_INTERVAL_MINUTES', 0)
        
        if self.MODBUS_ENABLED:
            # With Modbus, we can poll much more frequently
            if self.DYNAMIC_PRICING_ENABLED:
                # Dynamic pricing benefits from frequent updates
                default_interval = 5
                min_interval = 1
            else:
                # TOU users can use moderate frequency  
                default_interval = 10
                min_interval = 5
        else:
            # Cloud API rate limiting - more conservative
            if self.DYNAMIC_PRICING_ENABLED:
                # Dynamic pricing users get higher frequency but still limited
                default_interval = 15  
                min_interval = 15
            else:
                # Standard TOU users
                default_interval = 30
                min_interval = 30
        
        # Use user setting if provided and valid, otherwise use calculated default
        if user_interval > 0:
            self.CHECK_INTERVAL_MINUTES = max(user_interval, min_interval)
        else:
            self.CHECK_INTERVAL_MINUTES = default_interval

    def is_peak_period_now(self) -> bool:
        """Check if current time is in any configured peak period."""
        if not self.TOU_ENABLED:
            return False
        
        from datetime import datetime
        current_hour = datetime.now().hour
        
        # Check primary peak period
        if is_peak_period(current_hour, self.PEAK_START_HOUR, self.PEAK_END_HOUR):
            return True
        
        # Check secondary peak period if configured
        if (self.PEAK2_START_HOUR is not None and self.PEAK2_END_HOUR is not None):
            if is_peak_period(current_hour, self.PEAK2_START_HOUR, self.PEAK2_END_HOUR):
                return True
        
        return False

    def validate(self) -> List[str]:
        """Validate configuration and return list of error messages."""
        errors = []
        
        # Required credentials
        if not self.FRANKLIN_USERNAME:
            errors.append("FRANKLIN_USERNAME is required")
        if not self.FRANKLIN_PASSWORD:
            errors.append("FRANKLIN_PASSWORD is required")
        if not self.FRANKLIN_GATEWAY_ID:
            errors.append("FRANKLIN_GATEWAY_ID is required")
        
        # Battery configuration
        if self.BATTERY_CAPACITY_KWH <= 0:
            errors.append("BATTERY_CAPACITY_KWH must be positive")
        if self.CHARGE_RATE_PER_HOUR <= 0:
            errors.append("CHARGE_RATE_PER_HOUR must be positive")
        if not (5 <= self.CHARGE_RATE_PER_HOUR <= 100):
            errors.append("CHARGE_RATE_PER_HOUR should be 5-100 %/hour (got {:.1f})".format(self.CHARGE_RATE_PER_HOUR))
        if not (10 <= self.BATTERY_CAPACITY_KWH <= 200):
            errors.append("BATTERY_CAPACITY_KWH should be 10-200 kWh (got {:.1f})".format(self.BATTERY_CAPACITY_KWH))
        
        # Target SOC validation
        if not (50 <= self.TARGET_SOC <= 100):
            errors.append("TARGET_SOC should be 50-100% (got {:.1f})".format(self.TARGET_SOC))
        
        # TOU validation with midnight-crossing support
        if self.TOU_ENABLED:
            if not validate_peak_hours(self.PEAK_START_HOUR, self.PEAK_END_HOUR):
                if self.PEAK_START_HOUR == self.PEAK_END_HOUR:
                    errors.append("PEAK_START_HOUR and PEAK_END_HOUR cannot be the same (zero duration)")
                else:
                    errors.append(f"Invalid peak hours: {self.PEAK_START_HOUR}-{self.PEAK_END_HOUR} (must be 0-23)")
            
            # Secondary peak validation
            if (self.PEAK2_START_HOUR is not None and self.PEAK2_END_HOUR is not None):
                if not validate_peak_hours(self.PEAK2_START_HOUR, self.PEAK2_END_HOUR):
                    if self.PEAK2_START_HOUR == self.PEAK2_END_HOUR:
                        errors.append("PEAK2_START_HOUR and PEAK2_END_HOUR cannot be the same")
                    else:
                        errors.append(f"Invalid secondary peak hours: {self.PEAK2_START_HOUR}-{self.PEAK2_END_HOUR}")
        
        # Modbus validation
        if self.MODBUS_ENABLED:
            if not self.MODBUS_HOST:
                errors.append("MODBUS_HOST is required when MODBUS_ENABLED=true")
            if not (1 <= self.MODBUS_PORT <= 65535):
                errors.append("MODBUS_PORT must be 1-65535")
            if self.MODBUS_TIMEOUT <= 0:
                errors.append("MODBUS_TIMEOUT must be positive")
        
        # Scheduling validation
        if self.CHECK_INTERVAL_MINUTES < 1:
            errors.append("CHECK_INTERVAL_MINUTES must be at least 1")
        if not self.MODBUS_ENABLED and self.CHECK_INTERVAL_MINUTES < 15:
            errors.append("CHECK_INTERVAL_MINUTES must be at least 15 when using cloud API (rate limit protection)")
        if self.PEAK_TRANSITION_BUFFER_MINUTES < 1:
            errors.append("PEAK_TRANSITION_BUFFER_MINUTES must be at least 1")
        if self.HOME_MODE not in ('tou', 'self_consumption'):
            errors.append("HOME_MODE must be 'tou' or 'self_consumption'")
        
        # Weather settings validation
        if self.WEATHER_ENABLED:
            if not self.WEATHER_STATION_ID:
                errors.append("WEATHER_STATION_ID required when WEATHER_ENABLED=true")
            if not self.WEATHER_API_KEY:
                errors.append("WEATHER_API_KEY required when WEATHER_ENABLED=true")
        
        # PVOutput settings validation
        if self.PVOUTPUT_ENABLED:
            if not self.PVOUTPUT_API_KEY:
                errors.append("PVOUTPUT_API_KEY required when PVOUTPUT_ENABLED=true")
            if not self.PVOUTPUT_SYSTEM_IDS:
                errors.append("PVOUTPUT_SYSTEM_IDS required when PVOUTPUT_ENABLED=true")
        
        # Dynamic pricing validation
        if self.DYNAMIC_PRICING_ENABLED:
            if self.PRICE_THRESHOLD_CENTS >= self.PRICE_CEILING_CENTS:
                errors.append("PRICE_THRESHOLD_CENTS must be less than PRICE_CEILING_CENTS")
            if (self.SOLAR_OVERRIDE_PRICE_CENTS is not None and
                    self.SOLAR_OVERRIDE_PRICE_CENTS > self.PRICE_THRESHOLD_CENTS):
                errors.append("SOLAR_OVERRIDE_PRICE_CENTS should be <= PRICE_THRESHOLD_CENTS "
                            "(override should only trigger at more aggressive prices)")
        
        # SolarEdge panel monitoring validation
        if self.SOLAREDGE_PANEL_MONITORING:
            if not self.SOLAREDGE_SITE_ID:
                errors.append("SOLAREDGE_SITE_ID required when SOLAREDGE_PANEL_MONITORING=true")
            if not self.SOLAREDGE_USERNAME:
                errors.append("SOLAREDGE_USERNAME required when SOLAREDGE_PANEL_MONITORING=true")
            if not self.SOLAREDGE_PASSWORD:
                errors.append("SOLAREDGE_PASSWORD required when SOLAREDGE_PANEL_MONITORING=true")
        
        return errors
    
    def get_enabled_features(self) -> List[str]:
        """Return list of enabled feature names."""
        features = []
        if self.SOLAR_ENABLED:
            features.append("Solar")
        if self.TOU_ENABLED:
            peak_desc = f"{self.PEAK_START_HOUR}:00-{self.PEAK_END_HOUR}:00"
            if self.PEAK_START_HOUR > self.PEAK_END_HOUR:
                peak_desc += " (crosses midnight)"
            features.append(f"TOU ({peak_desc})")
        if self.DYNAMIC_PRICING_ENABLED:
            features.append(f"Dynamic Pricing ({self.PRICING_PROVIDER})")
        if self.MODBUS_ENABLED:
            features.append(f"Modbus TCP ({self.MODBUS_HOST}:{self.MODBUS_PORT})")
        if self.WEATHER_ENABLED:
            features.append(f"Weather ({self.WEATHER_STATION_ID})")
        if self.FORECAST_HOUSE_KWP > 0:
            features.append(f"Solar Forecast ({self.FORECAST_HOUSE_KWP} kWp, tilt={self.FORECAST_HOUSE_TILT}°)")
        if self.PVOUTPUT_ENABLED:
            features.append("PVOutput")
        if self.TELEMETRY_ENABLED:
            features.append("Anonymous Telemetry")
        if self.SOLAREDGE_PANEL_MONITORING:
            features.append(f"SolarEdge Panel Monitoring (site {self.SOLAREDGE_SITE_ID})")
        if self.ADAPTIVE_ENGINE_ENABLED:
            features.append("V4.0 Adaptive Engine")
        if self.SOLAR_ARRAYS:
            arrays = [a.strip() for a in self.SOLAR_ARRAYS.split(',') if a.strip()]
            features.append(f"Solar Arrays ({', '.join(arrays)})")
        elif self.ENPHASE_ENABLED:
            features.append("Enphase Solar (legacy)")
        return features
    
    def get_disabled_features(self) -> List[str]:
        """Return list of disabled feature names."""
        features = []
        if not self.SOLAR_ENABLED:
            features.append("Solar")
        if not self.TOU_ENABLED:
            features.append("TOU")
        if not self.DYNAMIC_PRICING_ENABLED:
            features.append("Dynamic Pricing")
        if not self.MODBUS_ENABLED:
            features.append("Modbus TCP")
        if not self.WEATHER_ENABLED:
            features.append("Weather")
        if not self.PVOUTPUT_ENABLED:
            features.append("PVOutput")
        if not self.TELEMETRY_ENABLED:
            features.append("Telemetry")
        if not self.ADAPTIVE_ENGINE_ENABLED:
            features.append("V4.0 Adaptive Engine")
        if not self.SOLAREDGE_PANEL_MONITORING:
            features.append("SolarEdge Panel Monitoring")
        return features
    
    def get_config_summary(self) -> str:
        """Return a formatted summary of current configuration."""
        lines = [
            "=" * 60,
            f"CONFIGURATION SUMMARY - v{VERSION}",
            "=" * 60,
            "",
            "ENABLED FEATURES:",
        ]
        
        for feature in self.get_enabled_features():
            lines.append(f"  [x] {feature}")
        
        if self.get_disabled_features():
            lines.append("")
            lines.append("DISABLED FEATURES:")
            for feature in self.get_disabled_features():
                lines.append(f"  [ ] {feature}")
        
        lines.extend([
            "",
            "BATTERY SETTINGS:",
            f"  Capacity: {self.BATTERY_CAPACITY_KWH} kWh",
            f"  Charge Rate: {self.CHARGE_RATE_PER_HOUR}%/hour",
            f"  Target SOC: {self.TARGET_SOC}%",
            f"  Strategy: {self.CHARGING_STRATEGY}",
        ])
        
        lines.extend([
            "",
            "SCHEDULING:",
            f"  Check Interval: {self.CHECK_INTERVAL_MINUTES} minutes",
            f"  Data Source: {'Modbus TCP' if self.MODBUS_ENABLED else 'Cloud API'}",
            f"  Peak Buffer: {self.PEAK_TRANSITION_BUFFER_MINUTES} minutes before/after",
            f"  Home Mode: {self.HOME_MODE}",
        ])
        
        if self.TOU_ENABLED:
            lines.extend([
                "",
                "TOU SETTINGS:",
                f"  Primary Peak: {self.PEAK_START_HOUR}:00-{self.PEAK_END_HOUR}:00 {self.PEAK_DAYS}",
            ])
            if self.PEAK_START_HOUR > self.PEAK_END_HOUR:
                lines.append(f"    (Crosses midnight)")
            if self.PEAK2_START_HOUR and self.PEAK2_END_HOUR:
                lines.append(f"  Secondary Peak: {self.PEAK2_START_HOUR}:00-{self.PEAK2_END_HOUR}:00 {self.PEAK2_DAYS}")
        
        if self.DYNAMIC_PRICING_ENABLED:
            lines.extend([
                "",
                "DYNAMIC PRICING:",
                f"  Provider: {self.PRICING_PROVIDER}",
                f"  Charge Threshold: {self.PRICE_THRESHOLD_CENTS} cents/kWh",
                f"  Price Ceiling: {self.PRICE_CEILING_CENTS} cents/kWh",
                f"  Solar Override: {self.SOLAR_OVERRIDE_PRICE_CENTS} cents/kWh"
                    if self.SOLAR_OVERRIDE_PRICE_CENTS is not None
                    else "  Solar Override: disabled (solar-first always preferred)",
            ])
        
        if self.MODBUS_ENABLED:
            lines.extend([
                "",
                "MODBUS SETTINGS:",
                f"  Host: {self.MODBUS_HOST}:{self.MODBUS_PORT}",
                f"  Timeout: {self.MODBUS_TIMEOUT}s",
                f"  Retry Attempts: {self.MODBUS_RETRY_ATTEMPTS}",
            ])
        
        lines.append("=" * 60)
        
        return "\n".join(lines)
    
    def to_dict(self) -> dict:
        """Export configuration as dictionary (excludes sensitive data)."""
        return {
            'version': VERSION,
            'battery_capacity_kwh': self.BATTERY_CAPACITY_KWH,
            'charge_rate_per_hour': self.CHARGE_RATE_PER_HOUR,
            'target_soc': self.TARGET_SOC,
            'charging_strategy': self.CHARGING_STRATEGY,
            'features': {
                'solar_enabled': self.SOLAR_ENABLED,
                'tou_enabled': self.TOU_ENABLED,
                'dynamic_pricing_enabled': self.DYNAMIC_PRICING_ENABLED,
                'modbus_enabled': self.MODBUS_ENABLED,
                'weather_enabled': self.WEATHER_ENABLED,
                'pvoutput_enabled': self.PVOUTPUT_ENABLED,
                'telemetry_enabled': self.TELEMETRY_ENABLED,
                'adaptive_engine_enabled': self.ADAPTIVE_ENGINE_ENABLED,
                'solaredge_panel_monitoring': self.SOLAREDGE_PANEL_MONITORING,
                'solar_arrays': self.SOLAR_ARRAYS,
            },
            'tou': {
                'peak_start_hour': self.PEAK_START_HOUR,
                'peak_end_hour': self.PEAK_END_HOUR,
                'peak_days': self.PEAK_DAYS,
                'crosses_midnight': self.PEAK_START_HOUR > self.PEAK_END_HOUR,
            } if self.TOU_ENABLED else None,
            'scheduling': {
                'check_interval_minutes': self.CHECK_INTERVAL_MINUTES,
                'peak_transition_buffer_minutes': self.PEAK_TRANSITION_BUFFER_MINUTES,
                'home_mode': self.HOME_MODE,
                'data_source': 'modbus' if self.MODBUS_ENABLED else 'cloud_api',
            },
            'dynamic_pricing': {
                'provider': self.PRICING_PROVIDER,
                'threshold_cents': self.PRICE_THRESHOLD_CENTS,
                'ceiling_cents': self.PRICE_CEILING_CENTS,
                'solar_override_cents': self.SOLAR_OVERRIDE_PRICE_CENTS,
            } if self.DYNAMIC_PRICING_ENABLED else None,
            'solar': {
                'capacity_kw': self.SOLAR_CAPACITY_KW,
                'min_for_wait': self.MIN_SOLAR_FOR_WAIT,
            } if self.SOLAR_ENABLED else None,
            'modbus': {
                'host': self.MODBUS_HOST,
                'port': self.MODBUS_PORT,
                'timeout': self.MODBUS_TIMEOUT,
            } if self.MODBUS_ENABLED else None,
        }


# Global configuration instance
config = Config()


def configure_logging(log_file=None):
    """Configure root logger based on DEBUG_MODE setting.

    Centralises logging setup so every script gets the same format,
    level, and (optionally) file handler.  Reads ``config.DEBUG_MODE``
    to choose between DEBUG and INFO.

    Args:
        log_file: Optional path to a log file.  When provided a
                  ``FileHandler`` is added alongside the default
                  ``StreamHandler`` (console).

    Note:
        ``logging.basicConfig`` is a no-op once the root logger already
        has handlers.  We clear any existing handlers first so that the
        function is safe to call from ``__main__`` blocks even when an
        importing module already configured logging.
    """
    import logging

    root = logging.getLogger()
    # Remove pre-existing handlers so this call always takes effect
    root.handlers.clear()

    level = logging.DEBUG if config.DEBUG_MODE else logging.INFO
    handlers = [logging.StreamHandler()]
    if log_file is not None:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


if __name__ == "__main__":
    # When run directly, print configuration summary
    print(config.get_config_summary())
    
    errors = config.validate()
    if errors:
        print("\nCONFIGURATION ERRORS:")
        for error in errors:
            print(f"  - {error}")
    else:
        print("\nConfiguration is valid.")
        
    # Test midnight-crossing peak periods
    from datetime import datetime
    print(f"\nPEAK PERIOD TEST:")
    print(f"  Current time: {datetime.now().hour}:xx")
    print(f"  Is peak period: {config.is_peak_period_now()}")
