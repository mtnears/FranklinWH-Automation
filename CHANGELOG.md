# Changelog

All notable changes to FranklinWH Battery Automation.

---

## v4.1.0 — March 2026

### SQLite Database Migration (Breaking Change)
- All data collection migrated from CSV files to a SQLite database (`data/franklin.db`). This is the largest structural change since the project began — no supported upgrade path from previous versions; fresh install required
- `db.py` — unified database layer; all 17 tables initialized automatically on first run
- New collectors writing directly to SQLite: `collect_franklin_cloud.py`, `collect_modbus.py`, `collect_solar_enphase.py`, `collect_weather_db.py`, `collect_pv_output.py`, `collect_device_inventory.py`
- `rollup_daily_energy.py` — daily energy summary rollup with `home_load_kwh` instantaneous power fallback for Modbus rows that don't populate cumulative counters
- Removed CSV-era scripts: `collect_enphase.py`, `collect_pvoutput.py`, `collect_solaredge.py`, `collect_weather.py`, `capture_grid_status.py`, `daily_status_report.py`, `generate_weekly_charts.py`

### Adaptive Engine Hardening
- **Post-peak solar discharge** — after peak, engine stays in Self-Consumption to burn free solar stored in the battery. Computes net solar excess (solar charged − peak discharge used), sets target SOC drain point, returns to TOU once reached
- **Taper ceiling** (`TAPER_CEILING_PCT`) — caps grid charging ceiling for non-export systems to prevent curtailment. Tunable via `.env`; start at 95 and lower by 5 per sunny day until curtailment clears
- **Pre-peak gate** — within 30 min of peak, holds current mode rather than starting a new EB burst unless already charging
- **Anchor drift fix** — `_get_soc_at_peak_end()` pins to first reading at-or-after peak end, eliminating float arithmetic window drift across engine cycles
- **Peak discharge fallback** — `_compute_peak_discharge_kwh()` queries SOC at peak window from `system_readings` when `daily_savings` rollup hasn't run yet
- Engine writes `curtailed_kwh` and `engine_priority` to `system_readings` after each decision cycle

### Modbus-First Mode Verification
- Replaced routine cloud API mode verification with Modbus register 15507 reads (OnGridMode: 0=Backup, 1=TOU, 2=SC, 3=Manual)
- Cloud API reserved for actual `set_mode()` calls and fallback verification only (~2-4 calls/day vs every 30 min)
- Eliminates routine cloud API polling; resolves phone app session logout issues caused by concurrent API access

### Version Management
- Single `VERSION` file in repo root; `config.py`, `system_profile.py`, `scheduler.py` all read from it
- `ENGINE_VERSION` env var deprecated — `config.py` defaults to VERSION file automatically
- Dashboard About card reads version dynamically from `/api/version` endpoint
- GitHub release check with "Update Available" badge (cached in localStorage, once/day)

### System Profile Overhaul
- `scan_db()` replaces CSV scan; solar interval uses actual time intervals between readings instead of hardcoded 15-min CSV assumption (1-min Modbus rows were inflating solar totals by ~15×)
- Capacity bug fixed — `BATTERY_CAPACITY_KWH` was being mapped to per-battery capacity, doubling the total
- Weekly rebuild job added to scheduler (Sunday 3 AM); profile rebuilt on container restart before first engine cycle

### Dashboard — Fire HD 10 Optimized
- **Plotly.js analytics tab** replaces static weekly PNG charts — interactive charts with zoom, pan, hover tooltips, and touch support. Date range selection, carousel navigation
- Layout validated and optimized for Fire HD 10 tablet (1507×943 CSS pixels) in Fully Kiosk Browser
- About card with dynamic version from `/api/version` and update-available badge from GitHub releases API
- All chart data sourced from SQLite via `generate_dashboard_data.py`

### Device Inventory Enhancement
- `collect_device_inventory.py` enriched with `get_home_gateway_list()` API data via raw httpx
- Gateway record includes `realSysHdVersion` (hardware revision), `protocolVer`, full firmware string
- aPower 2 battery model identification from serial prefix `0015` (positions 4–8)

### Telemetry v2
- Schema v2 payload (~2.7KB) with 13 new config flags and 10 new health signal queries
- Curtailment query fixed to `MAX()-MIN()` for cumulative counter (was incorrectly using `SUM`)
- Engine version and expanded config reporting included

