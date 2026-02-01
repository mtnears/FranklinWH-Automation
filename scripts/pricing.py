#!/usr/bin/env python3
"""
Dynamic Pricing Module for FranklinWH Battery Automation

Fetches real-time electricity prices from supported providers:
- ComEd Hourly Pricing (Illinois)
- Future: ERCOT (Texas), CAISO (California), etc.

Usage:
    from pricing import get_current_price, should_charge_at_price
    
    price = get_current_price()
    if price and price < config.PRICE_THRESHOLD_CENTS:
        # Cheap power - consider charging
"""
import requests
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass
import logging

try:
    from config import config
except ImportError:
    config = None

logger = logging.getLogger(__name__)


@dataclass
class PriceData:
    """Container for price information."""
    price_cents: float
    timestamp: datetime
    provider: str
    
    @property
    def price_dollars(self) -> float:
        return self.price_cents / 100


class PricingProvider:
    """Base class for pricing providers."""
    
    def get_current_price(self) -> Optional[PriceData]:
        """Get current electricity price."""
        raise NotImplementedError
    
    def get_price_history(self, hours: int = 24) -> List[PriceData]:
        """Get price history for the specified hours."""
        raise NotImplementedError
    
    def get_price_forecast(self) -> List[PriceData]:
        """Get price forecast if available."""
        return []  # Not all providers have forecasts


class ComEdPricing(PricingProvider):
    """
    ComEd Hourly Pricing API
    
    API Documentation: https://hourlypricing.comed.com/hp-api/
    
    Returns prices in cents per kWh.
    """
    
    BASE_URL = "https://hourlypricing.comed.com/api"
    
    def get_current_price(self) -> Optional[PriceData]:
        """
        Get current hour average price from ComEd.
        
        Returns:
            PriceData with current price, or None if API fails
        """
        try:
            response = requests.get(
                f"{self.BASE_URL}",
                params={
                    "type": "currenthouraverage",
                    "format": "json"
                },
                timeout=10
            )
            response.raise_for_status()
            
            data = response.json()
            if data and len(data) > 0:
                price_cents = float(data[0]['price'])
                millis = int(data[0]['millisUTC'])
                timestamp = datetime.utcfromtimestamp(millis / 1000)
                
                return PriceData(
                    price_cents=price_cents,
                    timestamp=timestamp,
                    provider="comed"
                )
            
            return None
            
        except requests.exceptions.RequestException as e:
            logger.warning(f"ComEd API request failed: {e}")
            return None
        except (KeyError, ValueError, IndexError) as e:
            logger.warning(f"ComEd API response parse error: {e}")
            return None
    
    def get_price_history(self, hours: int = 24) -> List[PriceData]:
        """
        Get 5-minute price data for the last N hours.
        
        Args:
            hours: Number of hours of history to fetch (max 24)
            
        Returns:
            List of PriceData objects, newest first
        """
        try:
            response = requests.get(
                f"{self.BASE_URL}",
                params={
                    "type": "5minutefeed",
                    "format": "json"
                },
                timeout=15
            )
            response.raise_for_status()
            
            data = response.json()
            prices = []
            
            cutoff = datetime.utcnow() - timedelta(hours=hours)
            
            for item in data:
                try:
                    price_cents = float(item['price'])
                    millis = int(item['millisUTC'])
                    timestamp = datetime.utcfromtimestamp(millis / 1000)
                    
                    if timestamp >= cutoff:
                        prices.append(PriceData(
                            price_cents=price_cents,
                            timestamp=timestamp,
                            provider="comed"
                        ))
                except (KeyError, ValueError):
                    continue
            
            # Sort newest first
            prices.sort(key=lambda x: x.timestamp, reverse=True)
            return prices
            
        except requests.exceptions.RequestException as e:
            logger.warning(f"ComEd API history request failed: {e}")
            return []
    
    def get_price_stats(self, hours: int = 24) -> Dict:
        """
        Get price statistics for the specified period.
        
        Returns dict with: min, max, avg, current, trend
        """
        history = self.get_price_history(hours)
        current = self.get_current_price()
        
        if not history:
            return {}
        
        prices = [p.price_cents for p in history]
        
        # Calculate trend (last 2 hours vs previous 2 hours)
        recent = [p.price_cents for p in history if 
                  p.timestamp >= datetime.utcnow() - timedelta(hours=2)]
        older = [p.price_cents for p in history if 
                 datetime.utcnow() - timedelta(hours=4) <= p.timestamp < datetime.utcnow() - timedelta(hours=2)]
        
        if recent and older:
            trend = sum(recent) / len(recent) - sum(older) / len(older)
        else:
            trend = 0
        
        return {
            'min': min(prices),
            'max': max(prices),
            'avg': sum(prices) / len(prices),
            'current': current.price_cents if current else None,
            'trend': trend,  # Positive = prices rising
            'trend_direction': 'rising' if trend > 0.5 else 'falling' if trend < -0.5 else 'stable',
            'sample_count': len(prices),
            'period_hours': hours
        }


