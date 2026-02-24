#!/usr/bin/env python3
"""
adaptive_engine.py — Adaptive Decision Engine for FranklinWH Battery Automation v4.0

Replaces smart_decision.py's should_charge_from_grid() with a continuous
optimization engine that asks: "What is the optimal mode for this system
right now?" using forecasts, learned system behavior, rate awareness,
and reserve constraints.

The engine runs every decision cycle (configured interval) and produces
a Decision: hold, switch_to_backup (grid charge), switch_to_self_consumption, or switch_to_tou.

Decision Priority Stack:
  1. Grid offline → hold
  2. Emergency preparedness → enforce floor SOC
  3. Dynamic pricing override → charge on negative/credit pricing
  4. Peak rate protection → never buy at peak
  5. Solar curtailment prevention → log wasted solar
  6. Dynamic pricing cheap power → charge below threshold
  7. Forecast-aware charging gap → charge only what solar won't provide
  8. Consumption-aware self-consumption → use solar, avoid grid
  9. Default → self-consumption

Part of the v4.0 Adaptive Decision Engine.
"""

import json
import os
import logging
from config import configure_logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

from system_profile import SystemProfile, load_profile, build_profile, save_profile
from rate_schedule import RateSchedule, load_rate_schedule

try:
    from solar_forecast import get_forecast_engine, SolarForecastEngine, MorningPlan
    HAS_SOLAR_FORECAST = True
except ImportError:
    HAS_SOLAR_FORECAST = False

logger = logging.getLogger('adaptive_engine')

# ---------------------------------------------------------------------------
# Configuration Defaults
# ---------------------------------------------------------------------------

# Target SOC for pre-peak charging (%)
DEFAULT_TARGET_SOC = 95.0

# Minimum SOC before we consider grid charging (%)
DEFAULT_MIN_SOC_TRIGGER = 50.0

# Curtailment tracking: SOC threshold above which to log curtailment
CURTAILMENT_SOC_THRESHOLD = 95.0

# Minimum solar (kW) to consider "producing"
MIN_SOLAR_PRODUCING_KW = 0.1

# Mode switch cooldown (seconds) - prevents flapping
MODE_SWITCH_COOLDOWN_S = 300

# EB deferral — minimum buffer hours beyond charge time before triggering EB.
# EB is aggressive (~8kW grid charging). It charges fast — a 7.8 kWh gap
# takes ~1 hour. There's no reason to start at 5am for a 5pm peak.
# The engine recalculates every cycle (30 min), so deferring is safe:
# TOU drift and solar may shrink the gap naturally, and EB can always
# catch up later. This constant sets the minimum comfortable buffer.
# Example: charge_time=1h, min_buffer=max(2.0, 1.0)=2.0h → don't start
# EB until hours_to_peak <= 3.0h (i.e., ~2pm for 5pm peak).
EB_DEFERRAL_MIN_BUFFER_HOURS = 2.0

# Solar headroom management — drain battery to absorb forecast solar
HEADROOM_MIN_HOURS_TO_PEAK = 6.0       # Don't drain if peak is closer than this
HEADROOM_MIN_SOC_FLOOR = 20.0          # Never drain below this (backup reserve)
HEADROOM_DEFAULT_DRAIN_BUFFER_PCT = 3.0  # Minimum buffer below target for TOU drift
HEADROOM_CURTAILMENT_THRESHOLD = 3.0   # kWh excess solar before activating drain


# ---------------------------------------------------------------------------
# TOU Drift Tracker
# ---------------------------------------------------------------------------

class TOUDriftTracker:
    """Tracks observed phantom grid→battery charge rate during TOU mode.

    Franklin's TOU "aPower charges from solar" mode sometimes charges
    the battery from grid alongside solar, despite the configuration
    suggesting it shouldn't. The rate varies by system, day, and conditions.

    This tracker observes actual grid→battery flow during TOU mode and
    maintains a rolling average that the engine uses to:
      1. Set drain buffers when managing solar headroom
      2. Predict SOC drift when parked in TOU mode
      3. Report via telemetry for cross-system learning
    """

    def __init__(self, max_samples: int = 48):
        """
        Args:
            max_samples: Rolling window size (48 = 24h at 30-min intervals)
        """
        self.max_samples = max_samples
        self._samples: list = []           # (timestamp, grid_to_bat_kw, solar_kw, soc_pct)
        self._drift_rate_kw: float = 0.0   # Rolling avg grid→bat in TOU
        self._drift_rate_pct_per_hour: float = 0.0
        self._last_soc: Optional[float] = None
        self._last_time: Optional[datetime] = None

    def observe(self, timestamp: datetime, grid_to_bat_kw: float,
                solar_kw: float, soc_pct: float, mode: str):
        """Record an observation. Only tracks during TOU mode."""
        if mode != "time_of_use":
            self._last_soc = None
            self._last_time = None
            return

        self._samples.append((timestamp, grid_to_bat_kw, solar_kw, soc_pct))
        if len(self._samples) > self.max_samples:
            self._samples = self._samples[-self.max_samples:]

        # Calculate SOC drift rate from consecutive TOU observations
        if self._last_soc is not None and self._last_time is not None:
            dt_hours = (timestamp - self._last_time).total_seconds() / 3600.0
            if 0.05 < dt_hours < 1.5:  # Reasonable interval
                dsoc = soc_pct - self._last_soc
                if dsoc > 0:  # Only count upward drift
                    drift_pct_h = dsoc / dt_hours
                    if self._drift_rate_pct_per_hour == 0:
                        self._drift_rate_pct_per_hour = drift_pct_h
                    else:
                        # Exponential moving average, α=0.2
                        self._drift_rate_pct_per_hour = (
                            0.8 * self._drift_rate_pct_per_hour + 0.2 * drift_pct_h
                        )

        self._last_soc = soc_pct
        self._last_time = timestamp

        # Average grid→bat rate from samples where solar is present
        solar_samples = [(ts, g2b, sol, soc) for ts, g2b, sol, soc in self._samples
                         if sol > MIN_SOLAR_PRODUCING_KW and g2b > 0]
        if solar_samples:
            self._drift_rate_kw = sum(s[1] for s in solar_samples) / len(solar_samples)

    @property
    def drift_rate_kw(self) -> float:
        """Average grid→battery charge rate observed during TOU mode (kW)."""
        return round(self._drift_rate_kw, 2)

    @property
    def drift_rate_pct_per_hour(self) -> float:
        """SOC% gained per hour from TOU drift."""
        return round(self._drift_rate_pct_per_hour, 2)

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    def expected_drift_kwh(self, hours: float, battery_capacity_kwh: float) -> float:
        """Estimate kWh of drift charging over a given period."""
        return self._drift_rate_pct_per_hour * hours / 100.0 * battery_capacity_kwh

    def to_dict(self) -> dict:
        return {
            'drift_rate_kw': self.drift_rate_kw,
            'drift_rate_pct_per_hour': self.drift_rate_pct_per_hour,
            'sample_count': self.sample_count,
        }


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class SystemState:
    """Current system state snapshot — all inputs the engine needs."""
    timestamp: datetime
    soc_percent: float
    solar_kw: float
    grid_kw: float
    battery_kw: float
    home_load_kw: float
    grid_online: bool
    current_mode: str           # "self_consumption" or "emergency_backup"

    # Rate info (populated by engine)
    current_tier: str = ""
    current_rate_cents: float = 0.0
    is_peak: bool = False
    hours_to_peak: Optional[float] = None
    peak_duration_hours: float = 0.0
    rate_spread_cents: float = 0.0

    # Forecast info (populated by engine when available)
    forecast_solar_kwh: float = 0.0    # Remaining solar before peak

    # Charging breakdown (from Modbus/API data)
    grid_to_battery_kw: float = 0.0    # Current grid→battery charge rate
    solar_to_battery_kw: float = 0.0   # Current solar→battery charge rate

    # Override info
    emergency_prep_active: bool = False
    emergency_prep_floor: float = 0.0

    # Dynamic pricing (future)
    dynamic_price_cents: Optional[float] = None


