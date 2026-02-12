# FranklinWH Battery Automation — Roadmap

This document outlines planned features and improvements for the FranklinWH Battery Automation project. Items are listed in rough priority order. Contributions, feedback, and discussion on any of these are welcome — open an issue or comment on an existing one.

---

## 🔜 Up Next

### Script Status Dashboard Tab
**Priority: High**

Add a new tab to the web dashboard that provides at-a-glance visibility into the health of all scheduled automation scripts. Currently, if a script starts failing silently (wrong config, API down, etc.), the only way to notice is by manually checking logs. This tab will surface problems immediately.

Planned features:
- List of all scheduled scripts with their configured frequency
- Last run time and exit status for each script
- Success/failure counts since the Docker container started
- Recent failure log with error messages
- Data source health indicators (Modbus connection, Enphase availability, Cloud API status)
- Visual alerts for scripts that have failed recently or repeatedly

Data will be parsed from the existing `scheduler.log` — no new dependencies required.

### Forecast-Aware Charging & Adaptive Solar Optimization
**Priority: High** · [Related: Issue #_]

The current system uses a fixed schedule for peak protection (5–8 PM on weekdays) and a fixed off-grid window (5–10 PM). This works well but leaves optimization on the table, especially for non-export systems where a full battery means solar gets curtailed (wasted).

The goal is to make the system smarter about *how much* to charge and *when* to go off-grid based on how much solar is actually expected that day.

**Morning Planning Phase**
- Integrate a solar forecast provider (Solcast, Forecast.Solar, or similar)
- At a configurable time (e.g. 6 AM), calculate the day's expected solar production minus estimated household consumption
- Determine the "gap" between current SOC and the peak-readiness target that solar alone can't fill
- If there's a gap: grid-charge only enough to close it, during the cheapest available rate window
- If solar is sufficient: suppress grid charging entirely to leave maximum headroom for free solar absorption

**Intraday Adaptation**
- Monitor actual solar production vs. forecast throughout the day
- Recalculate the gap mid-day if tracking behind forecast
- Hard protection: always ensure target SOC is met before peak, grid-charge if necessary regardless of rate

**Post-Peak Optimization**
- High-solar days: stay off-grid past peak to consume stored solar energy (it's free)
- Low-solar days: return to grid at peak end to preserve SOC for overnight
- Dynamic duration based on SOC at peak end and daily solar generation

**Weekend Strategy**
- E-TOU-D (PG&E) has no peak period on weekends; other rate plans may differ
- Weekends shift focus from peak avoidance to pure solar self-consumption — stay off-grid long enough to use what solar generated, then return to grid
- Core principle: *use what you generated that day for free while maintaining a safety reserve, but don't stay off-grid longer than the solar energy justifies*

**Rate Schedule Flexibility**
- Move from simple peak start/end configuration to a full rate schedule supporting multiple tiers (off-peak, mid-peak, peak) with configurable time windows
- Support structures like SMUD TOD (three tiers with a morning off-peak charging window) alongside PG&E E-TOU-D (two tiers)
- Weekend/weekday/holiday differentiation

**Zero-Export Curtailment Protection**
- Ensure that morning SOC + forecasted solar production won't cap the battery before peak production hours end
- Prevents the catch-22 where charging too aggressively wastes solar, but charging too conservatively misses the peak target

---

## 📋 Planned

### Multi-Gateway Management
**Priority: Medium**

Support for users with multiple FranklinWH aGate systems. Currently the automation manages a single gateway. This would allow coordinated management of multiple battery systems, potentially with different configurations or roles (e.g., one prioritizing solar, another prioritizing backup).

### Telemetry Service
**Priority: Low**

Anonymous, opt-in usage telemetry to help understand how the automation is being used across different configurations and rate plans. The telemetry module (`telemetry.py`) is built but not yet wired to a real collection endpoint. When activated, it will send anonymized performance metrics (no credentials or personal data) to help improve default settings and decision logic.

---

## ✅ Recently Completed

### v3.5.0 — Modbus TCP + Enphase Local Integration (Feb 2026)
- **Local-first data collection**: SOC and grid power via Modbus TCP (26ms response, was 5000ms via cloud API)
- **Enphase local solar**: Reads production data from local Enphase gateway instead of cloud
- **SOC Trend Tracker**: Derives solar-to-battery charging rate from SOC changes over time, persists across cycles
- **Mode State Tracker**: Tracks battery mode locally with periodic cloud verification, reducing API calls
- **Hybrid architecture**: Modbus for fast monitoring, cloud API only for mode switching and verification
- **Dashboard fixes**: Weekly reports tab now scans actual report files instead of guessing dates; CSV parser handles mixed-format historical data
- **Weather collection fix**: Added missing config path for weather data logging
