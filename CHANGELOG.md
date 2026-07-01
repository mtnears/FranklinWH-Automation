# Changelog

All notable changes to FranklinWH Battery Automation.

---

## v4.6.1 — June 2026

A bug-fix and collector-maintenance release. Two independent efforts: a mode-detection fix that restores correct behavior for users whose Franklin schedule has a custom name, and a rewrite of the SolarEdge panel collector against SolarEdge's new authenticated energy API.

### Mode Detection Fix — Custom Schedule Names (#21)
- **Root cause:** `detect_mode()` treated the gateway `mode_name` as authoritative only for three substring matches (backup/emergency, tou/time, self/consumption). Any other schedule name — e.g. a cloud-only NEM 3.0 user's "Custom NEM 3.0" — matched none and fell through to the `run_status` fallback, which is unreliable on FranklinWH firmware (observed stuck at `1` = emergency_backup regardless of actual mode). The engine then believed it was already in Emergency Backup and skipped the switch it had correctly decided to make: the hardware stayed in TOU while the log read `Mode unchanged: emergency_backup`. Pre-peak urgent charging was silently defeated for affected cloud-only users.
- **Regression origin:** the v3.3.0 name-detection fix ended with an `else → home mode` catch-all that made the name block exhaustive. That catch-all was lost when an intermediate `tou`/`time` branch was later added, reopening the drop-through to `run_status`.
- **Fix:** when `mode_name` is present it is authoritative. Names that aren't backup/self now map to the user's `HOME_MODE` (`tou` → `time_of_use`, `self_consumption` → `self_consumption`) instead of falling through. `run_status` is consulted only when `mode_name` is entirely absent. Isolated to `smart_decision.py`.
- Modbus-equipped installs (which verify mode via register 15507) were unaffected — this only bit cloud-only users whose schedule name missed the substrings.

