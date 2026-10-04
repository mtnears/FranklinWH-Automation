# Changelog

All notable changes to FranklinWH Battery Automation.

---

## v4.7.0-rc.3 — October 2026 (branch `v4.7-rc`)

Readiness fixes for other installs.

- **Finding the aGate with no IP configured:** the search now also tries the LAN address the dashboard was opened on, the host's gateway (host networking) and common home subnets, after any device IPs already in the configuration. The Connection step's Test uses the page's address first.
- **A search that finds nothing is not repeated for an hour.** The log says to enter the aGate IP in the configuration review instead of rescanning the network every 5 minutes.
- **No site-specific aGate IP in code defaults:** `MODBUS_HOST` defaults to blank.

---

## v4.7.0-rc.2 — October 2026 (branch `v4.7-rc`)

Fixes from the rc.1 live test.

- **Configuration review, Connection step:** one **aGate IP address** row, used by both the local connection and Modbus, followed by the Local connection and Modbus switches, each with its own Test. A separate local-connection address is now an Advanced override, for the rare aGate that is only reachable on another interface.
- **Franklin account Test:** when Franklin's cloud login is down it now says so in plain words (server error, Franklin's side, not needed while the local connection works) instead of a raw HTTP error.
- **Cloud collector:** reuses the saved session token. While Franklin's login is down and the local connection is working, it logs one warning instead of failing every 15 minutes.
- **Dashboard:** rebuilt immediately after an override switch or cancel, so the mode badge updates within seconds.
- The engine's Features line lists the local connection.

---

## v4.7.0-rc.1 — October 2026 (branch `v4.7-rc`)

The engine no longer depends on the Franklin cloud to run your battery.

### Local connection
- **Readings and mode changes go straight to the aGate over your home network.** Every engine cycle reads the aGate directly and changes the operating mode directly, then checks that the change took. This keeps working when the Franklin cloud login is down, which it has been since October 3.
- **With Modbus on, Modbus checks the local connection.** Every reading is compared with Modbus (battery %, grid power, mode), and every mode change is confirmed by the mode the aGate reports over Modbus. Modbus is also the first fallback for readings.
- **Without Modbus, the local connection is enough.** Installs whose installer has not turned Modbus on get local readings and mode changes too. A new 5-minute collector writes the readings to the database when Modbus is off.
- **The cloud is the last resort.** It is only used when the local connection fails. It now reuses the last good session token instead of logging in each time, so a broken login page does not stop it.
- **Mode changes keep your reserves.** A local mode change selects the aGate's own Self-Consumption, Time of Use or Emergency Backup mode, each with the reserve you set in the Franklin app.
- **If every connection fails, the dashboard shows a red banner**, the log records an error, and the next engine cycle tries again. The banner clears on the next successful change.

### Configuration review
- **Connection step:** a new "Local connection" switch (on by default) and an optional aGate IP. The IP falls back to the Modbus IP. **Test** checks the connection and lists the aGate's modes. With no IP set, it searches your network for the aGate.
- The Franklin account email, password and gateway ID are only required when no local connection is set up.

### Telemetry
- New `local_control` section. It reports which connections are on and, for the last 1, 7 and 30 days: success rate and latency per connection for readings and mode changes, which connection carried each mode change, how long confirmation took, how often local and Modbus readings disagreed, and how often the saved cloud token was reused. It sends counts and timings only, never addresses or error text.

### Upgrading
- Rebuild the image (`docker compose build --no-cache`), then `down` and `up`. The new dependency installs during the build.
- No configuration changes are needed. The local connection is on by default and uses your Modbus IP. To turn it off, set it off in the Connection step.

---

## v4.7.0-beta.3 — October 2026 (branch `v4.7-config-review`)

Fixes from the beta.2 live test pass.

### Dashboard
- **Settings card shows every setting's value in effect.** `GET /api/v1/settings/config` listed only stored rows, so settings left at their built-in default (battery count, planning floor, solar fill ceiling) showed as `null`. It now lists every registry setting with its effective value and marks defaulted ones `source: default`.
- **The Configuration line shows the source, the review and the counts** (for example "source store · reviewed 2026-10-03 10:27 (4.7.0-beta.2) · 48 settings: 20 set, 28 at default"), not the Phase-1 migration stamp. The same set/default count is used by the dashboard, Configuration health and the commit summary.
- Labels match the review ("planning floor", "grid charge / solar fill ceiling"); Preview and Setup checklist are styled as buttons.

