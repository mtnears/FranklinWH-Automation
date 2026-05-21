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
  5. Solar curtailment prevention → SC when SOC at ceiling with solar producing
  6. Dynamic pricing cheap power → charge below threshold
  7. Continuous target tracking (non-export) + EB gap charging:
     a) Compute target_soc from forecast solar — where SOC should be NOW
     b) If SOC > target: SC to drain (load-profile-aware timing)
     c) If SOC < target: TOU to charge (solar fills battery)
     d) If gap exists that solar can't fill: EB (last-responsible-moment)
  8. Default → TOU

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

# Overnight drain — REPLACED by continuous target tracking in v4.1.
# These constants are retained only as fallbacks if the DB load profile
# query fails (e.g., fresh install with no historical data).
OVERNIGHT_DRAIN_SOLAR_THRESHOLD_KW = 0.3   # kW below which solar is considered done

# Daytime solar headroom — REPLACED by continuous target tracking in v4.1.
# Retained only for P5 safety net (curtailment at 95%+).

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
# Continuous Target Tracking — v4.1
# ---------------------------------------------------------------------------
# Replaces Phase 1/2 headroom, daytime headroom, and overnight drain with
# a single mechanism. Every cycle: compute target_soc from forecast solar,
# compare to current SOC, pick mode.

# Where we want SOC at peak start (not 100 — Franklin shuts solar gate at ~99%)
CT_PEAK_ENTRY_TARGET = 98.0

# Dead band around target to prevent flapping (±this value)
CT_TOLERANCE_PCT = 3.0

# Safety margin scaling by weather confidence
CT_BASE_SAFETY_PCT = 3.0     # Safety margin at wx_score=1.0 (clear sky)
CT_MAX_SAFETY_PCT = 12.0     # Safety margin at wx_score=0.0 (overcast)

# Hard minimum: target never drops below reserve + peak_need + this
CT_MIN_FLOOR_ABOVE_RESERVE = 5.0

# Peak survival margin: when projecting SC charge forward to peak, SOC must
# reach reserve + peak_need + this margin to choose SC over TOU.
# Provides buffer for load variability (HVAC spikes, dryer, etc).
CT_PEAK_SURVIVAL_MARGIN_PCT = 10.0

# SC commit margin: how close projected SOC must come to the dynamic target
# before SC is acceptable. Used in addition to survival floor. Survival floor
# alone allows SC to commit when projection only "survives peak" but leaves
# the battery well below target — on small-solar / large-battery / cloudy
# scenarios this prevents EB from ever firing, since SC commit preempts the
# gap evaluation. Comparing against target_soc - this margin means SC only
# commits when it will land close to target; otherwise CT returns TOU and
# the EB gap logic gets a chance to fire grid charging at the last
# responsible moment.
CT_SC_COMMIT_MARGIN_PCT = 3.0

# Partial-peak protection (Priority 4.5) safety margin.
# On three-tier rate plans (e.g., PG&E EV2-A) partial-peak windows wrap the
# sacred peak. Battery may not cover the full expensive window, so P4.5 has
# to decide: discharge through partial-peak (SC) or preserve battery for
# peak (TOU, accepting partial-peak imports at the mid-tier rate).
# Safety margin (kWh) is added to forecast peak demand when computing the
# threshold. Higher = more conservative (favors TOU, more peak headroom).
# Lower = more aggressive (favors SC, accepts risk of running out late
# peak). 2.0 kWh ≈ 7% on a 27 kWh system.
P45_SAFETY_MARGIN_KWH = float(os.environ.get('P45_SAFETY_MARGIN_KWH', '2.0'))

# Minimum forecast solar surplus (kWh) before target tracking engages.
# Below this, the system just parks in TOU and lets solar/grid handle it.
CT_MIN_FORECAST_SOLAR_KWH = 2.0

# Solar refill credit window (hours). The floor's peak_need component can be
# discounted by forecast solar that will refill the battery before peak,
# but only if peak is within this window. Beyond it, forecast confidence
# decays (multi-day forecasts are unreliable) and we revert to full peak_need
# reservation. 24h covers today's and tomorrow's peak; weekend peaks 30-40h
# out get no credit.
CT_SOLAR_REFILL_MAX_HOURS = 24.0

