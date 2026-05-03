# FranklinWH Battery Automation — Roadmap

Planned features and improvements. Items are listed in rough priority order. Contributions, feedback, and discussion are welcome — open an [issue](https://github.com/mtnears/FranklinWH-Automation/issues).

---

## 🔧 In Progress

### Telemetry Expansion
**Priority: Medium**

Initial telemetry deployment is complete — opt-in anonymous collection with dashboard consent flow, v2 schema with expanded config flags and health signals. Next steps: build `scripts/aggregate.py` for the [franklin-telemetry](https://github.com/mtnears/franklin-telemetry) collection repo to generate `summary.json` and a community dashboard with anonymous fleet statistics (system sizes, utility distribution, engine versions, aggregate performance). Expand the collection repo README with full privacy policy, data schema documentation, and opt-out instructions.

### Open Issue Resolution
**Priority: Medium**

Beta tester on ComEd (Illinois) dynamic pricing encountering mode detection issues (`detected=unknown`) and P7 gap charging behavior that may not suit dynamic hourly pricing without fixed peak periods. Enhanced diagnostic bundle deployed for data collection — pending further verification and log analysis to determine whether ComEd requires a dynamic pricing path distinct from fixed TOU schedules.

### SolarEdge Inverter Local Data
**Priority: Medium**

`solaredge_inverter_readings` table exists and schema is defined. SE7600H (device_id=1) confirmed working via Modbus TCP at 192.168.4.213:1502. SE11400H (device_id=2) not yet responding — awaiting verification by installer. Once both inverters are confirmed, a production collector can be built for inverter-level data (AC/DC power, temperature, status) at the same cadence as the house system.

---

## 📋 Planned

### Dashboard Conditional UI
**Priority: Medium**

The dashboard currently assumes a solar+battery system on a single-peak TOU rate. Multiple user profiles need tailored views:
- **Solar-less systems:** Hide Solar Status card and suppress zero-value solar fields when no solar arrays are configured.
- **Battery-only arbitrage users:** Show rate arbitrage stats (buy low/sell high metrics) instead of solar production stats.
- **Dynamic pricing users:** Rate Plan card should show current price, next price change, and price trend instead of static E-TOU-D info.
- **Multiple peak periods:** Some rate plans have mid-peak, super off-peak, etc. Peak bar and countdown currently assume single 5-8 PM peak — needs multi-period support.

### Automation Savings Calculation Update
**Priority: Medium**

Savings calculations need updating for v4 changes:
- Three-mode strategy changes what counts as "savings"
- Energy source tracking affects how grid vs solar vs battery discharge is valued
- CARE discount interaction: consumption charges reduced ~38.8% but export credits unaffected — verify math accounts for this asymmetry
- Dynamic pricing: savings should use actual price at time of charge/discharge, not flat rate assumptions

### Home Load & Battery Power via Modbus
**Priority: Medium**

`home_load_kw` and `battery_kw` are currently hardcoded to `0.0` in the Modbus data path (`home_load_kwh` daily rollup is now working via instantaneous power fallback). Quick win: energy balance derivation after Modbus+Enphase merge. Proper fix: validate Modbus registers 82 (battery power) and 83 (home load) during active charging and peak discharge. Resolves flat zero lines in Power Flow charts.

### Hybrid ML Engine Evolution
**Priority: Medium**

Evolve the engine from pure algorithmic to hybrid ML. Keep algorithmic safety/mode logic as guardrails, add learned models for: solar prediction (actual vs forecast accuracy), load prediction (beyond static hourly averages), and SC/TOU timing optimization (from historical outcomes). Data foundation exists in SQLite. The algorithmic tuning keeps circling on the same variables a trained model could learn from data.

### Script Status Dashboard — Description Updates
**Priority: Low**

The Script Status dashboard tab needs updated descriptions for the new v4.1 scripts and scheduled tasks added in this release.

### Multi-Gateway Management
**Priority: Low**

Support for users with multiple FranklinWH aGate systems. Coordinated management of multiple battery systems with independent configurations.

---

## 💡 Future Ideas

- **Weekend strategy optimization** — Pure solar self-consumption on non-peak days with dynamic off-grid duration. The forecast engine now provides the solar production estimates needed to plan optimal weekend discharge.
- **Holiday schedule support** — Rate schedule awareness for utility holidays
- **Modbus write exploration** — Investigate direct Modbus register control for mode switching (currently requires cloud API; DIY orchestration is possible but fragile and unsupported)
- **SolarEdge local API** — Direct panel monitoring if/when local API access becomes available
- **Home Assistant integration** — MQTT discovery for HA dashboards alongside the built-in web dashboard
- **Dashboard device auto-detection** — Automatically detect viewport dimensions and adapt layout, replacing manual device presets
- **Dashboard cache-busting** — Version query strings on asset URLs (`?v=X`) to avoid manual cache clearing in Fully Kiosk Browser on updates
- **Utility billing data import (Opower)** — Integration with the [tronikos/opower](https://github.com/tronikos/opower) library to pull historical usage and cost data from supported utilities (PG&E, ComEd, SMUD, Exelon subsidiaries, and 25+ others). Would enable bill estimation, savings validation against actual utility data, and historical import for new users who don't have local data yet. Caveat: utility data arrives with ~48 hour delay and auth is web-scraping based (fragile). Local Modbus/Enphase data is more timely for real-time decisions, so this would primarily serve billing validation and historical backfill. *(Community request — [#10](https://github.com/mtnears/FranklinWH-Automation/issues/10))*

---

## ✅ Recently Completed

### v4.3.0 — Cloud Persistence + Override Bundle (May 2026)
- **Cloud-only data persistence fix** — `system_readings` rows from the cloud collector now populate primary fields (SOC, solar, grid, battery, load, mode, grid_status). Previously these were NULL on cloud-only systems, breaking load profile learning and SOC-target override exit. Modbus remains source of truth on hybrid systems via `COALESCE` semantics in the UPDATE branch.
- **Manual override UI redesign** — six time-based chips replaced with four outcome-oriented options: until SOC reached, for duration (custom h/m), until specific time (HH:MM), until canceled. Last custom duration remembered in localStorage. Banner shows time-remaining for time-based overrides.
- **Override resilience** — `_read_latest_soc()` looks back 30 minutes for non-NULL SOC and falls back to direct cloud API call (30s cache) if DB unavailable. Defends override exit against future regressions.
- **Modbus job guard** — `MODBUS_ENABLED=false` now actually disables the Modbus collection job, eliminating timeout-error noise on cloud-only systems.
- **Dashboard config display fix** — backup reserve now correctly reads `BACKUP_RESERVE_PCT` (was reading non-existent `MIN_SOC_RESERVE`, defaulted to 20 regardless of `.env`).
- **Solar forecast calibration** — house array tilt corrected (22°→18°), azimuth (-65°→0°), removed WNW penalty and noon-shift. R²=0.835 against measured production.

### v4.2.2 — Adaptive Engine Cooldown Fix (April 2026)
- **No-op switch poisoning fix** — `last_mode_switch` was being updated even when the engine decided to stay in the current mode, causing subsequent decisions in the same cycle to be blocked by a phantom 300-second cooldown. Fix: only update on actual mode transitions.
- `enrich_state()` rounds `current_rate_cents` to 3 decimal places to remove floating-point noise from intelligence_log.

### v4.2.1 — Data Export Tab (April 2026)
- New Energy & Billing Data Export tab with three API endpoints, date picker, billing-period dropdown, summary cards, and CSV download.
- Billing period dropdown label fix and fallback render path for older periods that pre-date `daily_energy_summary`.

### v4.2.0 — Multi-Window Peaks + Modbus Hardening (April 2026)
- **Multi-window peak support** — `rate_schedule.json` accepts multiple peak windows per rate plan (peak + partial-peak + off-peak), with seasonal selection via a `months` array on each window. Engine and peak bar both consume the multi-window structure.
- **Modbus sanity bounds** — near-`0xFFFF` register values that previously slipped through exact-sentinel checks now trip explicit upper bounds (`MAX_PLAUSIBLE_SOLAR_W=25000`, `MAX_PLAUSIBLE_LOAD_W=50000`) and are discarded as Modbus errors.
- **Inventory dedup** — `collect_device_inventory.py` deduplicates within a write to prevent duplicate rows from rapid firmware-probe cycles.
- **Dynamic dashboard rate plan card** — reflects current schedule, tier, and rate from `rate_schedule.py` instead of hardcoded `E-TOU-D`.
- **Diagnostic bundle enhancements** — `system_info.json` now includes engine_version, git_commit, and rate_schedule_name.

### v4.1.1 — Bug Fixes (March 2026)
- `collect_modbus.py` — `ValueError` raised when register values were unavailable and substituted with `'?'` then formatted with `:.3f`. Format expression now guarded against non-numeric fallback.
- Inventory dedup race condition where rapid firmware probes could write duplicate `device_inventory` rows.

### v4.1.0 — SQLite Migration, Engine Hardening, Analytics Dashboard (March 2026)
- **SQLite database layer** (`db.py`): All data storage migrated from CSV to SQLite. 17 tables cover system readings, solar data, weather, device inventory, billing, and engine decisions. DB initializes automatically on first run.
- **New collectors**: `collect_franklin_cloud.py`, `collect_modbus.py`, `collect_solar_enphase.py`, `collect_weather_db.py`, `collect_pv_output.py`, `collect_device_inventory.py`, `rollup_daily_energy.py` — all write directly to SQLite
- **Modbus-first mode verification**: Replaced routine cloud API mode checks with Modbus register 15507 reads (OnGridMode: 0=Backup, 1=TOU, 2=SC, 3=Manual). Cloud API reserved for actual mode switches only (~2-4/day). Eliminates routine cloud API polling and resolves phone app session logout issues.
- **Post-peak solar discharge**: Engine stays in Self-Consumption after peak to burn net solar surplus stored in the battery. Computes solar excess (solar charged − peak discharge used), sets target SOC drain point, returns to TOU once reached
- **Taper ceiling** (`TAPER_CEILING_PCT`): Caps grid charging ceiling for non-export systems to prevent curtailment. Tunable per-system based on observed curtailment behavior
- **Pre-peak gate**: Within 30 min of peak, holds current mode instead of starting new EB burst unless already charging
- **Anchor drift fix**: `_get_soc_at_peak_end()` pins to first reading at-or-after peak end, eliminating float arithmetic window drift across engine cycles
- **Peak discharge fallback**: `_compute_peak_discharge_kwh()` queries SOC at peak window from `system_readings` when `daily_savings` rollup hasn't run yet
- **home_load_kwh rollup fix**: `rollup_daily_energy.py` uses instantaneous power fallback (`daily_kwh_from_instantaneous()`) for Modbus rows that don't populate cumulative counters
- **System profile overhaul**: `scan_db()` replaces CSV scan; solar interval uses actual reading timestamps; capacity bug fixed (was doubling total kWh); weekly rebuild job added
- **Version management**: Single `VERSION` file in repo root, wired to `config.py`, `system_profile.py`, `scheduler.py`. Dashboard About card reads dynamically from `/api/version`. GitHub release check with "Update Available" badge (localStorage cached, once/day). `ENGINE_VERSION` env var deprecated.
- **Plotly.js analytics tab**: Interactive charts replace static weekly PNGs. Date range selection, carousel, zoom/pan/hover, touch support. All data sourced from SQLite
- **Fire HD 10 dashboard optimization**: Layout validated at 1507×943 CSS pixels for Fully Kiosk Browser tablet display
- **Export system support** (`SOLAR_EXPORT=true`): Post-peak solar discharge and curtailment protection both skip on export systems. Set in `.env` for NEM2/NEM3 full-export setups
- **Device inventory enrichment**: Gateway record enriched with `realSysHdVersion` (hw 1.3), `protocolVer`, full firmware string via `get_home_gateway_list()`. aPower 2 identification from serial prefix `0015`.
- **Telemetry v2**: Schema v2 payload (~2.7KB) with 13 new config flags, 10 health signal queries, engine version reporting. Curtailment query fixed to `MAX()-MIN()` for cumulative counter.
- **Solar health monitor wired**: Nightly panel health report at 8:30 PM using 21-day rolling window. SolarEdge per-optimizer health scoring
- **Engine writes to system_readings**: `curtailed_kwh` and `engine_priority` populated after each decision cycle
- **Open-Meteo solar forecast**: Replaced Forecast.Solar (12 req/day limit, recurring outages) with Open-Meteo. No API key required, 10,000 free calls/day, returns `global_tilted_irradiance` already corrected for tilt/azimuth. Existing calibration layer applies on top.
- **Documentation refresh**: All 6 docs/ files updated for v4.1 (CONFIGURATION_REFERENCE, DOCKER_INSTALLATION, TROUBLESHOOTING, WEB_DASHBOARD, INSTALLATION, MODBUS_REGISTER_MAP). CHANGELOG moved to repo root with full version history back to v1.0.

### v4.0.3 — Overnight Preservation, Solar Deferral, Hourly Gap Model (Feb 2026)
- **Overnight battery preservation (P8 fix)**: P8 default mode changed from Self-Consumption to TOU. Battery now holds charge overnight with the grid powering the home at off-peak rates.
- **Solar-first charging deferral (P7 fix)**: P7 gap charging checks whether solar is actively producing and whether there's enough buffer time before peak. Defers grid charging when solar can plausibly fill the gap.
- **Hourly net-to-battery model (forecast fix)**: Morning plan `forecast_to_battery_kwh` replaced daily-total subtraction with per-hour surplus calculation. Reduces morning gap overestimation by 5-15 kWh on typical days.
- **Centralized debug logging**: `configure_logging()` applied across 9 files. Based on PR [#5](https://github.com/mtnears/FranklinWH-Automation/pull/5) by [@cecilkootz](https://github.com/cecilkootz).

### v4.0.2 — Forecast Engine, Kiosk Dashboard, Weekend Fix (Feb 2026)
- **Solar forecast integration**: `solar_forecast.py` wired into adaptive engine with graceful fallback.
- **Forecast-aware P7 charging**: Grid charges to `morning_ceiling_pct` instead of `target_soc`, leaving headroom for free solar.
- **Weekend peak detection fix**: Three v3.5-era components used clock math without checking day-of-week. Fixed to check `is_peak_day()`.
- **Fire HD 10 kiosk optimization**: Viewport fix, full-height layout, SVG icon refactor, CSS sky gradient.
- **New Solar Status card**: Live production, daily generation, self-powered percentage, net status.
- **Three-mode strategy**: TOU / Self-Consumption / Emergency Backup replaces two-mode system.
- **Weekly charts rewrite**: 5 charts with SOC timeline, mode markers, decision engine activity, curtailment tracker.

### v4.0.1 — Mode Switch Verification & Peak Safety Net (Feb 2026)
- **Root cause fix**: Mode detection was reading cached local state instead of actual hardware mode.
- **Switch verification**: Every mode switch retries up to 3x with hardware confirmation via cloud API.
- **Peak safety net**: Forces immediate switch to Self-Consumption if hardware is in Emergency Backup during peak.
- **Tiered cloud verification**: Every-cycle hardware checks during peak, 15-minute intervals otherwise.

### v4.0 — Adaptive Forecast Engine (Feb 2026)
- **Adaptive decision engine**: 8-phase priority system (P1-P8) replaces fixed time-based rules.
- **Forecast-aware charging**: Calculates morning SOC ceiling based on expected solar.
- **Curtailment protection**: Detects battery full + solar producing and switches modes.
- **Rate schedule flexibility**: Supports PG&E E-TOU-D, SMUD TOD, ComEd dynamic pricing, and custom schedules.
- **Anonymous telemetry**: Opt-in system with dashboard consent flow.
- **Diagnostic reporting**: One-click sanitized diagnostic bundle from dashboard.

### v3.5.1 — Script Status Dashboard & Daily Savings Fix (Feb 2026)
- Script Status dashboard tab with real-time health monitoring of all scheduled scripts
- Daily Savings calculation fix (argument handling, schedule timing, CSV format evolution)

### v3.5.0 — Modbus TCP + Enphase Local Integration (Feb 2026)
- Local-first data collection: SOC and grid power via Modbus TCP (26ms, was 5000ms via cloud API)
- Enphase local solar production reads
- Hybrid architecture: Modbus for monitoring, cloud API for mode switching
- Grid disconnect detection via Modbus registers

### v3.4.0 — Clock-Aligned Scheduling & Solar Override (Jan 2026)
- Clock-aligned 30-minute scheduling at :00 and :30
- Solar charging override for grid-charge deadlines
- Manual override API with auto-expiring timers
