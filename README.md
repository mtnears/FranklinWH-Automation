# FranklinWH Battery Automation

**Intelligent solar-first battery automation for FranklinWH batteries**

Adaptive charging system that optimizes for Time-of-Use (TOU) electricity rates, dynamic hourly pricing, and solar self-consumption. The v4 engine continuously evaluates the optimal battery mode every cycle using forecast-aware logic, real-time data, and rate schedule awareness.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Sponsors](https://img.shields.io/badge/Sponsor-❤-ea4aaa)](https://github.com/sponsors/mtnears)

---

## Key Features

- **Three-Mode Strategy** — TOU (solar → battery), Self-Consumption (battery → home during peak), Emergency Backup (grid gap-fill only). Maximizes solar utilization while minimizing grid charging costs
- **Adaptive Decision Engine** — 8-phase priority system (P1-P8) continuously asks "what is the optimal mode right now?" instead of following rigid time-based rules
- **Forecast-Aware Charging** — Calculates dynamic charging gap based on SOC, expected solar, and time to peak. Limits morning grid charging on high-solar days to leave headroom for free solar
- **Curtailment Protection** — Detects when battery is full during solar production and switches modes to prevent wasting free energy
- **Hybrid Data Collection** — Modbus TCP for fast local monitoring (26ms) with Franklin cloud API for mode switching. Falls back gracefully if Modbus isn't available
- **Rate Schedule Flexibility** — Supports PG&E E-TOU-D, SMUD TOD, ComEd dynamic pricing, and custom schedules with multiple peak windows
- **Peak Safety Net** — Hardware mode verification during peak hours ensures the battery is never charging from the grid at peak rates, even if a mode switch fails
- **Per-Battery Monitoring** — Individual SOC tracking for multi-battery systems
- **Web Dashboard** — Real-time energy flow visualization, weekly performance charts, system health monitoring, and one-click diagnostic reporting
- **Manual Override System** — Self-consumption and emergency backup buttons with auto-expiring timers
- **Anonymous Telemetry** — Opt-in usage stats to help guide development ([public collection repo](https://github.com/mtnears/franklin-telemetry))
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
P7  Pre-peak charging — Emergency Backup burst only if solar can't cover the gap
P8  Default — TOU mode, solar charges battery while grid covers home
```

Each decision is logged with its priority level: `[v4 P7] Charging gap: 4.2 kWh, grid charging needed`

### Three-Mode Strategy

The v4 engine uses three battery modes to optimize across all conditions:

| Mode | When | What Happens |
|------|------|-------------|
| **TOU** | Default (overnight, daytime, waiting for solar) | Solar → battery, grid → home. Battery holds charge overnight instead of draining. |
| **Self-Consumption** | Peak hours only | Battery discharges to power home, avoids expensive grid rates. |
| **Emergency Backup** | Short gap-fill bursts only | Grid charges battery at max rate. Used only when forecast shows solar won't meet peak target. |

A typical day: **TOU overnight** (battery holds steady, grid powers home at off-peak rates) → **TOU daytime** (solar fills battery, grid covers house loads) → **brief Emergency Backup** if needed (grid tops off what solar can't cover) → **Self-Consumption at peak** (battery powers home) → **back to TOU after peak**.

### Required: TOU Tariff Configuration

The three-mode strategy requires a TOU tariff configured in the FranklinWH app with a specific sub-mode. **This is required even if you don't have solar.**

1. Open the FranklinWH app → **Settings → Tariff Settings**
2. If no tariff exists, create one. Set a single schedule: **12:00 AM to 12:00 AM, every day, every month**
3. Set the mode for every time period to **"aPower charges from solar"**
4. If you already have a tariff, edit each existing time period and change them all to **"aPower charges from solar"**

This tells the Franklin hardware to route solar production to the battery while the grid handles your home loads. The automation handles all mode switching from there.

> **Note:** In the app's **Settings → Mode** screen, you can also set the backup reserve SOC percentage for TOU and Self-Consumption. This is the minimum battery level the system will maintain. The v4 engine respects whatever you configure here. A typical setting is 20%.

### Startup Grace Period

On container restart, the first decision cycle observes and logs baseline data without switching modes. This prevents aggressive Emergency Backup charging on startup before the engine has context. Normal decisions begin on the second cycle (30 minutes later).

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
docker compose build
docker compose up -d

# 3. Open the dashboard
# http://your-server-ip:8100

# 4. Watch the logs
docker logs -f franklin-automation
```

You should see `FranklinWH Automation Scheduler v4.0` in the startup banner, the three-mode configuration notice, and decision lines like:
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

See [.env.example](.env.example) for all options including weather, solar arrays, SolarEdge panel monitoring, dynamic pricing, Modbus, and telemetry.

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

### TOU Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `PEAK_START_HOUR` | `17` | Peak period start (24hr) |
| `PEAK_END_HOUR` | `20` | Peak period end (24hr) |
| `PEAK2_START_HOUR` | — | Optional second peak window |
| `PEAK2_END_HOUR` | — | Optional second peak window |
| `PEAK_DAYS` | `weekdays` | `weekdays`, `weekends`, or `all` |
| `HOME_MODE` | `tou` | Default resting mode: `tou` (recommended for v4 three-mode strategy) or `self_consumption` |

See [CONFIGURATION_REFERENCE.md](docs/CONFIGURATION_REFERENCE.md) for complete details.

---

## Dashboard

Real-time monitoring at `http://YOUR-SERVER-IP:8100`:

- **Live Dashboard** — Battery SOC, energy flow, charging status, peak countdown, system health indicators
- **Weekly Reports** — 7-day SOC timeline, daily summaries, power flow charts
- **Script Status** — All scheduled scripts with run status, success/fail counts, error history
- **System Logs** — Intelligence log, scheduler log, monitoring data with auto-refresh
- **Override Controls** — Self-consumption and emergency backup buttons with auto-expiring timers
- **Diagnostic Reporting** — One-click sanitized diagnostic bundle for issue reporting

### Override System

Quick-access mode overrides from the dashboard or API:

```bash
# Emergency backup for 4 hours
curl -X POST http://your-server:8100/api/override \
  -H "Content-Type: application/json" \
  -d '{"mode": "emergency_backup", "duration": "4h"}'

# Cancel override (engine resumes)
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
- **Battery:** FranklinWH aPower2 (2× FHP, 30 kWh total)
- **Solar:** 28.26 kW capacity (dual-meter, 16-panel Enphase house + 60-panel SolarEdge barn)
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

Enphase Local API
└── Solar production (house array)
```

### Core Scripts

| Script | Purpose |
|--------|---------|
| `smart_decision.py` | Main decision engine — v4 adaptive with v3.5 fallback |
| `adaptive_engine.py` | v4 priority-based decision logic |
| `data_sources.py` | Unified Modbus/Cloud/Enphase data with fallback |
| `config.py` | Configuration management from `.env` |
| `scheduler.py` | Task runner, web server, API endpoints |
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

You can also open a [GitHub Issue](https://github.com/mtnears/FranklinWH-Automation/issues/new) or post in [GitHub Discussions](https://github.com/mtnears/FranklinWH-Automation/discussions).

Please include: log output, rate plan, setup details (battery count, solar size, Modbus enabled), and what happened vs. what you expected.

---

## Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

- Report bugs with log excerpts
- Share configurations for different utilities
- Submit PRs for new pricing providers or rate schedules
- Open discussions for feature ideas

---

## License

MIT License — See [LICENSE](LICENSE)

---

## Credits

Built using the [franklinwh](https://pypi.org/project/franklinwh/) Python library by richö butts.

**Built with ☀️ for the FranklinWH community**
