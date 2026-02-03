# Changelog

All notable changes to FranklinWH Battery Automation.

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
