#!/usr/bin/env python3
"""
Configuration Management for FranklinWH Battery Automation

Loads settings from environment variables (.env file) with sensible defaults.
Provides a centralized configuration object used by all scripts.

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
    
    # ===== TOU Settings =====
    PEAK_START_HOUR: int = field(default_factory=lambda: get_int('PEAK_START_HOUR', 17))
    PEAK_END_HOUR: int = field(default_factory=lambda: get_int('PEAK_END_HOUR', 20))
    PEAK_DAYS: str = field(default_factory=lambda: os.getenv('PEAK_DAYS', 'weekdays'))
    
    # Optional second peak period
    PEAK2_START_HOUR: Optional[int] = field(default_factory=lambda: get_int('PEAK2_START_HOUR', 0) or None)
    PEAK2_END_HOUR: Optional[int] = field(default_factory=lambda: get_int('PEAK2_END_HOUR', 0) or None)
    PEAK2_DAYS: str = field(default_factory=lambda: os.getenv('PEAK2_DAYS', 'weekdays'))
    
    # ===== Dynamic Pricing Settings =====
    PRICING_PROVIDER: str = field(default_factory=lambda: os.getenv('PRICING_PROVIDER', 'comed'))
    PRICE_THRESHOLD_CENTS: float = field(default_factory=lambda: get_float('PRICE_THRESHOLD_CENTS', 4.0))
    PRICE_CEILING_CENTS: float = field(default_factory=lambda: get_float('PRICE_CEILING_CENTS', 10.0))
    
    # ===== Solar Settings =====
    SOLAR_CAPACITY_KW: float = field(default_factory=lambda: get_float('SOLAR_CAPACITY_KW', 0.0))
    MIN_SOLAR_FOR_WAIT: float = field(default_factory=lambda: get_float('MIN_SOLAR_FOR_WAIT', 0.5))
    
    # ===== Weather Settings =====
    WEATHER_PROVIDER: str = field(default_factory=lambda: os.getenv('WEATHER_PROVIDER', 'wunderground'))
    WEATHER_STATION_ID: str = field(default_factory=lambda: os.getenv('WEATHER_STATION_ID', ''))
    WEATHER_API_KEY: str = field(default_factory=lambda: os.getenv('WEATHER_API_KEY', ''))
    CLOUDY_THRESHOLD_PERCENT: int = field(default_factory=lambda: get_int('CLOUDY_THRESHOLD_PERCENT', 50))
    
    # ===== PVOutput Settings =====
    PVOUTPUT_API_KEY: str = field(default_factory=lambda: os.getenv('PVOUTPUT_API_KEY', ''))
    PVOUTPUT_SYSTEM_IDS: List[str] = field(default_factory=lambda: get_list('PVOUTPUT_SYSTEM_IDS'))
    
    # ===== Decision Tuning =====
    TARGET_SOC: float = field(default_factory=lambda: get_float('TARGET_SOC', 95.0))
    SAFETY_MARGIN_HOURS: float = field(default_factory=lambda: get_float('SAFETY_MARGIN_HOURS', 0.5))
    CHARGING_STRATEGY: str = field(default_factory=lambda: os.getenv('CHARGING_STRATEGY', 'balanced'))
    
    # ===== System Paths =====
    BASE_DIR: Path = field(default_factory=lambda: Path(os.getenv('BASE_DIR', '/volume1/docker/franklin')))
    LOG_DIR: Path = field(default_factory=lambda: Path(os.getenv('LOG_DIR', '/volume1/docker/franklin/logs')))
    DATA_DIR: Path = field(default_factory=lambda: Path(os.getenv('DATA_DIR', '/volume1/docker/franklin/data')))
    WEB_DIR: Path = field(default_factory=lambda: Path(os.getenv('WEB_DIR', '/volume1/web')))
    
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
        self.BASE_DIR = Path(self.BASE_DIR)
        self.LOG_DIR = Path(self.LOG_DIR)
        self.DATA_DIR = Path(self.DATA_DIR)
        self.WEB_DIR = Path(self.WEB_DIR)
    
    @property
    def LOG_FILE(self) -> Path:
        """Path to continuous monitoring CSV."""
        return self.LOG_DIR / "continuous_monitoring.csv"
    
    @property
    def INTELLIGENCE_LOG(self) -> Path:
        """Path to decision/intelligence log."""
        return self.LOG_DIR / "solar_intelligence.log"
    
    @property
    def STATE_FILE(self) -> Path:
        """Path to last mode state file."""
        return self.LOG_DIR / "last_mode.txt"
    
    @property
    def PEAK_STATE_FILE(self) -> Path:
        """Path to peak state tracking file."""
        return self.LOG_DIR / "peak_state.txt"
    
    @property
    def WEATHER_LOG(self) -> Path:
        """Path to weather data CSV."""
        return self.LOG_DIR / "weather_data.csv"
    
    def validate(self) -> List[str]:
        """
        Validate configuration and return list of errors.
        Returns empty list if configuration is valid.
        """
        errors = []
        
        # Required credentials
        if not self.FRANKLIN_USERNAME:
            errors.append("FRANKLIN_USERNAME is required")
        if not self.FRANKLIN_PASSWORD:
            errors.append("FRANKLIN_PASSWORD is required")
        if not self.FRANKLIN_GATEWAY_ID:
            errors.append("FRANKLIN_GATEWAY_ID is required")
        
        # Battery settings
        if self.BATTERY_CAPACITY_KWH <= 0:
            errors.append("BATTERY_CAPACITY_KWH must be positive")
        if self.CHARGE_RATE_PER_HOUR <= 0:
            errors.append("CHARGE_RATE_PER_HOUR must be positive")
        
        # TOU settings validation
        if self.TOU_ENABLED:
            if not (0 <= self.PEAK_START_HOUR <= 23):
                errors.append("PEAK_START_HOUR must be 0-23")
            if not (0 <= self.PEAK_END_HOUR <= 23):
                errors.append("PEAK_END_HOUR must be 0-23")
            if self.PEAK_START_HOUR >= self.PEAK_END_HOUR:
                errors.append("PEAK_START_HOUR must be before PEAK_END_HOUR")
        
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
        
        return errors
    
    def get_enabled_features(self) -> List[str]:
        """Return list of enabled feature names."""
        features = []
        if self.SOLAR_ENABLED:
            features.append("Solar")
        if self.TOU_ENABLED:
            features.append(f"TOU ({self.PEAK_START_HOUR}:00-{self.PEAK_END_HOUR}:00)")
        if self.DYNAMIC_PRICING_ENABLED:
            features.append(f"Dynamic Pricing ({self.PRICING_PROVIDER})")
        if self.WEATHER_ENABLED:
            features.append(f"Weather ({self.WEATHER_STATION_ID})")
        if self.PVOUTPUT_ENABLED:
            features.append("PVOutput")
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
        if not self.WEATHER_ENABLED:
            features.append("Weather")
        if not self.PVOUTPUT_ENABLED:
            features.append("PVOutput")
        return features
    
    def get_config_summary(self) -> str:
        """Return a formatted summary of current configuration."""
        lines = [
            "=" * 60,
            "CONFIGURATION SUMMARY",
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
        
        if self.DYNAMIC_PRICING_ENABLED:
            lines.extend([
                "",
                "DYNAMIC PRICING:",
                f"  Provider: {self.PRICING_PROVIDER}",
                f"  Charge Threshold: {self.PRICE_THRESHOLD_CENTS} cents/kWh",
                f"  Price Ceiling: {self.PRICE_CEILING_CENTS} cents/kWh",
            ])
        
        lines.append("=" * 60)
        
        return "\n".join(lines)
    
    def to_dict(self) -> dict:
        """Export configuration as dictionary (excludes sensitive data)."""
        return {
            'battery_capacity_kwh': self.BATTERY_CAPACITY_KWH,
            'charge_rate_per_hour': self.CHARGE_RATE_PER_HOUR,
            'target_soc': self.TARGET_SOC,
            'charging_strategy': self.CHARGING_STRATEGY,
            'features': {
                'solar_enabled': self.SOLAR_ENABLED,
                'tou_enabled': self.TOU_ENABLED,
                'dynamic_pricing_enabled': self.DYNAMIC_PRICING_ENABLED,
                'weather_enabled': self.WEATHER_ENABLED,
                'pvoutput_enabled': self.PVOUTPUT_ENABLED,
            },
            'tou': {
                'peak_start_hour': self.PEAK_START_HOUR,
                'peak_end_hour': self.PEAK_END_HOUR,
                'peak_days': self.PEAK_DAYS,
            } if self.TOU_ENABLED else None,
            'dynamic_pricing': {
                'provider': self.PRICING_PROVIDER,
                'threshold_cents': self.PRICE_THRESHOLD_CENTS,
                'ceiling_cents': self.PRICE_CEILING_CENTS,
            } if self.DYNAMIC_PRICING_ENABLED else None,
            'solar': {
                'capacity_kw': self.SOLAR_CAPACITY_KW,
                'min_for_wait': self.MIN_SOLAR_FOR_WAIT,
            } if self.SOLAR_ENABLED else None,
        }


# Global configuration instance
config = Config()


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