### Other Changes
- `rate_schedule.py` — rate schedule management with JSON config file (`data/rate_schedule.json`)
- `diagnostic_bundle.py` — one-click sanitized diagnostic bundle from dashboard with credentials auto-stripped
- `collect_solaredge_panels.py` — per-optimizer panel health monitoring with 21-day rolling window
- **Export system support** (`SOLAR_EXPORT=true`) — post-peak solar discharge and curtailment protection both skip on export systems
- Removed dead `import csv` from `smart_decision.py`

### Upgrade Notes
**Fresh install required.** There is no supported upgrade path from v3.x or v4.0.x. Back up your `.env`, clone fresh, copy your settings (reviewing `.env.example` for new variables), and start the new container. Historical CSV data is not migrated. See README for full upgrade instructions.

---

## v4.0.3 — February 2026

### Overnight Battery Preservation
- P8 default mode changed from Self-Consumption to TOU. Battery now holds charge overnight with the grid powering the home at off-peak rates instead of draining the battery

### Solar-First Charging Deferral
- P7 gap charging checks whether solar is actively producing and whether there's enough buffer time before peak. Defers grid charging when solar can plausibly fill the gap

### Hourly Net-to-Battery Model
- Morning plan `forecast_to_battery_kwh` replaced daily-total subtraction with per-hour surplus calculation. Each hour's solar is reduced by that hour's expected load before summing. Reduces morning gap overestimation by 5-15 kWh on typical days