### Configuration review
- **Header shows when the store last changed** (a commit, sync or admin tool), not the last sync from `.env`.
- **The step tabs wrap** instead of scrolling behind a scrollbar.
- **A red dot marks any step with a value that needs fixing**, and the bottom bar shows how many need fixing with a link to the step.
- **Rates:** each season shows "matches" or "edited (n)" with a revert; edited prices and times are outlined. The tariff row says the checklist links PG&amp;E's schedules, with other utilities to follow (also in the PDF).
- **Solar arrays:** the AC-capacity warning appears once, on the array row (Derived points to it); "DC size in store" only shows when it differs from the DC size field.
- Whole-number values show without a trailing ".0"; retyping the same number is not an edit.
- **SolarEdge array cards have a Test:** the monitoring API (site ID and key: name, peak power, status, last update) and, with local Modbus on, each inverter's model, status and output.

### Fixes
- **`config_admin.py prune-retired`** deletes `app_config` rows no registry setting describes (Phase-1 names nothing reads, e.g. `rates.care_active`, `notify.*`). It snapshots first.
- Configuration health checks read registry settings through `settings.get`, so defaulted values (battery count, strategy, export compensation) are seen; the NEM check uses `billing.export_compensation` instead of the retired `solar.nem_version` row.

---

## v4.7.0-beta.2 — October 2026 (branch `v4.7-config-review`)

Fixes from the first live tester review.

### Configuration review
- **Site-specific settings no longer have built-in defaults.** These are latitude/longitude, the battery-array DC size, tilt and azimuth, and the aGate IP. The old defaults were the developer's own values, so a "matches" value was stored as "no row", and the forecast then logged "site location not configured" on every cycle.
  - The review now requires these settings (the aGate IP only when Modbus is on; the array values only when forecasting is on) and always stores them.
  - `config_admin.py fill-site` writes the missing rows once from `.env` for installs that committed beta.1.
- **Re-reviewing after a commit pre-selects your reviewed values.** Previously "no row" (a value left at its default) read as "not in the store", so `.env` was pre-selected again. For example, a CARE discount you had removed came back.
- **Out-of-range or missing values are flagged as you type.** Commit stays disabled until they're fixed, and the Summary lists them. The server checks still apply.
- **The page reloads its data after a commit or rollback restart,** so the header and banner show the configuration that's actually running.
- **Solar arrays:** the AC-capacity hint now also appears on the AC capacity row. On non-export battery arrays it explains that curtailment holds the observed maximum below nameplate.
- **Engine behavior:** warns when Strategy is Export but no battery-charging array exports.
- **Rates:** a "(midnight)" label on windows that end at 00:00, and a link to the Setup Checklist's new "Where to find your official rate schedule" section (PG&amp;E tariffs page). The PDF is now served at `/SETUP_CHECKLIST.pdf`.
- **Weather:** relabeled as Weather Underground PWS, with help text on API keys and on using a nearby station. The Test now reports the station's distance from your site, and warns above 15 km.
- **Telemetry country uses ISO 3166-1 alpha-2 codes** (US, CA, GB…). Both the review and the telemetry consent pop-up use the same 249-country list (`scripts/regions.py`, `GET /api/v1/regions`); the dropdown shows full names and stores the two-letter code.
  - The consent pop-up's old "UK" becomes GB.
  - `TELEMETRY_REGION` used to be a US *state* code (CA meant California), so the review flags 2-letter state values and asks you to pick your country.
  - Telemetry's `region` field is now always a country code, with `region_scheme: iso3166-1`. The state inferred from the weather station is sent separately as ISO 3166-2 in `region_state` (e.g. US-CA).
- **Readability:** muted text contrast raised from about 3.1:1 to about 6.4:1, on both the review and the dashboard. Help and footnote text on the review page goes from 12 px to 13 px.

### Fixes
- **Startup "`.env values ignored`" and `dump_effective_config.py`** compare effective values, so a setting that only falls back to the code default is no longer reported as a difference. `config_admin.py authority store` accepts reviewed differences once a review has been committed.
- **`db.init_db()`'s historical mode-normalization scan** now runs once and is stamped in `app_state`. It used to scan all of `system_readings` at the start of every collector run, including Dashboard Data every minute.