# Default hourly load profile (kW) when DB history is unavailable.
# Index 0 = midnight, 23 = 11 PM. Based on typical US residential.
CT_DEFAULT_HOURLY_LOAD = [
    0.8, 0.7, 0.7, 0.7, 0.8, 0.9,   # 00-05: overnight baseline
    1.2, 1.5, 1.8, 2.0, 2.2, 2.3,   # 06-11: morning ramp
    2.5, 2.5, 2.3, 2.2, 2.0, 2.5,   # 12-17: afternoon
    3.0, 2.8, 2.5, 2.0, 1.5, 1.0,   # 18-23: evening peak then wind-down
]


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

    # Three-tier rate plan support (Priority 4.5)
    # Populated by enrich_state() — see rate_schedule.is_partial_peak() etc.
    is_partial_peak: bool = False
    is_expensive: bool = False                    # peak OR partial_peak
    peak_remaining_hours: float = 0.0             # peak h left in current expensive window
    partial_peak_remaining_hours: float = 0.0     # partial h left in current expensive window

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

        # Continuous target tracking state
        self._hourly_load_kw = list(CT_DEFAULT_HOURLY_LOAD)  # 24-element array, index=hour
        self._load_profile_from_db()  # Override defaults with actual history

        # Retained for backward compatibility with dashboard/telemetry readers
        self.solar_discharge_kwh: float = 0.0
        self.solar_discharge_value_cents: float = 0.0
        self.solar_discharge_activations: int = 0
        self._solar_discharge_target_soc: Optional[float] = None
        self._solar_discharge_start_soc: Optional[float] = None

        logger.info(f"AdaptiveEngine initialized: target_soc={target_soc}%, "
                    f"rate_schedule={rate_schedule.name}, "
                    f"forecast_engine={'yes' if self.forecast_engine else 'no'}, "
                    f"solar_export={'yes' if self.solar_export else 'no'}, "
                    f"load_profile_avg={sum(self._hourly_load_kw)/24:.1f}kW")

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

    def _load_profile_from_db(self):
        """Load hourly average home load from system_readings history.

        Queries the last 14 days of home_load_kw data, grouped by hour,
        to build a 24-element load profile. This is used by the continuous
        target tracker for load-aware drain timing — evening hours drain
        faster than overnight because load is higher.

        Falls back to CT_DEFAULT_HOURLY_LOAD if DB is unavailable or has
        insufficient data.
        """
        try:
            import sqlite3
            db_path = os.path.join(os.getenv('DATA_DIR', '/app/data'), 'franklin.db')
            conn = sqlite3.connect(db_path, timeout=10)
            cutoff = (datetime.now() - timedelta(days=14)).strftime('%Y-%m-%d')
            rows = conn.execute(
                "SELECT CAST(strftime('%H', timestamp) AS INT) as hour, "
                "       AVG(home_load_kw) as avg_kw, "
                "       COUNT(*) as n "
                "FROM system_readings "
                "WHERE timestamp > ? AND home_load_kw > 0 "
                "GROUP BY hour "
                "ORDER BY hour",
                (cutoff,)
            ).fetchall()
            conn.close()

            if len(rows) >= 20:
                profile = list(CT_DEFAULT_HOURLY_LOAD)
                for hour, avg_kw, n in rows:
                    if 0 <= hour <= 23 and n >= 10:
                        profile[hour] = round(max(avg_kw, 0.3), 2)
                self._hourly_load_kw = profile
                logger.info(f"Loaded hourly load profile from DB: "
                            f"night={profile[2]:.1f}kW, "
                            f"morning={profile[8]:.1f}kW, "
                            f"afternoon={profile[14]:.1f}kW, "
                            f"evening={profile[20]:.1f}kW")
            else:
                logger.info(f"Insufficient DB history ({len(rows)} hours) — using default load profile")
        except Exception as e:
            logger.debug(f"Load profile DB query failed: {e} — using defaults")

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
        state.current_tier, rate_cents = self.rates.current_tier(state.timestamp)
        state.current_rate_cents = round(rate_cents, 3)  # avoid float noise in display
        state.is_peak = self.rates.is_peak(state.timestamp)
        state.hours_to_peak = self.rates.hours_to_peak(state.timestamp)
        state.peak_duration_hours = self.rates.peak_duration_hours(state.timestamp)
        state.rate_spread_cents = self.rates.rate_spread(state.timestamp)

        # Three-tier rate plan: partial-peak awareness for Priority 4.5
        state.is_partial_peak = self.rates.is_partial_peak(state.timestamp)
        state.is_expensive = state.is_peak or state.is_partial_peak
        peak_h, partial_h = self.rates.expensive_window_remaining_hours(state.timestamp)
        state.peak_remaining_hours = peak_h
        state.partial_peak_remaining_hours = partial_h

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

        # --- Priority 4.5: Partial-Peak Conditional Protection ---
        # Three-tier rate plans (e.g., PG&E EV2-A) wrap the sacred peak window
        # with partial-peak windows at a mid-tier rate. Battery may not cover
        # the full expensive window, so we prioritize peak coverage:
        #   - Pre-peak partial + battery can cover peak → SC (discharge through)
        #   - Pre-peak partial + battery can't cover peak → TOU (preserve battery),
        #     then chain to EB gap eval which may grid-charge if time-to-peak
        #     is tight enough
        #   - Post-peak partial → SC (use remaining battery; tomorrow's peak
        #     is recoverable via overnight + solar)
        if state.is_partial_peak:
            pp_decision = self._evaluate_partial_peak(state)
            if pp_decision:
                if pp_decision.action == "switch_to_tou":
                    eb_decision = self._evaluate_eb_gap(state)
                    if eb_decision:
                        return eb_decision
                return pp_decision

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

        # --- Priority 7: Continuous Target Tracking + EB Gap Charging ---
        # Two sub-concerns evaluated together:
        #   A) Continuous target (non-export only): compute target_soc from
        #      forecast solar, compare to current SOC, drain or charge.
        #   B) EB gap charging: if solar can't fill the gap to peak, grid charge
        #      using last-responsible-moment timing.
        #
        # When CT returns SC or hold, those are active decisions — use them.
        # When CT returns TOU (parking the battery), EB gets a chance to
        # override: TOU just idles, but EB actively closes the gap via grid
        # charging when time-to-peak is tight. Without this, CT's TOU blocks
        # EB from ever evaluating on low-solar days.
        if not self.solar_export:
            ct_decision = self._evaluate_continuous_target(state)
            if ct_decision:
                if ct_decision.action == "switch_to_tou":
                    eb_decision = self._evaluate_eb_gap(state)
                    if eb_decision:
                        return eb_decision
                return ct_decision

        eb_decision = self._evaluate_eb_gap(state)
        if eb_decision:
            return eb_decision

        # --- Default: TOU ---
        backup_reserve = self.config.get('backup_reserve_pct', 20.0)
        if state.soc_percent <= backup_reserve + 2.0:
            return self._decide(
                state, "time_of_use",
                f"No action needed, SOC {state.soc_percent:.0f}% near reserve "
                f"({backup_reserve:.0f}%) — TOU to let grid power home",
                confidence=0.85, priority=8,
                action="switch_to_tou",
            )

        return self._decide(
            state, "time_of_use",
            "No action needed — TOU (preserve battery, grid powers home)",
            confidence=0.8, priority=8,
            action="switch_to_tou",
        )

    # ===================================================================
    # Continuous Target Tracking — v4.1
    # ===================================================================

    def _get_remaining_solar_kwh(self, state: SystemState) -> tuple:
        """Get remaining solar-to-battery forecast for headroom management.

        Simple two-path decision based on observed reality, not clock time:

          1. Solar IS producing right now → use morning plan (remaining today)
          2. Solar is NOT producing → use next solar day's forecast from cache

        "Next solar day" is determined by the cache: try today first (covers
        the after-midnight case where today's solar hasn't started yet), then
        tomorrow (covers the after-sunset case where today's solar is done).

        Returns (remaining_kwh, wx_score, forecast_source).
        """
        solar_producing = state.solar_kw > OVERNIGHT_DRAIN_SOLAR_THRESHOLD_KW

        # --- Solar producing: use morning plan for remaining today ---
        if solar_producing:
            plan = self._get_morning_plan(state.soc_percent, state.timestamp)
            if plan is not None and plan.forecast_to_battery_kwh > 0:
                return (
                    plan.forecast_to_battery_kwh,
                    getattr(plan, 'weather_score', 0.5),
                    plan.forecast_source,
                )
            # Solar is producing but plan says 0 — rare edge case, fall through

        # --- No solar: use next solar day's full-day forecast from cache ---
        if self.forecast_engine is not None:
            next_solar_kwh, next_solar_date = self._get_next_solar_day_forecast(state.timestamp)
            if next_solar_kwh is not None and next_solar_kwh > 0:
                battery_kwh = self.config.get('battery_capacity_kwh', 27.2)
                battery_portion = min(next_solar_kwh * 0.85, battery_kwh * 0.95)
                wx_score = self._get_forecast_wx_score(next_solar_date)
                logger.info(f"CT next solar day forecast ({next_solar_date}): "
                            f"{next_solar_kwh:.1f}kWh total, "
                            f"{battery_portion:.1f}kWh to battery, wx={wx_score:.2f}")
                return (battery_portion, wx_score, 'forecast_tomorrow')

        # --- Fallback: morning plan (may return 0) ---
        plan = self._get_morning_plan(state.soc_percent, state.timestamp)
        if plan is not None:
            return (
                plan.forecast_to_battery_kwh,
                getattr(plan, 'weather_score', 0.5),
                plan.forecast_source,
            )

        # Fallback: learned profile
        peak_start = self.rates.next_peak_start(state.timestamp)
        if peak_start and hasattr(self.profile, 'solar'):
            remaining = self.profile.solar.forecast_remaining_kwh(
                state.timestamp, peak_start.hour
            )
            return (remaining, 0.5, 'learned_profile')

        return (0.0, 0.5, 'none')

    def _get_next_solar_day_forecast(self, timestamp: datetime) -> tuple:
        """Get the total solar forecast for the next solar day.

        Tries today's date first in the cache. If today has no forecast or
        today's solar is already fully accounted for (morning plan returned 0
        during production), tries tomorrow. This handles both:
          - After midnight before sunrise: today's forecast is what we need
          - After sunset before midnight: tomorrow's forecast is what we need
        Without any clock-time checks — just cache availability.

        Returns (total_kwh, date_string) or (None, None) if not cached.
        """
        if self.forecast_engine is None:
            return (None, None)
        try:
            cache = getattr(self.forecast_engine, '_cache', None)
            if not cache or not isinstance(cache, dict):
                return (None, None)

            today = timestamp.strftime('%Y-%m-%d')
            tomorrow = (timestamp + timedelta(days=1)).strftime('%Y-%m-%d')

            # Try today first — covers the after-midnight case
            if today in cache:
                entry = cache[today]
                total = entry.total_kwh if hasattr(entry, 'total_kwh') else None
                if total is not None and total > 0:
                    # Check if today's solar has meaningful production remaining.
                    # Sum hourly watt_hours from current hour onward.
                    remaining_wh = 0
                    current_hour = timestamp.hour
                    hourly = getattr(entry, 'hourly', None)
                    if hourly:
                        for h in hourly:
                            h_hour = h.hour if hasattr(h, 'hour') else h.get('hour', 0)
                            h_wh = h.watt_hours if hasattr(h, 'watt_hours') else h.get('watt_hours', 0)
                            if h_hour >= current_hour:
                                remaining_wh += h_wh
                    remaining_kwh = remaining_wh / 1000.0
                    if remaining_kwh > 1.0:
                        return (total, today)

            # Today exhausted or not available — try tomorrow
            if tomorrow in cache:
                entry = cache[tomorrow]
                total = entry.total_kwh if hasattr(entry, 'total_kwh') else None
                if total is not None and total > 0:
                    return (total, tomorrow)

        except Exception as e:
            logger.debug(f"Next solar day forecast lookup failed: {e}")
        return (None, None)

    def _get_forecast_wx_score(self, date_str: str) -> float:
        """Get weather score for a specific cached forecast date.

        Returns a value between 0.0 and 1.0. Defaults to 0.75 if unavailable.
        """
        if self.forecast_engine is None:
            return 0.75
        try:
            cache = getattr(self.forecast_engine, '_cache', None)
            if cache and isinstance(cache, dict) and date_str in cache:
                entry = cache[date_str]
                ws = getattr(entry, 'weather_score', None)
                if ws is not None and isinstance(ws, (int, float)):
                    return max(0.0, min(1.0, ws))
                cf = getattr(entry, 'calibration_factor', None)
                if cf is not None and isinstance(cf, (int, float)):
                    return max(0.0, min(1.0, cf))
                if isinstance(entry, dict):
                    ws = entry.get('weather_score')
                    if ws is not None and isinstance(ws, (int, float)):
                        return max(0.0, min(1.0, ws))
                    cf = entry.get('calibration_factor')
                    if cf is not None and isinstance(cf, (int, float)):
                        return max(0.0, min(1.0, cf))
        except Exception:
            pass
        return 0.75

    def _compute_intraday_solar_factor(self, state: SystemState) -> float:
        """Compute correction factor: actual solar vs forecast for current hour.

        Compares recent actual solar production (from system_readings) to
        the forecast cache value for the current hour. Returns a multiplier
        to apply to future forecast hours in the SC projection.

        Uses the higher of: current instantaneous reading, or the average
        of recent readings from the current and previous hour. This smooths
        out momentary cloud dips without losing responsiveness.

        Returns 1.0 (no correction) when:
          - No forecast cache available
          - Forecast shows zero for current hour (avoid divide-by-zero)
          - Solar isn't meaningfully producing
          - Factor would be < 1.0 (forecast already optimistic enough)

        The factor is capped at 3.0 to prevent runaway projections from
        a single anomalous reading.
        """
        if state.solar_kw < 0.3:
            return 1.0

        # Get forecast value for current hour
        forecast_kw = None
        if self.forecast_engine is not None:
            try:
                today = state.timestamp.strftime('%Y-%m-%d')
                cache = getattr(self.forecast_engine, '_cache', None)
                if cache and today in cache:
                    entry = cache[today]
                    hourly = getattr(entry, 'hourly', None)
                    if hourly:
                        current_hour = state.timestamp.hour
                        for h in hourly:
                            h_hour = h.hour if hasattr(h, 'hour') else h.get('hour', 0)
                            if h_hour == current_hour:
                                h_watts = h.watts if hasattr(h, 'watts') else h.get('watts', 0)
                                forecast_kw = h_watts / 1000.0
                                break
            except Exception:
                pass

        if forecast_kw is None or forecast_kw < 0.2:
            return 1.0

        # Get smoothed actual solar from recent system_readings
        # Use DB average over last 2 hours for stability
        avg_actual_kw = state.solar_kw  # fallback: instantaneous
        try:
            import sqlite3
            db_path = os.path.join(os.getenv('DATA_DIR', '/app/data'), 'franklin.db')
            conn = sqlite3.connect(db_path, timeout=5)
            cutoff = (state.timestamp - timedelta(hours=2)).strftime('%Y-%m-%d %H:%M:%S')
            row = conn.execute(
                "SELECT AVG(solar_kw) FROM system_readings "
                "WHERE timestamp >= ? AND solar_kw > 0.1",
                (cutoff,)
            ).fetchone()
            conn.close()
            if row and row[0] and row[0] > 0.3:
                avg_actual_kw = max(row[0], state.solar_kw)
        except Exception:
            pass

        factor = avg_actual_kw / forecast_kw

        # Only correct upward — if forecast is already optimistic, don't reduce
        if factor < 1.0:
            return 1.0

        # Cap at 3.0 to prevent runaway from anomalous readings
        factor = min(factor, 3.0)

        if factor > 1.15:
            logger.info(
                f"Intraday solar correction: actual={avg_actual_kw:.1f}kW vs "
                f"forecast={forecast_kw:.1f}kW → factor={factor:.2f}"
            )

        return round(factor, 3)

    def _project_soc_in_sc(self, state: SystemState, hours_to_peak: float,
                            battery_kwh: float) -> float:
        """Project SOC at peak start if we switch to SC now.

        In SC mode, the battery charges from solar surplus (solar - home load)
        when solar exceeds load, and discharges when load exceeds solar.

        Walks forward from now to peak start in 1-hour steps using:
          - Hourly solar forecast from the forecast engine cache
          - Intraday correction factor (actual vs forecast for current hour)
          - Hourly load profile from DB history
          - Current SOC as starting point

        The intraday correction is the key anti-flapping mechanism: if actual
        solar is running 2x above forecast (common with the calibration model),
        the projection uses the corrected values rather than the crushed
        forecast. This eliminates the marginal pass/fail oscillation that
        causes TOU↔SC flapping.

        Returns projected SOC at peak start. If forecast data isn't available,
        uses current solar and load as constant estimates (conservative).
        """
        current_soc = state.soc_percent
        now_hour = state.timestamp.hour + state.timestamp.minute / 60.0
        steps = int(hours_to_peak)
        if steps < 1:
            return current_soc

        # Intraday correction: actual solar vs forecast for current hour
        solar_correction = self._compute_intraday_solar_factor(state)

        # Try to get hourly solar forecast from cache
        hourly_solar = None
        if self.forecast_engine is not None:
            try:
                today = state.timestamp.strftime('%Y-%m-%d')
                cache = getattr(self.forecast_engine, '_cache', None)
                if cache and today in cache:
                    entry = cache[today]
                    hourly = getattr(entry, 'hourly', None)
                    if hourly:
                        hourly_solar = {}
                        for h in hourly:
                            h_hour = h.hour if hasattr(h, 'hour') else h.get('hour', 0)
                            h_watts = h.watts if hasattr(h, 'watts') else h.get('watts', 0)
                            hourly_solar[h_hour] = h_watts / 1000.0  # Convert to kW
            except Exception:
                pass

        projected_soc = current_soc

        for step in range(min(steps, 12)):  # Cap at 12 hours lookahead
            hour = int(now_hour + step) % 24

            # Solar for this hour — apply intraday correction to forecast values
            if hourly_solar and hour in hourly_solar:
                solar_kw = hourly_solar[hour] * solar_correction
            else:
                # Fallback: use current solar, tapering off toward sunset
                solar_kw = max(0, state.solar_kw * (1.0 - step * 0.15))

            # Cap at array physical maximum (IQ8MC: 16 panels x 330W = 5.28 kW)
            solar_kw = min(solar_kw, 5.3)

            # Load for this hour
            load_kw = self._hourly_load_kw[hour]

            # SC surplus: solar - load. Positive = battery charges, negative = battery discharges
            surplus_kw = solar_kw - load_kw
            surplus_kwh = surplus_kw * 1.0  # 1 hour step
            soc_change = surplus_kwh / battery_kwh * 100.0

            projected_soc += soc_change
            # Clamp to physical limits
            projected_soc = max(0, min(100, projected_soc))

        return projected_soc

    def _get_solar_ramp_hour(self, timestamp: datetime) -> float:
        """Get the hour when solar production exceeds average home load.

        Uses SUN_SCHEDULE sunrise + a ramp offset. In March, sunrise is ~7 AM
        but solar doesn't exceed ~2 kW load until ~10 AM. The offset accounts
        for panel orientation, shading, and seasonal angle.
        """
        sunrise = 7.0
        try:
            from solar_forecast import SUN_SCHEDULE
            sunrise, _ = SUN_SCHEDULE.get(timestamp.month, (7.0, 18.0))
        except ImportError:
            pass
        # Solar ramp to meaningful production: sunrise + 2.5-3h typically
        return sunrise + 3.0

    def _get_sunset_hour(self, timestamp: datetime) -> float:
        """Get sunset hour for the current month."""
        try:
            from solar_forecast import SUN_SCHEDULE
            _, sunset = SUN_SCHEDULE.get(timestamp.month, (7.0, 18.0))
            return sunset
        except ImportError:
            return 18.0

    def _evaluate_continuous_target(self, state: SystemState) -> Optional[Decision]:
        """Continuous target tracking — the unified headroom management method.

        Every cycle, computes where SOC SHOULD be right now to absorb the
        remaining solar forecast without curtailment, while maintaining enough
        charge for peak. Compares current SOC to target and picks mode.

        Replaces: Phase 1 surplus drain, Phase 2 rate management, daytime
        headroom, and overnight drain. One calculation, one comparison,
        one mode decision.

        Returns None when target tracking doesn't apply (no forecast, export
        system, or insufficient solar to matter).
        """
        battery_kwh = self.config.get(
            'battery_capacity_kwh',
            getattr(getattr(self.profile, 'capacity', None),
                    'total_capacity_kwh', 27.2)
        )
        backup_reserve = self.config.get('backup_reserve_pct', 20.0)

        # --- Get forecast solar ---
        remaining_solar_kwh, wx_score, forecast_source = self._get_remaining_solar_kwh(state)

        if remaining_solar_kwh < CT_MIN_FORECAST_SOLAR_KWH and forecast_source == 'none':
            return None  # No forecast data at all — fall through to default

        # --- Compute target SOC ---
        remaining_solar_pct = remaining_solar_kwh / battery_kwh * 100.0

        # Raw target: where SOC should be now to land at entry target after absorbing solar
        raw_target = CT_PEAK_ENTRY_TARGET - remaining_solar_pct

        # Floor: never drain below reserve + peak need + safety
        avg_load_kw = sum(self._hourly_load_kw) / 24.0
        peak_duration = state.peak_duration_hours if state.peak_duration_hours > 0 else 3.0
        hours_to_peak = self._resolve_hours_to_next_peak(state)
        peak_need_kwh = avg_load_kw * peak_duration
        peak_need_pct = peak_need_kwh / battery_kwh * 100.0

        # Solar refill credit: forecast solar landing in battery before peak
        # discounts the peak_need reservation in the floor. The reasoning: if
        # we're confident solar will refill the battery before peak starts,
        # we don't need to reserve the full peak_need overnight — the battery
        # will be replenished by solar during the day.
        #
        # Discount = forecast_solar_to_battery × wx_score, capped at peak_need.
        # wx_score (0-1) acts as a confidence multiplier — cloudy/uncertain
        # forecasts get less credit. When forecast is strong and confident,
        # effective_peak_need collapses near 0 and the floor drops to
        # reserve + safety, freeing up the overnight drain window. When
        # forecast is weak or peak is too far out for confidence, the full
        # peak_need reservation is preserved.
        #
        # Only applied when peak is within CT_SOLAR_REFILL_MAX_HOURS (default
        # 24h). Multi-day-out peaks (weekends) get no credit because forecast
        # confidence decays past one day.
        solar_refill_pct = 0.0
        if (hours_to_peak is not None
                and hours_to_peak > 0
                and hours_to_peak < CT_SOLAR_REFILL_MAX_HOURS
                and remaining_solar_kwh > 0):
            solar_refill_kwh = remaining_solar_kwh * wx_score
            solar_refill_pct = min(
                solar_refill_kwh / battery_kwh * 100.0,
                peak_need_pct
            )
        effective_peak_need_pct = max(0.0, peak_need_pct - solar_refill_pct)

        safety_pct = (CT_BASE_SAFETY_PCT
                      + (CT_MAX_SAFETY_PCT - CT_BASE_SAFETY_PCT) * (1.0 - wx_score))
        floor_pct = backup_reserve + effective_peak_need_pct + safety_pct
        hard_min = backup_reserve + effective_peak_need_pct + CT_MIN_FLOOR_ABOVE_RESERVE
        floor_pct = max(floor_pct, hard_min)

        target_soc = max(raw_target, floor_pct)

        # --- Build metrics ---
        metrics = {
            'ct_target_soc': round(target_soc, 1),
            'ct_floor_pct': round(floor_pct, 1),
            'ct_raw_target': round(raw_target, 1),
            'ct_remaining_solar_kwh': round(remaining_solar_kwh, 1),
            'ct_remaining_solar_pct': round(remaining_solar_pct, 1),
            'ct_safety_pct': round(safety_pct, 1),
            'ct_peak_need_pct': round(peak_need_pct, 1),
            'ct_effective_peak_need_pct': round(effective_peak_need_pct, 1),
            'ct_solar_refill_pct': round(solar_refill_pct, 1),
            'ct_wx_score': round(wx_score, 2),
            'ct_forecast_source': forecast_source,
            'soc': round(state.soc_percent, 1),
            'hours_to_peak': round(hours_to_peak, 1) if hours_to_peak is not None else None,
        }

        # --- Check if CT should engage ---
        # CT stays active when:
        #   - Tomorrow's forecast is loaded (drain management)
        #   - Solar is currently producing (fill management)
        #   - Remaining forecast is significant (> threshold)
        # CT disengages only when remaining solar is tiny, no solar producing,
        # and we're not using tomorrow's forecast for drain planning.
        solar_producing = state.solar_kw > MIN_SOLAR_PRODUCING_KW
        ct_should_engage = (
            remaining_solar_kwh >= CT_MIN_FORECAST_SOLAR_KWH
            or forecast_source == 'forecast_tomorrow'
            or solar_producing
        )
        if not ct_should_engage:
            return None

        # --- Compare SOC to target ---
        soc = state.soc_percent
        above_target = soc > target_soc + CT_TOLERANCE_PCT
        below_target = soc < target_soc - CT_TOLERANCE_PCT
        at_target = not above_target and not below_target

        now_hour = state.timestamp.hour + state.timestamp.minute / 60.0
        solar_ramp_hour = self._get_solar_ramp_hour(state.timestamp)
        sunset_hour = self._get_sunset_hour(state.timestamp)
        solar_exceeds_load = state.solar_kw > avg_load_kw

        metrics['ct_solar_ramp_hour'] = round(solar_ramp_hour, 1)
        metrics['ct_sunset_hour'] = round(sunset_hour, 1)

        # =====================================================================
        # SOC ABOVE TARGET — need to drain
        # =====================================================================
        if above_target:
            drain_pct = soc - target_soc
            metrics['ct_drain_pct'] = round(drain_pct, 1)

            # Case A: Solar is producing and exceeds home load.
            # SC is ideal — solar powers home, surplus trickles to battery slowly,
            # battery may even drain slightly if load > solar.
            if solar_exceeds_load:
                return self._decide(
                    state, "self_consumption",
                    f"CT: SOC {soc:.0f}% > target {target_soc:.0f}%, "
                    f"solar {state.solar_kw:.1f}kW > load — SC to throttle fill",
                    confidence=0.85, priority=7,
                    action="switch_to_self_consumption", metrics=metrics,
                )

            # Case B: Solar producing but below load — SC drains battery
            # because home draws difference from battery.
            if solar_producing and not solar_exceeds_load:
                return self._decide(
                    state, "self_consumption",
                    f"CT: SOC {soc:.0f}% > target {target_soc:.0f}%, "
                    f"solar {state.solar_kw:.1f}kW < load — SC to drain",
                    confidence=0.85, priority=7,
                    action="switch_to_self_consumption", metrics=metrics,
                )

            # Case C: No solar (evening/overnight/pre-dawn).
            # SOC is above target — drain via SC. The home runs off battery,
            # SOC drops toward target. This is the continuous approach: if
            # SOC is above where it should be, act now. The target already
            # incorporates the floor (reserve + peak need + safety), so
            # draining to target is always safe.
            #
            # The cost is small: off-peak grid rate × kWh drained (~$0.34
            # for 10% SOC). The alternative — waiting and then curtailing
            # solar tomorrow — costs 3-7x more. Starting during high-load
            # evening hours is also more efficient (3.5 kW vs 1.0 kW overnight),
            # so acting now rather than deferring gets the drain done faster.
            return self._decide(
                state, "self_consumption",
                f"CT: SOC {soc:.0f}% > target {target_soc:.0f}%, "
                f"no solar — SC to drain "
                f"({drain_pct:.0f}% = {drain_pct/100*battery_kwh:.1f}kWh)",
                confidence=0.85, priority=7,
                action="switch_to_self_consumption", metrics=metrics,
            )

        # =====================================================================
        # SOC BELOW TARGET — need to charge
        # =====================================================================
        if below_target:
            deficit_pct = target_soc - soc
            metrics['ct_deficit_pct'] = round(deficit_pct, 1)

            # Below target = charge. The question is TOU vs SC.
            #
            # TOU: grid powers home, ALL solar goes to battery. Aggressive charge.
            # SC:  solar powers home first, only surplus charges battery. Gentle.
            #
            # Last-responsible-moment approach:
            #   1. Stay in TOU until solar actually exceeds home load RIGHT NOW.
            #      No projections, no forecasts — observe reality. This prevents
            #      premature SC switches during the morning ramp when solar is
            #      0.1-0.3 kW and would just drain the battery in SC mode.
            #
            #   2. Once solar > load (observed, not forecast), SC is net-positive
            #      for the battery. Run the projection to confirm SC can reach
            #      the survival floor by peak. If yes, commit to SC.
            #
            #   3. If solar > load but projection says SC can't make it to the
            #      floor, stay in TOU — we need the aggressive charge rate.
            #
            # This eliminates the entire class of morning flapping: the engine
            # never considers SC until solar is genuinely productive, and once
            # it commits, the projection uses intraday-corrected values that
            # reflect reality rather than the crushed forecast.

            if (state.solar_kw > state.home_load_kw
                    and hours_to_peak is not None
                    and hours_to_peak > 0
                    and state.current_mode != 'emergency_backup'):

                # When peak is far away (>24h, e.g., weekend), projecting to
                # peak is meaningless — 12 hours of overnight drain makes SC
                # always look bad. Instead, project to end of today's solar.
                # The question becomes: "will SC keep SOC healthy through
                # today's remaining sun?" not "can SC get me to Monday peak?"
                #
                # When peak is today/tomorrow (<24h), project to peak as before.
                if hours_to_peak > 24:
                    hours_to_sunset = max(0, sunset_hour - now_hour)
                    projection_hours = max(1, hours_to_sunset)
                    # On no-peak days, survival floor is just the CT floor —
                    # we don't need peak_need margin, just enough to not drain
                    # below reserve overnight.
                    survival_floor = floor_pct
                else:
                    projection_hours = hours_to_peak
                    survival_floor = backup_reserve + peak_need_pct + CT_PEAK_SURVIVAL_MARGIN_PCT

                sc_projected_soc = self._project_soc_in_sc(
                    state, projection_hours, battery_kwh
                )

                # Log the intraday correction factor for diagnostics
                solar_correction = self._compute_intraday_solar_factor(state)
                metrics['ct_sc_projected_soc'] = round(sc_projected_soc, 1)
                metrics['ct_survival_floor'] = round(survival_floor, 1)
                metrics['ct_solar_correction'] = round(solar_correction, 2)
                metrics['ct_projection_hours'] = round(projection_hours, 1)

                horizon = 'sunset' if hours_to_peak > 24 else 'peak'

                # SC commit gate. The threshold is the higher of survival floor
                # and (target_soc - small margin). Comparing only against survival
                # floor causes SC to commit any time projection clears "survive
                # peak" — even when projection is well below target. On small
                # solar or cloudy days this locks the engine into SC for the rest
                # of the day, preempting EB gap charging entirely. Using the
                # target-aware threshold means SC commits when projection will
                # land close to target; otherwise we return TOU and the chained
                # EB gap evaluation gets to apply last-responsible-moment timing.
                sc_commit_threshold = max(
                    survival_floor,
                    target_soc - CT_SC_COMMIT_MARGIN_PCT,
                )
                metrics['ct_sc_commit_threshold'] = round(sc_commit_threshold, 1)

                if sc_projected_soc >= sc_commit_threshold:
                    return self._decide(
                        state, "self_consumption",
                        f"CT: SOC {soc:.0f}% < target {target_soc:.0f}%, "
                        f"solar {state.solar_kw:.1f}kW > load {state.home_load_kw:.1f}kW, "
                        f"SC projects to {sc_projected_soc:.0f}% by {horizon} "
                        f"(need {sc_commit_threshold:.0f}%) — SC commit",
                        confidence=0.85, priority=7,
                        action="switch_to_self_consumption", metrics=metrics,
                    )
                else:
                    metrics['ct_sc_rejected'] = True
                    return self._decide(
                        state, "time_of_use",
                        f"CT: SOC {soc:.0f}% < target {target_soc:.0f}%, "
                        f"solar > load but SC projects only {sc_projected_soc:.0f}% by {horizon} "
                        f"(need {sc_commit_threshold:.0f}%) — TOU for full charge",
                        confidence=0.8, priority=7,
                        action="switch_to_tou", metrics=metrics,
                    )

            # Already in SC during solar hours with SOC rising — hold SC.
            #
            # When the system committed to SC earlier and SOC has been climbing,
            # a momentary dip in solar (cloud, HVAC spike) doesn't warrant
            # switching to TOU. The risk of staying in SC is tiny (lose ~1% SOC
            # per 30 min if solar < load) while the risk of switching to TOU is
            # pushing SOC toward 99% faster than needed, especially on no-peak days.
            #
            # Only break SC if: solar has genuinely ended (near sunset), SOC is
            # dropping toward the floor, or solar has been gone for a sustained period.
            if (state.current_mode == 'self_consumption'
                    and solar_producing
                    and now_hour < sunset_hour - 0.5):
                # Check if SOC has been rising — compare to reading from ~1 hour ago
                soc_was_rising = False
                try:
                    import sqlite3
                    db_path = os.path.join(os.getenv('DATA_DIR', '/app/data'), 'franklin.db')
                    conn = sqlite3.connect(db_path, timeout=5)
                    cutoff = (state.timestamp - timedelta(minutes=90)).strftime('%Y-%m-%d %H:%M:%S')
                    row = conn.execute(
                        "SELECT MIN(soc_pct) FROM system_readings "
                        "WHERE timestamp >= ? AND soc_pct IS NOT NULL",
                        (cutoff,)
                    ).fetchone()
                    conn.close()
                    if row and row[0] is not None:
                        soc_was_rising = (soc > row[0])
                except Exception:
                    pass

                if soc_was_rising and soc > floor_pct:
                    return self._decide(
                        state, "self_consumption",
                        f"CT: SOC {soc:.0f}% < target {target_soc:.0f}%, "
                        f"solar {state.solar_kw:.1f}kW ≤ load {state.home_load_kw:.1f}kW "
                        f"but SC committed, SOC rising — hold SC",
                        confidence=0.8, priority=7,
                        action="hold", metrics=metrics,
                    )

            return self._decide(
                state, "time_of_use",
                f"CT: SOC {soc:.0f}% < target {target_soc:.0f}%, "
                f"solar {state.solar_kw:.1f}kW ≤ load {state.home_load_kw:.1f}kW — TOU",
                confidence=0.8, priority=7,
                action="switch_to_tou", metrics=metrics,
            )

        # =====================================================================
        # SOC AT TARGET — hold (dead band)
        # =====================================================================
        if at_target:
            # If near peak and SOC covers peak need, ride SC into peak
            if (hours_to_peak is not None and hours_to_peak <= 3.0
                    and soc >= backup_reserve + peak_need_pct):
                return self._decide(
                    state, "self_consumption",
                    f"CT: SOC {soc:.0f}% ≈ target {target_soc:.0f}%, "
                    f"{hours_to_peak:.1f}h to peak — riding SC into peak",
                    confidence=0.85, priority=7,
                    action="switch_to_self_consumption", metrics=metrics,
                )

            # Otherwise hold current mode
            hold_mode = state.current_mode if state.current_mode in (
                'time_of_use', 'self_consumption') else 'time_of_use'
            return self._decide(
                state, hold_mode,
                f"CT: SOC {soc:.0f}% ≈ target {target_soc:.0f}% "
                f"(±{CT_TOLERANCE_PCT:.0f}%) — hold {hold_mode}",
                confidence=0.8, priority=7,
                action="hold", metrics=metrics,
            )

        return None  # Should not reach here

    # ===================================================================
    # Partial-Peak Conditional Protection (Priority 4.5)
    # ===================================================================

    def _evaluate_partial_peak(self, state: SystemState) -> Optional[Decision]:
        """Decide what to do during a partial-peak window.

        On three-tier rate plans (e.g., PG&E EV2-A), partial-peak windows
        wrap the sacred peak window — typically 3-4pm before peak and
        9pm-midnight after peak. Mid-priced: avoid imports when possible
        but accept gracefully when battery can't cover the full expensive
        window. Peak coverage is prioritized over partial-peak coverage.

        Returns:
            Decision when in a partial-peak window (caller may chain to
            EB gap eval on TOU action). Returns None if not in partial-peak
            (caller should fall through to subsequent priorities).
        """
        if not state.is_partial_peak:
            return None

        battery_kwh = self.config.get(
            'battery_capacity_kwh',
            getattr(getattr(self.profile, 'capacity', None),
                    'total_capacity_kwh', 27.2)
        )
        reserve_pct = self.config.get('backup_reserve_pct', 20.0)
        available_kwh = max(
            0.0,
            (state.soc_percent - reserve_pct) / 100.0 * battery_kwh
        )

        metrics = {
            'soc': round(state.soc_percent, 1),
            'p45_available_kwh': round(available_kwh, 1),
            'p45_peak_remaining_h': round(state.peak_remaining_hours, 1),
            'p45_partial_remaining_h': round(state.partial_peak_remaining_hours, 1),
        }

        # Track curtailment when SOC is high with solar producing — matches
        # the P4 / P5 pattern so partial-peak doesn't blind the curtailment log.
        if (state.soc_percent >= CURTAILMENT_SOC_THRESHOLD
                and state.solar_kw > MIN_SOLAR_PRODUCING_KW):
            self._track_curtailment(state)

        # --- Post-peak partial-peak (today's peak is already done) ---
        # The next peak is tomorrow — overnight off-peak charging plus
        # tomorrow's solar will refill the battery. No reason to preserve
        # battery now. Discharging saves ~$0.10/kWh vs partial-peak imports.
        if state.peak_remaining_hours <= 0:
            if available_kwh > 0.5:
                return self._decide(
                    state, "self_consumption",
                    f"Partial-peak (post-peak): SOC {state.soc_percent:.0f}% — "
                    f"discharging {available_kwh:.1f}kWh remaining, "
                    f"saves vs partial-peak imports",
                    confidence=0.85, priority=4,
                    action="switch_to_self_consumption", metrics=metrics,
                )
            return self._decide(
                state, "time_of_use",
                f"Partial-peak (post-peak): SOC {state.soc_percent:.0f}% at reserve — "
                f"TOU, let grid power home",
                confidence=0.85, priority=4,
                action="switch_to_tou", metrics=metrics,
            )

        # --- Pre-peak partial-peak (sacred peak still ahead today) ---
        # Forecast peak demand and decide whether battery can afford to
        # discharge through this partial-peak window while still covering peak.
        peak_demand_kwh = self._forecast_peak_demand(state)
        threshold = peak_demand_kwh + P45_SAFETY_MARGIN_KWH
        metrics['p45_peak_demand_kwh'] = round(peak_demand_kwh, 1)
        metrics['p45_safety_margin_kwh'] = round(P45_SAFETY_MARGIN_KWH, 1)
        metrics['p45_threshold_kwh'] = round(threshold, 1)

        if available_kwh >= threshold:
            return self._decide(
                state, "self_consumption",
                f"Partial-peak (pre-peak): {available_kwh:.1f}kWh available ≥ "
                f"{peak_demand_kwh:.1f}kWh peak need + {P45_SAFETY_MARGIN_KWH:.1f}kWh margin — "
                f"discharging through",
                confidence=0.85, priority=4,
                action="switch_to_self_consumption", metrics=metrics,
            )

        return self._decide(
            state, "time_of_use",
            f"Partial-peak (pre-peak): {available_kwh:.1f}kWh available < "
            f"{peak_demand_kwh:.1f}kWh peak need + {P45_SAFETY_MARGIN_KWH:.1f}kWh margin — "
            f"TOU to preserve battery for peak",
            confidence=0.85, priority=4,
            action="switch_to_tou", metrics=metrics,
        )

    def _forecast_peak_demand(self, state: SystemState) -> float:
        """Estimate kWh battery draw during the upcoming peak window.

        Walks hour-by-hour across the next peak window summing the learned
        hourly load profile. Does not currently subtract expected solar
        contribution — evening peaks (typical for PG&E and most CA utilities)
        see minimal solar overlap, and v1 is intentionally conservative to
        favor preserving battery for peak.

        Returns 0.0 if no peak window is found in the schedule (e.g.,
        weekends/holidays on rate plans without daily peak windows).
        """
        peak_start = self.rates.next_peak_start(state.timestamp)
        if peak_start is None:
            return 0.0

        peak_end = self.rates.next_peak_end(state.timestamp)
        if peak_end is None or peak_end <= peak_start:
            # Fallback: use peak_duration_hours from state
            peak_dur = state.peak_duration_hours if state.peak_duration_hours > 0 else 3.0
            peak_end = peak_start + timedelta(hours=peak_dur)

        total_kwh = 0.0
        check = peak_start
        safety_limit = peak_start + timedelta(hours=12)  # never walk past 12h

        while check < peak_end and check < safety_limit:
            # Walk to the top of the next hour (or to peak_end, whichever first)
            next_hour = check.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            slot_end = min(next_hour, peak_end)
            hours_in_slot = (slot_end - check).total_seconds() / 3600.0
            hour_idx = check.hour
            if 0 <= hour_idx < len(self._hourly_load_kw):
                load_kw = self._hourly_load_kw[hour_idx]
            else:
                load_kw = 2.0  # safe default
            total_kwh += load_kw * hours_in_slot
            check = slot_end

        return total_kwh

    # ===================================================================
    # EB Gap Charging
    # ===================================================================

    def _resolve_hours_to_next_peak(self, state: SystemState) -> Optional[float]:
        """Resolve hours until the next peak period, even on weekends/holidays.

        state.hours_to_peak may be None on non-peak days (weekends, holidays).
        This helper falls through to next_peak_start() to find the actual next
        peak, which might be Monday or the next weekday. Returns None only if
        TOU is completely disabled.
        """
        if state.hours_to_peak is not None and state.hours_to_peak > 0:
            return state.hours_to_peak

        if state.is_peak:
            return 0.0

        next_peak = self.rates.next_peak_start(state.timestamp)
        if next_peak is not None:
            delta = (next_peak - state.timestamp).total_seconds() / 3600.0
            return max(0.0, delta)

        return None

    def _evaluate_eb_gap(self, state: SystemState) -> Optional[Decision]:
        """EB gap charging — grid charges only what solar can't provide.

        Only relevant within 12 hours of peak. Uses last-responsible-moment
        deferral to avoid premature grid charging. EB is aggressive (~8kW)
        and charges fast, so there's never a reason to start hours early.

        Falls through to legacy gap logic when forecast engine is unavailable.
        """
        if state.is_peak:
            return None

        hours_to_peak = self._resolve_hours_to_next_peak(state)
        if hours_to_peak is None or hours_to_peak <= 0:
            return None
        if hours_to_peak > 12:
            return None

        plan = self._get_morning_plan(state.soc_percent, state.timestamp)
        if plan is not None:
            return self._evaluate_gap_with_plan(state, plan, hours_to_peak)

        return self._evaluate_gap_legacy(state)

    def _evaluate_gap_with_plan(self, state: SystemState, plan: 'MorningPlan',
                                 hours_to_peak: float) -> Optional[Decision]:
        """P7 EB gap charging with forecast — grid charges only what solar can't provide.

        EB deferral philosophy: Emergency Backup is aggressive (~8kW grid charging).
        It charges fast — a typical gap takes under an hour. There is never a reason
        to trigger EB hours before peak. The engine recalculates every 30-minute cycle,
        so deferring is always safe: TOU drift and solar production may naturally shrink
        the gap, and EB can catch up later. EB should only fire when the time buffer
        is genuinely tight — i.e., we're close enough to peak that we need to act NOW.
        """
        gap_kwh = plan.gap_kwh
        ceiling_pct = plan.morning_ceiling_pct

        taper_cap = TAPER_CEILING_PCT
        if not self.solar_export and ceiling_pct > taper_cap:
            logger.debug(
                f"Taper ceiling cap: {ceiling_pct:.0f}% → {taper_cap:.0f}%"
            )
            ceiling_pct = taper_cap
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
            'hours_to_peak': round(hours_to_peak, 1),
        }

        if gap_kwh <= 0:
            return self._decide(
                state, "time_of_use",
                f"Solar surplus: {plan.recommendation}",
                confidence=0.85 if plan.forecast_source.startswith('forecast_solar') else 0.7,
                priority=7, action="switch_to_tou", metrics=metrics,
            )

        if gap_kwh < 1.0:
            return self._decide(
                state, "time_of_use",
                f"Tiny gap ({gap_kwh:.1f} kWh) — solar/natural will cover. {plan.recommendation}",
                confidence=0.8, priority=7, action="switch_to_tou", metrics=metrics,
            )

        if gap_kwh < 2.0 and state.solar_kw > 0.3 and hours_to_peak > 4:
            return self._decide(
                state, "time_of_use",
                f"Small gap ({gap_kwh:.1f} kWh) with solar producing ({state.solar_kw:.1f} kW) "
                f"and {hours_to_peak:.1f}h to peak — letting solar handle it",
                confidence=0.75, priority=7, action="switch_to_tou", metrics=metrics,
            )

        if state.soc_percent >= ceiling_pct:
            return self._decide(
                state, "time_of_use",
                f"SOC {state.soc_percent:.0f}% ≥ forecast ceiling {ceiling_pct:.0f}% — "
                f"solar fills the rest. {plan.recommendation}",
                confidence=0.85, priority=7, action="switch_to_tou",
                metrics=metrics,
            )

        cheapest_tier, cheapest_rate = self.rates.cheapest_rate_before_peak(state.timestamp)
        if state.current_rate_cents <= cheapest_rate:
            charge_time_hours = self.profile.time_to_charge_kwh(
                state.soc_percent, ceiling_pct
            ) if hasattr(self.profile, 'time_to_charge_kwh') else gap_kwh / 5.0

            if hours_to_peak <= PRE_PEAK_GATE_HOURS:
                if state.current_mode != 'emergency_backup':
                    metrics['pre_peak_gate'] = True
                    return self._decide(
                        state, state.current_mode,
                        f"Pre-peak gate: {hours_to_peak:.1f}h to peak, "
                        f"SOC {state.soc_percent:.0f}%, not in EB — "
                        f"holding {state.current_mode} (gap {gap_kwh:.1f} kWh "
                        f"not worth late EB switch)",
                        confidence=0.85, priority=7, action="hold",
                        metrics=metrics,
                    )
                else:
                    if state.soc_percent >= ceiling_pct:
                        return self._decide(
                            state, "self_consumption",
                            f"Pre-peak gate: EB reached ceiling {ceiling_pct:.0f}% "
                            f"(SOC {state.soc_percent:.0f}%) — switching to SC for peak",
                            confidence=0.9, priority=7,
                            action="switch_to_self_consumption",
                            metrics=metrics,
                        )

            safety_margin_hours = 0.5
            buffer_hours = hours_to_peak - charge_time_hours

            metrics['charge_time_hours'] = round(charge_time_hours, 2)
            metrics['buffer_hours'] = round(buffer_hours, 1)
            metrics['min_buffer_hours'] = round(safety_margin_hours, 1)

            if buffer_hours > safety_margin_hours + 1.0:
                return self._decide(
                    state, "time_of_use",
                    f"Forecast gap ({gap_kwh:.1f} kWh, {charge_time_hours:.1f}h to charge) "
                    f"but {buffer_hours:.1f}h buffer — "
                    f"no rush, deferring EB. Reassess next cycle.",
                    confidence=0.8, priority=7, action="switch_to_tou",
                    metrics=metrics,
                )

            solar_contribution_pct = (plan.forecast_remaining_kwh / gap_kwh * 100) if gap_kwh > 0 else 0
            if (state.solar_kw > MIN_SOLAR_PRODUCING_KW
                    and buffer_hours > safety_margin_hours
                    and solar_contribution_pct >= 15):
                hold_mode = state.current_mode if state.current_mode in ('time_of_use', 'self_consumption') else 'time_of_use'
                hold_action = 'hold' if state.current_mode == hold_mode else 'switch_to_tou'
                return self._decide(
                    state, hold_mode,
                    f"Forecast gap ({gap_kwh:.1f} kWh, {charge_time_hours:.1f}h to charge) "
                    f"but solar producing ({state.solar_kw:.1f} kW, "
                    f"{solar_contribution_pct:.0f}% of gap) with "
                    f"{buffer_hours:.1f}h buffer — deferring to let solar fill",
                    confidence=0.75, priority=7, action=hold_action,
                    metrics=metrics,
                )

            # Solar producing but negligible contribution (<15% of gap):
            # Don't defer EB — fall through to EB trigger below.
            # On low-solar days (rainy/overcast), 0.2 kW against a 13 kWh gap
            # should not prevent grid charging when the buffer is tight.

            if charge_time_hours <= hours_to_peak:
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
                    f"Forecast gap: {gap_kwh:.1f} kWh, only {hours_to_peak:.1f}h to peak "
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

        if not self.solar_export and target_soc > TAPER_CEILING_PCT:
            target_soc = TAPER_CEILING_PCT

        target_kwh = cap.kwh_at_soc(target_soc)

        peak_start = self.rates.next_peak_start(state.timestamp)
        if peak_start is None:
            return None
        expected_consumption_kwh = self.profile.consumption.expected_kwh(
            state.timestamp, peak_start
        )

        forecast_solar_kwh = state.forecast_solar_kwh
        net_solar_to_battery = max(0, forecast_solar_kwh - expected_consumption_kwh)
        gap_kwh = target_kwh - current_kwh - net_solar_to_battery

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
                state, "time_of_use",
                f"No charging gap — solar forecast ({forecast_solar_kwh:.1f} kWh) covers "
                f"the {target_kwh - current_kwh:.1f} kWh needed",
                confidence=0.8, priority=7,
                action="switch_to_tou", metrics=metrics,
            )

        if gap_kwh < 1.0:
            return self._decide(
                state, "time_of_use",
                f"Tiny charging gap ({gap_kwh:.1f} kWh) — not worth grid charging",
                confidence=0.8, priority=7,
                action="switch_to_tou", metrics=metrics,
            )
        if gap_kwh < 2.0 and state.solar_kw > 0.3 and state.hours_to_peak > 4:
            return self._decide(
                state, "time_of_use",
                f"Small gap ({gap_kwh:.1f} kWh) with solar producing ({state.solar_kw:.1f} kW) "
                f"and {state.hours_to_peak:.1f}h to peak — letting solar handle it",
                confidence=0.75, priority=7,
                action="switch_to_tou", metrics=metrics,
            )

        cheapest_tier, cheapest_rate = self.rates.cheapest_rate_before_peak(state.timestamp)
        if state.current_rate_cents <= cheapest_rate:
            if state.hours_to_peak <= PRE_PEAK_GATE_HOURS:
                if state.current_mode != 'emergency_backup':
                    metrics['pre_peak_gate'] = True
                    return self._decide(
                        state, state.current_mode,
                        f"Pre-peak gate: {state.hours_to_peak:.1f}h to peak, "
                        f"SOC {state.soc_percent:.0f}%, not in EB — "
                        f"holding {state.current_mode}",
                        confidence=0.85, priority=7, action="hold", metrics=metrics,
                    )
                elif state.soc_percent >= target_soc:
                    return self._decide(
                        state, "self_consumption",
                        f"Pre-peak gate: EB reached target {target_soc:.0f}% — SC for peak",
                        confidence=0.9, priority=7,
                        action="switch_to_self_consumption", metrics=metrics,
                    )

            safety_margin_hours = 0.5
            buffer_hours = state.hours_to_peak - charge_time_hours
            metrics['buffer_hours'] = round(buffer_hours, 1)

            if buffer_hours > safety_margin_hours + 1.0:
                return self._decide(
                    state, "time_of_use",
                    f"Charging gap ({gap_kwh:.1f} kWh, {charge_time_hours:.1f}h to charge) "
                    f"but {buffer_hours:.1f}h buffer — deferring EB.",
                    confidence=0.8, priority=7, action="switch_to_tou", metrics=metrics,
                )

            if (state.solar_kw > MIN_SOLAR_PRODUCING_KW
                    and buffer_hours > safety_margin_hours):
                return self._decide(
                    state, "self_consumption",
                    f"Charging gap ({gap_kwh:.1f} kWh) but solar producing "
                    f"({state.solar_kw:.1f} kW) with {buffer_hours:.1f}h buffer — deferring",
                    confidence=0.75, priority=7, action="hold", metrics=metrics,
                )

            if charge_time_hours <= state.hours_to_peak:
                return self._decide(
                    state, "emergency_backup",
                    f"Charging gap: {gap_kwh:.1f} kWh, buffer tight ({buffer_hours:.1f}h) "
                    f"— charging at {state.current_rate_cents}¢/kWh",
                    confidence=0.85, priority=7,
                    action="switch_to_backup", metrics=metrics,
                )
            else:
                return self._decide(
                    state, "emergency_backup",
                    f"Charging gap: {gap_kwh:.1f} kWh, only {state.hours_to_peak:.1f}h to peak "
                    f"(need {charge_time_hours:.1f}h) — charging urgently",
                    confidence=0.95, priority=7,
                    action="switch_to_backup", metrics=metrics,
                )
        else:
            return self._decide(
                state, "self_consumption",
                f"Charging gap ({gap_kwh:.1f} kWh) but waiting for cheaper rate "
                f"({cheapest_tier} @ {cheapest_rate}¢ vs current {state.current_rate_cents}¢)",
                confidence=0.7, priority=7,
                action="switch_to_self_consumption", metrics=metrics,
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

        # Check cooldown (only meaningful for real mode changes)
        if action in ("switch_to_backup", "switch_to_self_consumption", "switch_to_tou"):
            if self.last_mode_switch and self.last_decision and mode != state.current_mode:
                elapsed = (state.timestamp - self.last_mode_switch).total_seconds()
                if elapsed < MODE_SWITCH_COOLDOWN_S:
                    action = "hold"
                    reason += f" (cooldown: {int(MODE_SWITCH_COOLDOWN_S - elapsed)}s remaining)"

        # Track mode switches — only record when the mode actually changes.
        # Recording noop switches (target == current) poisoned cooldown state
        # for subsequent _decide() calls within the same cycle, blocking
        # legitimate follow-up evaluations (e.g. SC branch running before EB branch).
        if action in ("switch_to_backup", "switch_to_self_consumption", "switch_to_tou"):
            if mode != state.current_mode:
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
        override_path = self.config.get('override_path', 'logs/override.json')
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
            'load_profile_avg_kw': round(sum(self._hourly_load_kw) / 24.0, 2),
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