### SolarEdge Panel Collector — New Authenticated Energy API
- SolarEdge retired the basic-auth energy endpoint (`/solaredge-apigw/`) the per-optimizer collector relied on. `collect_solaredge_panels.py` rewritten against the new `/services/layout/energy/site/{id}/by-inverter` endpoint, which requires an AWS Cognito JWT (`se_monitoring_auth` cookie + `x-se-user-id` header derived from the token's `uuid` claim) plus browser-context headers
- Auth via `pycognito` SRP handshake (the client has `USER_PASSWORD_AUTH` disabled); access and refresh tokens cache to `data/solaredge_se_token.json` so SRP doesn't re-run every 15-minute cycle. The old layout endpoint still works on basic auth and is retained for optimizer inventory
- New `get_energy()` returns per-optimizer energy plus a period-relative `color` health metric in a single call; the parse loop joins layout inventory to energy by hardware serial, and `apply_color_health()` overlays color onto health status in an escalate-only pattern then recomputes counts
- New `scripts/collect_solaredge_modbus.py` — inverter-level Modbus collector (SE7600H confirmed at 192.168.4.213:1502; SE11400H pending installer verification)
- New `scripts/migrate_solaredge_inventory.py` — one-time inventory migration
- `config.py`: added `SOLAREDGE_MODBUS_ENABLED` — `getattr(config, 'SOLAREDGE_MODBUS_ENABLED', False)` previously always returned False, silently skipping the Modbus collector's scheduler registration while the startup banner still printed "every 5 min"
- `scheduler.py`: lifted the panel-job suppression (the collector now self-gates its `solar_barn.json` write when Modbus is enabled) and registered the Modbus job
- `requirements.txt`: added `pycognito>=2024.5.1`

### Credits
Mode-detection issue reported by community user **miztahsparklez** (NEM 3.0, SCE, cloud-only) as a follow-up on #21.

---

## v4.6.0 — June 2026

Foundation release: configuration consolidation, a canonical rate resolver, and a read-only settings page with configuration validation. Behavior is unchanged for a correctly-configured system — this release changes where configuration is *read from* and surfaces what was previously invisible. `.env` and `rate_schedule.json` are **not** retired; they remain authoritative and required.

### DB-Resident Configuration Store (Phase 1)
- New `scripts/config_store.py` — a SQLite-backed configuration store with four tables and accessors: `app_config` (typed key/value, scope-aware for future multi-aGate), `solar_arrays` (per-array inventory with an explicit `charges_battery` flag), the `rate_plans`/`rate_seasons`/`rate_tiers`/`rate_windows`/`rate_holidays` set, and `app_state` (schema version, migration stamps, reserved for the setup-wizard state machine)
- New `scripts/migrate_v46.py` — manual one-shot migration (run via `docker exec`, not auto-invoked) that **copies** `.env` and `rate_schedule.json` into the store without modifying either file. Records each value's source (`env` vs code `default`) so the settings page can flag never-reviewed defaults. Idempotent: re-runs refresh env/default rows, preserve user edits, and re-import the rate plan atomically
- **#18 fix surfaced at import:** the migration rejects the legacy per-window `months` key (silently ignored by the old parser, which caused seasons to never switch for some users) rather than dropping it silently; `--allow-legacy-months` downgrades to a warning. Also validates season month coverage and overlap
- Secrets stored base64-**obscured** (not encrypted — prevents shoulder-surfing and accidental log/screenshot exposure). The three Franklin credentials never enter the store; they stay in `.env`
- `db.py`: `system_reading()` gains `battery_kw_direct` and `active_reserve_pct` columns; `init_db()` ensures them idempotently at startup so the collector can never race the migration
- `collect_modbus.py` (PR #25): parallel-logs register 1048 (battery power read directly) alongside the derived `battery_kw` (load − solar − grid), and derives a single mode-conditional `active_reserve_pct` alongside the legacy `self_reserve_pct`/`tou_reserve_pct` pair. Parallel logging validates the direct read before a later authority swap

### Canonical Rate Resolver + Engine Peak Repoint (Phase 2)
- New `scripts/rate_config.py` — **one** per-date resolver for tier rates and the peak window, replacing divergent rate-reading paths. Resolution order: v4.6 rate tables (per-date season) → `rate_history` overlay for peak/off_peak (CARE-first, mirroring the engine's proven path) → `rate_schedule.json` fallback for un-migrated installs. Returns nothing rather than fabricating rates when no source resolves. Units are explicit (cents canonical, dollars on request) to prevent the 100× class of error
- **Savings calculator fix:** `calculate_daily_savings.py` previously computed savings from hardcoded fictional rates (0.60/0.41 $/kWh) that existed nowhere in any config, and a hardcoded 17:00–20:00 peak window. It now resolves rates and the peak window per date through `rate_config`, and reads battery capacity from `app_config`. Dates with no resolvable rate are skipped, not invented. Savings history recomputes against the rates actually in effect on each date
- **#26 fix:** the decision engine's peak detection read `PEAK_START_HOUR`/`PEAK_END_HOUR` from `.env`, which silently drifted from the rate schedule when `seasons[]` window logic was added — the dashboard and savings moved onto the resolved seasonal window but the engine never did, causing overnight discharge after a season change for users whose env peak hours were stale. `smart_decision.py` now resolves the peak window from the rate schedule via `rate_config`; the legacy env vars become fallback-only, and a `CONFIG CONFLICT` line is written to `intelligence_log` when env disagrees with the schedule instead of failing silently

### Read-Only Settings Page + Configuration Validation (Phase 3)
- `scheduler.py`: four new endpoints — `/api/v1/settings/config` (app_config by category, source flags, secrets redacted on the wire), `/api/v1/settings/arrays` (battery classification, credentials masked), `/api/v1/settings/rate-plan` (plan, seasons with merged tier rates, windows, and today's live resolution), and `/api/v1/settings/recommendations` (the validation engine)
- Configuration Health checks: legacy-env-vs-schedule peak window conflict (#26), `SOLAR_EXPORT` strategy vs net-metering/export-capability (#21 — surfaces the active charging strategy in plain language, since `SOLAR_EXPORT` is a strategy flag, not a capability flag), season month coverage/overlap (#18), unreviewed array classification, battery-capacity-vs-count consistency, and the `SOLAR_CAPACITY_KW` semantic check (battery-connected array only, not summed with separately-metered arrays)
- `power_dashboard.html`: the placeholder "Automation Thresholds" card — which displayed `.env` variables that don't exist (`PEAK_SOC_TARGET`, `CHARGE_RATE_KW`) and a hardcoded 30 kWh capacity — is replaced with a live configuration view: automation summary (active strategy, today's window and rates, battery, the grid-charge-ceiling vs solar-target distinction), Configuration Health, rate plan, arrays, and the full configuration browser with source flags

### Notes
- This release is additive. `.env` and `rate_schedule.json` are unchanged and still read by the system; do not delete or trim either. Editing config still works as before (edit `.env`/the JSON, restart, re-run the migration to refresh the store). Retiring the old files to a credentials-only `.env` is planned for a future release via a migration with backups
- The new `battery_kw_direct` column is parallel-validation data for an upcoming accuracy improvement; it is logged but not yet consumed by decision logic
- Multi-aGate groundwork: `app_config` is keyed `(scope, key)` and `solar_arrays.gateway_id` ties arrays to a gateway, so per-gateway configuration is possible later without a schema change

---

## v4.4.1 — May 2026

### Per-Season Rate Auto-Switching
- **Root cause fix:** `rate_schedule.json` `seasons` block previously supported `windows` overrides per season but not `tier_rates`. On three-tier plans (PG&E EV2-A and similar), the existing `rate_history` DB-based seasonal switching only covered `peak` and `off_peak` columns. `partial_peak` rates stayed frozen at whatever JSON last said when seasons changed — silent miscalculation on the June 1 / October 1 boundary with no error to trip on
- **Fix:** `seasons[].tier_rates` now overrides all tiers (including `partial_peak`) when the current month matches a season's `months` array. Precedence: JSON base `tiers` → seasonal `tier_rates` override → DB `rate_history` override (peak/off_peak only)
- Configs without a `seasons` block behave exactly as before — fully backward compatible
- New `_validate_seasons()` helper logs WARNING for: overlapping months between seasons, missing month coverage, invalid month values (non-int or out of 1–12 range), empty `months` list, unknown tier names in `tier_rates`, non-list seasons value
- `data/rate_schedule.example.json` adds a new `pge_ev2_a_care` example demonstrating the pattern; `pepco_r_tou_p_seasonal` `_comment` updated to note `tier_rates` is now also supported per season

### Version Banner Reads From VERSION File
- New `scripts/version.py` helper reads from `/app/VERSION` (container) with fallback to script-relative repo-root path for out-of-container testing. Result cached after first successful read
- `smart_decision.py` engine startup banner replaced hardcoded `"v4.0 Adaptive"` with `f"v{get_version()} Adaptive"` — banner stays in sync with releases automatically
- Docstring `v4.0` references in `adaptive_engine.py`, `rate_schedule.py`, `solar_forecast.py`, `system_profile.py`, and `smart_decision.py` left unchanged — these refer to the engine architecture branch, not runtime version

---

## v4.4.0 — May 2026

### Three-Tier Rate Support — Peak / Partial-Peak / Off-Peak
- **New Priority 4.5** in `adaptive_engine.py` between P4 (peak protection) and P5 (curtailment protection). Activates during partial-peak windows on three-tier plans
- Pre-peak vs post-peak partial-peak differentiation: when peak is still ahead, preserve battery and fall through to TOU when SOC is low so battery can recharge before sacred peak; when peak is behind, free to discharge via Self-Consumption with no SOC floor concern
- New helpers in `rate_schedule.py`:
  - `is_partial_peak(dt)` — true when current tier is `partial_peak`
  - `is_expensive(dt)` — true for peak OR partial-peak (avoid imports if possible)
  - `expensive_window_remaining_hours(dt)` — walks forward in 5-minute increments to find the next off-peak transition. Returns `(peak_hours_remaining, partial_peak_hours_remaining)` for the P4.5 decision branch
- Decision messages tagged `[P4.5]` for partial-peak entries in `intelligence_log`
- Two-tier plans continue to work unchanged — P4.5 is a no-op when no `partial_peak` tier is defined in `rate_schedule.json`

### Reference Configuration Updated to PG&E EV2-A with CARE
- Shipped `data/rate_schedule.json` reconfigured for PG&E EV2-A with CARE: 4–9pm peak, 3–4pm and 9pm–12am partial-peak, every day including weekends and holidays
- Empty `holidays` list with `holiday_tier: "off_peak"` as defensive default — EV2-A tariff has no holiday exception
- Other plan examples (E-TOU-D, SMUD TOU, SCE TOU-D-PRIME, Pepco R-TOU-P seasonal, ComEd) remain available in `data/rate_schedule.example.json`

### Dashboard
- Rate Plan card displays all three tiers with current-rate highlight
- Analytics tab amber bands distinguish peak (darker) and partial-peak (lighter)
- Peak countdown bar reads multi-window peak periods from `rate_schedule.json`

### Other Changes
- `BATTERY_CAPACITY_KWH` reference for aPower 2: 13.6 kWh per battery (multi-battery installs sum, e.g. 27.2 kWh for 2× aPower 2)
- Engine peak entry SOC requirement now accounts for partial-peak hours when computing target

---

## v4.3.1 — May 2026

### Adaptive Engine — Target-Aware SC Commit
- **Root cause fix:** Continuous Target Tracking strategy gated Self-Consumption commit on whether projection cleared the *survival floor* (reserve + peak need + safety). On systems where solar can't realistically fill the battery to target — small solar relative to capacity, cloudy days, or any configuration where forecast solar falls short — projection cleared survival even when it landed well below target. The engine committed to SC for the rest of the day, locking out the chained EB gap evaluation that would otherwise have grid-charged at the last responsible moment. Result: undercharged at peak, EB never firing despite a real shortfall
- **Fix:** new target-aware threshold. SC commits only when projection lands within `CT_SC_COMMIT_MARGIN_PCT` (default 3.0%) of the dynamic target. When projection falls short, CT returns TOU and the EB gap logic gets to apply last-responsible-moment timing as designed
- Both small-solar and well-sized installs are now handled by the same logic — sunny days continue to commit to SC normally; cloudy or under-sized days fall through to EB gap evaluation
- New `ct_sc_commit_threshold` metric recorded in `intelligence_log` rows where CT is the winning decision — visible in diagnostic bundles
- Decision messages reference the unified threshold instead of the bare survival floor

### Credits
Reported as Issue #21 by community user **miztahsparklez** (NEM 3.0, SCE TOU-D-PRIME, cloud-only). Diagnostic bundle analysis confirmed the SC commit gate as the root cause — projection consistently cleared survival floor (~59%) while landing well below configured target (95-100%), so EB never fired and battery sat at 60-65% going into peak. Issue #22 and additional related community reports addressed in the same release.

---

## v4.3.0 — May 2026

### Cloud-Only Data Persistence (Bug Fix — Affects All Cloud-Only Users)
- **Root cause fix:** `system_readings` rows from `collect_franklin_cloud.py` were inserting with NULL for the primary fields (`soc_pct`, `solar_kw`, `grid_kw`, `battery_kw`, `home_load_kw`, `mode`, `grid_status`). Cloud-only systems were running with no usable history in the database
- Two downstream symptoms resolved:
  - Engine load profile learning was operating on default 1.8 kW assumptions instead of measured load — produced bad SC vs TOU projections on cloud-only systems
  - SOC-target manual overrides never exited because `_read_latest_soc()` reads `soc_pct` from `system_readings`, which was NULL
- `db.py system_reading_update_cloud()` — primary fields added to signature. UPDATE branch uses `COALESCE(col, ?)` so Modbus values are preserved when both data sources populate the same row. Cloud-only INSERT branch populates everything
- `collect_franklin_cloud.py` — extracts `soc_pct/solar_kw/grid_kw/battery_kw/home_load_kw/grid_status` from `stats.current` (mirrors `data_sources.CloudDataSource`). `grid_status` mapping handles the franklinwh enum (`NORMAL`, `OFFGRID`, etc.) in addition to Modbus-style strings (`Connected`/`Disconnected`)

### Override Resilience
- `_read_latest_soc()` in both `scheduler.py` and `smart_decision.py` upgraded with a 30-minute DB freshness window plus Franklin cloud API fallback (30-second cache) when DB returns no recent SOC. Defends override SOC-target exit against future regressions and against any data collection gap
- `adaptive_engine.py` — `override_path` default aligned to `logs/override.json` (was `data/override.json`), matching what `smart_decision.py` actually passes

### Manual Override UI Redesign
- Override modal: 6 chips replaced with 4 outcome-oriented options:
  - **Until SOC reached** — existing slider, charge up or drain down to target
  - **For duration** — hours + minutes inputs, custom durations up to 24 h, last entry remembered in localStorage so 2h 10m pre-fills next time
  - **Until specific time** — HH:MM picker, defaults to current time + 1 h rounded to nearest 15 min, rolls to tomorrow if past, today/tomorrow indicator
  - **Until canceled** — no expiry, exits only on manual cancel
- Banner now shows "until 18:30 (2h 15m remaining)" for time-based overrides
- POST `/api/override` accepts new payload shapes:
  - `{duration: 'until_soc', exit_soc_pct}`
  - `{duration: 'custom', duration_minutes}` (1–1440 validated)
  - `{duration: 'until_time', until_time: 'HH:MM'}` (regex-validated)
  - `{duration: 'until_cancel'}`
- Old chip values (`1h`/`2h`/`4h`/`8h`) removed from `dur_map`

### Other Changes
- Modbus collection job now guarded by `MODBUS_ENABLED` — eliminates timeout-error noise in `scheduler_log` on cloud-only deployments
- Dashboard config display: `min_soc_reserve` field now reads from `BACKUP_RESERVE_PCT` (was reading non-existent `MIN_SOC_RESERVE`, defaulted to 20 regardless of `.env` value). Settings tab env-var label corrected to match
- `solar_forecast.py` — house array tilt 22°→18°, azimuth -65°→0°, removed WNW penalty multiplier and noon-shift correction. Yesterday correction factor now passes today's weather to `correction_factor()`. Calibration model `ratio = 0.520·score + −0.040 (R²=0.835)` validated against measured production

### Credits
Investigation triggered by community user **miztahsparklez** (NEM 3.0, SCE TOU-D-PRIME, cloud-only). Root cause and downstream effects confirmed across `db.py`, `collect_franklin_cloud.py`, `scheduler.py`, `smart_decision.py`, and `generate_dashboard_data.py`.

---

## v4.2.2 — April 2026

### Adaptive Engine Cooldown Fix
- **Root cause fix:** `adaptive_engine.py` `_decide()` updated `self.last_mode_switch` even on no-op transitions (target == current_mode). Subsequent decisions in the same cycle saw a phantom 300-second cooldown and were blocked
- Fix: only update `last_mode_switch` when the target differs from current mode

### State Logging
- `enrich_state()` now rounds `current_rate_cents` to 3 decimal places — eliminates floating-point noise in intelligence_log entries

---

## v4.2.1 — April 2026

### Data Export Tab (Energy & Billing)
- New dashboard tab providing CSV export of historical data
- Three API endpoints: available date ranges, billing-period selection, range export
- Date picker with billing-period dropdown for quick selection of utility billing windows
- Summary cards display total usage, generation, net, and charges for the selected range
- CSV download with all relevant fields for the selected period

### Bug Fixes
- Billing period dropdown label fix (was showing internal IDs instead of human-readable labels)
- Fallback render path for older billing periods that pre-date the `daily_energy_summary` table

---

## v4.2.0 — April 2026

### Multi-Window Peak Support
- `rate_schedule.json` now supports multiple peak windows per rate plan (peak + partial-peak + off-peak), driven from the schedule definition rather than hardcoded peak start/end hours
- Peak bar and engine logic both consume the multi-window structure

### Seasonal Window Selection
- Rate windows can specify a `months` array — automatic seasonal switching as PG&E and other utilities define different summer vs winter peak periods within the same plan

### Modbus Sanity Bounds
- Near-`0xFFFF` register values (e.g., 65531, 65532) slipped through exact-sentinel checks and showed up as 65 MW solar production etc.
- Added explicit upper bounds: `MAX_PLAUSIBLE_SOLAR_W = 25,000`, `MAX_PLAUSIBLE_LOAD_W = 50,000`. Values above these are treated as Modbus errors and discarded
- Fixed `collect_modbus.py` `ValueError` from `:.3f` format applied to the `'?'` string fallback when fields were unavailable

### Inventory Dedup
- `collect_device_inventory.py` snapshot logic deduplicates within a write — prior versions could create duplicate device records on repeated firmware probes

### Dynamic Dashboard Rate Plan Card
- Rate Plan card on dashboard now reflects the current schedule's name, current tier, and current rate live from `rate_schedule.py` instead of hardcoded `E-TOU-D` text

### Diagnostic Bundle Enhancements
- `system_info.json` in the bundle now includes `engine_version`, `git_commit`, and `rate_schedule_name` so issue triage can quickly identify what version produced the bundle

---

## v4.1.1 — March 2026

### Bug Fixes
- `collect_modbus.py` — `ValueError` raised when register values were unavailable and substituted with the `'?'` sentinel string, then passed through `f'{val:.3f}'` formatting. Format expression now guarded against non-numeric fallback
- Inventory dedup race condition where rapid firmware-probe cycles could write duplicate `device_inventory` rows

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
- **Open-Meteo solar forecast** — replaced Forecast.Solar with Open-Meteo for solar forecasting. No API key required, 10,000 free calls/day, hourly global tilted irradiance already corrected for array tilt/azimuth. Existing calibration and fallback logic unchanged
- `rate_schedule.py` — rate schedule management with JSON config file (`data/rate_schedule.json`)
- `diagnostic_bundle.py` — one-click sanitized diagnostic bundle from dashboard with credentials auto-stripped
- `collect_solaredge_panels.py` — per-optimizer panel health monitoring with 21-day rolling window
- **Export system support** (`SOLAR_EXPORT=true`) — post-peak solar discharge and curtailment protection both skip on export systems
- Removed dead `import csv` from `smart_decision.py`
- **Documentation refresh** — all 6 docs/ files updated for v4.1 (Configuration Reference, Docker Installation, Troubleshooting, Web Dashboard, Installation, Modbus Register Map). CHANGELOG moved to repo root with full version history back to v1.0

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
