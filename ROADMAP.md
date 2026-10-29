# FranklinWH Battery Automation — Roadmap

Planned features and improvements, in rough priority order. Contributions, feedback and discussion are welcome — open an [issue](https://github.com/mtnears/FranklinWH-Automation/issues). The maintainer runs one FranklinWH system (PG&E, NEM2, two aPower 2, Enphase + SolarEdge), so reports from other utilities, NEM3 and export setups shape what gets built next.

---

## ✅ Just shipped: v4.7.0 (October 2026)

Guided setup for new installs, a rate plan editor, and the engine running on the configuration store. See the [release notes](docs/RELEASE_NOTES_v4.7.0.md). What we most want from v4.7.0: **a few new installs going through the guided setup and telling us what was confusing or refused**. The clean-install path was verified in simulation against the maintainer's configuration; it has not yet met a stranger's house.

---

## 🔧 In Progress: v4.7.1

### Dashboard alert banners
**Priority: Medium** · optional, pluggable banners for utility outage maps and PSPS notices, NWS weather alerts, and (planned) burn-day status and nearby fire/evacuation feeds. Built on the `v4.7-alerts` branch during the v4.7 cycle; being rebased onto v4.7.0 and merged.

### Decision Engine card for export users (#28)
**Priority: Medium** · the card is blank for installs on the export path because that path emits a different metrics schema. Fix: log the CT metrics every cycle and serve them from one `/api/engine-metrics` contract.

### Diagnostic bundle privacy
**Priority: High** · allowlist what the bundle exports, read configuration from the store, scrub coordinates from logs, and remove the bundles still attached to old public issues.

### Smaller fixes
- Decision-log labels: partial-peak self-consumption and EB cycles show as "TOU"; add the rate to the partial-peak text and rename post-peak.
- The scheduler keeps only the first 200 characters of a collector's stderr, which hides the real error (seen with the Franklin cloud `get_stats` null `runtimeData` failures).
- Telemetry: de-duplicate installs on the fleet dashboard (count unique installs, not reports) and finish the install-UUID churn cleanup.

---

## 📋 Planned: v4.8 engine work

### Rate tiers ranked by price
**Priority: High** · with the rate plan editor in place, the engine should derive its behaviour from the plan rather than from the single named `peak` tier: rank the tiers by price per day so the most expensive window drives EB timing and peak protection, and add per-tier "grid charging allowed" flags in the editor. This retires `CHARGING_STRATEGY` and makes two-peak and super-off-peak plans first-class.

### Export and NEM 3.0 economics (#30, #21)
**Priority: Medium** · a first request for battery export to the grid under SCE's NBT ("vacation export mode"). Needs NEM 3.0 export-rate modelling (the month × hour export credit matrix), explicit engine path selection instead of the single `SOLAR_EXPORT` flag, and the metrics contract above. Design pass pending; input from NEM3 and export users wanted.

### Forecast calibration
**Priority: Medium** · the solar forecast runs high on some days; review the intraday calibration and the yesterday-correction, and the hot-day peak-need forecast for summer.

### Solar-day learning across the clock change
**Priority: Medium** · the learned production window is clock-time based and relearns over several days after a DST change. Review after the November 2026 fall-back and decide whether it should be anchored to sunrise/sunset offsets instead.

### Engine timing
**Priority: Low** · trace a ~22 s pause observed in the P7 gap evaluation.

---

## 📋 Planned: platform

### Telemetry endpoint move
**Priority: Medium** · the anonymous telemetry endpoint (`mtnears.com/telemetry`) moves off GoDaddy to a small VPS by early 2027. The URL does not change, so no install needs reconfiguring; a short gap in reports during the cutover is expected.

### Direct battery power register
**Priority: Medium** · battery power read directly from Modbus register 1048 (`battery_kw_direct`) is still parallel-logged alongside the derived value. Once a longer soak confirms it tracks across charge and discharge cycles, it becomes authoritative and the deprecated `self_reserve_pct`/`tou_reserve_pct` columns are dropped.

### Dashboard conditional UI
**Priority: Medium** · the dashboard still assumes solar + battery on a single-peak TOU plan. Solar-less systems should lose the solar cards; battery-only arbitrage users should see buy/sell metrics; dynamic-pricing users should see the current and next price; multi-period plans need a multi-period peak bar.

### Savings calculation
**Priority: Medium** · dynamic pricing should use the price at the time of charge and discharge; export-credit asymmetry under discount programs needs checking; NEM 3.0 export rates (see above).

### Multi-gateway management
**Priority: Low** · support for more than one aGate. The store is keyed by scope and arrays carry a gateway id, so this is representable without a schema change.

### Script Status descriptions
**Priority: Low** · update the Script Status tab for the scripts and jobs added since v4.1.

---

## 💡 Future ideas

- **Hybrid ML engine** — keep the algorithmic safety and mode logic as guardrails; learn solar and load prediction and SC/TOU timing from the history already in SQLite.
- **Utility/plan picker** — build the rate plan from a utility database (OpenEI URDB) instead of typing it; the editor built in v4.7 is the place it would land.
- **Weekend strategy optimization** — pure solar self-consumption on non-peak days with a planned off-grid duration from the forecast.
- **Utility billing data import (Opower)** — pull usage and cost history from supported utilities for bill validation and backfill ([#10](https://github.com/mtnears/FranklinWH-Automation/issues/10)); auth is scraping-based and data lags ~48 h, so this serves billing, not real-time decisions.
- **Home Assistant integration** — MQTT discovery for HA dashboards alongside the built-in dashboard (this project stays standalone automation, not an HA add-on).
- **Dashboard** — device auto-detection replacing manual presets; cache-busting asset URLs for kiosk browsers.

Concluded: direct Modbus writes for mode control need an installer unlock that most owners cannot get, so mode switching uses the aGate's local connection (port 9000) with the cloud as fallback, as shipped in v4.7.

---

## ✅ Recently Completed

### v4.7.0 — Guided Setup, Rate Plan Editor, Store-Backed Engine (October 2026)
- **Guided setup for new installs:** a secrets-only `.env` (`.env.minimal.example`), every other setting entered once in the dashboard; required settings enforced at commit (site, battery, export compensation, per-array details, feature keys); the engine observes only until the setup is committed. The configuration review from the rc series is the same flow for existing installs.
- **Rate plan editor:** add and remove seasons, tiers and time windows with templates; the plan must cover every month, price every tier it uses per season and have no overlapping windows; plans built in the editor have no inherited prices, and imported plans mark inherited ones.
- **Engine reads rates from the configuration store** (`rate_schedule.json` only as a fallback for un-migrated installs), so review edits reach the engine.
- **Local connection (rc.1–rc.6):** readings and mode switches over the aGate's local port with Modbus cross-check and cloud fallback; cloud token reuse; int16 power unwrap for >32.7 kW flows; daily rollup carry-over fix; SoH column; PVOutput catch-up; telemetry peak metric v2 and review-edit counts.
- **Build:** all dependencies and the base image pinned to the tested set; the v4 engine is the default (`ADAPTIVE_ENGINE_ENABLED` had defaulted to false in code).

### v4.6.1 — Mode Detection Fix + SolarEdge Collector Auth Migration (June 2026)
- **Mode detection fix (#21):** `detect_mode()` fell through to the unreliable `run_status` field for any Franklin schedule whose name didn't contain backup/tou/self substrings (e.g. a cloud-only user's "Custom NEM 3.0"), so the engine believed it was already in Emergency Backup and skipped switches it had correctly decided to make. Restored the exhaustive name-based detection from v3.3.0, mapping unmatched names to `HOME_MODE`. Cloud-only users only; Modbus installs verify via register 15507 and were unaffected.
- **SolarEdge panel collector rewrite:** migrated `collect_solaredge_panels.py` to SolarEdge's new Cognito-authenticated by-inverter energy API (the old basic-auth endpoint was retired). SRP auth via `pycognito`, tokens cached to disk. Added `collect_solaredge_modbus.py` (inverter-level Modbus collector) and `migrate_solaredge_inventory.py`.
- Fixed `SOLAREDGE_MODBUS_ENABLED` never being exposed on `config`, which had silently skipped the Modbus collector's scheduler registration.

### v4.6.0 — Configuration Consolidation + Settings Page (June 2026)
- **DB-resident config store** (`config_store.py`): `app_config`, `solar_arrays`, the rate-plan table set, and `app_state` in SQLite, with accessors. `migrate_v46.py` copies `.env` and `rate_schedule.json` into the store without modifying either, recording each value's source (env vs default).
- **One canonical rate resolver** (`rate_config.py`): single per-date resolver for tier rates and the peak window, replacing divergent rate-reading paths. Savings calculator fixed — was computing from hardcoded fictional rates (0.60/0.41) and a fixed peak window; now resolves per date and reads capacity from config.
- **Engine peak repoint (#26):** the decision engine followed the rate schedule's peak window instead of legacy `.env` peak hours; env becomes fallback-only with a `CONFIG CONFLICT` log line on disagreement.
- **Read-only Settings page + validation:** new `/api/v1/settings/*` endpoints and a live configuration view replacing the placeholder thresholds card. Configuration Health flags peak-window conflicts (#26), `SOLAR_EXPORT`-vs-NEM mismatches (#21), season coverage/overlap (#18), unreviewed arrays, and capacity/solar-capacity-semantic issues.
- **PR #25:** parallel-logs direct battery power (register 1048) and a single mode-conditional `active_reserve_pct` alongside the legacy reserve pair, ahead of a v4.6.1 authority swap.
- Additive release — `.env` and `rate_schedule.json` remain authoritative and required.

### v4.4.1 — Per-Season Rate Auto-Switching (May 2026)
- **Per-season tier_rates override** — `rate_schedule.json` `seasons` block now supports `tier_rates` overrides in addition to the existing `windows` overrides. Plans with summer/winter rate differences (PG&E EV2-A, etc.) flip automatically on the seasonal boundary without manual JSON edits
- Three-tier plans previously had a silent gap: `rate_history` DB only stored peak/off_peak, so `partial_peak` stayed frozen across season changes. JSON-level seasons block closes that gap; DB precedence preserved for peak/off_peak
- **Version banner from VERSION file** — new `scripts/version.py` helper; engine banner now reads dynamically from `/app/VERSION` instead of hardcoded `v4.0`
- Validation warnings for season misconfig (overlap, missing months, invalid types, unknown tier names)
- Backward compatible — configs without `seasons` block unchanged

### v4.4.0 — Three-Tier Rate Support (May 2026)
- **Priority 4.5 partial-peak protection** — new decision priority between peak (P4) and curtailment (P5). Pre-peak partial-peak preserves battery for sacred peak; post-peak partial-peak discharges freely
- New `is_partial_peak()`, `is_expensive()`, and `expensive_window_remaining_hours()` helpers in `rate_schedule.py`
- Reference `rate_schedule.json` updated to PG&E EV2-A with CARE; existing plan examples (E-TOU-D, SMUD, SCE, Pepco, ComEd) remain in `data/rate_schedule.example.json`
- Dashboard rate plan card displays all three tiers; analytics tab amber bands distinguish peak (darker) and partial-peak (lighter)
- Two-tier plans unchanged — P4.5 is a no-op when no `partial_peak` tier is defined

### v4.3.1 — Target-Aware SC Commit (May 2026)
- **Root cause fix** for under-target peak entry on small-solar / cloudy-day systems. CT strategy was committing to SC when projection cleared survival floor (~59%) even when landing well below configured target (95-100%), locking out EB gap evaluation
- Fix: new `CT_SC_COMMIT_MARGIN_PCT` constant (default 3.0%) — SC commits only when projection lands within margin of target. Cloudy / under-sized days fall through to EB gap logic as designed
- New `ct_sc_commit_threshold` metric in `intelligence_log` rows where CT wins
- Reported by **miztahsparklez** (Issue #21). Issue #22 and additional related community reports addressed in the same release

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
