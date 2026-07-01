# FranklinWH Battery Automation

**Intelligent solar-first battery automation for FranklinWH batteries**

Adaptive charging system that optimizes for Time-of-Use (TOU) electricity rates, dynamic hourly pricing, and solar self-consumption. The v4 engine continuously evaluates the optimal battery mode every cycle using forecast-aware logic, real-time data, and rate schedule awareness.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Sponsors](https://img.shields.io/badge/Sponsor-❤-ea4aaa)](https://github.com/sponsors/mtnears)

---

## What's New in v4.4

### Three-Tier Rate Support — Peak / Partial-Peak / Off-Peak (v4.4.0)

First-class support for three-tier rate plans (e.g., PG&E EV2-A) where partial-peak windows surround a sacred peak. A new **Priority 4.5** sits between peak protection (P4) and curtailment protection (P5):

- **Pre-peak partial-peak** (peak still ahead) — preserve battery so it can recharge before sacred peak; Self-Consumption only when SOC is comfortable, fall through to TOU when low
- **Post-peak partial-peak** (peak already done) — free to discharge via Self-Consumption, peak is behind so no SOC floor concern

The branch decision uses a new `expensive_window_remaining_hours()` helper in `rate_schedule.py` that walks forward to find the next off-peak transition, plus `is_partial_peak()` and `is_expensive()` helpers. Two-tier plans continue to work unchanged — P4.5 is a no-op when no `partial_peak` tier is defined.

The reference `rate_schedule.json` shipped with the project is now configured for PG&E EV2-A with CARE. Other plan examples (E-TOU-D, SMUD, SCE TOU-D-PRIME, Pepco R-TOU-P, ComEd) live in `data/rate_schedule.example.json`.

### Per-Season Rate Auto-Switching (v4.4.1)

`rate_schedule.json` gained an optional `seasons` block that overrides `tier_rates` and/or `windows` per calendar month:

```json
"seasons": [
  {"name": "summer", "months": [6, 7, 8, 9],
   "tier_rates": {"peak": 34.976, "partial_peak": 27.794, "off_peak": 14.663}},
  {"name": "winter", "months": [10, 11, 12, 1, 2, 3, 4, 5],
   "tier_rates": {"peak": 26.714, "partial_peak": 25.628, "off_peak": 14.663}}
]
```

Plans where rates differ summer vs winter flip automatically on the seasonal boundary — no manual JSON edits required on June 1 / October 1. Closes a silent gap on three-tier plans: the existing `rate_history` DB-based switching only covered peak/off_peak, so `partial_peak` previously stayed frozen at whatever JSON last said when seasons changed. Backward compatible — configs without a `seasons` block work exactly as before.

Validation warnings at startup catch common misconfig: overlapping months between seasons, missing month coverage, invalid month values, unknown tier names in `tier_rates`.

### Version Banner Reads From VERSION File (v4.4.1)

A new `scripts/version.py` helper reads from the repo-root `VERSION` file. The engine startup banner in `intelligence_log` now reads `FranklinWH Smart Decision Engine v4.4.1 Adaptive` dynamically — no more hardcoded version strings drifting from reality on release.

---

For older releases — v4.3.1 (target-aware SC commit fix), v4.3 (cloud-only persistence), v4.2 (multi-window peaks, Data Export tab), v4.1 (SQLite migration) — see [CHANGELOG.md](CHANGELOG.md).

---

## Upgrading

### To v4.6 (from v4.1+)

v4.6 is an in-place upgrade — no fresh install. After pulling, run the one-time configuration migration, which copies your `.env` and `rate_schedule.json` into the new SQLite config store (neither file is modified):

```bash
git pull
docker compose build --no-cache
docker compose down
docker compose up -d

# Preview the migration first (writes nothing):
docker exec franklin-automation python3 /app/scripts/migrate_v46.py --dry-run --battery-array <your-array-id>

# Then run it:
docker exec franklin-automation python3 /app/scripts/migrate_v46.py --battery-array <your-array-id>
```

`<your-array-id>` is the array that charges your Franklin battery (e.g. `house`), as defined by `SOLAR_ARRAYS` in your `.env`. If you have a single array it's auto-detected and the flag is optional. After migrating, open the **Settings** tab → **Configuration Health** — anything it flags is the validation working as intended.

Your `.env` and `rate_schedule.json` remain authoritative and required — the migration is additive. To change a setting later, edit `.env` (or the JSON), restart, and re-run the migration to refresh the store.

### Fresh Install (from v3.5 / v4.0)

The v4.1 data-layer rewrite touched nearly every script and replaced the entire storage layer. There is no supported in-place upgrade path from v3.5 or v4.0 — a fresh install is required.

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
- **Configuration Store + Settings Page** (v4.6) — A consolidated, read-only view of your entire configuration on the Settings tab, backed by SQLite, with a Configuration Health section that validates your setup and flags conflicts. One canonical rate resolver keeps the engine, savings, and dashboard in agreement on what rates and peak window apply each day
- **Hybrid Data Collection** — Modbus TCP for fast local monitoring (26ms) with Franklin cloud API for mode switching. Falls back gracefully if Modbus isn't available
- **Rate Schedule Flexibility** — Supports two-tier (PG&E E-TOU-D, SCE TOU-D, etc.), three-tier with partial-peak (PG&E EV2-A), dynamic hourly pricing (ComEd), and custom schedules with multiple peak windows and per-season auto-switching for rates and windows
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
P1    Emergency override (manual override active, grid disconnected)
P2    Grid disconnect protection (skip mode switches during outages)
P3    Peak imminent — ensure target SOC is met
P4    Peak active — switch to Self-Consumption, battery powers home
P4.5  Partial-peak active (three-tier plans only) — Self-Consumption when SOC permits pre-peak; free discharge post-peak
P5    Curtailment protection — battery full + solar producing = don't waste it
P6    Forecast-aware gap analysis — calculate if solar can fill the gap before peak
P7    Pre-peak charging — Emergency Backup burst only if solar can't cover the gap (defers if solar active)
P8    Default — TOU mode, solar charges battery while grid covers home
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

All settings live in your `.env` file (and your TOU schedule in `rate_schedule.json`). No code edits needed.

As of **v4.6**, the system also keeps a consolidated, read-only copy of your configuration in its SQLite database, viewable on the **Settings** tab. This is how the system — and you — can see everything in one place, with a Configuration Health section that flags conflicts (mismatched peak windows, seasonal coverage gaps, unreviewed arrays, and more). Important: the store is a *copy*. Your `.env` and `rate_schedule.json` are still the source of truth and are still required — **don't delete or trim them.** To change a setting, edit `.env` (or the JSON), restart, and re-run the migration to refresh the store (see [Upgrading](#upgrading)). Editing configuration directly from the UI, and a guided setup wizard for new installs, are planned on top of this foundation.

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

As of **v4.6**, the engine's peak window comes from your **rate schedule** (`rate_schedule.json`, season-aware), not these `.env` variables. The variables below are kept as a fallback for installs without a resolvable schedule window, and the Settings tab flags a `CONFIG CONFLICT` if they disagree with your schedule. Keep them in sync with your schedule, or let the schedule drive and treat these as a backstop.

| Setting | Default | Description |
|---------|---------|-------------|
| `PEAK_START_HOUR` | `17` | Fallback peak start (24hr) — superseded by the rate schedule window |
| `PEAK_END_HOUR` | `20` | Fallback peak end (24hr) — superseded by the rate schedule window |
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
- **Utility:** PG&E EV2-A with CARE discount, NEM2
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