---

## v4.7.0-beta — October 2026 (branch `v4.7-config-review`)

The configuration store becomes the source of truth, through a one-time guided review. Full notes: [docs/RELEASE_NOTES_v4.7.0-beta.md](docs/RELEASE_NOTES_v4.7.0-beta.md).

### Configuration review
- **Settings registry (`settings.py`)** classifies every setting. Settings and detected values move to the store. Secrets, deployment values and dev-tool names stay in `.env`, and retired keys are ignored. Every consumer reads through one accessor; `check_settings_access.py` enforces that.
- **Review screens (`web/config_review.html`, `GET /api/v1/review`)** cover 11 steps. Each step shows the `.env` value, the store value, the value in effect and the proposed value, with help and hints. `?preview=1` saves nothing.
- **Commit (`config_commit.py`, `POST /api/v1/review/commit`):**
  - Values are validated on the server against the registry.
  - The config tables are snapshotted before writing, and everything is written in one transaction.
  - The store is then checked against what was reviewed, and the rate plan must resolve. A failure restores the snapshot.
  - Committing sets `config.authority=store` and restarts the scheduler in place (`os.execv`, between jobs).
  - Refused within the peak guard (−20/+5 min).
  - Values at their code default are not stored.
  - An install without a rate plan in the store gets `rate_schedule.json` imported.
- **Rollback (`POST /api/v1/review/rollback`, or `config_admin.py authority env`):** the store is kept. `config_admin.py restore <snapshot> --yes` restores an earlier configuration.
- **Live tests (`review_tests.py`, `POST /api/v1/review/test`)** for Modbus, Envoy, Franklin cloud, SolarEdge, PVOutput and Weather Underground. They run in a subprocess with a timeout and use the secrets from `.env`. Results are scrubbed of secrets before they're returned.
- **Dashboard:** a banner asks you to take the review until it has been committed and the system runs on the store.
- **Telemetry:** adds `config_authority`, `review_completed` and `review_completed_version`.
- **`config_admin.py sync`:** leaves rate plans edited in the review alone unless `--overwrite-user`.
- **`docs/SETUP_CHECKLIST.pdf`:** a fillable checklist generated from the registry (`tools/build_setup_checklist.py`).

### Solar day and forecast
- **Learned solar day (`solar_day.py`):** production start, ramp and end are learned relative to the computed sunrise and sunset, replacing the fixed `SUN_SCHEDULE` table, which ran about 55–85 min late on sunset. The learned solar cap (p99.9 of observed output, capped at the configured AC size) replaces the hardcoded 5.3 kW.
- **`forecast_history` table (`record_forecast_history.py`, 06:40 / 20:40):** durable evening and morning forecasts alongside the actual production.
- **The forecast array, site, battery capacity and solar cap** come from settings and detected hardware instead of code constants.

### Fixes
- **`rate_schedule.json`:** an invalid file now logs the file, line and column and raises, instead of failing silently.
- **Dashboard update badge:** versions are now compared numerically, so a beta or newer install is never offered an older release.

---

## v4.6.3 — September 2026

Emergency Backup timing on three-tier rate plans. Engine and rate-schedule changes only (`adaptive_engine.py`, `rate_schedule.py`); no new settings.

### EB charges in the cheap window, not the partial-peak shoulder
- **Root cause:** EB's last-responsible-moment timing counted down to **peak start**. On plans with a partial-peak shoulder before peak (e.g. PG&E EV2-A, partial-peak 3–4pm, peak 4–9pm) that let EB defer into the shoulder, and at 3pm the cheapest rate "remaining before peak" was the partial-peak rate itself, so EB fired there. Most EB episodes since late August started at 3pm at the partial-peak rate.
- **Fix:** EB now counts down to the close of the cheap window: the start of the contiguous partial-peak/peak run leading into peak, taken from each install's own rate schedule (`charge_deadline_before_peak()`). On two-tier plans that is the peak start, so behavior is unchanged. If the cheap window is missed, EB can still close a real gap before peak (partial-peak is cheaper than peak); those decisions are logged as "Cheap window missed".

