#!/usr/bin/env python3
"""
Solar Health Monitor v1.0

Analyzes per-panel health using historical data rather than point-in-time
comparisons. Outputs solar_health_report.json for dashboard consumption.

Data sources:
  - solaredge_panel_peaks.json: lifetime peak watts, daily energy peaks, health ratios
  - solaredge_daily_history_barn.json: daily energy per panel (2,239 days)
  - solar_barn.json: current real-time production
  - solar_house.json: Enphase house array (cross-array weather reference)

Health scoring methodology:
  1. Lifetime energy comparison — panel vs string/array average lifetime Wh
  2. Recent trend analysis — rolling 7/14/30 day production vs peers
  3. Peak capacity check — current peak watts vs historical peak (age-adjusted)
  4. Cross-array weather normalization — use house array as control group
  5. Minimum production threshold — skip health scoring on low-production days

Output: solar_health_report.json with per-panel scores and array summary

Designed to run daily (or on-demand) via scheduler or cron.
"""

import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

# ─── Configuration ───────────────────────────────────────────────────────────

# Paths — adjust if running outside Docker
DATA_DIR = Path("/app/data") if Path("/app/data").exists() else Path(__file__).resolve().parent.parent / "data"
WEB_DIR = Path("/app/web") if Path("/app/web").exists() else Path(__file__).resolve().parent.parent / "web"

# Input files
PEAKS_JSON = DATA_DIR / "solaredge_panel_peaks.json"
DAILY_HISTORY_JSON = DATA_DIR / "solaredge_panel_history.json"
BARN_JSON = WEB_DIR / "solar_barn.json"
HOUSE_JSON = WEB_DIR / "solar_house.json"

# Output
HEALTH_REPORT_JSON = WEB_DIR / "solar_health_report.json"
WATCHLIST_JSON = DATA_DIR / "solar_watchlist.json"

# ─── SolarEdge Barn Configuration ────────────────────────────────────────────
PANEL_NAMEPLATE_W = 355        # LG LG355S2W-A5
PANEL_INSTALL_DATE = "2019-05-01"  # Approximate install
DEGRADATION_YEAR1 = 0.02       # 2% first year
DEGRADATION_ANNUAL = 0.005     # 0.5% per year after

# ─── Enphase House Configuration ─────────────────────────────────────────────
ENPHASE_DAILY_HISTORY_JSON = DATA_DIR / "enphase_daily_history_house.json"
ENPHASE_HOUSE_JSON = WEB_DIR / "solar_house.json"
ENPHASE_NAMEPLATE_W = 410      # IQ8+ microinverters, panel wattage
ENPHASE_INSTALL_DATE = "2025-09-01"  # Approximate install
ENPHASE_PANEL_COUNT = 16

# Watchlist — panels stay on the list even after recovery
# confidence_score: 0-100, increases when flagged, decays when healthy
WATCHLIST_ALERT_BOOST = 25     # Points added per alert detection
WATCHLIST_WATCH_BOOST = 15     # Points added per watch detection
WATCHLIST_FAIR_BOOST = 5       # Points added per fair detection
WATCHLIST_DECAY_PER_RUN = 3    # Points removed when panel appears healthy
WATCHLIST_ACTIVE_THRESHOLD = 20  # Score above this = actively on watchlist
WATCHLIST_CLEAR_THRESHOLD = 5    # Score below this = removed from watchlist

# Thresholds — tuned for real-world data where lifetime spread is ~92-107%
MIN_DAILY_WH_FOR_SCORING = 500   # Skip health checks if panel daily Wh < 500
MIN_SAMPLES_FOR_TREND = 5        # Need at least 5 good days for trend analysis
FAIR_THRESHOLD = 0.96            # < 96% of string avg = fair
WATCH_THRESHOLD = 0.93           # < 93% of string avg = watch  
ALERT_THRESHOLD = 0.90           # < 90% of string avg = alert
TREND_DECLINE_PCT = -10          # > 10% decline over 30 days = declining trend

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [HealthMonitor] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ─── Helper Functions ────────────────────────────────────────────────────────

def load_json(path):
    """Load JSON file, return empty dict/list on failure."""
    try:
        if path.exists():
            return json.loads(path.read_text())
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"Failed to load {path.name}: {e}")
    return {}


def expected_capacity_pct(install_date_str=PANEL_INSTALL_DATE):
    """Calculate expected panel capacity as % of nameplate, accounting for age degradation."""
    install = datetime.strptime(install_date_str, "%Y-%m-%d")
    age_years = (datetime.now() - install).days / 365.25
    if age_years <= 1:
        return 1.0 - (DEGRADATION_YEAR1 * age_years)
    else:
        return (1.0 - DEGRADATION_YEAR1) - (DEGRADATION_ANNUAL * (age_years - 1))


def get_string_for_serial(serial, peaks_data, serial_to_string=None):
    """Get the string assignment for a panel serial."""
    if serial_to_string and serial in serial_to_string:
        return serial_to_string[serial]
    if serial in peaks_data:
        return peaks_data[serial].get("string", "unknown")
    return "unknown"


# ─── Core Analysis ───────────────────────────────────────────────────────────

def analyze_lifetime_energy(peaks_data, serial_to_string):
    """
    Compare each panel's lifetime energy against its string average.
    Returns dict of serial -> {lifetime_wh, string_avg_wh, ratio, status}
    """
    # Group by string
    string_panels = defaultdict(list)
    for sn, p in peaks_data.items():
        string = serial_to_string.get(sn, p.get("string", "unknown"))
        lifetime = p.get("lifetime_wh", 0)
        if lifetime > 0:
            string_panels[string].append((sn, lifetime))

    results = {}
    for string, panels in string_panels.items():
        if not panels:
            continue
        avg_wh = sum(wh for _, wh in panels) / len(panels)
        for sn, wh in panels:
            ratio = wh / avg_wh if avg_wh > 0 else 1.0
            results[sn] = {
                "lifetime_wh": round(wh, 1),
                "string_avg_wh": round(avg_wh, 1),
                "lifetime_ratio": round(ratio, 4),
                "string": string,
            }
    return results


