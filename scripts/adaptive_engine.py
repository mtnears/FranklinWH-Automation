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
  8. Solar headroom + post-peak/weekend solar discharge + default TOU
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

from system_profile import SystemProfile, SystemCapacity, load_profile, save_profile
from rate_schedule import RateSchedule, load_rate_schedule

try:
    from solar_forecast import get_forecast_engine, SolarForecastEngine, MorningPlan
    HAS_SOLAR_FORECAST = True
except ImportError:
    HAS_SOLAR_FORECAST = False

try:
    import db as db_mod
    HAS_DB = True
except ImportError:
    HAS_DB = False

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

# Post-peak / non-peak solar discharge — burn free solar energy instead of TOU
# Applies to: weekday evenings after peak ends, AND weekends/non-peak days
# Skip entirely for export systems (they sell surplus for credit)
POST_PEAK_WINDOW_HOURS = 10.0          # Hours after peak end to consider (covers 8PM-6AM)
POST_PEAK_MIN_SOLAR_EXCESS_KWH = 1.0  # Minimum solar excess worth discharging
POST_PEAK_DISCHARGE_FLOOR_PCT = 40.0  # Never discharge below this SOC
POST_PEAK_MIN_SOC_ABOVE_RESERVE = 10.0  # Must be this many % above reserve to bother

# Taper-aware ceiling — hard cap on grid charging target SOC.
# Above this SOC, battery charge rate tapers significantly on Franklin systems,
# causing solar curtailment on non-export systems. Grid charging above the taper
# knee wastes time at reduced rate when solar could fill the same space more
# efficiently during peak production hours.
# Adjustable: lower if observing curtailment above 80%, raise if peak discharge
# consistently needs more headroom. Overridden by env TAPER_CEILING_PCT.
TAPER_CEILING_PCT = float(os.environ.get('TAPER_CEILING_PCT', '85'))

# Pre-peak one-way gate — once within this many hours of peak, if the engine
# has already decided SOC is adequate and moved to TOU/SC, don't flip back to
# EB. The reasoning: if the gap calc was satisfied at the previous cycle, a
# partial EB burst in the last 30 min adds minimal SOC and isn't worth the
# mode switch and grid cost. Exception: if already in EB from prior cycle
# and still below ceiling, let it finish.
PRE_PEAK_GATE_HOURS = 0.5  # 30 minutes before peak


# ---------------------------------------------------------------------------
# TOU Drift Tracker
# ---------------------------------------------------------------------------

# DB path for drift persistence (same DB as all other tables)
_DRIFT_DB_PATH = os.path.join(os.getenv('DATA_DIR', '/app/data'), 'franklin.db')

_DRIFT_DDL = """
CREATE TABLE IF NOT EXISTS tou_drift_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    grid_to_battery_kw REAL NOT NULL,
    solar_kw REAL,
    soc_pct REAL,
    drift_rate_pct_per_hour REAL,
    mode TEXT DEFAULT 'time_of_use'
);
CREATE INDEX IF NOT EXISTS idx_tou_drift_ts ON tou_drift_log(timestamp);
"""


def _drift_db_init():
    """Ensure tou_drift_log table exists."""
    try:
        import sqlite3
        conn = sqlite3.connect(_DRIFT_DB_PATH, timeout=10)
        conn.executescript(_DRIFT_DDL)
        conn.close()
    except Exception as e:
        logger.debug(f"Drift DB init: {e}")


def _drift_db_store(timestamp: str, grid_to_bat_kw: float,
                    solar_kw: float, soc_pct: float,
                    drift_rate_pct_h: float):
    """Write a drift observation to the database."""
    try:
        import sqlite3
        conn = sqlite3.connect(_DRIFT_DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "INSERT INTO tou_drift_log "
            "(timestamp, grid_to_battery_kw, solar_kw, soc_pct, drift_rate_pct_per_hour) "
            "VALUES (?, ?, ?, ?, ?)",
            (timestamp, round(grid_to_bat_kw, 4), round(solar_kw, 3),
             round(soc_pct, 1), round(drift_rate_pct_h, 4)),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"Drift DB store: {e}")