# Provider registry
PROVIDERS = {
    'comed': ComEdPricing,
    # Future providers:
    # 'ercot': ERCOTPricing,
    # 'caiso': CAISOPricing,
}


def get_provider(provider_name: str = None) -> Optional[PricingProvider]:
    """
    Get pricing provider instance.
    
    Args:
        provider_name: Provider name, or None to use config default
        
    Returns:
        PricingProvider instance or None if not found
    """
    if provider_name is None:
        if config:
            provider_name = config.PRICING_PROVIDER
        else:
            provider_name = 'comed'
    
    provider_class = PROVIDERS.get(provider_name.lower())
    if provider_class:
        return provider_class()
    
    logger.warning(f"Unknown pricing provider: {provider_name}")
    return None


def get_current_price(provider_name: str = None) -> Optional[float]:
    """
    Get current electricity price in cents/kWh.
    
    Convenience function for simple price checks.
    
    Args:
        provider_name: Provider name, or None to use config default
        
    Returns:
        Price in cents/kWh, or None if unavailable
    """
    provider = get_provider(provider_name)
    if not provider:
        return None
    
    price_data = provider.get_current_price()
    if price_data:
        return price_data.price_cents
    
    return None


def should_charge_at_current_price(
    threshold_cents: float = None,
    ceiling_cents: float = None,
    provider_name: str = None
) -> Tuple[bool, str]:
    """
    Determine if current price is favorable for grid charging.
    
    Args:
        threshold_cents: Price below which to charge (default from config)
        ceiling_cents: Price above which to never charge (default from config)
        provider_name: Provider name (default from config)
        
    Returns:
        Tuple of (should_charge: bool, reason: str)
    """
    # Get defaults from config
    if config:
        if threshold_cents is None:
            threshold_cents = config.PRICE_THRESHOLD_CENTS
        if ceiling_cents is None:
            ceiling_cents = config.PRICE_CEILING_CENTS
    else:
        threshold_cents = threshold_cents or 4.0
        ceiling_cents = ceiling_cents or 10.0
    
    price = get_current_price(provider_name)
    
    if price is None:
        return False, "Price data unavailable - using default behavior"
    
    if price > ceiling_cents:
        return False, f"Price too high ({price:.1f}c > {ceiling_cents:.1f}c ceiling)"
    
    if price < threshold_cents:
        return True, f"Cheap power available ({price:.1f}c < {threshold_cents:.1f}c threshold)"
    
    return False, f"Price acceptable but not cheap ({price:.1f}c)"