def analyze_recent_trends(daily_history, peaks_data, serial_to_string, days_back=30):
    """
    Analyze recent daily production trends per panel.
    
    daily_history format: {date: {optimizer_id: wh_value}}
    Where optimizer_id is like "1.0.1", "2.0.13" — NOT serial numbers.
    
    We need to map optimizer IDs to serial numbers using peaks_data
    which has both (via the string field like "String 1.0" and inverter info).
    
    If mapping isn't possible, we analyze by optimizer ID and the caller
    can match later.
    
    Returns dict of identifier -> {
        recent_avg_wh, string_recent_avg_wh, recent_ratio,
        trend_pct (change over period), days_analyzed
    }
    """
    if not daily_history:
        return {}

    # Handle the panel_history.json format which has a wrapper:
    # {metadata: {...}, daily: {date: {serial: wh}}, ...}
    raw_daily = daily_history
    if "daily" in daily_history and isinstance(daily_history["daily"], dict):
        raw_daily = daily_history["daily"]

    # Build records from date-keyed format: {date: {panel_id: wh}}
    records = []
    if isinstance(raw_daily, dict):
        first_key = next(iter(raw_daily), "")
        # Date-keyed format (YYYY-MM-DD -> {panel_id: wh})
        if len(first_key) == 10 and '-' in first_key:
            for date, panels in raw_daily.items():
                if isinstance(panels, dict):
                    for panel_id, wh in panels.items():
                        if isinstance(wh, (int, float)):
                            records.append({"date": date, "panel_id": panel_id, "wh": float(wh)})

    if not records:
        logger.warning("Could not parse daily history format")
        return {}

    # Build optimizer_id -> serial mapping from peaks data
    # peaks_data has string like "String 1.0" and inverter info
    # optimizer IDs are like "1.0.1" which maps to String 1.0, position 1
    # We may not have a direct mapping, so we'll analyze by optimizer_id
    # and build a cross-reference for the report

    # Filter to recent days
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    recent = [r for r in records if r.get("date", "") >= cutoff]

    if not recent:
        logger.info(f"No daily history records in last {days_back} days")
        return {}

    # Group by panel_id
    panel_daily = defaultdict(list)
    for r in recent:
        pid = r["panel_id"]
        panel_daily[pid].append({"date": r["date"], "wh": r["wh"]})

    # Sort each panel's data by date
    for pid in panel_daily:
        panel_daily[pid].sort(key=lambda x: x["date"])

    # Determine string for each panel - use serial_to_string mapping
    def get_panel_string(panel_id):
        if panel_id in serial_to_string:
            return serial_to_string[panel_id]
        if panel_id in peaks_data:
            return peaks_data[panel_id].get("string", "unknown")
        # Fallback: try optimizer ID pattern (e.g., "1.0.13" -> "String 1.0")
        parts = panel_id.split(".")
        if len(parts) >= 2 and parts[0].isdigit():
            return f"String {parts[0]}.{parts[1]}"
        return "unknown"

    # Calculate per-panel metrics
    results = {}
    string_avgs = defaultdict(list)

    # First pass: calculate how many good days each panel has
    # Panels with very few good days are FAILING, not "insufficient data"
    panel_good_day_counts = {}
    for pid, days in panel_daily.items():
        good_days = [d for d in days if d["wh"] >= MIN_DAILY_WH_FOR_SCORING]
        panel_good_day_counts[pid] = (len(good_days), len(days))

    # Find the typical number of good days for healthy panels
    all_good_counts = [gc for gc, _ in panel_good_day_counts.values()]
    median_good_days = sorted(all_good_counts)[len(all_good_counts)//2] if all_good_counts else 0

    for pid, days in panel_daily.items():
        if len(days) < MIN_SAMPLES_FOR_TREND:
            continue

        good_days = [d for d in days if d["wh"] >= MIN_DAILY_WH_FOR_SCORING]
        good_count, total_count = panel_good_day_counts[pid]
        
        # Calculate good_days_ratio — what fraction of days did this panel 
        # produce meaningful output vs healthy peers?
        good_days_ratio = good_count / median_good_days if median_good_days > 0 else 1.0

        if len(good_days) < MIN_SAMPLES_FOR_TREND:
            # Not enough good days — but this IS a signal if peers have plenty
            results[pid] = {
                "recent_avg_wh": 0,
                "days_analyzed": len(good_days),
                "days_total": len(days),
                "good_days_ratio": round(good_days_ratio, 3),
                "status": "insufficient_data" if median_good_days < 10 else "low_output",
            }
            continue

        avg_wh = sum(d["wh"] for d in good_days) / len(good_days)
        string = get_panel_string(pid)
        string_avgs[string].append((pid, avg_wh))

        # Trend: compare first half vs second half of period
        mid = len(good_days) // 2
        if mid >= 2:
            first_half_avg = sum(d["wh"] for d in good_days[:mid]) / mid
            second_half_avg = sum(d["wh"] for d in good_days[mid:]) / (len(good_days) - mid)
            trend_pct = ((second_half_avg - first_half_avg) / first_half_avg * 100) if first_half_avg > 0 else 0
        else:
            trend_pct = 0

        results[pid] = {
            "recent_avg_wh": round(avg_wh, 1),
            "days_analyzed": len(good_days),
            "days_total": len(days),
            "good_days_ratio": round(good_days_ratio, 3),
            "trend_pct": round(trend_pct, 1),
        }

    # Calculate ratios against string averages
    for string, panels in string_avgs.items():
        if not panels:
            continue
        s_avg = sum(wh for _, wh in panels) / len(panels)
        for pid, avg_wh in panels:
            if pid in results:
                results[pid]["string_recent_avg_wh"] = round(s_avg, 1)
                results[pid]["recent_ratio"] = round(avg_wh / s_avg, 4) if s_avg > 0 else 1.0

    return results


def analyze_peak_capacity(peaks_data):
    """
    Compare each panel's historical peak watts against expected capacity.
    Returns dict of serial -> {peak_watts, expected_watts, capacity_ratio}
    """
    expected_pct = expected_capacity_pct()
    expected_watts = PANEL_NAMEPLATE_W * expected_pct

    results = {}
    for sn, p in peaks_data.items():
        peak_w = p.get("peak_watts", 0)
        if peak_w > 0:
            # Use PTC rating (321.9W) as realistic ceiling, not STC nameplate
            ptc_adjusted = 321.9 * expected_pct
            ratio = peak_w / ptc_adjusted if ptc_adjusted > 0 else 1.0
            results[sn] = {
                "peak_watts": round(peak_w, 1),
                "peak_date": p.get("peak_watts_date", "unknown"),
                "expected_ptc_watts": round(ptc_adjusted, 1),
                "capacity_ratio": round(ratio, 4),
            }
    return results


def classify_panel_health(lifetime, recent, capacity, monthly, serial, opt_id=""):
    """
    Combine all analysis dimensions into a single health classification.
    
    Dimensions:
    1. Lifetime energy ratio vs string (long-term cumulative)
    2. Recent 30-day production vs string (current snapshot)
    3. Peak capacity vs expected (hardware ceiling check)
    4. Monthly historical trends (cyclical patterns, year-over-year)
    5. Good days ratio (intermittent failure detection)
    """
    details = []

    # Extract all dimension values
    lt = lifetime.get(serial, {})
    lt_ratio = lt.get("lifetime_ratio", 1.0)
    
    rc = recent.get(serial, {}) or recent.get(opt_id, {})
    rc_ratio = rc.get("recent_ratio", 1.0)
    rc_status = rc.get("status", "")
    has_recent = rc_status not in ("insufficient_data", "low_output", "") and rc_ratio > 0
    is_low_output = rc_status == "low_output"
    good_days_ratio = rc.get("good_days_ratio", 1.0)
    
    trend = rc.get("trend_pct", 0)
    
    cap = capacity.get(serial, {})
    cap_ratio = cap.get("capacity_ratio", 1.0)
    
    mt = monthly.get(serial, {})
    mt_classification = mt.get("classification", "stable")
    mt_cyclical = mt.get("cyclical_score", 0)
    mt_bad_months = mt.get("bad_month_count", 0)
    mt_yoy = mt.get("yoy_change_pct", 0)
    mt_anomalies = mt.get("anomaly_count", 0)

    # Build detail messages
    if is_low_output:
        details.append(f"Very low output — only {rc.get('days_analyzed', 0)}/{rc.get('days_total', 0)} days above threshold")
    if lt_ratio < FAIR_THRESHOLD:
        details.append(f"Lifetime energy {lt_ratio:.0%} of string avg")
    if has_recent and rc_ratio < FAIR_THRESHOLD:
        details.append(f"Recent production {rc_ratio:.0%} of string avg")
    if good_days_ratio < 0.85 and not is_low_output:
        details.append(f"Intermittent — {good_days_ratio:.0%} of expected good days")
    if mt_classification == "equipment":
        details.append(f"Historical pattern: equipment failure ({mt_bad_months} bad months, cyclical:{mt_cyclical})")
    elif mt_classification == "declining":
        details.append(f"Historical pattern: declining {mt_yoy:+.0f}% year-over-year")
    elif mt_classification == "shading":
        details.append(f"Historical pattern: possible shading ({mt_bad_months} bad months, winter only)")
    elif mt_classification == "intermittent":
        details.append(f"Historical pattern: intermittent ({mt_bad_months} bad months)")
    if mt_anomalies > 0:
        details.append(f"Data anomalies: {mt_anomalies} readings above theoretical max")
    if trend < TREND_DECLINE_PCT:
        details.append(f"Declining {trend:+.0f}% trend")
    elif trend > 15 and has_recent and rc_ratio < FAIR_THRESHOLD:
        details.append(f"Recovering {trend:+.0f}% trend")
    if cap_ratio > 0 and cap_ratio < 0.80:
        details.append(f"Peak capacity {cap_ratio:.0%} of expected")

    # ─── Classification Logic ────────────────────────────────────────
    
    lifetime_bad = lt_ratio < WATCH_THRESHOLD
    lifetime_low = lt_ratio < FAIR_THRESHOLD
    recent_bad = has_recent and rc_ratio < 0.85
    recent_low = has_recent and rc_ratio < FAIR_THRESHOLD
    declining = trend < TREND_DECLINE_PCT
    recovering = trend > 10
    strong_recovery = trend > 18
    intermittent = good_days_ratio < 0.80
    equipment_issue = mt_classification == "equipment"
    historical_decline = mt_classification == "declining"
    cyclical = mt_cyclical >= 60 and mt_bad_months >= 1  # Need both cycles AND bad months
    
    # ALERT: Confirmed failures
    if is_low_output:
        status = "alert"
    elif equipment_issue and (recent_bad or is_low_output or lifetime_bad):
        # Historical equipment pattern + current confirmation
        status = "alert"
    elif recent_bad and lifetime_bad:
        status = "alert"
    elif recent_bad and declining:
        status = "alert"
    elif has_recent and rc_ratio < 0.80 and not strong_recovery:
        status = "alert"
    elif recent_bad and not recovering:
        status = "alert"
    # WATCH: Strong signals or historical patterns without current confirmation
    elif equipment_issue:
        # Historical equipment issue but currently OK — keep watching
        status = "watch"
    elif cyclical:
        # Known cyclical failure pattern
        status = "watch"
    elif recent_bad and recovering:
        status = "watch"
    elif intermittent and not recovering:
        status = "watch"
    elif lifetime_bad and not has_recent:
        status = "watch"
    elif has_recent and rc_ratio < 0.90 and not recovering:
        status = "watch"
    elif historical_decline:
        status = "watch"
    # FAIR: Minor concerns
    elif recent_low and not recovering:
        status = "fair"
    elif lifetime_low and recent_low:
        status = "fair"
    elif lifetime_low:
        status = "fair"
    elif mt_classification == "shading":
        status = "fair"
    # EXCELLENT: Above average
    elif lt_ratio > 1.02 and (not has_recent or rc_ratio > 1.0):
        status = "excellent"
    elif has_recent and rc_ratio > 1.03:
        status = "excellent"
    else:
        status = "good"

    score_components = []
    if lt_ratio > 0:
        score_components.append(min(5, lt_ratio / 0.20))
    if has_recent:
        score_components.append(min(5, rc_ratio / 0.20))
    avg_score = sum(score_components) / len(score_components) if score_components else 3.0

    return status, round(avg_score, 2), details


# ─── Warranty-Style Historical Analysis ──────────────────────────────────────

def analyze_monthly_trends(daily_history, serial_to_string):
    """
    Port of the warranty analysis methodology:
    1. Monthly ratios vs array median — weather-normalized comparison
    2. Year-over-year comparison — separate shading from equipment failure  
    3. Cyclical pattern detection — drop/recover/drop patterns
    4. Anomaly detection — physically impossible readings
    
    Uses the full historical dataset (2,235 days) not just last 30.
    
    Returns dict of serial -> {
        bad_months: count of months below 70% of array,
        worst_month_ratio: lowest monthly ratio ever,
        yoy_decline: year-over-year change (negative = degrading),
        cyclical_score: 0-100 measuring drop/recover pattern strength,
        anomaly_count: readings above theoretical max,
        classification: "equipment"|"shading"|"stable"|"declining"
    }
    """
    if not daily_history:
        return {}
    
    # Extract daily data
    raw_daily = daily_history
    if "daily" in daily_history and isinstance(daily_history["daily"], dict):
        raw_daily = daily_history["daily"]
    
    if not raw_daily or not isinstance(raw_daily, dict):
        return {}
    
    # Check first key is a date
    first_key = next(iter(raw_daily), "")
    if not (len(first_key) == 10 and '-' in first_key):
        return {}
    
    # Build monthly data: {month: {serial: [daily_wh_values]}}
    from statistics import median
    monthly = defaultdict(lambda: defaultdict(list))
    
    for date_str, panels in raw_daily.items():
        if not isinstance(panels, dict):
            continue
        month_key = date_str[:7]  # YYYY-MM
        for sn, wh in panels.items():
            if isinstance(wh, (int, float)) and wh > 10:
                monthly[month_key][sn].append(float(wh))
    
    if not monthly:
        return {}
    
    sorted_months = sorted(monthly.keys())
    logger.info(f"Monthly trend analysis: {len(sorted_months)} months ({sorted_months[0]} to {sorted_months[-1]})")
    
    # Compute monthly ratios vs array median
    monthly_ratios = defaultdict(dict)  # serial -> {month: ratio}
    for m in sorted_months:
        m_data = monthly[m]
        # Get median daily output per panel for this month
        panel_medians = {}
        for sn, vals in m_data.items():
            if len(vals) >= 3:
                panel_medians[sn] = median(vals)
        
        if not panel_medians:
            continue
        
        array_med = median(panel_medians.values())
        if array_med > 0:
            for sn, med in panel_medians.items():
                monthly_ratios[sn][m] = med / array_med
    
    # Theoretical max daily Wh (355W × 8 peak hours × 1.1 safety margin)
    THEORETICAL_MAX_WH = 355 * 8 * 1.1  # ~3,124 Wh
    
    results = {}
    for sn in monthly_ratios:
        ratios = monthly_ratios[sn]
        if len(ratios) < 6:
            continue
        
        # 1. Bad months: count months below 70% of array median
        bad_months = [(m, r) for m, r in ratios.items() if r < 0.70]
        
        # 2. Worst month
        worst_ratio = min(ratios.values()) if ratios else 1.0
        
        # 3. Year-over-year comparison
        # Group by calendar month, compare across years
        by_cal_month = defaultdict(list)  # {01: [(2024, ratio), (2025, ratio)]}
        for m, r in ratios.items():
            year = int(m[:4])
            cal_month = m[5:7]
            by_cal_month[cal_month].append((year, r))
        
        yoy_changes = []
        for cal_month, year_ratios in by_cal_month.items():
            if len(year_ratios) >= 2:
                year_ratios.sort()
                # Compare most recent year to earliest year
                earliest_r = year_ratios[0][1]
                latest_r = year_ratios[-1][1]
                if earliest_r > 0:
                    yoy_changes.append((latest_r - earliest_r) / earliest_r * 100)
        
        avg_yoy = sum(yoy_changes) / len(yoy_changes) if yoy_changes else 0
        
        # 4. Cyclical pattern detection
        # Look for drop-recover-drop patterns: ratio goes below 0.85, 
        # comes back above 0.95, then drops below 0.85 again
        sorted_ratio_values = [ratios[m] for m in sorted(ratios.keys())]
        cycles = 0
        in_dip = False
        recovered = False
        for r in sorted_ratio_values:
            if r < 0.85:
                if recovered:
                    cycles += 1  # completed a cycle
                    recovered = False
                in_dip = True
            elif r > 0.95 and in_dip:
                recovered = True
                in_dip = False
        cyclical_score = min(100, cycles * 30)  # 0-100, each cycle adds 30
        
        # 5. Anomaly detection — only count ISOLATED anomalies, not string-wide events
        # The April 2023 event affected ALL String 2 optimizers simultaneously,
        # which is a data/inverter issue, not a per-panel problem.
        anomaly_dates = []
        for m_key in sorted_months:
            for wh in monthly[m_key].get(sn, []):
                if wh > THEORETICAL_MAX_WH:
                    anomaly_dates.append(m_key)
        # We'll filter string-wide anomalies below after collecting all panels
        anomaly_count_raw = len(anomaly_dates)
        
        # 6. Classification
        # Require BOTH cyclical pattern AND bad months for "equipment" 
        # Cyclical score alone (without bad months) just means normal seasonal variation
        string = serial_to_string.get(sn, "unknown")
        bad_in_summer = any(m[5:7] in ('05', '06', '07', '08') for m, r in bad_months)
        
        if len(bad_months) >= 4 and bad_in_summer:
            classification = "equipment"
        elif cyclical_score >= 60 and len(bad_months) >= 1:
            classification = "equipment"
        elif avg_yoy < -10:
            classification = "declining"
        elif len(bad_months) >= 3 and not bad_in_summer:
            classification = "shading"
        elif len(bad_months) >= 1:
            classification = "intermittent"
        else:
            classification = "stable"
        
        results[sn] = {
            "bad_month_count": len(bad_months),
            "bad_months": [(m, round(r, 3)) for m, r in sorted(bad_months)],
            "worst_month_ratio": round(worst_ratio, 3),
            "yoy_change_pct": round(avg_yoy, 1),
            "cyclical_score": cyclical_score,
            "anomaly_count_raw": anomaly_count_raw,
            "anomaly_dates": anomaly_dates,
            "classification": classification,
            "months_analyzed": len(ratios),
            "string": string,
        }
    
    # Post-processing: filter string-wide anomalies
    # If >50% of panels on a string have anomalies in the same month,
    # it's a string/inverter issue, not a per-panel problem
    string_anomaly_months = defaultdict(lambda: defaultdict(int))
    string_panel_counts = defaultdict(int)
    for sn, r in results.items():
        s = r.get("string", "unknown")
        string_panel_counts[s] += 1
        for m in r.get("anomaly_dates", []):
            string_anomaly_months[s][m] += 1
    
    # Identify string-wide anomaly months (>50% of string affected)
    string_wide_months = set()
    for s, months in string_anomaly_months.items():
        panel_count = string_panel_counts[s]
        for m, count in months.items():
            if count > panel_count * 0.5:
                string_wide_months.add((s, m))
    
    if string_wide_months:
        logger.info(f"Filtered {len(string_wide_months)} string-wide anomaly events "
                    f"(e.g., {list(string_wide_months)[:3]})")
    
    # Remove string-wide anomalies from per-panel counts
    for sn, r in results.items():
        s = r.get("string", "unknown")
        isolated = [m for m in r.get("anomaly_dates", []) if (s, m) not in string_wide_months]
        r["anomaly_count"] = len(isolated)
        # Keep raw count for reference
        r["anomaly_count_string_wide"] = r["anomaly_count_raw"] - len(isolated)
    
    # Log findings
    equipment = [sn for sn, r in results.items() if r["classification"] == "equipment"]
    declining = [sn for sn, r in results.items() if r["classification"] == "declining"]
    shading = [sn for sn, r in results.items() if r["classification"] == "shading"]
    intermittent = [sn for sn, r in results.items() if r["classification"] == "intermittent"]
    
    logger.info(f"Monthly trends: {len(equipment)} equipment, {len(declining)} declining, "
                f"{len(shading)} shading, {len(intermittent)} intermittent, "
                f"{len(results) - len(equipment) - len(declining) - len(shading) - len(intermittent)} stable")
    
    return results



# ─── Enphase House Array Analysis ────────────────────────────────────────────

def analyze_enphase_house():
    """
    Analyze Enphase house array health.
    
    Data available:
    - enphase_daily_history_house.json: 30-day rolling, per-panel daily stats
      Format: {date: {serial: {max_watts_today, peak_max, cumulative_watts, samples}}}
    - solar_house.json: current real-time production
    
    With only ~30 days of history and a young system (< 1 year), we focus on:
    1. Relative comparison — panels vs array average (current snapshot)
    2. Peak capacity — max_watts_today vs maxReportWatts (firmware peak)
    3. Consistency — how many days each panel hit a meaningful output
    
    Returns dict of results similar to SolarEdge format for unified reporting.
    """
    daily_history = load_json(ENPHASE_DAILY_HISTORY_JSON)
    house_current = load_json(ENPHASE_HOUSE_JSON)
    
    if not daily_history:
        logger.info("No Enphase daily history found — skipping house array")
        return {}
    
    logger.info(f"Enphase daily history: {len(daily_history)} days")
    
    # Build per-panel stats across all available days
    panel_stats = defaultdict(lambda: {
        "total_cumulative_watts": 0,
        "total_samples": 0,
        "max_watts_ever": 0,
        "firmware_max": 0,
        "good_days": 0,
        "total_days": 0,
        "daily_maxes": [],
    })
    
    # Enphase threshold — lower than SolarEdge since these are smaller panels
    # and the system is roof-mounted (more shading potential)
    ENPHASE_MIN_DAILY_MAX_W = 100  # Panel should hit at least 100W peak on a good day
    
    for date_str, panels in sorted(daily_history.items()):
        if not isinstance(panels, dict):
            continue
        for serial, stats in panels.items():
            if not isinstance(stats, dict):
                continue
            ps = panel_stats[serial]
            ps["total_days"] += 1
            
            max_today = stats.get("max_watts_today", 0)
            firmware_max = stats.get("peak_max", 0)
            cumulative = stats.get("cumulative_watts", 0)
            samples = stats.get("samples", 0)
            
            ps["daily_maxes"].append(max_today)
            ps["total_cumulative_watts"] += cumulative
            ps["total_samples"] += samples
            ps["max_watts_ever"] = max(ps["max_watts_ever"], max_today)
            ps["firmware_max"] = max(ps["firmware_max"], firmware_max)
            
            if max_today >= ENPHASE_MIN_DAILY_MAX_W:
                ps["good_days"] += 1
    
    if not panel_stats:
        logger.info("No Enphase panel data found in daily history")
        return {}
    
    # Calculate per-panel averages and ratios
    # Use average daily max watts as the comparison metric
    panel_avg_max = {}
    for serial, ps in panel_stats.items():
        good_maxes = [m for m in ps["daily_maxes"] if m >= ENPHASE_MIN_DAILY_MAX_W]
        if good_maxes:
            panel_avg_max[serial] = sum(good_maxes) / len(good_maxes)
        else:
            panel_avg_max[serial] = 0
    
    array_avg = sum(panel_avg_max.values()) / len(panel_avg_max) if panel_avg_max else 0
    
    # Find median good days for consistency check
    good_day_counts = [ps["good_days"] for ps in panel_stats.values()]
    median_good_days = sorted(good_day_counts)[len(good_day_counts) // 2] if good_day_counts else 0
    
    results = {}
    for serial, ps in panel_stats.items():
        avg_max = panel_avg_max.get(serial, 0)
        ratio = avg_max / array_avg if array_avg > 0 else 1.0
        good_days_ratio = ps["good_days"] / median_good_days if median_good_days > 0 else 1.0
        
        # Classify
        is_low = ps["good_days"] < max(3, median_good_days * 0.5)
        details = []
        
        if is_low:
            status = "alert"
            details.append(f"Very low output — only {ps['good_days']}/{ps['total_days']} days above threshold")
        elif ratio < 0.85:
            status = "watch"
            details.append(f"Production {ratio:.0%} of array avg")
        elif ratio < 0.93:
            status = "fair"
            details.append(f"Production {ratio:.0%} of array avg")
        elif ratio > 1.03:
            status = "excellent"
        else:
            status = "good"
        
        if good_days_ratio < 0.80 and not is_low:
            details.append(f"Intermittent — {good_days_ratio:.0%} of expected good days")
            if status == "good":
                status = "fair"
        
        results[serial] = {
            "serial": serial,
            "array": "house",
            "string": "Enphase",
            "status": status,
            "score": round(ratio * 5, 2),
            "details": details,
            # Recent (all we have is ~30 days)
            "recent_avg_max_watts": round(avg_max, 1),
            "recent_ratio": round(ratio, 4),
            "recent_days": ps["good_days"],
            "good_days_ratio": round(good_days_ratio, 3),
            "total_days": ps["total_days"],
            # Capacity
            "max_watts_ever": ps["max_watts_ever"],
            "firmware_max": ps["firmware_max"],
            # Placeholders for unified format
            "lifetime_wh": 0,
            "lifetime_ratio": 1.0,
            "trend_pct": 0,
            "peak_watts": ps["max_watts_ever"],
            "peak_date": "",
            "capacity_ratio": ps["max_watts_ever"] / ENPHASE_NAMEPLATE_W if ENPHASE_NAMEPLATE_W > 0 else 1.0,
            "monthly_classification": "",
            "bad_month_count": 0,
            "cyclical_score": 0,
            "yoy_change_pct": 0,
            "anomaly_count": 0,
        }
    
    # Count statuses
    counts = defaultdict(int)
    for r in results.values():
        counts[r["status"]] += 1
    
    logger.info(f"Enphase house: {len(results)} panels — "
                f"{counts['excellent']} excellent, {counts['good']} good, "
                f"{counts['fair']} fair, {counts['watch']} watch, {counts['alert']} alert")
    
    return results


# ─── Watchlist Persistence ────────────────────────────────────────────────────

def load_watchlist():
    """Load persistent watchlist from disk."""
    wl = load_json(WATCHLIST_JSON)
    if not wl or "panels" not in wl:
        return {"panels": {}, "history": []}
    return wl


def update_watchlist(watchlist, panel_results):
    """
    Update the watchlist based on current health results.
    
    - Panels flagged as alert/watch/fair: boost confidence score
    - Panels currently healthy: decay confidence score slowly
    - Panels that cross the active threshold: added to watchlist
    - Panels that drop below clear threshold: removed
    
    Returns updated watchlist with history entry.
    """
    panels = watchlist.get("panels", {})
    history = watchlist.get("history", [])
    today = datetime.now().strftime("%Y-%m-%d")
    
    flagged_today = []
    cleared_today = []
    
    for sn, result in panel_results.items():
        status = result.get("status", "good")
        prev = panels.get(sn, {})
        prev_score = prev.get("confidence_score", 0)
        
        # Boost or decay
        if status == "alert":
            new_score = min(100, prev_score + WATCHLIST_ALERT_BOOST)
        elif status == "watch":
            new_score = min(100, prev_score + WATCHLIST_WATCH_BOOST)
        elif status == "fair":
            new_score = min(100, prev_score + WATCHLIST_FAIR_BOOST)
        else:
            # Healthy — decay slowly
            new_score = max(0, prev_score - WATCHLIST_DECAY_PER_RUN)
        
        if new_score >= WATCHLIST_ACTIVE_THRESHOLD:
            # On the watchlist
            if sn not in panels or not panels[sn].get("on_watchlist"):
                flagged_today.append(sn)
            
            panels[sn] = {
                "confidence_score": new_score,
                "on_watchlist": True,
                "current_status": status,
                "first_flagged": prev.get("first_flagged", today),
                "last_flagged": today if status in ("alert", "watch", "fair") else prev.get("last_flagged", today),
                "times_flagged": prev.get("times_flagged", 0) + (1 if status in ("alert", "watch", "fair") else 0),
                "worst_status": _worse_status(prev.get("worst_status", "good"), status),
                "string": result.get("string", prev.get("string", "unknown")),
                "details": result.get("details", []),
            }
        elif new_score < WATCHLIST_CLEAR_THRESHOLD and sn in panels:
            # Cleared from watchlist
            if panels[sn].get("on_watchlist"):
                cleared_today.append(sn)
            del panels[sn]
        elif sn in panels:
            # Still tracked but below active threshold
            panels[sn]["confidence_score"] = new_score
            panels[sn]["current_status"] = status
            panels[sn]["on_watchlist"] = False
    
    # Add history entry
    if flagged_today or cleared_today:
        history.append({
            "date": today,
            "flagged": flagged_today,
            "cleared": cleared_today,
        })
    
    # Keep last 90 days of history
    history = history[-90:]
    
    return {"panels": panels, "history": history}


def _worse_status(a, b):
    """Return the worse of two statuses."""
    order = {"alert": 0, "watch": 1, "fair": 2, "good": 3, "excellent": 4}
    return a if order.get(a, 9) < order.get(b, 9) else b


# ─── Main Analysis ───────────────────────────────────────────────────────────

def run_health_analysis():
    """Run full health analysis and output report."""
    logger.info("Starting Solar Health Monitor analysis...")

    # Load data sources
    peaks = load_json(PEAKS_JSON)
    daily_history_raw = load_json(DAILY_HISTORY_JSON)
    barn_current = load_json(BARN_JSON)
    house_current = load_json(HOUSE_JSON)

    if not peaks:
        logger.error("No peaks data found — cannot run analysis")
        return None

    # Extract optimizer_map if present (optimizer_id -> serial)
    optimizer_map = {}
    serial_to_optid = {}
    if isinstance(daily_history_raw, dict) and "optimizer_map" in daily_history_raw:
        optimizer_map = daily_history_raw["optimizer_map"]
        for opt_id, serial in optimizer_map.items():
            serial_to_optid[serial] = opt_id
        logger.info(f"Optimizer map: {len(optimizer_map)} entries")
    
    # Build serial -> string mapping from optimizer IDs
    # "1.0.13" -> "String 1.0", "2.0.5" -> "String 2.0"
    serial_to_string = {}
    for opt_id, serial in optimizer_map.items():
        parts = opt_id.split(".")
        if len(parts) >= 2:
            serial_to_string[serial] = f"String {parts[0]}.{parts[1]}"

    # Also check peaks data for string field as fallback
    for sn, p in peaks.items():
        if sn not in serial_to_string and p.get("string"):
            serial_to_string[sn] = p["string"]

    logger.info(f"Loaded: {len(peaks)} panels from peaks, daily history: {'yes' if daily_history_raw else 'no'}, "
                f"string mapping: {len(serial_to_string)} panels")

    # Run each analysis dimension
    lifetime_analysis = analyze_lifetime_energy(peaks, serial_to_string)
    recent_analysis = analyze_recent_trends(daily_history_raw, peaks, serial_to_string, days_back=30)
    capacity_analysis = analyze_peak_capacity(peaks)
    monthly_analysis = analyze_monthly_trends(daily_history_raw, serial_to_string)

    logger.info(f"Analysis: {len(lifetime_analysis)} lifetime, {len(recent_analysis)} recent, "
                f"{len(capacity_analysis)} capacity, {len(monthly_analysis)} monthly")

    # serial_to_optid was already built from optimizer_map above
    if serial_to_optid:
        logger.info(f"Mapped {len(serial_to_optid)} serials to optimizer IDs")
    else:
        logger.warning("No optimizer map available — recent trends will use serial keys directly")

    # Classify each panel
    panel_results = {}
    health_counts = {"excellent": 0, "good": 0, "fair": 0, "watch": 0, "alert": 0}
    underperformers = []

    for sn in peaks:
        # Map serial to optimizer_id for recent analysis lookup
        opt_id = serial_to_optid.get(sn, "")
        
        status, score, details = classify_panel_health(
            lifetime_analysis, recent_analysis, capacity_analysis, monthly_analysis, sn, opt_id
        )

        string = get_string_for_serial(sn, peaks, serial_to_string)
        lt = lifetime_analysis.get(sn, {})
        rc = recent_analysis.get(sn, {}) or recent_analysis.get(opt_id, {})
        cap = capacity_analysis.get(sn, {})
        mt = monthly_analysis.get(sn, {})

        panel_results[sn] = {
            "serial": sn,
            "optimizer_id": opt_id,
            "string": string,
            "status": status,
            "score": score,
            "details": details,
            # Lifetime
            "lifetime_wh": lt.get("lifetime_wh", 0),
            "lifetime_ratio": lt.get("lifetime_ratio", 1.0),
            # Recent
            "recent_avg_wh": rc.get("recent_avg_wh", 0),
            "recent_ratio": rc.get("recent_ratio", 1.0),
            "recent_days": rc.get("days_analyzed", 0),
            "good_days_ratio": rc.get("good_days_ratio", 1.0),
            "trend_pct": rc.get("trend_pct", 0),
            # Capacity
            "peak_watts": cap.get("peak_watts", 0),
            "peak_date": cap.get("peak_date", ""),
            "capacity_ratio": cap.get("capacity_ratio", 1.0),
            # Monthly historical
            "monthly_classification": mt.get("classification", ""),
            "bad_month_count": mt.get("bad_month_count", 0),
            "cyclical_score": mt.get("cyclical_score", 0),
            "yoy_change_pct": mt.get("yoy_change_pct", 0),
            "anomaly_count": mt.get("anomaly_count", 0),
        }

        health_counts[status] = health_counts.get(status, 0) + 1
        if status in ("watch", "alert"):
            underperformers.append({
                "serial": sn,
                "string": string,
                "status": status,
                "score": score,
                "lifetime_ratio": lt.get("lifetime_ratio", 1.0),
                "recent_ratio": rc.get("recent_ratio", 1.0),
                "details": details,
            })

    # Sort underperformers worst-first
    status_order = {"alert": 0, "watch": 1, "fair": 2}
    underperformers.sort(key=lambda x: (status_order.get(x["status"], 9), x.get("lifetime_ratio", 1)))

    # ─── Enphase House Array ─────────────────────────────────────────
    enphase_results = analyze_enphase_house()
    enphase_health_counts = {"excellent": 0, "good": 0, "fair": 0, "watch": 0, "alert": 0}
    enphase_underperformers = []
    
    for sn, result in enphase_results.items():
        status = result.get("status", "good")
        enphase_health_counts[status] = enphase_health_counts.get(status, 0) + 1
        if status in ("watch", "alert"):
            enphase_underperformers.append({
                "serial": sn,
                "string": "Enphase",
                "status": status,
                "score": result.get("score", 3.0),
                "lifetime_ratio": 1.0,
                "recent_ratio": result.get("recent_ratio", 1.0),
                "details": result.get("details", []),
            })
    
    # Merge Enphase into unified panel results (prefixed to avoid serial collisions)
    all_panel_results = dict(panel_results)  # SolarEdge barn
    for sn, result in enphase_results.items():
        all_panel_results[f"enphase_{sn}"] = result
    
    all_underperformers = underperformers + enphase_underperformers

    # ─── Watchlist ─────────────────────────────────────────────────────
    watchlist = load_watchlist()
    watchlist = update_watchlist(watchlist, all_panel_results)
    
    # Save watchlist
    try:
        WATCHLIST_JSON.write_text(json.dumps(watchlist, indent=2))
    except IOError as e:
        logger.error(f"Failed to write watchlist: {e}")
    
    # Count watchlist panels (currently healthy but historically flagged)
    watchlist_active = {sn: info for sn, info in watchlist["panels"].items() 
                        if info.get("on_watchlist") and info.get("current_status") in ("good", "excellent")}
    
    # Add watchlist panels to underperformers with a "watchlist" status
    for sn, wl_info in watchlist_active.items():
        if sn in all_panel_results:
            all_panel_results[sn]["watchlist"] = True
            all_panel_results[sn]["watchlist_score"] = wl_info["confidence_score"]
            all_panel_results[sn]["watchlist_first_flagged"] = wl_info.get("first_flagged", "")
            all_panel_results[sn]["watchlist_times_flagged"] = wl_info.get("times_flagged", 0)
            all_panel_results[sn]["watchlist_worst_status"] = wl_info.get("worst_status", "")
    
    # Build array-level summary
    all_lifetime_ratios = [p.get("lifetime_ratio", 1.0) for p in panel_results.values() if p.get("lifetime_wh", 0) > 0]

    expected_pct = expected_capacity_pct()

    # Combined health counts
    combined_counts = {}
    for status in ["excellent", "good", "fair", "watch", "alert"]:
        combined_counts[status] = health_counts.get(status, 0) + enphase_health_counts.get(status, 0)

    summary = {
        "timestamp": datetime.now().isoformat(),
        "panel_count": len(all_panel_results),
        "health_counts": combined_counts,
        "underperformer_count": len(all_underperformers),
        "arrays": {
            "barn": {
                "panel_count": len(panel_results),
                "health_counts": health_counts,
                "age_years": round((datetime.now() - datetime.strptime(PANEL_INSTALL_DATE, "%Y-%m-%d")).days / 365.25, 1),
                "model": "LG LG355S2W-A5",
                "system": "SolarEdge",
            },
            "house": {
                "panel_count": len(enphase_results),
                "health_counts": enphase_health_counts,
                "age_years": round((datetime.now() - datetime.strptime(ENPHASE_INSTALL_DATE, "%Y-%m-%d")).days / 365.25, 1),
                "model": "Enphase IQ8+",
                "system": "Enphase",
            },
        },
        "array_age_years": round((datetime.now() - datetime.strptime(PANEL_INSTALL_DATE, "%Y-%m-%d")).days / 365.25, 1),
        "expected_capacity_pct": round(expected_pct * 100, 1),
        "panel_spec": {
            "model": "LG LG355S2W-A5",
            "nameplate_watts": PANEL_NAMEPLATE_W,
            "ptc_watts": 321.9,
            "install_date": PANEL_INSTALL_DATE,
        },
        "lifetime_spread": {
            "min_ratio": round(min(all_lifetime_ratios), 4) if all_lifetime_ratios else 0,
            "max_ratio": round(max(all_lifetime_ratios), 4) if all_lifetime_ratios else 0,
            "std_dev": round(_std_dev(all_lifetime_ratios), 4) if all_lifetime_ratios else 0,
        },
    }

    # Build the report
    report = {
        "version": "1.2",
        "summary": summary,
        "health_counts": combined_counts,
        "underperformers": all_underperformers,
        "watchlist": {
            "active_count": len([p for p in watchlist["panels"].values() if p.get("on_watchlist")]),
            "currently_healthy_but_watched": len(watchlist_active),
            "panels": {sn: info for sn, info in watchlist["panels"].items() if info.get("on_watchlist")},
        },
        "panels": all_panel_results,
    }

    # Write output
    try:
        HEALTH_REPORT_JSON.write_text(json.dumps(report, indent=2))
        logger.info(f"Report written to {HEALTH_REPORT_JSON}")
    except IOError as e:
        logger.error(f"Failed to write report: {e}")

    # Log summary
    total = len(all_panel_results)
    logger.info(f"Health Summary: {combined_counts['excellent']} excellent, {combined_counts['good']} good, "
                f"{combined_counts['fair']} fair, {combined_counts['watch']} watch, {combined_counts['alert']} alert")
    if underperformers:
        logger.info(f"Underperformers ({len(underperformers)}):")
        for up in underperformers[:10]:
            logger.info(f"  {up['serial']} ({up['string']}): {up['status'].upper()} — "
                        f"lifetime {up['lifetime_ratio']:.0%}, recent {up['recent_ratio']:.0%} "
                        f"| {'; '.join(up['details'][:2])}")

    return report


def _std_dev(values):
    """Simple standard deviation calculation."""
    if len(values) < 2:
        return 0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    return variance ** 0.5


# ─── Entry Point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    report = run_health_analysis()
    if report:
        summary = report["summary"]
        hc = report["health_counts"]
        arrays = summary.get("arrays", {})
        print(f"\n{'='*60}")
        print(f"  SOLAR HEALTH REPORT — {summary['timestamp'][:10]}")
        print(f"{'='*60}")
        print(f"  Total panels: {summary['panel_count']}")
        
        for arr_name, arr_info in arrays.items():
            ahc = arr_info.get("health_counts", {})
            total_arr = arr_info.get("panel_count", 0)
            if total_arr > 0:
                print(f"\n  {arr_info.get('system', arr_name)} {arr_name} ({total_arr} panels, {arr_info.get('age_years', '?')}yr):")
                print(f"    🟢 {ahc.get('excellent',0)} excellent  🟢 {ahc.get('good',0)} good  "
                      f"🟡 {ahc.get('fair',0)} fair  🟠 {ahc.get('watch',0)} watch  🔴 {ahc.get('alert',0)} alert")
        
        print(f"\n  Combined:")
        print(f"    🟢 Excellent: {hc['excellent']}")
        print(f"    🟢 Good:      {hc['good']}")
        print(f"    🟡 Fair:      {hc['fair']}")
        print(f"    🟠 Watch:     {hc['watch']}")
        print(f"    🔴 Alert:     {hc['alert']}")

        ups = report["underperformers"]
        if ups:
            print(f"\n  ⚠️  Underperformers ({len(ups)}):")
            for up in ups:
                print(f"    {up['serial']} [{up['string']}] — {up['status'].upper()}")
                for d in up['details']:
                    print(f"      → {d}")

        wl = report.get("watchlist", {})
        wl_panels = wl.get("panels", {})
        healthy_watched = wl.get("currently_healthy_but_watched", 0)
        if wl_panels:
            print(f"\n  👁️  Watchlist ({len(wl_panels)} tracked, {healthy_watched} currently healthy):")
            for sn, info in sorted(wl_panels.items(), key=lambda x: -x[1].get("confidence_score", 0)):
                status_now = info.get("current_status", "?")
                score = info.get("confidence_score", 0)
                times = info.get("times_flagged", 0)
                worst = info.get("worst_status", "?")
                first = info.get("first_flagged", "?")
                marker = "✓ healthy now" if status_now in ("good", "excellent") else status_now.upper()
                print(f"    {sn} [{info.get('string', '?')}] — score:{score} worst:{worst} flagged:{times}x since:{first} ({marker})")

        print(f"{'='*60}\n")
    else:
        print("Health analysis failed — check logs")
        sys.exit(1)
