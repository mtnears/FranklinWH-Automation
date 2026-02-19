#!/usr/bin/env python3
"""
adaptive_engine.py — Adaptive Decision Engine for FranklinWH Battery Automation v4.0

Replaces smart_decision.py's should_charge_from_grid() with a continuous
optimization engine that asks: "What is the optimal mode for this system
right now?" using forecasts, learned system behavior, rate awareness,
and reserve constraints.

The engine runs every decision cycle (configured interval) and produces
a Decision: hold, switch_to_backup (grid charge), or switch_to_self_consumption.

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
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

from system_profile import SystemProfile, load_profile, build_profile, save_profile
from rate_schedule import RateSchedule, load_rate_schedule

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
    action: str                     # "hold", "switch_to_backup", "switch_to_self_consumption"
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

        # Cumulative metrics for current session
        self.curtailed_kwh = 0.0
        self.curtailed_value_cents = 0.0
        self.decisions_made = 0

        logger.info(f"AdaptiveEngine initialized: target_soc={target_soc}%, "
                    f"rate_schedule={rate_schedule.name}")

    def enrich_state(self, state: SystemState) -> SystemState:
        """Populate rate and forecast fields on the system state."""
        state.current_tier, state.current_rate_cents = self.rates.current_tier(state.timestamp)
        state.is_peak = self.rates.is_peak(state.timestamp)
        state.hours_to_peak = self.rates.hours_to_peak(state.timestamp)
        state.peak_duration_hours = self.rates.peak_duration_hours(state.timestamp)
        state.rate_spread_cents = self.rates.rate_spread(state.timestamp)

        # Solar forecast: use learned profile as fallback
        if state.hours_to_peak is not None and state.hours_to_peak > 0:
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

        # --- Priority 8: Consumption-Aware Self-Consumption ---
        if state.hours_to_peak is None or state.hours_to_peak > 12:
            # No peak coming soon — just self-consume
            return self._decide(
                state, "self_consumption",
                "No peak approaching — self-consumption",
                confidence=0.8, priority=8,
                action="switch_to_self_consumption",
            )

        # --- Priority 9: Default ---
        return self._decide(
            state, "self_consumption",
            "Default — self-consumption",
            confidence=0.7, priority=9,
            action="switch_to_self_consumption",
        )

    def _evaluate_charging_gap(self, state: SystemState) -> Optional[Decision]:
        """Priority 7: Calculate if we need grid charging to reach target SOC by peak.
        
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
                action="switch_to_self_consumption",
                metrics=metrics,
            )

        # Is now the cheapest time to charge before peak?
        cheapest_tier, cheapest_rate = self.rates.cheapest_rate_before_peak(state.timestamp)
        if state.current_rate_cents <= cheapest_rate:
            # Verify we have enough time
            if charge_time_hours <= state.hours_to_peak:
                return self._decide(
                    state, "emergency_backup",
                    f"Charging gap: {gap_kwh:.1f} kWh needed, "
                    f"{charge_time_hours:.1f}h to charge, "
                    f"{state.hours_to_peak:.1f}h until peak — charging at {state.current_rate_cents}¢/kWh",
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
            if mode == state.current_mode or mode == "self_consumption":
                action = "hold" if mode == state.current_mode else "switch_to_self_consumption"
            else:
                action = "switch_to_backup"

        # Check cooldown
        if action in ("switch_to_backup", "switch_to_self_consumption"):
            if self.last_mode_switch and self.last_decision:
                elapsed = (state.timestamp - self.last_mode_switch).total_seconds()
                if elapsed < MODE_SWITCH_COOLDOWN_S and mode != state.current_mode:
                    action = "hold"
                    reason += f" (cooldown: {int(MODE_SWITCH_COOLDOWN_S - elapsed)}s remaining)"

        # Track mode switches
        if action in ("switch_to_backup", "switch_to_self_consumption"):
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
        return {
            'decisions_made': self.decisions_made,
            'curtailed_kwh': round(self.curtailed_kwh, 3),
            'curtailed_value_cents': round(self.curtailed_value_cents, 1),
            'target_soc': self.target_soc,
            'last_decision': self.last_decision.to_dict() if self.last_decision else None,
        }


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

    # Create engine
    target_soc = config.get('target_soc', DEFAULT_TARGET_SOC)
    engine = AdaptiveEngine(profile, rate_schedule, target_soc, config)

    return engine


# ---------------------------------------------------------------------------
# CLI — Run standalone for testing
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

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