def get_overnight_charging_recommendation(
    target_soc: float,
    current_soc: float,
    solar_forecast_kwh: float = 0,
    provider_name: str = None
) -> Dict:
    """
    Get recommendation for overnight charging based on prices and solar forecast.
    
    This is the key intelligence for integrating dynamic pricing with solar.
    
    Args:
        target_soc: Target state of charge (%)
        current_soc: Current state of charge (%)
        solar_forecast_kwh: Expected solar production tomorrow (kWh)
        provider_name: Pricing provider
        
    Returns:
        Dict with recommendation and reasoning
    """
    provider = get_provider(provider_name)
    if not provider:
        return {
            'recommend_charge': False,
            'reason': 'Pricing provider unavailable',
            'confidence': 'low'
        }
    
    # Get price statistics
    stats = provider.get_price_stats(hours=24)
    current_price = stats.get('current')
    
    if current_price is None:
        return {
            'recommend_charge': False,
            'reason': 'Current price unavailable',
            'confidence': 'low'
        }
    
    # Calculate how much energy we need
    if config:
        battery_capacity = config.BATTERY_CAPACITY_KWH
    else:
        battery_capacity = 30.0
    
    soc_needed = target_soc - current_soc
    kwh_needed = (soc_needed / 100) * battery_capacity
    
    # Will solar cover our needs?
    solar_covers_need = solar_forecast_kwh >= kwh_needed
    
    # Is current price in the cheap range?
    avg_price = stats.get('avg', 5.0)
    is_cheap = current_price < avg_price * 0.7  # 30% below average
    is_very_cheap = current_price < 2.0  # Under 2 cents is almost always worth it
    
    # Decision logic
    if solar_covers_need and not is_very_cheap:
        return {
            'recommend_charge': False,
            'reason': f'Solar forecast ({solar_forecast_kwh:.1f} kWh) covers need ({kwh_needed:.1f} kWh)',
            'confidence': 'high',
            'current_price': current_price,
            'kwh_needed': kwh_needed
        }
    
    if is_very_cheap:
        return {
            'recommend_charge': True,
            'reason': f'Very cheap power ({current_price:.1f}c) - worth charging even with solar coming',
            'confidence': 'high',
            'current_price': current_price,
            'kwh_needed': kwh_needed
        }
    
    if not solar_covers_need and is_cheap:
        gap = kwh_needed - solar_forecast_kwh
        return {
            'recommend_charge': True,
            'reason': f'Solar forecast short by {gap:.1f} kWh and price is cheap ({current_price:.1f}c)',
            'confidence': 'medium',
            'current_price': current_price,
            'kwh_needed': kwh_needed,
            'recommended_kwh': gap  # Only charge what solar won't cover
        }
    
    if not solar_covers_need:
        return {
            'recommend_charge': False,
            'reason': f'Need charging but price not cheap enough ({current_price:.1f}c vs avg {avg_price:.1f}c)',
            'confidence': 'medium',
            'current_price': current_price,
            'kwh_needed': kwh_needed,
            'wait_for_cheaper': True
        }
    
    return {
        'recommend_charge': False,
        'reason': 'Default: wait for solar',
        'confidence': 'low',
        'current_price': current_price
    }


if __name__ == "__main__":
    # Test the pricing module
    print("=" * 60)
    print("DYNAMIC PRICING MODULE TEST")
    print("=" * 60)
    
    # Test ComEd
    print("\nTesting ComEd API...")
    comed = ComEdPricing()
    
    current = comed.get_current_price()
    if current:
        print(f"  Current price: {current.price_cents:.2f} cents/kWh")
        print(f"  Timestamp: {current.timestamp}")
    else:
        print("  Failed to get current price")
    
    print("\nPrice statistics (24h):")
    stats = comed.get_price_stats(24)
    if stats:
        print(f"  Min: {stats['min']:.2f}c")
        print(f"  Max: {stats['max']:.2f}c")
        print(f"  Avg: {stats['avg']:.2f}c")
        print(f"  Trend: {stats['trend_direction']}")
    
    print("\nCharge recommendation:")
    should_charge, reason = should_charge_at_current_price()
    print(f"  Should charge: {should_charge}")
    print(f"  Reason: {reason}")
    
    print("\nOvernight recommendation (50% SOC, target 95%, 20kWh solar forecast):")
    rec = get_overnight_charging_recommendation(95, 50, 20)
    print(f"  Recommend: {rec['recommend_charge']}")
    print(f"  Reason: {rec['reason']}")
    print(f"  Confidence: {rec['confidence']}")