### Centralized Debug Logging
- `configure_logging()` applied across 9 files. Based on PR [#5](https://github.com/mtnears/FranklinWH-Automation/pull/5) by [@cecilkootz](https://github.com/cecilkootz)

---

## v4.0.2 — February 2026

### Solar Forecast Integration
- `solar_forecast.py` wired into adaptive engine with graceful fallback to learned profile if API unavailable
- Forecast-aware P7 charging — grid charges to `morning_ceiling_pct` instead of `target_soc`, leaving headroom for free solar

### Weekend Peak Detection Fix
- Three v3.5-era components used clock math without checking day-of-week. Fixed to check `is_peak_day()`

### Fire HD 10 Kiosk Optimization
- Viewport fix, full-height layout, SVG icon refactor, CSS sky gradient
- New Solar Status card — live production, daily generation, self-powered percentage, net status

### Three-Mode Strategy
- TOU / Self-Consumption / Emergency Backup replaces two-mode system
- TOU as resting mode (battery holds, grid powers home), SC for peak discharge and solar absorption, EB for targeted grid charging bursts only

### Weekly Charts Rewrite
- 5 charts with SOC timeline, mode markers, decision engine activity, curtailment tracker

---

## v4.0.1 — February 2026

### Mode Switch Verification
- **Root cause fix:** Mode detection was reading cached local state instead of actual hardware mode
- Every mode switch retries up to 3× with hardware confirmation via cloud API
- 10-minute cooldown prevents repeated switching on consecutive cycles

### Peak Safety Net
- Forces immediate switch to Self-Consumption if hardware is in Emergency Backup during peak
- Tiered cloud verification — every-cycle hardware checks during peak, 15-minute intervals otherwise

---

## v4.0 — February 2026

### Adaptive Decision Engine
- 8-phase priority system (P1-P8) replaces fixed time-based rules
- Continuous evaluation: "what is the optimal mode right now?" instead of rigid schedules
- Priority stack: emergency override → grid disconnect → peak imminent → peak active → curtailment → forecast gap → pre-peak charging → TOU default

### Forecast-Aware Charging
- Calculates dynamic morning SOC ceiling based on expected solar production
- Limits grid charging on high-solar days to leave headroom for free solar
- Falls back to weather calibration or learned historical profile when forecast API unavailable

### Curtailment Protection
- Detects battery full + solar producing and switches modes to prevent wasting free energy
- Non-export systems only (export systems send surplus to grid for credit)

### Rate Schedule Flexibility
- Supports PG&E E-TOU-D, SMUD TOD, ComEd dynamic pricing, and custom schedules
- Multiple peak windows, configurable peak days, CARE discount support

### Anonymous Telemetry
- Opt-in system with dashboard consent flow popup
- No personal data, credentials, or identifiable information collected
- Public collection repo: [mtnears/franklin-telemetry](https://github.com/mtnears/franklin-telemetry)

### Diagnostic Reporting
- One-click sanitized diagnostic bundle from dashboard
- Credentials auto-stripped from all output

---

## v3.5.1 — February 2026

### Script Status Dashboard
- New Script Status tab with real-time health monitoring of all scheduled scripts
- Success/fail counts, last run time, error history

### Daily Savings Fix
- Argument handling, schedule timing, and CSV format evolution fixes

---

## v3.5.0 — February 2026

### Modbus TCP + Enphase Local Integration
- Local-first data collection: SOC and grid power via Modbus TCP (26ms response, was 5,000ms via cloud API)
- Enphase local solar production reads via HTTPS on LAN
- Hybrid architecture: Modbus for monitoring, cloud API for mode switching
- Grid disconnect detection via Modbus registers

---

## v3.4.0 — February 2026

### Clock-Aligned 30-Minute Scheduling
- Decision checks now run at fixed :00 and :30 each hour instead of interval-from-start
- 30-minute minimum interval enforced to reduce Franklin Cloud API load — temporary conservative default until API rate limit guidance is formalized
- Pre-peak check moved from 5 to 10 minutes before peak start
- Eliminates timing drift caused by container start time

### Solar Override Fix
- Solar-to-battery charging rate now properly overrides "out of time" grid-charge deadlines
- When solar ETA can beat the clock, system stays in TOU instead of switching to backup
- Prevents unnecessary grid charging during strong solar production in tight pre-peak windows

### Stale API Value Correction
- Franklin API reports stale `gridChBat`/`soChBat` values when battery is discharging
- Values now zeroed out automatically during discharge periods
- Fixes misleading log entries and prevents incorrect solar estimation

### PVOutput Config Integration
- PVOutput collector reads credentials from `.env` instead of hardcoded placeholders
- Multi-system support via `PVOUTPUT_SYSTEM_IDS` comma-separated list
- Automatic system name mapping with fallback for unknown system IDs

### Manual Override API
- New REST endpoints: `POST /api/override` and `POST /api/override/cancel`
- Dashboard buttons for Self Consumption and Emergency Backup modes
- Overrides auto-expire after configurable duration (default 2 hours)
- Override status displayed as banner on dashboard

### Upgrade Notes
Update your `.env` defaults (optional — old values still work):
```bash
CHECK_INTERVAL_MINUTES=30          # Was 15, now minimum 30
PEAK_TRANSITION_BUFFER_MINUTES=10  # Was 5
```
Then:
```bash
git pull
docker compose down
docker compose build --no-cache
docker compose up -d
```

---

## v3.3.0 — February 2026

### Mode Detection Fix
- Mode detection now uses the gateway `name` field instead of `run_status`
- Resolves firmware issue where `run_status` could report incorrect values
- Name-based detection is reliable across all firmware versions:
  "Emergency Backup" → backup, "Self Consumption" → self_consumption, else → home mode

### Negative Pricing / Solar Override
- New `SOLAR_OVERRIDE_PRICE_CENTS` setting for dynamic pricing users
- When grid price drops to or below the threshold, charges from grid even when solar is producing
- Overrides both solar-first preference AND peak period protection
- Use case: ComEd and similar utilities with negative pricing periods where the grid pays you to consume
- All pricing thresholds now support negative values natively
- Removed hardcoded 2.0¢ "very cheap" threshold — all thresholds are now configurable

### Decision Engine Restructure
- New Layer 0: Credit/negative price override (highest priority, supersedes peak protection)
- Layer 1–5 unchanged: Peak protection → Solar → Dynamic pricing → TOU → Fallback
- All price comparisons use `<=` for correct handling of zero and negative values

### Enhanced Dashboard Data
- Dashboard data generator now calls `_status()` API for enriched system data
- New `extended` block in JSON with per-battery SOC, power, and serial numbers
- Environment data: ambient temperature, cellular signal strength, WiFi signal
- Energy totals: today's solar, grid import/export, load, battery charge/discharge, generator
- Lifetime totals: cumulative energy by source (battery, grid, solar, generator)
- Charging breakdown: grid-to-battery vs solar-to-battery rates
- Hardware status: BMS, power electronics, main switch, generator, V2L
- New `config` block exports automation settings for the Settings tab
- Gateway ID included as top-level field for multi-gateway support
- Mode detection via name field matches decision engine logic

### Switch Reliability
- 5-second initial verification + 8-second retry for mode switches
- 10-minute switch cooldown prevents repeated switching on consecutive cycles
- Verification logging shows `name=` field for accurate confirmation
- "Mode changed" log only appears on verified successful switches

### New Configuration
```bash
# Optional — only for dynamic pricing users
SOLAR_OVERRIDE_PRICE_CENTS=0    # Grab free/negative pricing (leave unset to disable)
```

### Upgrade Notes
If you use dynamic pricing, optionally add `SOLAR_OVERRIDE_PRICE_CENTS` to your `.env`.
If you don't use dynamic pricing, no `.env` changes are needed.

```bash
git pull
docker compose down
docker compose build --no-cache
docker compose up -d
```

---

## v3.2.0 — February 2026

### API-Native Mode Management
- Mode detection reads directly from the gateway's `run_status` field via `_status()` API
- Mode switching uses direct `set_mode()` API calls through the franklinwh library
- Eliminates dependency on state files (`last_mode.txt`, `peak_state.txt`)
- Eliminates external switch scripts (`switch_to_backup_v2.py`, `switch_to_tou_v2.py`)
- Universal mode detection via `run_status` codes: 1=Emergency Backup, 2=TOU, 3=Self Consumption
- Post-switch verification confirms mode actually changed
- Single API client instance for stats, status, and mode switching

### Schedule-Aware Timing
- Pre-peak check pinned to exact time (PEAK_START - buffer minutes)
- Post-peak check pinned to exact time (PEAK_END + 1 minute)
- Guarantees peak transitions are never missed regardless of polling interval
- Supports secondary peak period with same transition logic

### Per-Battery Monitoring
- Individual SOC tracking per battery (`fhpSoc` array)
- Individual power output per battery (`fhpPower` array)
- Automatic detection of battery count
- Per-battery data in intelligence log and CSV

### Enriched Data Logging
- Ambient temperature (°F/°C) from gateway sensor
- Cellular signal strength
- Grid-to-battery and solar-to-battery charging rates
- Dynamic CSV columns for per-battery SOC

### New Configuration
- `CHECK_INTERVAL_MINUTES` — configurable polling frequency (1-60 min)
- `PEAK_TRANSITION_BUFFER_MINUTES` — how early to check before peak
- `HOME_MODE` — normal operating mode (`tou` or `self_consumption`)

### Upgrade Notes
Add these lines to your `.env` file before rebuilding:
```bash
CHECK_INTERVAL_MINUTES=15
PEAK_TRANSITION_BUFFER_MINUTES=5
HOME_MODE=tou
```
Then: `git pull && docker compose down && docker compose build --no-cache && docker compose up -d`

---

## v3.1.1 — January 2026

### Docker Improvements
- Fixed Docker path handling for all scripts
- Daily savings automation added to scheduler
- Historical data migration into Docker system

### Dashboard
- Enhanced three-tab dashboard (Live, Weekly Reports, System Logs)
- Fixed charging status logic (±0.1kW standby threshold)
- PVOutput sync integration

---

## v3.1.0 — January 2026

### Complete Docker Overhaul
- Self-contained Docker package with internal scheduler
- Built-in web dashboard with nginx (port 8100)
- Proper permission handling
- No external cron or Task Scheduler needed
- All tasks managed by internal Python scheduler

### Dashboard Features
- Real-time battery status and energy flow visualization
- Weekly performance charts (SOC timeline, daily summary, power flow)
- System logs viewer with auto-refresh
- Savings tracker

---

## v3.0.0 — January 2026

### Configuration-Driven Architecture
- All settings via `.env` file — no more editing Python scripts
- Feature toggles: Solar, TOU, Dynamic Pricing, Weather, PVOutput
- Configurable peak hours, peak days, charge strategies
- `config.py` module for centralized configuration management

### New Features
- Dynamic pricing support (ComEd hourly pricing API)
- Multiple peak period support (split peaks)
- Configurable charging strategy (conservative/balanced/aggressive)
- Weather data collection integration
- PVOutput solar tracking integration

### Backward Compatibility
- Default settings match v2.x behavior
- Existing log files and data preserved

---

## v2.0 — December 2025

### Smart 15-Minute Decision Engine
- Single `smart_decision.py` replaces three separate scripts
- Runs every 15 minutes instead of 3 fixed times per day
- Peak state tracking prevents mode changes during peak hours
- 5-attempt API retry logic for reliability
- Fixed critical midnight rollover bug from v1

### Improvements over v1
- More responsive to changing solar conditions
- Handles edge cases (midnight rollover, daylight saving time)
- Comprehensive logging with intelligence log
- CSV data logging for historical analysis

---

## v1.0 — November 2025

### Initial Release
- Three-tier automation: morning, midday, final safety check
- Fixed schedule: 8 AM, 2 PM, 3:30 PM
- Basic solar vs grid charging logic
- Franklin WH API integration via franklinwh library

---

**Repository:** https://github.com/mtnears/FranklinWH-Automation