def _drift_db_load_recent(days: int = 7) -> tuple:
    """Load rolling average drift rate from recent observations.

    Returns (avg_drift_rate_pct_per_hour, avg_drift_rate_kw, sample_count).
    Only uses observations where grid_to_battery_kw > 0 (actual drift).
    """
    try:
        import sqlite3
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        conn = sqlite3.connect(_DRIFT_DB_PATH, timeout=10)

        # Average of per-observation drift rates (pct/hr)
        row = conn.execute(
            "SELECT AVG(drift_rate_pct_per_hour), AVG(grid_to_battery_kw), COUNT(*) "
            "FROM tou_drift_log "
            "WHERE timestamp >= ? AND drift_rate_pct_per_hour > 0",
            (cutoff,),
        ).fetchone()
        conn.close()

        if row and row[2] > 0:
            return (row[0] or 0.0, row[1] or 0.0, row[2])
    except Exception as e:
        logger.debug(f"Drift DB load: {e}")
    return (0.0, 0.0, 0)


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

    Observations are persisted to the tou_drift_log SQLite table so the
    rolling average survives container restarts and engine re-initialization.
    On init, loads the 7-day average from the database as the starting point.
    """

    def __init__(self, max_samples: int = 48, load_from_db: bool = True):
        """
        Args:
            max_samples: In-memory rolling window size (48 = 24h at 30-min intervals)
            load_from_db: Whether to seed from database on init
        """
        self.max_samples = max_samples
        self._samples: list = []           # (timestamp, grid_to_bat_kw, solar_kw, soc_pct)
        self._drift_rate_kw: float = 0.0   # Rolling avg grid→bat in TOU
        self._drift_rate_pct_per_hour: float = 0.0
        self._last_soc: Optional[float] = None
        self._last_time: Optional[datetime] = None
        self._db_sample_count: int = 0     # Historical samples from DB

        # Initialize DB table and load historical average
        if load_from_db:
            _drift_db_init()
            avg_pct_h, avg_kw, count = _drift_db_load_recent(days=7)
            if count > 0:
                self._drift_rate_pct_per_hour = avg_pct_h
                self._drift_rate_kw = avg_kw
                self._db_sample_count = count
                logger.info(f"TOU drift loaded from DB: {avg_pct_h:.2f}%/hr, "
                            f"{avg_kw:.3f}kW avg, {count} samples (7d)")

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
        drift_pct_h = 0.0
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

        # Persist to database (every observation in TOU mode)
        _drift_db_store(
            timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            grid_to_bat_kw, solar_kw, soc_pct, drift_pct_h,
        )

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
        return len(self._samples) + self._db_sample_count

    def expected_drift_kwh(self, hours: float, battery_capacity_kwh: float) -> float:
        """Estimate kWh of drift charging over a given period."""
        return self._drift_rate_pct_per_hour * hours / 100.0 * battery_capacity_kwh

    def to_dict(self) -> dict:
        return {
            'drift_rate_kw': self.drift_rate_kw,
            'drift_rate_pct_per_hour': self.drift_rate_pct_per_hour,
            'sample_count': self.sample_count,
            'db_samples_loaded': self._db_sample_count,
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

        # Solar discharge tracking (post-peak + weekend)
        self.solar_discharge_kwh = 0.0          # kWh discharged via solar discharge feature
        self.solar_discharge_value_cents = 0.0   # Value of avoided grid import
        self.solar_discharge_activations = 0     # Number of cycles spent in SC for solar discharge
        self._solar_discharge_target_soc: Optional[float] = None  # Current target if active
        self._solar_discharge_start_soc: Optional[float] = None   # SOC when discharge started

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
                solar_export=self.solar_export,
                tou_drift_kwh=self._estimate_drift_to_peak(peak_start),
            )

            self._morning_plan = plan
            self._morning_plan_time = timestamp
            logger.info(f"Morning plan refreshed: {plan.to_log_str()}")
            return plan

        except Exception as e:
            logger.warning(f"Morning plan error: {e}")
            return self._morning_plan  # Return stale plan rather than None

    def _estimate_drift_to_peak(self, peak_start_hour: int) -> float:
        """Estimate TOU drift kWh from now until peak start.

        TOU drift is the phantom grid→battery charging that occurs in TOU mode.
        The drift tracker measures the actual rate. We extrapolate over the
        remaining hours until peak to give the morning plan a drift credit,
        preventing the gap from being inflated by unaccounted-for charging.
        """
        now = datetime.now()
        hours_to_peak = max(0, peak_start_hour - now.hour - now.minute / 60.0)
        if hours_to_peak <= 0:
            return 0.0

        drift_rate = self.tou_drift.drift_rate_pct_per_hour
        if drift_rate <= 0:
            return 0.0

        battery_kwh = self.config.get('battery_capacity_kwh', 30.0)
        return drift_rate * hours_to_peak / 100.0 * battery_kwh

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
            # EXCEPTION: Solar discharge. On non-export systems, if the battery
            # has free solar energy it should be self-consumed rather than wasted.
            # Two scenarios:
            #   A) Weekday post-peak: solar charged battery, peak used some,
            #      net excess should be burned overnight.
            #   B) Weekend/non-peak: solar charged battery with no peak to
            #      protect against — self-consume it that evening.
            # The method computes a TARGET SOC based on net solar excess.
            # Once SOC reaches the target, it returns None and we fall
            # through to TOU. This prevents draining past the solar excess.
            #
            # The headroom logic above handles a separate concern: making
            # room for TOMORROW's solar forecast. Both can coexist.

            # Post-peak solar discharge: burn free solar energy overnight
            post_peak_decision = self._evaluate_post_peak_discharge(state)
            if post_peak_decision:
                return post_peak_decision

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

    def _evaluate_post_peak_discharge(self, state: SystemState) -> Optional[Decision]:
        """Evaluate whether to discharge free solar energy via Self-Consumption.

        Covers two scenarios:
          A) Weekday post-peak: peak just ended, battery has solar excess after
             peak discharge. Burn only the net solar excess overnight.
          B) Weekend / non-peak day: no peak at all today, but solar charged
             the battery. That energy should be self-consumed rather than
             sitting idle in TOU while the grid powers the home.

        Export systems skip this entirely — they sell surplus for credit,
        so there's no "wasted" solar in the battery.

        IMPORTANT: The target SOC is anchored to the SOC at peak-end (from
        system_readings DB), NOT recalculated from current SOC each cycle.
        Without this anchor, every 30-min cycle recomputes
        target = current_soc - excess_pct, creating a moving target that
        chases SOC down to the floor instead of draining only the solar excess.
        """
        if self.solar_export:
            return None

        backup_reserve = self.config.get('backup_reserve_pct', 20.0)
        discharge_floor = max(POST_PEAK_DISCHARGE_FLOOR_PCT, backup_reserve + 5.0)

        if state.soc_percent <= discharge_floor:
            return None

        if state.soc_percent < backup_reserve + POST_PEAK_MIN_SOC_ABOVE_RESERVE:
            return None

        battery_kwh = self.config.get(
            'battery_capacity_kwh',
            getattr(getattr(self.profile, 'capacity', None),
                    'total_capacity_kwh', 30.0)
        )

        # Determine which scenario we're in and get solar excess
        is_post_peak = False
        hours_since = self.rates.hours_since_peak_end(state.timestamp)
        if hours_since is not None and hours_since <= POST_PEAK_WINDOW_HOURS:
            is_post_peak = True

        # Non-peak day: today had no peak at all (weekend/holiday).
        is_non_peak_day = (hours_since is None and
                           (state.hours_to_peak is None or state.hours_to_peak > 12))
        if not is_post_peak and not is_non_peak_day:
            return None

        # Get today's solar charging data
        solar_data = self._get_today_solar_data(state.timestamp)
        if solar_data is None:
            return None

        solar_charged_kwh = solar_data['solar_charged_kwh']
        grid_charged_kwh = solar_data['grid_charged_kwh']
        solar_ratio = solar_data['solar_ratio']
        peak_discharge_kwh = solar_data.get('peak_discharge_kwh', 0)

        # Net solar excess: solar that went into the battery minus peak usage
        if is_post_peak and not is_non_peak_day:
            solar_excess_kwh = max(0, solar_charged_kwh - peak_discharge_kwh)
            scenario = "post-peak"
        else:
            solar_excess_kwh = solar_charged_kwh
            scenario = "non-peak day"

        if solar_excess_kwh < POST_PEAK_MIN_SOLAR_EXCESS_KWH:
            return None

        # --- ANCHORED TARGET SOC ---
        # Look up SOC at peak-end from system_readings so the target is
        # fixed across engine cycles. Without this, target drifts downward
        # every 30 minutes because it recalculates from current (falling) SOC.
        anchor_soc = self._get_soc_at_peak_end(state.timestamp)
        if anchor_soc is None:
            # Fallback for non-peak days or missing data: use current SOC
            # but only on the first cycle (when SOC is still near peak-end level)
            if is_non_peak_day:
                anchor_soc = state.soc_percent
            else:
                return None  # Can't compute target without anchor

        excess_pct = solar_excess_kwh / battery_kwh * 100
        target_soc = anchor_soc - excess_pct
        target_soc = max(target_soc, discharge_floor)

        # If we're already at or below the target, done — fall through to TOU
        if state.soc_percent <= target_soc + 2.0:
            return None

        drain_kwh = (state.soc_percent - target_soc) / 100 * battery_kwh

        # Track solar discharge session
        self.solar_discharge_activations += 1
        self._solar_discharge_target_soc = target_soc
        if self._solar_discharge_start_soc is None:
            self._solar_discharge_start_soc = state.soc_percent

        # Estimate kWh discharged this cycle (interval-based)
        interval_hours = self.config.get('decision_interval_minutes', 30) / 60.0
        if state.home_load_kw > 0:
            cycle_kwh = min(state.home_load_kw * interval_hours, drain_kwh)
        else:
            cycle_kwh = drain_kwh * (interval_hours / 8.0)  # rough estimate over 8h
        _, current_rate = self.rates.current_tier(state.timestamp)
        self.solar_discharge_kwh += cycle_kwh
        self.solar_discharge_value_cents += cycle_kwh * current_rate

        metrics = {
            'scenario': scenario,
            'solar_charged_kwh': round(solar_charged_kwh, 1),
            'grid_charged_kwh': round(grid_charged_kwh, 1),
            'solar_ratio': round(solar_ratio, 3),
            'peak_discharge_kwh': round(peak_discharge_kwh, 1),
            'solar_excess_kwh': round(solar_excess_kwh, 1),
            'target_soc': round(target_soc, 1),
            'anchor_soc': round(anchor_soc, 1),
            'drain_kwh': round(drain_kwh, 1),
            'discharge_floor': round(discharge_floor, 1),
        }

        if is_post_peak:
            metrics['hours_since_peak_end'] = round(hours_since, 1)

        return self._decide(
            state, "self_consumption",
            f"Solar discharge ({scenario}): {solar_excess_kwh:.1f} kWh free solar "
            f"(ratio {solar_ratio:.0%}) — draining {state.soc_percent:.0f}% → "
            f"{target_soc:.0f}% (anchor {anchor_soc:.0f}%, {drain_kwh:.1f} kWh)",
            confidence=0.85, priority=8,
            action="switch_to_self_consumption",
            metrics=metrics,
        )

    def _get_soc_at_peak_end(self, timestamp: datetime) -> Optional[float]:
        """Look up SOC at the most recent peak-end from system_readings.

        Finds the system_readings row closest to when peak ended today.
        This provides a stable anchor for post-peak discharge target
        calculation that doesn't drift as SOC decreases each cycle.

        Returns SOC percentage at peak end, or None if unavailable.
        """
        if not HAS_DB:
            return None

        try:
            # Find when peak ended
            hours_since = self.rates.hours_since_peak_end(timestamp)
            if hours_since is None:
                return None

            peak_end_approx = timestamp - timedelta(hours=hours_since)
            # Search for the closest reading within +/- 15 minutes of peak end
            window_start = (peak_end_approx - timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
            window_end = (peak_end_approx + timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')

            rows = db_mod.query(
                "SELECT soc_pct, timestamp FROM system_readings "
                "WHERE timestamp BETWEEN ? AND ? "
                "AND soc_pct IS NOT NULL "
                "ORDER BY timestamp",
                (window_start, window_end),
            )

            if rows:
                # Use the FIRST reading at or after peak end for a stable anchor.
                # The window is peak_end ± 15 min. As hours_since_peak_end grows,
                # float drift can shift the window edges, so pinning to the first
                # reading (closest to actual peak end) prevents anchor drift.
                best = None
                peak_end_str = peak_end_approx.strftime('%Y-%m-%d %H:%M:%S')
                for r in rows:
                    if r['timestamp'] >= peak_end_str:
                        best = r
                        break
                if best is None:
                    best = rows[-1]  # all readings before peak end; use latest
                soc = best.get('soc_pct')
                if soc is not None:
                    logger.debug(f"Peak-end SOC anchor: {soc}% at {best['timestamp']}")
                    return float(soc)

            # Wider fallback: first reading after peak end
            rows = db_mod.query(
                "SELECT soc_pct, timestamp FROM system_readings "
                "WHERE timestamp >= ? AND soc_pct IS NOT NULL "
                "ORDER BY timestamp LIMIT 1",
                (peak_end_approx.strftime('%Y-%m-%d %H:%M:%S'),),
            )
            if rows:
                soc = rows[0].get('soc_pct')
                if soc is not None:
                    return float(soc)

        except Exception as e:
            logger.debug(f"Peak-end SOC lookup: {e}")

        return None

    def _compute_peak_discharge_kwh(self, date_str: str) -> float:
        """Compute peak discharge from system_readings SOC delta during peak window.

        When daily_savings hasn't run yet (it runs at 00:05 AM), the fallback
        paths in _get_today_solar_data() have no peak_discharge_kwh. This method
        computes it in real-time from SOC at peak start vs peak end.

        Returns estimated kWh discharged during peak, or 0 if data unavailable.
        """
        if not HAS_DB:
            return 0.0

        try:
            battery_kwh = self.config.get(
                'battery_capacity_kwh',
                getattr(getattr(self.profile, 'capacity', None),
                        'total_capacity_kwh', 30.0)
            )

            peak_start = f"{date_str} 17:00:00"
            peak_end = f"{date_str} 20:00:00"

            start_rows = db_mod.query(
                "SELECT soc_pct FROM system_readings "
                "WHERE timestamp >= ? AND timestamp <= ? "
                "AND soc_pct IS NOT NULL "
                "ORDER BY timestamp LIMIT 1",
                (peak_start, f"{date_str} 17:15:00"),
            )

            end_rows = db_mod.query(
                "SELECT soc_pct FROM system_readings "
                "WHERE timestamp >= ? AND timestamp <= ? "
                "AND soc_pct IS NOT NULL "
                "ORDER BY timestamp DESC LIMIT 1",
                (f"{date_str} 19:45:00", f"{date_str} 20:15:00"),
            )

            if start_rows and end_rows:
                soc_start = start_rows[0].get('soc_pct', 0) or 0
                soc_end = end_rows[0].get('soc_pct', 0) or 0
                discharge_pct = max(0, soc_start - soc_end)
                discharge_kwh = discharge_pct / 100.0 * battery_kwh
                logger.debug(
                    f"Peak discharge from SOC delta: {soc_start:.0f}% → "
                    f"{soc_end:.0f}% = {discharge_kwh:.1f} kWh"
                )
                return round(discharge_kwh, 2)

        except Exception as e:
            logger.debug(f"Peak discharge SOC delta calc failed: {e}")

        return 0.0

    def _get_today_solar_data(self, timestamp: datetime) -> Optional[dict]:
        """Get today's solar charging breakdown from the database.

        Preferred source: daily_savings table (computed by calculate_daily_savings.py
        with proper kWh math from cumulative counters).

        Fallback: system_readings solar_to_battery_kw / grid_to_battery_kw,
        but these are instantaneous kW readings and need interval-weighted
        conversion to kWh.

        Returns dict with: solar_charged_kwh, grid_charged_kwh, solar_ratio,
        peak_discharge_kwh. Returns None if insufficient data.
        """
        if not HAS_DB:
            return None

        try:
            date_str = timestamp.strftime('%Y-%m-%d')

            # Preferred: daily_savings (accurate kWh from cumulative counters)
            savings = db_mod.query(
                "SELECT solar_ratio, solar_charged_kwh, grid_charged_kwh, "
                "peak_discharge_kwh FROM daily_savings WHERE date = ?",
                (date_str,)
            )
            if savings and savings[0].get('solar_charged_kwh') is not None:
                s = savings[0]
                return {
                    'solar_charged_kwh': s['solar_charged_kwh'] or 0,
                    'grid_charged_kwh': s['grid_charged_kwh'] or 0,
                    'solar_ratio': s['solar_ratio'] or 0,
                    'peak_discharge_kwh': s.get('peak_discharge_kwh') or 0,
                    'source': 'daily_savings',
                }

            # Second choice: cumulative energy counters from system_readings.
            # kwh_solar and kwh_battery_charge are running totals from the
            # Franklin API — the diff between first and last reading of the
            # day gives actual kWh. This works even before midnight rollup.
            cumul = db_mod.query(
                "SELECT kwh_solar, kwh_battery_charge "
                "FROM system_readings "
                "WHERE timestamp LIKE ? "
                "AND kwh_battery_charge IS NOT NULL "
                "ORDER BY timestamp",
                (f"{date_str}%",)
            )
            if cumul and len(cumul) >= 2:
                first = cumul[0]
                last = cumul[-1]
                bat_charge_kwh = (last.get('kwh_battery_charge') or 0) - (first.get('kwh_battery_charge') or 0)
                solar_kwh = (last.get('kwh_solar') or 0) - (first.get('kwh_solar') or 0)
                if bat_charge_kwh > 0.5:
                    est_ratio = min(1.0, solar_kwh / max(bat_charge_kwh, solar_kwh)) if solar_kwh > 0 else 0
                    return {
                        'solar_charged_kwh': round(bat_charge_kwh * est_ratio, 2),
                        'grid_charged_kwh': round(bat_charge_kwh * (1 - est_ratio), 2),
                        'solar_ratio': round(est_ratio, 3),
                        'peak_discharge_kwh': self._compute_peak_discharge_kwh(date_str),
                        'source': 'cumulative_counters',
                    }

            # Last resort: raw system_readings (kW readings, need interval conversion)
            rows = db_mod.query(
                "SELECT solar_to_battery_kw, grid_to_battery_kw, timestamp "
                "FROM system_readings "
                "WHERE timestamp LIKE ? "
                "AND (solar_to_battery_kw IS NOT NULL "
                "     OR grid_to_battery_kw IS NOT NULL) "
                "ORDER BY timestamp",
                (f"{date_str}%",)
            )

            if not rows or len(rows) < 4:
                return None

            # Convert kW readings to kWh using intervals between readings
            total_solar_kwh = 0.0
            total_grid_kwh = 0.0
            for i in range(1, len(rows)):
                s2b = max(0, rows[i].get('solar_to_battery_kw') or 0)
                g2b = max(0, rows[i].get('grid_to_battery_kw') or 0)
                try:
                    t1 = datetime.strptime(rows[i-1]['timestamp'][:19], '%Y-%m-%d %H:%M:%S')
                    t2 = datetime.strptime(rows[i]['timestamp'][:19], '%Y-%m-%d %H:%M:%S')
                    interval_hours = (t2 - t1).total_seconds() / 3600.0
                    if 0 < interval_hours < 1.0:
                        total_solar_kwh += s2b * interval_hours
                        total_grid_kwh += g2b * interval_hours
                except (ValueError, KeyError):
                    continue

            # Sanity cap: solar_to_battery_kw from the cloud API can report
            # instantaneous flow direction even when battery is full and not
            # absorbing. Cap at battery capacity so we never claim more solar
            # charged than the battery can physically hold.
            battery_kwh = self.config.get(
                'battery_capacity_kwh',
                getattr(getattr(self.profile, 'capacity', None),
                        'total_capacity_kwh', 30.0)
            )
            total_solar_kwh = min(total_solar_kwh, battery_kwh)
            total_grid_kwh = min(total_grid_kwh, battery_kwh)

            total = total_solar_kwh + total_grid_kwh
            if total < 0.5:
                return None

            return {
                'solar_charged_kwh': round(total_solar_kwh, 2),
                'grid_charged_kwh': round(total_grid_kwh, 2),
                'solar_ratio': total_solar_kwh / total if total > 0 else 0,
                'peak_discharge_kwh': self._compute_peak_discharge_kwh(date_str),
                'source': 'readings_kwh',
            }

        except Exception as e:
            logger.debug(f"Solar data query failed: {e}")
            return None

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

        # --- Taper ceiling cap ---
        # On non-export systems, battery charge rate tapers above a SOC knee,
        # wasting solar that can't be absorbed. Cap the grid charging ceiling
        # so EB doesn't push into the taper zone — let solar fill the rest
        # during peak production hours when panels are strongest.
        taper_cap = TAPER_CEILING_PCT
        if not self.solar_export and ceiling_pct > taper_cap:
            logger.debug(
                f"Taper ceiling cap: {ceiling_pct:.0f}% → {taper_cap:.0f}%"
            )
            ceiling_pct = taper_cap
            # Recompute gap relative to capped ceiling
            battery_kwh = self.config.get(
                'battery_capacity_kwh',
                getattr(getattr(self.profile, 'capacity', None),
                        'total_capacity_kwh', 30.0)
            )
            capped_target_kwh = battery_kwh * ceiling_pct / 100.0
            gap_kwh = max(0, capped_target_kwh - plan.current_kwh - plan.forecast_to_battery_kwh)

        metrics = {
            'current_kwh': plan.current_kwh,
            'target_kwh': plan.target_kwh,
            'forecast_solar_kwh': plan.forecast_remaining_kwh,
            'expected_consumption_kwh': plan.expected_consumption_kwh,
            'net_solar_to_battery_kwh': plan.forecast_to_battery_kwh,
            'gap_kwh': round(gap_kwh, 1),
            'morning_ceiling_pct': round(ceiling_pct, 1),
            'taper_ceiling_pct': round(taper_cap, 1),
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

            # --- Pre-peak one-way gate (v4.0.6) ---
            # Once within PRE_PEAK_GATE_HOURS of peak, if the engine is NOT
            # already in EB, don't start a new EB burst. The reasoning:
            # if the gap calc was satisfied at the previous cycle and moved
            # to TOU/SC, a partial EB burst in the last 30 min adds minimal
            # SOC and isn't worth the mode switch and grid cost.
            # Exception: if already in EB from prior cycle, let it finish.
            if state.hours_to_peak <= PRE_PEAK_GATE_HOURS:
                if state.current_mode != 'emergency_backup':
                    metrics['pre_peak_gate'] = True
                    return self._decide(
                        state, state.current_mode,
                        f"Pre-peak gate: {state.hours_to_peak:.1f}h to peak, "
                        f"SOC {state.soc_percent:.0f}%, not in EB — "
                        f"holding {state.current_mode} (gap {gap_kwh:.1f} kWh "
                        f"not worth late EB switch)",
                        confidence=0.85, priority=7, action="hold",
                        metrics=metrics,
                    )
                else:
                    # Already in EB — let it keep charging to ceiling
                    if state.soc_percent >= ceiling_pct:
                        return self._decide(
                            state, "self_consumption",
                            f"Pre-peak gate: EB reached ceiling {ceiling_pct:.0f}% "
                            f"(SOC {state.soc_percent:.0f}%) — switching to SC for peak",
                            confidence=0.9, priority=7,
                            action="switch_to_self_consumption",
                            metrics=metrics,
                        )
                    # else: fall through to normal EB logic below

            # --- EB time deferral (v4.0.5) ---
            # EB charges fast (~5.5 kW). Only trigger when we genuinely need
            # to start now to finish before peak. The engine recalculates
            # every 30-min cycle, so deferring is always safe.
            #
            # Simple rule: don't start EB until charge_time + small safety
            # margin >= hours_to_peak. The margin gives one extra cycle for
            # the engine to react if something changes.
            safety_margin_hours = 0.5  # 30 min — one engine cycle
            buffer_hours = state.hours_to_peak - charge_time_hours

            # Add timing metrics for log visibility
            metrics['charge_time_hours'] = round(charge_time_hours, 2)
            metrics['buffer_hours'] = round(buffer_hours, 1)
            metrics['min_buffer_hours'] = round(safety_margin_hours, 1)

            # Plenty of time — defer EB, stay in TOU.
            if buffer_hours > safety_margin_hours + 1.0:
                # More than 1.5h of slack — no rush at all.
                # Solar and TOU drift may shrink the gap for free.
                return self._decide(
                    state, "time_of_use",
                    f"Forecast gap ({gap_kwh:.1f} kWh, {charge_time_hours:.1f}h to charge) "
                    f"but {buffer_hours:.1f}h buffer — "
                    f"no rush, deferring EB. Reassess next cycle.",
                    confidence=0.8, priority=7, action="switch_to_tou",
                    metrics=metrics,
                )

            # Buffer is getting tight but solar is actively producing —
            # give solar a chance to close the gap before resorting to EB.
            # Only defer if remaining solar is meaningful vs the gap (≥15%).
            # On low-solar days, switching to self_consumption just drains
            # the battery to power house load — staying in TOU is better
            # since the grid covers the house while we wait.
            solar_contribution_pct = (plan.forecast_remaining_kwh / gap_kwh * 100) if gap_kwh > 0 else 0
            if (state.solar_kw > MIN_SOLAR_PRODUCING_KW
                    and buffer_hours > safety_margin_hours
                    and solar_contribution_pct >= 15):
                return self._decide(
                    state, "self_consumption",
                    f"Forecast gap ({gap_kwh:.1f} kWh, {charge_time_hours:.1f}h to charge) "
                    f"but solar producing ({state.solar_kw:.1f} kW, "
                    f"{solar_contribution_pct:.0f}% of gap) with "
                    f"{buffer_hours:.1f}h buffer — deferring to let solar fill",
                    confidence=0.75, priority=7, action="hold",
                    metrics=metrics,
                )

            # Solar producing but contribution too small to justify
            # self_consumption — stay in TOU so grid covers house load.
            if (state.solar_kw > MIN_SOLAR_PRODUCING_KW
                    and buffer_hours > safety_margin_hours
                    and solar_contribution_pct < 15):
                return self._decide(
                    state, "time_of_use",
                    f"Forecast gap ({gap_kwh:.1f} kWh, {charge_time_hours:.1f}h to charge) "
                    f"but solar contribution minimal ({solar_contribution_pct:.0f}% of gap) "
                    f"with {buffer_hours:.1f}h buffer — staying in TOU, deferring EB.",
                    confidence=0.8, priority=7, action="switch_to_tou",
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
        target_soc = self.target_soc

        # Taper ceiling cap (non-export systems)
        if not self.solar_export and target_soc > TAPER_CEILING_PCT:
            target_soc = TAPER_CEILING_PCT

        target_kwh = cap.kwh_at_soc(target_soc)

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
                state.soc_percent, target_soc
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
            # --- Pre-peak one-way gate (v4.0.6) ---
            if state.hours_to_peak <= PRE_PEAK_GATE_HOURS:
                if state.current_mode != 'emergency_backup':
                    metrics['pre_peak_gate'] = True
                    return self._decide(
                        state, state.current_mode,
                        f"Pre-peak gate: {state.hours_to_peak:.1f}h to peak, "
                        f"SOC {state.soc_percent:.0f}%, not in EB — "
                        f"holding {state.current_mode} (gap {gap_kwh:.1f} kWh "
                        f"not worth late EB switch)",
                        confidence=0.85, priority=7, action="hold",
                        metrics=metrics,
                    )
                elif state.soc_percent >= target_soc:
                    return self._decide(
                        state, "self_consumption",
                        f"Pre-peak gate: EB reached target {target_soc:.0f}% "
                        f"(SOC {state.soc_percent:.0f}%) — switching to SC for peak",
                        confidence=0.9, priority=7,
                        action="switch_to_self_consumption",
                        metrics=metrics,
                    )

            # --- EB time deferral (v4.0.5) ---
            # Same logic as _evaluate_gap_with_plan — don't rush to EB.
            safety_margin_hours = 0.5
            buffer_hours = state.hours_to_peak - charge_time_hours

            metrics['buffer_hours'] = round(buffer_hours, 1)
            metrics['min_buffer_hours'] = round(safety_margin_hours, 1)

            # Plenty of time — defer EB, stay in TOU
            if buffer_hours > safety_margin_hours + 1.0:
                return self._decide(
                    state, "time_of_use",
                    f"Charging gap ({gap_kwh:.1f} kWh, {charge_time_hours:.1f}h to charge) "
                    f"but {buffer_hours:.1f}h buffer — "
                    f"no rush, deferring EB. Reassess next cycle.",
                    confidence=0.8, priority=7, action="switch_to_tou",
                    metrics=metrics,
                )

            # Solar-aware deferral — tighter buffer but solar still helping
            if (state.solar_kw > MIN_SOLAR_PRODUCING_KW
                    and buffer_hours > safety_margin_hours):
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
            'solar_discharge': {
                'session_kwh': round(self.solar_discharge_kwh, 3),
                'session_value_cents': round(self.solar_discharge_value_cents, 1),
                'activations': self.solar_discharge_activations,
                'target_soc': self._solar_discharge_target_soc,
                'start_soc': self._solar_discharge_start_soc,
            },
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
    profile_path: str = 'data/system_profile.json',
    rate_schedule_path: str = 'data/rate_schedule.json',
    config: Optional[dict] = None,
) -> AdaptiveEngine:
    """Create an AdaptiveEngine with loaded or freshly built profile.
    
    Call this from scheduler.py to initialize the engine.
    """
    config = config or {}

    # Load system profile (must exist as JSON — built by system_profile.py)
    profile = load_profile(profile_path)
    if profile is None:
        logger.warning("No saved profile found — using defaults")
        profile = SystemProfile(
            capacity=SystemCapacity(),
            last_rebuilt=datetime.now().isoformat(),
        )
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

    config = {
        'battery_count': 2,
        'capacity_per_battery_kwh': 13.6,
        'backup_reserve_pct': 20,
        'target_soc': 95.0,
        'decision_interval_minutes': 15,
    }

    engine = create_engine(
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
        ("9 PM, post-peak, HIGH SOC (solar day)",
         "2026-02-19 21:00:00", 80.0, 0.0, 0.0, 1.5, 1.5, True, "time_of_use"),
        ("11 PM, post-peak, moderate SOC",
         "2026-02-19 23:00:00", 55.0, 0.0, 1.0, 0.0, 1.0, True, "self_consumption"),
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