@dataclass
class Decision:
    """Output of the adaptive engine — what to do now."""
    mode: str                       # "self_consumption" or "emergency_backup"
    reason: str                     # Human-readable explanation
    confidence: float               # 0.0-1.0
    action: str                     # "hold", "switch_to_backup", "switch_to_self_consumption", "switch_to_tou"
    priority_level: int = 9         # Which priority stack level decided (1-9)
    metrics: dict = field(default_factory=dict)  # Curtailed kWh, gap info, etc.

    def to_dict(self) -> dict:
        return {
            'mode': self.mode,
            'reason': self.reason,
            'confidence': self.confidence,
            'action': self.action,
            'priority_level': self.priority_level,
            'metrics': self.metrics,
        }


# ---------------------------------------------------------------------------
# Adaptive Engine
# ---------------------------------------------------------------------------

class AdaptiveEngine:
    """The v4.0 decision engine.
    
    Initialized with a system profile and rate schedule.
    Call evaluate() each cycle with current system state.
    """

    def __init__(self,
                 profile: SystemProfile,
                 rate_schedule: RateSchedule,
                 target_soc: float = DEFAULT_TARGET_SOC,
                 config: Optional[dict] = None):
        self.profile = profile
        self.rates = rate_schedule
        self.target_soc = target_soc
        self.config = config or {}
        self.last_mode_switch: Optional[datetime] = None
        self.last_decision: Optional[Decision] = None

        # Solar forecast engine (v4.0 forecast-aware charging)
        self.forecast_engine: Optional['SolarForecastEngine'] = None
        self._morning_plan: Optional['MorningPlan'] = None
        self._morning_plan_time: Optional[datetime] = None
        self._init_forecast_engine()

        # TOU drift tracker (measures phantom grid→battery charging in TOU mode)
        self.tou_drift = TOUDriftTracker()

        # Export configuration — non-export systems waste solar when battery is full,
        # export systems send surplus to grid for credit (headroom mgmt not needed)
        self.solar_export = self.config.get('solar_export', False)

        # Cumulative metrics for current session
        self.curtailed_kwh = 0.0
        self.curtailed_value_cents = 0.0
        self.decisions_made = 0

        logger.info(f"AdaptiveEngine initialized: target_soc={target_soc}%, "
                    f"rate_schedule={rate_schedule.name}, "
                    f"forecast_engine={'yes' if self.forecast_engine else 'no'}, "
                    f"solar_export={'yes' if self.solar_export else 'no'}")

    def _init_forecast_engine(self):
        """Initialize the solar forecast engine if available."""
        if not HAS_SOLAR_FORECAST:
            logger.info("solar_forecast not available — using learned profile fallback")
            return

        try:
            self.forecast_engine = get_forecast_engine(
                config=self.config,
                solar_profile=self.profile,
            )
            logger.info("Solar forecast engine initialized")
        except Exception as e:
            logger.warning(f"Solar forecast engine init failed: {e} — using learned profile")
            self.forecast_engine = None

    def _get_morning_plan(self, soc_percent: float, timestamp: datetime) -> Optional['MorningPlan']:
        """Get or refresh the morning plan from the forecast engine.

        Recalculates every 30 minutes or when SOC changes significantly.
        Returns None if forecast engine is unavailable.
        """
        if not self.forecast_engine:
            return None

        # Check if we need a fresh plan
        refresh_needed = False
        if self._morning_plan is None:
            refresh_needed = True
        elif self._morning_plan_time is None:
            refresh_needed = True
        else:
            age_min = (timestamp - self._morning_plan_time).total_seconds() / 60.0
            if age_min >= 30:
                refresh_needed = True
            # Also refresh if SOC changed significantly (e.g., mode switch happened)
            elif abs(soc_percent - self._morning_plan.current_soc_pct) > 5.0:
                refresh_needed = True

        if not refresh_needed:
            return self._morning_plan

        try:
            battery_kwh = self.config.get('battery_capacity_kwh',
                                          self.profile.capacity.total_capacity_kwh if hasattr(self.profile, 'capacity') else 30.0)
            peak_start = self.config.get('peak_start_hour', 17)

            plan = self.forecast_engine.morning_plan(
                current_soc_pct=soc_percent,
                target_soc_pct=self.target_soc,
                battery_capacity_kwh=battery_kwh,
                peak_start_hour=peak_start,
                consumption_profile=getattr(self.profile, 'consumption', None),
            )

            self._morning_plan = plan
            self._morning_plan_time = timestamp
            logger.info(f"Morning plan refreshed: {plan.to_log_str()}")
            return plan

        except Exception as e:
            logger.warning(f"Morning plan error: {e}")
            return self._morning_plan  # Return stale plan rather than None

    def enrich_state(self, state: SystemState) -> SystemState:
        """Populate rate and forecast fields on the system state."""
        state.current_tier, state.current_rate_cents = self.rates.current_tier(state.timestamp)
        state.is_peak = self.rates.is_peak(state.timestamp)
        state.hours_to_peak = self.rates.hours_to_peak(state.timestamp)
        state.peak_duration_hours = self.rates.peak_duration_hours(state.timestamp)
        state.rate_spread_cents = self.rates.rate_spread(state.timestamp)

        # Solar forecast: prefer forecast engine, fall back to learned profile
        if state.hours_to_peak is not None and state.hours_to_peak > 0:
            plan = self._get_morning_plan(state.soc_percent, state.timestamp)
            if plan is not None:
                state.forecast_solar_kwh = plan.forecast_remaining_kwh
            else:
                # Fallback: learned profile historical average
                peak_start = self.rates.next_peak_start(state.timestamp)
                if peak_start:
                    state.forecast_solar_kwh = self.profile.solar.forecast_remaining_kwh(
                        state.timestamp, peak_start.hour
                    )

        # Emergency preparedness
        override = self._load_override()
        if override and override.get('active') and override.get('type') == 'emergency_prep':
            state.emergency_prep_active = True
            state.emergency_prep_floor = override.get('floor_pct', 80.0)

        return state

    def evaluate(self, state: SystemState) -> Decision:
        """Run the decision priority stack and return the optimal action.
        
        This is the main entry point called every decision cycle.
        """
        state = self.enrich_state(state)
        self.decisions_made += 1

        # Track TOU drift every cycle (learns phantom grid→battery rate)
        self.tou_drift.observe(
            state.timestamp, state.grid_to_battery_kw,
            state.solar_kw, state.soc_percent, state.current_mode,
        )

        # --- Priority 1: Grid Offline ---
        if not state.grid_online:
            return self._decide(
                state, "self_consumption",
                "Grid offline — holding current mode, island event",
                confidence=1.0, priority=1,
                action="hold",
            )

        # --- Priority 2: Emergency Preparedness ---
        if state.emergency_prep_active:
            floor = state.emergency_prep_floor
            if state.soc_percent < floor:
                return self._decide(
                    state, "emergency_backup",
                    f"Emergency prep: SOC {state.soc_percent:.0f}% below floor {floor:.0f}% — charging",
                    confidence=1.0, priority=2,
                    action="switch_to_backup",
                    metrics={'emergency_floor': floor},
                )
            else:
                return self._decide(
                    state, "self_consumption",
                    f"Emergency prep: SOC {state.soc_percent:.0f}% ≥ floor {floor:.0f}% — self-consumption above floor",
                    confidence=0.9, priority=2,
                    action="switch_to_self_consumption",
                    metrics={'emergency_floor': floor},
                )

        # --- Priority 3: Dynamic Pricing Override (future) ---
        if state.dynamic_price_cents is not None and state.dynamic_price_cents <= 0:
            return self._decide(
                state, "emergency_backup",
                f"Dynamic pricing: {state.dynamic_price_cents}¢/kWh — free/credit power, charging",
                confidence=1.0, priority=3,
                action="switch_to_backup",
                metrics={'dynamic_price': state.dynamic_price_cents},
            )

        # --- Priority 4: Peak Rate Protection ---
        if state.is_peak:
            self._track_curtailment(state)
            return self._decide(
                state, "self_consumption",
                f"Peak rate active ({state.current_rate_cents}¢/kWh) — self-consumption, no grid purchases",
                confidence=1.0, priority=4,
                action="switch_to_self_consumption",
            )

        # --- Priority 5: Solar Curtailment Prevention ---
        if (state.soc_percent >= CURTAILMENT_SOC_THRESHOLD
                and state.solar_kw > MIN_SOLAR_PRODUCING_KW):
            curtailed = self._track_curtailment(state)
            return self._decide(
                state, "self_consumption",
                f"Battery near full ({state.soc_percent:.0f}%) with solar producing ({state.solar_kw:.1f} kW) — "
                f"self-consumption, logging curtailment",
                confidence=0.9, priority=5,
                action="switch_to_self_consumption",
                metrics={'curtailed_kw': curtailed},
            )

        # --- Priority 6: Dynamic Pricing Cheap Power (future) ---
        charge_threshold = self.config.get('dynamic_charge_threshold_cents', None)
        if (state.dynamic_price_cents is not None
                and charge_threshold is not None
                and state.dynamic_price_cents <= charge_threshold
                and state.soc_percent < self.target_soc):
            return self._decide(
                state, "emergency_backup",
                f"Dynamic pricing: {state.dynamic_price_cents}¢ ≤ {charge_threshold}¢ threshold — charging",
                confidence=0.9, priority=6,
                action="switch_to_backup",
                metrics={'dynamic_price': state.dynamic_price_cents},
            )

        # --- Priority 7: Forecast-Aware Charging Gap ---
        decision = self._evaluate_charging_gap(state)
        if decision:
            return decision

        # --- Priority 8: Solar Headroom / Consumption-Aware Self-Consumption ---
        if state.hours_to_peak is None or state.hours_to_peak > 12:
            # On non-export systems, manage SOC to maximize solar absorption
            if not self.solar_export:
                headroom_decision = self._evaluate_solar_headroom(state)
                if headroom_decision:
                    return headroom_decision

            # --- Default mode: TOU (not SC) when no peak is approaching ---
            # Self-consumption drains the battery to the reserve floor overnight,
            # then the engine has to panic-charge from grid the next morning.
            # TOU lets grid power the home while battery holds its SOC.
            #
            # The headroom logic above already handles intentional draining —
            # if SOC is too high for tomorrow's solar, _evaluate_solar_headroom
            # will put us in SC to drain to the target, then park in TOU.
            # If headroom doesn't need draining, we should just be in TOU.
            #
            # SC is only better than TOU when there's active solar to consume,
            # and that case is handled by the headroom logic returning SC/TOU
            # cycling decisions above.
            backup_reserve = self.config.get('backup_reserve_pct', 20.0)

            # If already at or below backup reserve, definitely TOU —
            # Franklin won't discharge further anyway, just let grid run
            if state.soc_percent <= backup_reserve + 2.0:
                return self._decide(
                    state, "time_of_use",
                    f"No peak approaching, SOC {state.soc_percent:.0f}% near reserve "
                    f"({backup_reserve:.0f}%) — TOU to let grid power home",
                    confidence=0.85, priority=8,
                    action="switch_to_tou",
                )

            # Default: TOU preserves battery, grid powers home
            return self._decide(
                state, "time_of_use",
                "No peak approaching — TOU (preserve battery, grid powers home)",
                confidence=0.8, priority=8,
                action="switch_to_tou",
            )

        # --- Priority 9: Default ---
        return self._decide(
            state, "self_consumption",
            "Default — self-consumption",
            confidence=0.7, priority=9,
            action="switch_to_self_consumption",
        )

    def _evaluate_solar_headroom(self, state: SystemState) -> Optional[Decision]:
        """Manage SOC to maximize solar absorption when no peak is imminent.

        When the forecast predicts more solar production during peak sun hours
        than the home will consume during those same hours, the excess tries to
        charge the battery. If the battery doesn't have enough headroom, solar
        gets curtailed (wasted on non-export systems).

        Uses peak-hours surplus estimation rather than the morning plan's
        cumulative net_to_bat, because the morning plan includes overnight
        consumption that masks the daytime solar surplus signal.

        Non-export systems only — export systems send surplus to grid for
        credit, so curtailment isn't an issue.
        """
        plan = self._get_morning_plan(state.soc_percent, state.timestamp)
        if plan is None:
            return None

        # Battery capacity
        battery_kwh = self.config.get(
            'battery_capacity_kwh',
            getattr(getattr(self.profile, 'capacity', None),
                    'total_capacity_kwh', 30.0)
        )

        # --- Estimate peak-hours solar surplus ---
        # The morning plan gives us total remaining solar forecast.
        # We need to estimate what surplus will try to charge the battery
        # during peak production hours (roughly 9am-4pm, ~7 hours).
        #
        # Instead of plan.forecast_to_battery_kwh (which uses remaining
        # day totals and goes to 0 when consumption > solar at night),
        # we estimate: surplus = total_solar - consumption_during_solar_hours
        #
        # Consumption during solar hours uses the profile's hourly rate
        # for a conservative estimate.
        solar_hours = 7.0  # Approximate peak production window
        if hasattr(self.profile, 'consumption') and hasattr(self.profile.consumption, 'avg_kwh_per_hour'):
            avg_load_kw = self.profile.consumption.avg_kwh_per_hour
        else:
            avg_load_kw = plan.expected_consumption_kwh / 24.0 if plan.expected_consumption_kwh > 0 else 2.0
        consumption_during_solar = avg_load_kw * solar_hours

        forecast_solar_kwh = plan.forecast_remaining_kwh
        peak_surplus_kwh = max(0, forecast_solar_kwh - consumption_during_solar)

        if peak_surplus_kwh <= 0:
            return None  # No surplus solar expected

        # Current headroom vs. what's needed
        headroom_kwh = (100.0 - state.soc_percent) / 100.0 * battery_kwh
        excess_kwh = peak_surplus_kwh - headroom_kwh

        if excess_kwh < HEADROOM_CURTAILMENT_THRESHOLD:
            return None  # Enough headroom already

        # --- Calculate drain target ---
        # Leave room for all forecast peak-hours surplus
        target_soc = 100.0 - (peak_surplus_kwh / battery_kwh * 100.0)

        # Buffer for TOU drift (measured phantom grid charging)
        # Estimate ~4 hours of TOU mode before solar production ramps
        drift_hours = 4.0
        drift_buffer_pct = self.tou_drift.drift_rate_pct_per_hour * drift_hours
        drift_buffer_pct = max(drift_buffer_pct, HEADROOM_DEFAULT_DRAIN_BUFFER_PCT)
        target_soc -= drift_buffer_pct

        # Enforce minimum floor
        min_floor = max(
            HEADROOM_MIN_SOC_FLOOR,
            self.config.get('backup_reserve_pct', 20.0),
        )
        target_soc = max(target_soc, min_floor)

        # Safety: don't drain if peak is too close to recover
        if state.hours_to_peak is not None and state.hours_to_peak < HEADROOM_MIN_HOURS_TO_PEAK:
            return None

        metrics = {
            'headroom_target_soc': round(target_soc, 1),
            'current_soc': round(state.soc_percent, 1),
            'peak_surplus_kwh': round(peak_surplus_kwh, 1),
            'forecast_solar_kwh': round(forecast_solar_kwh, 1),
            'consumption_during_solar_kwh': round(consumption_during_solar, 1),
            'headroom_kwh': round(headroom_kwh, 1),
            'excess_kwh': round(excess_kwh, 1),
            'drift_buffer_pct': round(drift_buffer_pct, 1),
            'tou_drift_rate_pct_h': self.tou_drift.drift_rate_pct_per_hour,
            'tou_drift_samples': self.tou_drift.sample_count,
            'forecast_source': plan.forecast_source,
            'solar_export': False,
        }

        if state.soc_percent > target_soc + drift_buffer_pct:
            # SOC is above target+buffer — drain via self-consumption
            return self._decide(
                state, "self_consumption",
                f"Solar headroom: draining {state.soc_percent:.0f}% → "
                f"{target_soc:.0f}% target "
                f"({peak_surplus_kwh:.0f} kWh peak surplus forecast, "
                f"{excess_kwh:.0f} kWh would be curtailed, "
                f"drift buffer {drift_buffer_pct:.0f}%)",
                confidence=0.8, priority=8,
                action="switch_to_self_consumption",
                metrics=metrics,
            )

        elif state.soc_percent <= target_soc:
            # At or below target — park in TOU to hold position
            return self._decide(
                state, "time_of_use",
                f"Solar headroom: SOC {state.soc_percent:.0f}% at "
                f"target {target_soc:.0f}% — parking in TOU, "
                f"waiting for solar",
                confidence=0.8, priority=8,
                action="switch_to_tou",
                metrics=metrics,
            )

        else:
            # In the buffer zone (between target and target+buffer)
            # Hold current mode — don't flap
            return self._decide(
                state, state.current_mode,
                f"Solar headroom: SOC {state.soc_percent:.0f}% in buffer zone "
                f"({target_soc:.0f}%-{target_soc + drift_buffer_pct:.0f}%) — holding",
                confidence=0.75, priority=8,
                action="hold",
                metrics=metrics,
            )

    def _evaluate_charging_gap(self, state: SystemState) -> Optional[Decision]:
        """Priority 7: Calculate if we need grid charging to reach target SOC by peak.
        
        When the solar forecast engine is available, uses morning_plan() for a
        weather-aware gap calculation with a dynamic ceiling — only grid charges
        the amount solar can't provide, leaving headroom for free solar.
        
        Fallback: learned profile historical averages (original logic).
        
        gap_kwh = target_kwh - current_kwh - forecast_solar_to_battery_kwh
        If gap > 0 and current rate is the cheapest before peak: charge.
        If gap ≤ 0: solar will handle it.
        """
        if state.hours_to_peak is None:
            return None

        if state.hours_to_peak <= 0:
            return None  # Already in peak, handled by Priority 4

        if state.hours_to_peak > 12:
            return None  # Too far away to plan

        # --- Try forecast-aware morning plan first ---
        plan = self._get_morning_plan(state.soc_percent, state.timestamp)
        if plan is not None:
            return self._evaluate_gap_with_plan(state, plan)

        # --- Fallback: original learned-profile logic ---
        return self._evaluate_gap_legacy(state)

    def _evaluate_gap_with_plan(self, state: SystemState, plan: 'MorningPlan') -> Optional[Decision]:
        """P7 with forecast engine — uses morning_plan ceiling for smarter charging.
        
        EB deferral philosophy: Emergency Backup is aggressive (~8kW grid charging).
        It charges fast — a typical gap takes under an hour. There is never a reason
        to trigger EB hours before peak. The engine recalculates every 30-minute cycle,
        so deferring is always safe: TOU drift and solar production may naturally shrink
        the gap, and EB can catch up later. EB should only fire when the time buffer
        is genuinely tight — i.e., we're close enough to peak that we need to act NOW.
        """
        gap_kwh = plan.gap_kwh
        ceiling_pct = plan.morning_ceiling_pct

        metrics = {
            'current_kwh': plan.current_kwh,
            'target_kwh': plan.target_kwh,
            'forecast_solar_kwh': plan.forecast_remaining_kwh,
            'expected_consumption_kwh': plan.expected_consumption_kwh,
            'net_solar_to_battery_kwh': plan.forecast_to_battery_kwh,
            'gap_kwh': round(gap_kwh, 1),
            'morning_ceiling_pct': round(ceiling_pct, 1),
            'forecast_source': plan.forecast_source,
            'weather_score': plan.weather_score,
            'hours_to_peak': round(state.hours_to_peak, 1),
        }

        # Solar surplus — skip grid charging entirely
        if gap_kwh <= 0:
            return self._decide(
                state, "self_consumption",
                f"Solar surplus: {plan.recommendation}",
                confidence=0.85 if plan.forecast_source.startswith('forecast_solar') else 0.7,
                priority=7, action="hold", metrics=metrics,
            )

        # Tiny gap — not worth a mode switch
        if gap_kwh < 1.0:
            return self._decide(
                state, "self_consumption",
                f"Tiny gap ({gap_kwh:.1f} kWh) — solar/natural will cover. {plan.recommendation}",
                confidence=0.8, priority=7, action="hold", metrics=metrics,
            )

        # Small gap with active solar and plenty of time
        if gap_kwh < 2.0 and state.solar_kw > 0.3 and state.hours_to_peak > 4:
            return self._decide(
                state, "self_consumption",
                f"Small gap ({gap_kwh:.1f} kWh) with solar producing ({state.solar_kw:.1f} kW) "
                f"and {state.hours_to_peak:.1f}h to peak — letting solar handle it",
                confidence=0.75, priority=7, action="hold", metrics=metrics,
            )

        # Already at or above the forecast ceiling — solar takes it from here
        if state.soc_percent >= ceiling_pct:
            return self._decide(
                state, "self_consumption",
                f"SOC {state.soc_percent:.0f}% ≥ forecast ceiling {ceiling_pct:.0f}% — "
                f"solar fills the rest. {plan.recommendation}",
                confidence=0.85, priority=7, action="switch_to_self_consumption",
                metrics=metrics,
            )

        # Need to grid charge — but only to the ceiling, not target_soc
        # Is now the cheapest time?
        cheapest_tier, cheapest_rate = self.rates.cheapest_rate_before_peak(state.timestamp)
        if state.current_rate_cents <= cheapest_rate:
            charge_time_hours = self.profile.time_to_charge_kwh(
                state.soc_percent, ceiling_pct
            ) if hasattr(self.profile, 'time_to_charge_kwh') else gap_kwh / 5.0

            # --- EB time deferral (v4.0.4) ---
            # Calculate how much buffer we have beyond what's needed to charge.
            # EB is aggressive and charges fast. Don't start hours early —
            # stay in TOU and let TOU drift + solar shrink the gap naturally.
            # The engine recalculates every cycle, so this is always safe.
            buffer_hours = state.hours_to_peak - charge_time_hours
            min_buffer = max(EB_DEFERRAL_MIN_BUFFER_HOURS, charge_time_hours)

            # Add timing metrics for log visibility
            metrics['charge_time_hours'] = round(charge_time_hours, 2)
            metrics['buffer_hours'] = round(buffer_hours, 1)
            metrics['min_buffer_hours'] = round(min_buffer, 1)

            # Plenty of time — no rush, defer EB entirely.
            # Stay in TOU where grid powers home and any TOU drift
            # may slowly charge the battery, shrinking the gap for free.
            if buffer_hours > min_buffer:
                return self._decide(
                    state, "time_of_use",
                    f"Forecast gap ({gap_kwh:.1f} kWh, {charge_time_hours:.1f}h to charge) "
                    f"but {buffer_hours:.1f}h buffer vs {min_buffer:.1f}h needed — "
                    f"no rush, deferring EB. Reassess next cycle.",
                    confidence=0.8, priority=7, action="switch_to_tou",
                    metrics=metrics,
                )

            # Buffer is getting tight but solar is actively producing —
            # give solar a chance to close the gap before resorting to EB.
            if (state.solar_kw > MIN_SOLAR_PRODUCING_KW
                    and buffer_hours > max(1.0, charge_time_hours)):
                return self._decide(
                    state, "self_consumption",
                    f"Forecast gap ({gap_kwh:.1f} kWh, {charge_time_hours:.1f}h to charge) "
                    f"but solar producing ({state.solar_kw:.1f} kW) with "
                    f"{buffer_hours:.1f}h buffer — deferring to let solar fill",
                    confidence=0.75, priority=7, action="hold",
                    metrics=metrics,
                )

            # Time to charge — buffer is tight, need to act now
            if charge_time_hours <= state.hours_to_peak:
                return self._decide(
                    state, "emergency_backup",
                    f"Forecast gap: {gap_kwh:.1f} kWh → charge to {ceiling_pct:.0f}% "
                    f"(not {self.target_soc:.0f}%), solar fills the rest. "
                    f"Buffer tight ({buffer_hours:.1f}h). {plan.recommendation}",
                    confidence=0.85, priority=7, action="switch_to_backup",
                    metrics=metrics,
                )
            else:
                return self._decide(
                    state, "emergency_backup",
                    f"Forecast gap: {gap_kwh:.1f} kWh, only {state.hours_to_peak:.1f}h to peak "
                    f"(need {charge_time_hours:.1f}h) — charging urgently to {ceiling_pct:.0f}%",
                    confidence=0.95, priority=7, action="switch_to_backup",
                    metrics=metrics,
                )
        else:
            return self._decide(
                state, "self_consumption",
                f"Forecast gap ({gap_kwh:.1f} kWh) but waiting for cheaper rate "
                f"({cheapest_tier} @ {cheapest_rate}¢ vs current {state.current_rate_cents}¢)",
                confidence=0.7, priority=7, action="switch_to_self_consumption",
                metrics=metrics,
            )

    def _evaluate_gap_legacy(self, state: SystemState) -> Optional[Decision]:
        """P7 fallback — original learned-profile-based gap logic (no forecast engine).
        
        Same EB deferral philosophy as _evaluate_gap_with_plan — never rush to EB
        when there's plenty of time. The engine recalculates every cycle.
        """

        cap = self.profile.capacity
        current_kwh = cap.kwh_at_soc(state.soc_percent)
        target_kwh = cap.kwh_at_soc(self.target_soc)

        # Expected consumption between now and peak
        peak_start = self.rates.next_peak_start(state.timestamp)
        if peak_start is None:
            return None
        expected_consumption_kwh = self.profile.consumption.expected_kwh(
            state.timestamp, peak_start
        )

        # Forecast solar contribution (already accounts for house array only)
        forecast_solar_kwh = state.forecast_solar_kwh

        # Solar that actually charges battery = solar - consumption (when positive)
        # Simplified: assume surplus solar goes to battery
        net_solar_to_battery = max(0, forecast_solar_kwh - expected_consumption_kwh)

        # Gap calculation
        gap_kwh = target_kwh - current_kwh - net_solar_to_battery

        # Time needed to charge the gap
        if gap_kwh > 0:
            charge_time_hours = self.profile.time_to_charge_kwh(
                state.soc_percent, self.target_soc
            )
        else:
            charge_time_hours = 0

        metrics = {
            'current_kwh': round(current_kwh, 1),
            'target_kwh': round(target_kwh, 1),
            'forecast_solar_kwh': round(forecast_solar_kwh, 1),
            'expected_consumption_kwh': round(expected_consumption_kwh, 1),
            'net_solar_to_battery_kwh': round(net_solar_to_battery, 1),
            'gap_kwh': round(gap_kwh, 1),
            'charge_time_hours': round(charge_time_hours, 2),
            'hours_to_peak': round(state.hours_to_peak, 1),
        }

        if gap_kwh <= 0:
            return self._decide(
                state, "self_consumption",
                f"No charging gap — solar forecast ({forecast_solar_kwh:.1f} kWh) covers "
                f"the {target_kwh - current_kwh:.1f} kWh needed",
                confidence=0.8, priority=7,
                action="hold",
                metrics=metrics,
            )

        # Small gap guard: if gap is tiny and conditions are favorable, let solar handle it
        # - Gap < 2 kWh AND solar currently producing AND plenty of time to peak
        # - OR gap < 1 kWh regardless (not worth a mode switch for ~5 min of grid charging)
        if gap_kwh < 1.0:
            return self._decide(
                state, "self_consumption",
                f"Tiny charging gap ({gap_kwh:.1f} kWh) — not worth grid charging, solar/natural will cover",
                confidence=0.8, priority=7,
                action="hold",
                metrics=metrics,
            )
        if gap_kwh < 2.0 and state.solar_kw > 0.3 and state.hours_to_peak > 4:
            return self._decide(
                state, "self_consumption",
                f"Small gap ({gap_kwh:.1f} kWh) with solar producing ({state.solar_kw:.1f} kW) "
                f"and {state.hours_to_peak:.1f}h to peak — letting solar handle it",
                confidence=0.75, priority=7,
                action="hold",
                metrics=metrics,
            )

        # Is now the cheapest time to charge before peak?
        cheapest_tier, cheapest_rate = self.rates.cheapest_rate_before_peak(state.timestamp)
        if state.current_rate_cents <= cheapest_rate:
            # --- EB time deferral (v4.0.4) ---
            # Same logic as _evaluate_gap_with_plan — don't rush to EB.
            buffer_hours = state.hours_to_peak - charge_time_hours
            min_buffer = max(EB_DEFERRAL_MIN_BUFFER_HOURS, charge_time_hours)

            metrics['buffer_hours'] = round(buffer_hours, 1)
            metrics['min_buffer_hours'] = round(min_buffer, 1)

            # Plenty of time — defer EB, stay in TOU
            if buffer_hours > min_buffer:
                return self._decide(
                    state, "time_of_use",
                    f"Charging gap ({gap_kwh:.1f} kWh, {charge_time_hours:.1f}h to charge) "
                    f"but {buffer_hours:.1f}h buffer vs {min_buffer:.1f}h needed — "
                    f"no rush, deferring EB. Reassess next cycle.",
                    confidence=0.8, priority=7, action="switch_to_tou",
                    metrics=metrics,
                )

            # Solar-aware deferral — tighter buffer but solar still helping
            if (state.solar_kw > MIN_SOLAR_PRODUCING_KW
                    and buffer_hours > max(1.0, charge_time_hours)):
                return self._decide(
                    state, "self_consumption",
                    f"Charging gap ({gap_kwh:.1f} kWh, {charge_time_hours:.1f}h to charge) "
                    f"but solar producing ({state.solar_kw:.1f} kW) with "
                    f"{buffer_hours:.1f}h buffer — deferring to let solar fill",
                    confidence=0.75, priority=7, action="hold",
                    metrics=metrics,
                )

            # Verify we have enough time
            if charge_time_hours <= state.hours_to_peak:
                return self._decide(
                    state, "emergency_backup",
                    f"Charging gap: {gap_kwh:.1f} kWh needed, "
                    f"{charge_time_hours:.1f}h to charge, "
                    f"{state.hours_to_peak:.1f}h until peak — "
                    f"buffer tight ({buffer_hours:.1f}h), charging at {state.current_rate_cents}¢/kWh",
                    confidence=0.85, priority=7,
                    action="switch_to_backup",
                    metrics=metrics,
                )
            else:
                # Not enough time even at max rate — charge urgently
                return self._decide(
                    state, "emergency_backup",
                    f"Charging gap: {gap_kwh:.1f} kWh needed but only "
                    f"{state.hours_to_peak:.1f}h until peak (need {charge_time_hours:.1f}h) — "
                    f"charging urgently",
                    confidence=0.95, priority=7,
                    action="switch_to_backup",
                    metrics=metrics,
                )
        else:
            # There's a cheaper window coming — wait
            return self._decide(
                state, "self_consumption",
                f"Charging gap exists ({gap_kwh:.1f} kWh) but waiting for cheaper rate "
                f"({cheapest_tier} @ {cheapest_rate}¢ vs current {state.current_rate_cents}¢)",
                confidence=0.7, priority=7,
                action="switch_to_self_consumption",
                metrics=metrics,
            )

    def _track_curtailment(self, state: SystemState) -> float:
        """Track solar curtailment when battery is full and solar is producing.
        Returns estimated curtailed kW this interval."""
        if state.soc_percent < CURTAILMENT_SOC_THRESHOLD:
            return 0.0
        if state.solar_kw <= MIN_SOLAR_PRODUCING_KW:
            return 0.0

        # Curtailed = solar above what the house is consuming (excess that can't be stored)
        curtailed_kw = max(0, state.solar_kw - state.home_load_kw)
        if curtailed_kw > 0:
            # Estimate kWh for this interval
            interval_hours = self.config.get('decision_interval_minutes', 15) / 60.0
            curtailed_kwh = curtailed_kw * interval_hours
            value_cents = curtailed_kwh * state.current_rate_cents

            self.curtailed_kwh += curtailed_kwh
            self.curtailed_value_cents += value_cents

            logger.info(f"Solar curtailment: {curtailed_kw:.2f} kW ({curtailed_kwh:.3f} kWh, "
                       f"{value_cents:.1f}¢) — session total: {self.curtailed_kwh:.2f} kWh")

        return curtailed_kw

    def _decide(self, state: SystemState, mode: str, reason: str,
                confidence: float, priority: int,
                action: Optional[str] = None,
                metrics: Optional[dict] = None) -> Decision:
        """Create a Decision, resolving the action if not specified."""
        if action is None:
            if mode == state.current_mode:
                action = "hold"
            elif mode == "emergency_backup":
                action = "switch_to_backup"
            elif mode == "time_of_use":
                action = "switch_to_tou"
            else:
                action = "switch_to_self_consumption"

        # Check cooldown
        if action in ("switch_to_backup", "switch_to_self_consumption", "switch_to_tou"):
            if self.last_mode_switch and self.last_decision:
                elapsed = (state.timestamp - self.last_mode_switch).total_seconds()
                if elapsed < MODE_SWITCH_COOLDOWN_S and mode != state.current_mode:
                    action = "hold"
                    reason += f" (cooldown: {int(MODE_SWITCH_COOLDOWN_S - elapsed)}s remaining)"

        # Track mode switches
        if action in ("switch_to_backup", "switch_to_self_consumption", "switch_to_tou"):
            self.last_mode_switch = state.timestamp

        decision = Decision(
            mode=mode,
            reason=reason,
            confidence=confidence,
            action=action,
            priority_level=priority,
            metrics=metrics or {},
        )

        self.last_decision = decision
        logger.info(f"Decision [P{priority}]: {action} → {mode} | {reason}")
        return decision

    def _load_override(self) -> Optional[dict]:
        """Load override.json for emergency preparedness etc."""
        override_path = self.config.get('override_path', 'data/override.json')
        if os.path.exists(override_path):
            try:
                with open(override_path, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return None

    def get_status(self) -> dict:
        """Return engine status for dashboard/logging."""
        status = {
            'decisions_made': self.decisions_made,
            'curtailed_kwh': round(self.curtailed_kwh, 3),
            'curtailed_value_cents': round(self.curtailed_value_cents, 1),
            'target_soc': self.target_soc,
            'last_decision': self.last_decision.to_dict() if self.last_decision else None,
            'forecast_engine': self.forecast_engine is not None,
            'solar_export': self.solar_export,
            'tou_drift': self.tou_drift.to_dict(),
        }
        if self._morning_plan:
            status['morning_plan'] = {
                'ceiling_pct': self._morning_plan.morning_ceiling_pct,
                'gap_kwh': self._morning_plan.gap_kwh,
                'forecast_source': self._morning_plan.forecast_source,
                'weather_score': self._morning_plan.weather_score,
                'updated': self._morning_plan.timestamp,
            }
        return status


# ---------------------------------------------------------------------------
# Factory — Create engine from config
# ---------------------------------------------------------------------------

def create_engine(
    csv_path: str = 'data/continuous_monitoring.csv',
    profile_path: str = 'data/system_profile.json',
    rate_schedule_path: str = 'data/rate_schedule.json',
    config: Optional[dict] = None,
) -> AdaptiveEngine:
    """Create an AdaptiveEngine with loaded or freshly built profile.
    
    Call this from scheduler.py to initialize the engine.
    """
    config = config or {}

    # Load or build system profile
    profile = load_profile(profile_path)
    if profile is None:
        logger.info("No saved profile found — building from CSV history")
        profile = build_profile(csv_path, config)
        save_profile(profile, profile_path)

    # Load rate schedule
    rate_schedule = load_rate_schedule(rate_schedule_path)

    # Ensure battery and peak config are available for forecast engine
    if 'battery_capacity_kwh' not in config:
        cap = getattr(profile, 'capacity', None)
        if cap and hasattr(cap, 'total_capacity_kwh'):
            config['battery_capacity_kwh'] = cap.total_capacity_kwh
    if 'peak_start_hour' not in config:
        config['peak_start_hour'] = rate_schedule.peak_start_hour if hasattr(rate_schedule, 'peak_start_hour') else 17

    # Create engine
    target_soc = config.get('target_soc', DEFAULT_TARGET_SOC)
    engine = AdaptiveEngine(profile, rate_schedule, target_soc, config)

    return engine


# ---------------------------------------------------------------------------
# CLI — Run standalone for testing
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys

    configure_logging()

    csv_path = sys.argv[1] if len(sys.argv) > 1 else 'data/continuous_monitoring.csv'

    config = {
        'battery_count': 2,
        'capacity_per_battery_kwh': 13.6,
        'backup_reserve_pct': 20,
        'target_soc': 95.0,
        'decision_interval_minutes': 15,
    }

    engine = create_engine(
        csv_path=csv_path,
        profile_path='data/system_profile.json',
        rate_schedule_path='data/rate_schedule.json',
        config=config,
    )

    # Simulate decisions at various times and SOC levels
    print("\n" + "=" * 70)
    print("ADAPTIVE ENGINE — SCENARIO TESTS")
    print("=" * 70)

    scenarios = [
        # (description, timestamp, soc, solar, grid, battery, load, grid_online, mode)
        ("Early morning, low SOC, 13h to peak",
         "2026-02-19 04:00:00", 35.0, 0.0, 1.2, 0.0, 1.2, True, "self_consumption"),
        ("Morning, moderate SOC, solar starting",
         "2026-02-19 09:00:00", 45.0, 0.8, 0.5, -0.6, 0.7, True, "self_consumption"),
        ("Midday, good solar, SOC climbing",
         "2026-02-19 12:00:00", 60.0, 2.5, 0.0, -1.8, 0.7, True, "self_consumption"),
        ("3 PM, SOC=70, 2h to peak, solar fading",
         "2026-02-19 15:00:00", 70.0, 0.3, 0.5, 0.0, 0.8, True, "self_consumption"),
        ("4 PM, SOC=75, 1h to peak, no solar",
         "2026-02-19 16:00:00", 75.0, 0.0, 1.0, 0.0, 1.0, True, "self_consumption"),
        ("5:30 PM, IN PEAK, SOC=90",
         "2026-02-19 17:30:00", 90.0, 0.0, 0.0, 2.5, 2.5, True, "self_consumption"),
        ("6 PM, IN PEAK, SOC=40 (bad day)",
         "2026-02-19 18:00:00", 40.0, 0.0, 0.0, 3.0, 3.0, True, "self_consumption"),
        ("9 PM, post-peak, low SOC",
         "2026-02-19 21:00:00", 25.0, 0.0, 2.0, 0.0, 2.0, True, "self_consumption"),
        ("Saturday 10 AM, no peak today",
         "2026-02-21 10:00:00", 50.0, 1.5, 0.0, -1.0, 0.5, True, "self_consumption"),
        ("Grid offline!",
         "2026-02-19 14:00:00", 60.0, 1.5, 0.0, -1.0, 0.5, False, "self_consumption"),
        ("Battery full, solar still producing",
         "2026-02-19 13:00:00", 98.0, 2.8, 0.0, 0.0, 1.2, True, "self_consumption"),
    ]

    for desc, ts_str, soc, solar, grid, battery, load, grid_on, mode in scenarios:
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        state = SystemState(
            timestamp=ts,
            soc_percent=soc,
            solar_kw=solar,
            grid_kw=grid,
            battery_kw=battery,
            home_load_kw=load,
            grid_online=grid_on,
            current_mode=mode,
        )

        decision = engine.evaluate(state)
        print(f"\n{'─' * 70}")
        print(f"SCENARIO: {desc}")
        print(f"  Time: {ts_str} ({ts.strftime('%A')})")
        print(f"  SOC: {soc}% | Solar: {solar} kW | Load: {load} kW | Grid: {'ON' if grid_on else 'OFF'}")
        print(f"  Rate: {state.current_tier} @ {state.current_rate_cents}¢ | "
              f"Peak: {state.is_peak} | H2P: {state.hours_to_peak}")
        print(f"  ──────────")
        print(f"  DECISION [P{decision.priority_level}]: {decision.action}")
        print(f"  Mode: {decision.mode}")
        print(f"  Reason: {decision.reason}")
        print(f"  Confidence: {decision.confidence}")
        if decision.metrics:
            for k, v in decision.metrics.items():
                print(f"    {k}: {v}")

    # Engine status
    status = engine.get_status()
    print(f"\n{'=' * 70}")
    print(f"ENGINE STATUS")
    print(f"  Decisions made: {status['decisions_made']}")
    print(f"  Curtailed solar: {status['curtailed_kwh']} kWh ({status['curtailed_value_cents']}¢)")
