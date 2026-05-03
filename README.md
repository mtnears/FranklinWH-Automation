# FranklinWH Battery Automation

**Intelligent solar-first battery automation for FranklinWH batteries**

Adaptive charging system that optimizes for Time-of-Use (TOU) electricity rates, dynamic hourly pricing, and solar self-consumption. The v4 engine continuously evaluates the optimal battery mode every cycle using forecast-aware logic, real-time data, and rate schedule awareness.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Sponsors](https://img.shields.io/badge/Sponsor-❤-ea4aaa)](https://github.com/sponsors/mtnears)

---

## What's New in v4.3

### Cloud-Only Data Persistence Fix
A long-standing bug affecting every cloud-only user: `system_readings` rows from the cloud collector were inserting with NULL for the primary fields (SOC, solar, grid, battery, load, mode, grid_status). This silently broke load profile learning on cloud-only systems and prevented SOC-target manual overrides from ever exiting. v4.3 populates these fields correctly while preserving Modbus as the source of truth on hybrid systems via `COALESCE` semantics in the UPDATE branch.

### Manual Override UI Redesign
The override modal replaces six time-based chips with four outcome-oriented options:
- **Until SOC reached** — charge up or drain down to a target percentage
- **For duration** — hours + minutes inputs, custom durations up to 24 hours, last entry remembered
- **Until specific time** — HH:MM picker, rolls to tomorrow if past, today/tomorrow indicator
- **Until canceled** — no automatic expiry

The banner now shows "until 18:30 (2h 15m remaining)" for time-based overrides.

### Override Resilience
`_read_latest_soc()` (used by override SOC-target exit) now looks back 30 minutes for non-NULL SOC and falls back to a direct Franklin cloud API call (30-second cache) if the database has no recent SOC. Defends override exit against future regressions and any data collection gap.

### Other Fixes
- Modbus collection job guarded by `MODBUS_ENABLED` — no more timeout-error noise in `scheduler_log` on cloud-only deployments
- Dashboard config display now correctly reads `BACKUP_RESERVE_PCT` (was reading a non-existent variable and always showing 20%)
- `solar_forecast.py` — house array tilt and azimuth corrected; calibration validated at R²=0.835

For v4.2 (Data Export tab, multi-window peaks, seasonal rate windows) and v4.1.1, see [CHANGELOG.md](CHANGELOG.md).

---

## What's New in v4.1

### SQLite Database (Breaking Change)
All data collection has migrated from CSV files to a SQLite database (`data/franklin.db`). This is the largest structural change since the project began.

- `db.py` — new unified database layer; all 17 tables initialized on first run
- All collectors rewritten to write directly to SQLite: `collect_franklin_cloud.py`, `collect_modbus.py`, `collect_solar_enphase.py`, `collect_weather_db.py`, `collect_pv_output.py`, `collect_device_inventory.py`
- `rollup_daily_energy.py` — new daily energy rollup replacing CSV-based summaries
- Several old scripts removed: `collect_enphase.py`, `collect_pvoutput.py`, `collect_solaredge.py`, `collect_weather.py`, `capture_grid_status.py`, `daily_status_report.py`, `generate_weekly_charts.py`
- The database file is created automatically on first container startup — no migration needed for new installs

### Adaptive Engine Hardening
- **Post-peak solar discharge** — after peak, engine burns free solar stored in the battery via Self-Consumption instead of defaulting back to TOU. Computes net solar excess, sets a target SOC drain point, returns to TOU once reached
- **Taper ceiling** (`TAPER_CEILING_PCT`) — caps grid charging ceiling for non-export systems to prevent curtailment. Tunable via `.env`, default 85% (start at 95 and lower by 5 per sunny day until curtailment clears)
- **Pre-peak gate** — within 30 min of peak, engine holds current mode rather than starting a new EB burst if not already charging. Prevents unnecessary late charging cycles
- **Anchor drift fix** — `_get_soc_at_peak_end()` now pins to the first reading at-or-after peak end instead of `rows[-1]`, which drifted as float arithmetic shifted window edges
- **Peak discharge fallback** — new `_compute_peak_discharge_kwh()` queries SOC at peak start and end from `system_readings` when `daily_savings` hasn't run yet. Fixes post-peak target SOC being too aggressive early in the evening
- **Modbus-first mode verification** — routine cloud API mode checks replaced with Modbus register 15507 reads. Cloud API reserved for actual mode switches only

### System Profile Overhaul
- `system_profile.py` now reads from SQLite (`scan_db()`) instead of CSV, with CSV fallback
- Solar interval calculation uses actual time intervals between readings instead of hardcoded 15-min CSV assumption (1-min Modbus rows were inflating solar totals by ~15×)
- Capacity bug fixed — `BATTERY_CAPACITY_KWH` env var was being mapped to per-battery capacity, doubling the total
- Weekly rebuild job added to scheduler (Sunday 3 AM), runs before first engine cycle on restart
- Profile now shows 16 grid charge curve buckets with real taper data (80–85%: 12.2 kW, 85–90%: 7.7 kW, 90–95%: 4.9 kW, 95–100%: 4.7 kW)

### Dashboard — Fire HD 10 Optimized
- Layout validated and optimized for Fire HD 10 tablet (1507×943 CSS pixels) in Fully Kiosk Browser
- **Plotly.js analytics tab** replaces static weekly PNG charts — interactive charts with zoom, pan, hover tooltips, and touch support. Date range selection, carousel navigation
- All chart data now sourced from SQLite via `generate_dashboard_data.py`
- Static PNG generation removed
- Dynamic version display and "Update Available" badge via GitHub releases API

### Version Management
- Single `VERSION` file in repo root; read by `config.py`, `system_profile.py`, `scheduler.py`
- `ENGINE_VERSION` env var deprecated — no longer needed
- Dashboard About card and `/api/version` endpoint serve version dynamically

### Other Changes
- `collect_device_inventory.py` — tracks serial numbers and firmware versions across Enphase, SolarEdge, and Franklin devices; only writes on change. Gateway record enriched with hardware revision and protocol version
- `collect_solaredge_panels.py` — panel-level optimizer data for barn array health monitoring
- Telemetry v2 — expanded schema with 13 new config flags, 10 health signal queries, engine version reporting
- `rate_schedule.py` — rate schedule management with JSON config
- `diagnostic_bundle.py` — sanitized diagnostic bundle for issue reporting

---

## Upgrading — Fresh Install Required

This update touches nearly every script, replaces the entire data storage layer, and removes several files. There is no supported upgrade path from v3.5 or v4.0.

**Steps for existing users:**

```bash
# 1. Back up your .env and any data you want to keep
cp .env .env.backup

# 2. Clone fresh into a new directory
git clone https://github.com/mtnears/FranklinWH-Automation.git FranklinWH-v41
cd FranklinWH-v41

# 3. Copy your .env settings — review .env.example first, new vars have been added
cp ../<old-dir>/.env .env
nano .env   # Review and add any new required settings

# 4. Stop the old container
cd ../<old-dir> && docker compose down

# 5. Build and start fresh
cd ../FranklinWH-v41
docker compose build --no-cache
docker compose up -d

# 6. Verify startup
docker logs -f franklin-automation
```

Your historical data from CSV logs will not be migrated to the new SQLite database. The system starts collecting fresh from day one. If you need historical data preserved, open a [GitHub Issue](https://github.com/mtnears/FranklinWH-Automation/issues) before upgrading.

---

## Key Features

- **Three-Mode Strategy** — TOU (solar → battery), Self-Consumption (battery → home during peak), Emergency Backup (grid gap-fill only). Maximizes solar utilization while minimizing grid charging costs
- **Adaptive Decision Engine** — 8-phase priority system (P1-P8) continuously asks "what is the optimal mode right now?" instead of following rigid time-based rules
- **Forecast-Aware Charging** — Calculates dynamic charging gap based on SOC, expected solar, and time to peak. Limits morning grid charging on high-solar days to leave headroom for free solar. Defers grid charging when solar is actively producing and can fill the gap before peak
- **Curtailment Protection** — Detects when battery is full during solar production and switches modes to prevent wasting free energy
- **Post-Peak Solar Discharge** — Burns free solar stored in the battery after peak instead of importing from the grid overnight
- **SQLite Data Layer** — All readings, decisions, weather, and solar data stored in a local SQLite database. Fast queries, no CSV parsing, dashboard analytics from real data
- **Hybrid Data Collection** — Modbus TCP for fast local monitoring (26ms) with Franklin cloud API for mode switching. Falls back gracefully if Modbus isn't available
- **Rate Schedule Flexibility** — Supports PG&E E-TOU-D, SMUD TOD, ComEd dynamic pricing, and custom schedules with multiple peak windows
- **Peak Safety Net** — Hardware mode verification during peak hours ensures the battery is never charging from the grid at peak rates, even if a mode switch fails
- **Per-Battery Monitoring** — Individual SOC tracking for multi-battery systems
- **Web Dashboard** — Real-time energy flow, Plotly.js interactive analytics, system health monitoring, one-click diagnostic reporting. Optimized for Fire HD 10 tablet kiosk display
- **Manual Override System** — Self-consumption and emergency backup buttons with auto-expiring timers
- **Anonymous Telemetry** — Opt-in usage stats to help guide development
- **Docker Deployment** — Single command startup with built-in scheduler and dashboard

---

## How It Works

The v4 adaptive engine runs every cycle and evaluates an 8-phase priority stack:

```
P1  Emergency override (manual override active, grid disconnected)
P2  Grid disconnect protection (skip mode switches during outages)
P3  Peak imminent — ensure target SOC is met
P4  Peak active — switch to Self-Consumption, battery powers home
P5  Curtailment protection — battery full + solar producing = don't waste it
P6  Forecast-aware gap analysis — calculate if solar can fill the gap before peak
P7  Pre-peak charging — Emergency Backup burst only if solar can't cover the gap (defers if solar active)
P8  Default — TOU mode, solar charges battery while grid covers home
```

Each decision is logged with its priority level: `[v4 P7] Charging gap: 4.2 kWh, grid charging needed`

### Three-Mode Strategy

The v4 engine uses three battery modes to optimize across all conditions:

| Mode | When | What Happens |
|------|------|-------------|
| **TOU** | Default (overnight, daytime, waiting for solar) | Solar → battery, grid → home. Battery holds charge overnight instead of draining. |
| **Self-Consumption** | Peak hours + post-peak solar burn | Battery discharges to power home, avoids expensive grid rates. After peak, burns net solar surplus before returning to TOU. |
| **Emergency Backup** | Short gap-fill bursts only | Grid charges battery at max rate. Used only when forecast shows solar won't meet peak target. |

A typical day: **TOU overnight** (battery holds steady, grid powers home at off-peak rates) → **TOU daytime** (solar fills battery, grid covers house loads) → **brief Emergency Backup** if needed (grid tops off what solar can't cover) → **Self-Consumption at peak** (battery powers home) → **post-peak Self-Consumption** (burns net solar excess) → **back to TOU**.

### Required: TOU Tariff Configuration

The three-mode strategy requires a TOU tariff configured in the FranklinWH app with a specific sub-mode. **This is required even if you don't have solar.**

1. Open the FranklinWH app → **Settings → Tariff Settings**
2. If no tariff exists, create one. Set a single schedule: **12:00 AM to 12:00 AM, every day, every month**
3. Set the mode for every time period to **"aPower charges from solar"**
4. If you already have a tariff, edit each existing time period and change them all to **"aPower charges from solar"**

This tells the Franklin hardware to route solar production to the battery while the grid handles your home loads. The automation handles all mode switching from there.

> **Note:** In the app's **Settings → Mode** screen, you can also set the backup reserve SOC percentage for TOU and Self-Consumption. This is the minimum battery level the system will maintain. The v4 engine respects whatever you configure here. A typical setting is 20%.

### Mode Switch Verification

Every mode switch command is verified against the actual hardware state via the Franklin cloud API. If the hardware doesn't confirm the change, the system retries up to 3 times with increasing delays. During peak hours and the hour before peak, hardware mode is checked every cycle to catch any desync immediately.

---

## Requirements

- **Docker** on an always-on device (Synology NAS, Raspberry Pi, mini PC, etc.)
- **FranklinWH account credentials** (same as your mobile app login)
- **A configured `.env` file** — see [.env.example](.env.example) for all options

### Modbus TCP (Recommended, Not Required)

Modbus TCP gives you 100x faster local data collection (26ms vs 5,000ms cloud API) and works during Franklin cloud outages. If enabled, v4 uses it automatically for monitoring while the cloud API handles mode switching.

Without Modbus, v4 works fine using the Franklin cloud API for everything. All the same decisions are made; you just don't get the speed benefits.

To enable: contact your installer or Franklin support and request Modbus be enabled for SPAN panel integration. Then add to your `.env`:
```env
MODBUS_ENABLED=true
MODBUS_HOST=192.168.x.x   # Your aGate's IP address
MODBUS_PORT=502
```

See [MODBUS_REGISTER_MAP.md](docs/MODBUS_REGISTER_MAP.md) for the full register reference.

---

## Quick Start (Docker)

```bash
# 1. Clone and configure
git clone https://github.com/mtnears/FranklinWH-Automation.git
cd FranklinWH-Automation
cp .env.example .env
nano .env   # Set your credentials, battery config, TOU schedule

# 2. Build and start
docker compose build --no-cache
docker compose up -d

# 3. Open the dashboard
# http://your-server-ip:8100

# 4. Watch the logs
docker logs -f franklin-automation
```

You should see `FranklinWH Automation Scheduler` in the startup banner and decision lines like:
```
Decision: TIME_OF_USE mode ([v4 P8] No peak approaching — TOU default) via MODBUS+ENPHASE [v4]
```

### Required `.env` Settings

```env
FRANKLIN_USERNAME=your_email
FRANKLIN_PASSWORD=your_password
FRANKLIN_GATEWAY_ID=your_gateway_id
BATTERY_CAPACITY_KWH=13.6        # Your total battery capacity

# TOU schedule (adjust to your utility)
PEAK_START_HOUR=17
PEAK_END_HOUR=20
PEAK_DAYS=weekdays

# v4 engine
ADAPTIVE_ENGINE_ENABLED=true
```

See [.env.example](.env.example) for all options including weather, solar arrays, SolarEdge panel monitoring, dynamic pricing, Modbus, telemetry, and the new `TAPER_CEILING_PCT` tuning variable.

---

## Configuration

All settings live in your `.env` file. No code edits needed.

### Feature Toggles

| Feature | Default | Description |
|---------|---------|-------------|
| `ADAPTIVE_ENGINE_ENABLED` | `true` | v4 adaptive engine (falls back to v3.5 logic if disabled) |
| `SOLAR_ENABLED` | `true` | Solar-first charging logic |
| `TOU_ENABLED` | `true` | Time-of-Use peak protection |
| `MODBUS_ENABLED` | `false` | Fast local data via Modbus TCP |
| `DYNAMIC_PRICING_ENABLED` | `false` | Hourly pricing (ComEd, etc.) |
| `WEATHER_ENABLED` | `false` | Weather data collection |
| `CARE_RATE` | `false` | CARE/FERA discount program |
| `NEM_VERSION` | `nem2` | Net metering version (nem2 or nem3) |
| `SOLAR_EXPORT` | `false` | Export system (NEM2/NEM3 with grid export). Disables post-peak self-consumption discharge — set `true` for full-export setups |

### TOU Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `PEAK_START_HOUR` | `17` | Peak period start (24hr) |
| `PEAK_END_HOUR` | `20` | Peak period end (24hr) |
| `PEAK2_START_HOUR` | — | Optional second peak window |
| `PEAK2_END_HOUR` | — | Optional second peak window |
| `PEAK_DAYS` | `weekdays` | `weekdays`, `weekends`, or `all` |
| `HOME_MODE` | `tou` | Default resting mode |

### Engine Tuning

| Setting | Default | Description |
|---------|---------|-------------|
| `TAPER_CEILING_PCT` | `85` | Grid charging ceiling for non-export systems. Start at 95 and lower by 5 per sunny day until curtailment clears |

See [CONFIGURATION_REFERENCE.md](docs/CONFIGURATION_REFERENCE.md) for complete details.

---

## Dashboard

Real-time monitoring at `http://YOUR-SERVER-IP:8100`:

- **Live Dashboard** — Battery SOC, energy flow, charging status, peak countdown, system health indicators
- **Analytics** — Plotly.js interactive charts with date range selection, zoom, pan, and touch support. Sourced directly from SQLite
- **Script Status** — All scheduled scripts with run status, success/fail counts, error history
- **System Logs** — Intelligence log, scheduler log, monitoring data with auto-refresh
- **Override Controls** — Self-consumption and emergency backup buttons with auto-expiring timers
- **Diagnostic Reporting** — One-click sanitized diagnostic bundle for issue reporting

The dashboard is optimized for a **Fire HD 10 tablet** running Fully Kiosk Browser as a dedicated wall display, and works in any modern browser.

### Override System

Quick-access mode overrides from the dashboard or API. Four outcome-oriented options:

- **Until SOC reached** — charge up or drain down to a target percentage
- **For duration** — custom hours + minutes (up to 24 h)
- **Until specific time** — HH:MM, rolls to tomorrow if past
- **Until canceled** — runs until manually cancelled

```bash
# Emergency backup until SOC reaches 90%
curl -X POST http://your-server:8100/api/override \
  -H "Content-Type: application/json" \
  -d '{"mode": "emergency_backup", "duration": "until_soc", "exit_soc_pct": 90}'

# Self-consumption for 2 hours 30 minutes
curl -X POST http://your-server:8100/api/override \
  -H "Content-Type: application/json" \
  -d '{"mode": "self_consumption", "duration": "custom", "duration_minutes": 150}'

# Emergency backup until 18:30 (rolls to tomorrow if already past)
curl -X POST http://your-server:8100/api/override \
  -H "Content-Type: application/json" \
  -d '{"mode": "emergency_backup", "duration": "until_time", "until_time": "18:30"}'

# Self-consumption until manually cancelled
curl -X POST http://your-server:8100/api/override \
  -H "Content-Type: application/json" \
  -d '{"mode": "self_consumption", "duration": "until_cancel"}'

# Cancel active override (engine resumes)
curl -X DELETE http://your-server:8100/api/override
```

---

## Anonymous Telemetry (Opt-In)

On first dashboard load, a one-time popup asks if you'd like to opt in. No `.env` changes required.

**Collected:** system size (battery kWh, panel count), engine version, config flags, aggregate performance metrics, country (you select).

**NOT collected:** IP addresses, credentials, gateway IDs, serial numbers, exact location, raw energy data, or anything personally identifiable.

- Decline the popup and no data is ever sent
- Disable anytime: `TELEMETRY_ENABLED=false` in `.env`
- Public collection repo: [mtnears/franklin-telemetry](https://github.com/mtnears/franklin-telemetry)

---

## Results

### Tested Configuration
- **Battery:** FranklinWH aPower2 (2× FHP, 27.2 kWh total)
- **Solar:** 28.26 kW capacity (dual-meter, 16-panel Enphase house array + 60-panel SolarEdge barn array)
- **Utility:** PG&E E-TOU-D with CARE discount, NEM2
- **Location:** Georgetown, CA

### Performance
- **Peak Protection:** 95%+ success rate
- **API Reliability:** 99.5% uptime
- **Projected Annual Savings:** 58-65% reduction in True-Up costs
- **Data Collection:** 26ms local (Modbus) vs 5,000ms cloud API

---

## Architecture

```
Modbus TCP (local, 26-50ms)           Cloud API (remote, 2-7s)
├── SOC monitoring                     ├── Mode switching (with verification)
├── Grid power tracking                ├── Per-battery SOC
├── Grid disconnect detection          ├── Mode verification (tiered schedule)
├── Temperature monitoring             └── Reserve SOC changes
├── Voltage / frequency
└── Real-time dashboard updates

Enphase Local API (house array)        SolarEdge Cloud API (barn array)
└── Solar production + per-panel data  └── Per-optimizer panel health
```

### Core Scripts

| Script | Purpose |
|--------|---------|
| `smart_decision.py` | Main decision engine — v4 adaptive with v3.5 fallback |
| `adaptive_engine.py` | v4 priority-based decision logic (P1-P8) |
| `solar_forecast.py` | Solar production forecasting and morning gap calculation |
| `db.py` | SQLite database layer — all tables, queries, and schema init |
| `collect_franklin_cloud.py` | Franklin cloud API data collection |
| `collect_modbus.py` | Modbus TCP local data collection |
| `collect_solar_enphase.py` | Enphase local API — house array production + per-panel data |
| `collect_solaredge_panels.py` | SolarEdge cloud API — barn array optimizer health |
| `collect_weather_db.py` | Weather observation collection to SQLite |
| `collect_device_inventory.py` | Hardware inventory — firmware and serial number tracking |
| `rollup_daily_energy.py` | Daily energy summary rollup from SQLite readings |
| `data_sources.py` | Unified Modbus/Cloud/Enphase data with fallback |
| `config.py` | Configuration management from `.env` |
| `scheduler.py` | Task runner, web server, API endpoints |
| `system_profile.py` | Battery charge curve profiling from DB data |
| `telemetry_reporter.py` | Anonymous opt-in telemetry |

---

## Documentation

- [Configuration Reference](docs/CONFIGURATION_REFERENCE.md) — All settings explained
- [Docker Installation](docs/DOCKER_INSTALLATION.md) — Recommended setup path
- [Native Installation](docs/INSTALLATION.md) — For advanced users
- [Modbus Register Map](docs/MODBUS_REGISTER_MAP.md) — Full register reference
- [Web Dashboard](docs/WEB_DASHBOARD.md) — Dashboard setup and features
- [Troubleshooting](docs/TROUBLESHOOTING.md) — Common issues and solutions
- [Changelog](docs/CHANGELOG.md) — Version history
- [Roadmap](ROADMAP.md) — Planned features

---

## Reporting Issues

On the **System Logs** tab of the dashboard, click the **🐛 Report Issue** button. This generates a sanitized diagnostic bundle with credentials automatically stripped.

You can also open a [GitHub Issue](https://github.com/mtnears/FranklinWH-Automation/issues/new) with log output, your rate plan, setup details (battery count, solar size, Modbus enabled), and what happened vs. what you expected.

---

## Contributing

Contributions welcome!

- Report bugs with log excerpts
- Share configurations for different utilities
- Submit PRs for new pricing providers or rate schedules
- Open an [Issue](https://github.com/mtnears/FranklinWH-Automation/issues) for feature ideas

---

## License

MIT License — See [LICENSE](LICENSE)

---

## Credits

Built using the [franklinwh](https://pypi.org/project/franklinwh/) Python library by richö butts.

**Built with ☀️ for the FranklinWH community**