### EB charge-time estimate uses the observed EB rate
- Charge time came from the learned grid-charge curve, which mixes in TOU drift and partial grid charging (≈8.6 kW on a 3-aPower system that actually charges at ≈15.9 kW in EB). It now uses the median of this install's own EB charging readings over the last 60 days, falling back to the learned curve with fewer than 10 samples. Without this, the earlier deadline would have started EB hours sooner than needed.

### Same-cycle mode-switch cooldown
- A switch proposed and superseded within the same 30-minute cycle (e.g. continuous-target's TOU, then the EB decision chained after it) started the 5-minute cooldown, turning the EB decision into a hold and delaying EB a full cycle. The cooldown now measures from the previous cycle's switch.

### EB stops at its target between decision cycles
- EB charges at about 1% SOC per minute, but decisions only run every 30 minutes. EB therefore kept charging past its target until the next cycle. On Sep 26 it reached 75% around 14:55 but kept charging to 79% until the 15:00 cycle, running into partial-peak.
- While the engine holds EB toward a target, `smart_decision.py` now writes that target to `logs/eb_watch.json`. A new scheduler job, **EB Exit Watch**, runs every minute and reads SOC directly over Modbus (register 1037). Once SOC reaches the target, it runs one decision cycle early, and the engine's existing EB-complete logic returns TOU.
- The job does nothing when no watch file exists. It triggers at most one early run per 5 minutes, discards watch files older than 3 hours, and only applies to engine-driven EB. Manual overrides, emergency prep and dynamic-pricing EB are excluded.

### SolarEdge panel collector: cached-layout fallback
- SolarEdge retired the legacy basic-auth `layout/logical` endpoint, which has returned `410 Gone` since about Jul 27. The collector fetches layout first, so it had failed on every run and stopped recording barn per-optimizer data. The Cognito per-optimizer energy API still works.
- When the live layout fetch (or the legacy portal login) fails, the collector now uses the last cached layout, whatever its age. Layout is only inventory: which optimizer sits on which inverter and string. Energy data is matched by optimizer serial. The live fetch is retried at most once a day, with a single warning each time.
- A proper fix will capture SolarEdge's replacement layout endpoint.

### Scheduler stalls and job timeouts
- **Root cause:** several queries filtered with `date(timestamp) = ?`, which prevents SQLite from using the timestamp index, so each call scanned the whole table. The worst was the solar forecast's "yesterday correction" on `enphase_readings` (including per-panel JSON), run every decision cycle: 11 s on an idle system and 1–3 minutes under load. Because the scheduler runs jobs one at a time, these stalls cascaded into 5-minute timeouts across Smart Decision, Dashboard Data and the collectors (≈3–10 a day).
- **Fix:** the forecast query, the chart-data and intelligence-log API endpoints, and the `db.py` date helpers now use index-friendly ranges (`timestamp >= day AND timestamp < day + 1`). Results are identical.
- Dashboard Data (runs every minute) now times out at 90 s instead of 5 minutes, so a hung run can't delay the :00/:30 decision cycle by more than that.
- New `hang_watch.py`: if Smart Decision runs past 4 minutes (Dashboard Data past 75 s), the stack of every thread is appended to `logs/smart_decision_hang.txt` / `logs/dashboard_data_hang.txt`, so any remaining hang can be traced to the exact call. The watch starts on the scripts' first lines, before any heavy import, so it also catches stalls during start-up, for example when the host's CPU is saturated by other containers.
- If other containers on the same host can saturate the CPU, give `franklin-automation` a higher `cpu_shares` in a local `docker-compose.override.yml` (e.g. `cpu_shares: 4096`; Docker's default is 1024) so the scheduler keeps priority.

---

## v4.6.2 — September 2026

An engine behavior release from the NEM 3.0 charging discussion in #21, plus a telemetry fix. All engine changes are in `adaptive_engine.py`; no new settings.

### Emergency Backup commitment (#21)
- **Root cause:** EB was re-evaluated every 30-minute cycle through the same deferral checks that decided whether to start it. Once EB had partly closed the gap, the remaining gap often looked "solar-fillable," so EB dropped back to TOU after a single cycle and rarely reached its ceiling. This looked like a fixed 30-minute EB limit.
- **Fix:** once EB is running it continues until SOC reaches the grid-leg ceiling (`TAPER_CEILING_PCT`) or peak begins. On completion the engine returns **TOU** (holding the charge for peak) instead of self-consumption. EB's commitment also takes precedence over the pre-peak partial-peak check and continuous-target logic, which could previously abandon a charge mid-way.

### `SAFETY_MARGIN_HOURS` now works (#21)
- The v4 engine hardcoded a 0.5 h margin in both gap paths and never read the setting. It is now passed through from config. It acts as extra lead time: a larger value starts EB earlier before peak. Default standardized to 0.5 across `config.py`, the v4.6 config migration, docs and `.env.example` (config.py had drifted to 0.75), so behavior is unchanged unless you set it.

### Taper ceiling applies to export systems (#21)
- `TAPER_CEILING_PCT` previously bounded grid charging only when `SOLAR_EXPORT=false`. Export systems grid-charged to the full target, then exported the solar that grid energy displaced — a loss at NEM 3 export values. The ceiling now bounds the grid leg for every install; solar still fills above it.

### Pre-peak discharge no longer spends EB-charged energy
- The partial-peak pre-peak check (P4) could discharge minutes after EB finished, because its sufficiency test counted energy EB had just bought. On three-tier plans (e.g. PG&E EV2-A) this meant buying at the partial-peak rate and discharging at the same rate. Energy added in EB today is now earmarked for peak and excluded from the pre-peak surplus.

### Continuous-target fixes (#21)
- "SC committed, SOC rising — hold SC" compared SOC against the lowest reading in the last 90 minutes, so an EB-driven climb counted as SC holding its own and the engine stayed in SC while draining. The check now uses only the trailing run of self-consumption readings (at least 15 minutes).
- At target near peak, the engine rode self-consumption into peak even when solar was below load, spending stored energy at a pre-peak rate. It now does so only when solar covers the load; otherwise TOU holds the charge until peak.

### Mode switches no longer reset your aGate reserve (#21)
- **Root cause:** the cloud mode-switch call always carries a reserve SOC, which the aGate adopts for that mode. Engine switches sent `RESERVE_SOC_HOME` / `RESERVE_SOC_BACKUP` (undocumented settings, defaults 20% / 100%), and the override path did the same — so every switch silently reset any reserve the user had set in the Franklin app back to 20%. Same class of bug the Home Assistant integration fixed in richo/homeassistant-franklinwh#82.
- **Fix:** before switching, the engine reads the aGate's current reserve for the target mode (`selfMinSoc` / `touMinSoc` / `backupMaxSoc`) and sends it back unchanged. An explicit `RESERVE_SOC_HOME` / `RESERVE_SOC_BACKUP` in `.env` still takes precedence for anyone who wants the engine to enforce a value; the old defaults apply only if the device read fails. Each switch logs which source the reserve came from.
- Engine math also used `BACKUP_RESERVE_PCT` from config as the discharge floor, stopping discharge at 20% even when the aGate allowed lower. It now uses the aGate's live SC reserve from recent readings (`active_reserve_pct`, falling back to the legacy `self_reserve_pct`), with config as the last resort.

### Telemetry install UUID persistence
- **Root cause:** the install UUID was persisted only through the dashboard consent modal. Installs that enabled telemetry with `TELEMETRY_ENABLED=true` in `.env` never wrote it, so every daily report arrived with a new UUID (one site accumulated 100+). A new UUID is now written to `data/install_uuid` on first use; an existing dashboard-consent UUID still takes precedence.

### Other
- Log lines that appended the uncapped plan target after a capped ceiling ("charge to 75% ... Grid charge ... to 89%", or "charge to 99% (not 99%)") now state one consistent target.
- Docker healthcheck replaced: `pgrep` is not present in `python:3.11-slim`, so the container always reported unhealthy. It now probes the scheduler API (`/api/version`).
- `modbus_discovery.py`: register 15507 map corrected to the hardware-verified 1-indexed values (1=Backup, 2=Self-Consumption, 3=TOU, 4=Manual). Only affected the discovery CLI output and one log line — the collector and mode verification already used the correct map.
- Removed dead constant `EB_DEFERRAL_MIN_BUFFER_HOURS`.

### Credits
Issues reported and analyzed in detail by **miztahsparklez** on #21.

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
