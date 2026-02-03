# FranklinWH Battery Automation

**Intelligent solar-first battery automation for Franklin WH batteries**

Fully automated charging system that optimizes for Time-of-Use (TOU) electricity rates, dynamic hourly pricing, and solar self-consumption. Makes smart decisions every 15 minutes with comprehensive monitoring.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![GitHub Sponsors](https://img.shields.io/badge/Sponsor-❤-ea4aaa)](https://github.com/sponsors/mtnears)

---

## Key Features

- **API-Native Mode Management** - Reads and sets battery modes directly through the Franklin WH API
- **Schedule-Aware Timing** - Critical checks pinned to peak boundaries so mode switches are never late
- **Per-Battery Monitoring** - Individual SOC and power tracking for multi-battery systems
- **Peak State Tracking** - Prevents mode changes during expensive peak periods
- **Solar-First Intelligence** - Waits for solar production before grid charging
- **Dynamic Pricing Support** - Optional ComEd hourly pricing with negative price credit capture
- **Configurable Everything** - All settings via `.env` file, no code edits needed
- **Web Dashboard** - Real-time monitoring with energy flow visualization and system info
- **Weekly Reports** - Performance charts showing 7-day automation effectiveness
- **Robust API Handling** - 5-attempt retry logic for Franklin Cloud API
- **Docker Deployment** - Single command startup with built-in scheduler and dashboard

---

## What's New in v3.3.0

### Negative Pricing / Solar Override
- New `SOLAR_OVERRIDE_PRICE_CENTS` setting for dynamic pricing users
- Charges from grid even when solar is producing — captures utility credits during negative pricing
- Overrides solar-first preference AND peak protection when grid price is at or below threshold
- All pricing thresholds now support negative values natively

### Improved Mode Detection
- Mode detection uses gateway `name` field instead of `run_status` for reliable operation across firmware versions
- Switch verification with 5s initial check + 8s retry and 10-minute cooldown

### Enhanced Dashboard Data
- Dashboard data generator calls `_status()` API for enriched system information
- Per-battery SOC, environment data (temperature, signal), energy totals, hardware status
- Config export for Settings tab display
- Gateway ID for multi-gateway support

### Decision Engine Restructure
- New Layer 0: Credit/negative price override (highest priority)
- All price comparisons use `<=` for correct zero and negative handling
- Removed hardcoded 2.0¢ threshold — all thresholds configurable via `.env`

---

## Quick Start (Docker — Recommended)

### 1. Clone and Configure
```bash
git clone https://github.com/mtnears/FranklinWH-Automation.git
cd FranklinWH-Automation
cp .env.example .env
nano .env   # Set your credentials and preferences
```

**Required settings in `.env`:**
```bash
FRANKLIN_USERNAME=your_email@example.com
FRANKLIN_PASSWORD=your_password
FRANKLIN_GATEWAY_ID=your_gateway_id
BATTERY_CAPACITY_KWH=30
CHARGE_RATE_PER_HOUR=32
```

### 2. Build and Start
```bash
mkdir -p logs data web
docker compose build
docker compose up -d
```

### 3. Verify
```bash
docker logs franklin-automation 2>&1 | head -25
```

You should see the v3.3.0 banner with your configured features and scheduled tasks.

### 4. Access Dashboard
Open `http://YOUR-SERVER-IP:8100` in your browser.

See [DOCKER_INSTALLATION.md](docs/DOCKER_INSTALLATION.md) for complete setup details.

---

## How It Works

Every 15 minutes (configurable), plus pinned checks at peak boundaries:

```
┌─ Get battery stats via Franklin API (with retry logic)
├─ Detect current mode from API (name field)
├─ Check enabled features
├─ Layer 0: Price Override (if DYNAMIC_PRICING + SOLAR_OVERRIDE configured)
│   └─ Grid credit/negative pricing overrides everything
├─ Layer 1: Peak Protection (if TOU_ENABLED)
│   └─ NO ACTION during peak period
├─ Layer 2: Solar Assessment (if SOLAR_ENABLED)
│   └─ Use solar when available
├─ Layer 3: Dynamic Pricing (if DYNAMIC_PRICING_ENABLED)
│   └─ Charge when price is cheap
├─ Layer 4: Time-Based Logic
│   └─ Ensure ready for peak
└─ Execute mode switch via API if needed (with verification + cooldown)
```

### Peak Protection

The system guarantees battery readiness for peak hours:
- Calculates time needed to reach target SOC based on your measured charge rate
- Waits for solar as long as there's enough time buffer
- Switches to grid charging only when necessary
- Never changes modes during peak period
- Pre-peak check ensures mode is set before peak starts

---

## Deployment Options

| Method | Best For | Guide |
|--------|----------|-------|
| **Docker** (Recommended) | All users, easiest setup | [DOCKER_INSTALLATION.md](docs/DOCKER_INSTALLATION.md) |
| **Native** | Advanced users, custom setups | [INSTALLATION.md](docs/INSTALLATION.md) |

---

## Configuration

All settings live in your `.env` file. See [.env.example](.env.example) for all options.

### Feature Toggles

| Feature | Default | Description |
|---------|---------|-------------|
| `SOLAR_ENABLED` | `true` | Solar-first charging logic |
| `TOU_ENABLED` | `true` | Time-of-Use peak protection |
| `DYNAMIC_PRICING_ENABLED` | `false` | Hourly pricing (ComEd) |
| `WEATHER_ENABLED` | `false` | Weather data collection |
| `PVOUTPUT_ENABLED` | `false` | PVOutput solar tracking |

### Scheduling

| Setting | Default | Description |
|---------|---------|-------------|
| `CHECK_INTERVAL_MINUTES` | `15` | Decision frequency |
| `PEAK_TRANSITION_BUFFER_MINUTES` | `5` | Minutes before peak to check |
| `HOME_MODE` | `tou` | Normal mode: `tou` or `self_consumption` |

### TOU Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `PEAK_START_HOUR` | `17` | Peak period start (5 PM) |
| `PEAK_END_HOUR` | `20` | Peak period end (8 PM) |
| `PEAK_DAYS` | `weekdays` | Which days have peak pricing |

See [CONFIGURATION_REFERENCE.md](docs/CONFIGURATION_REFERENCE.md) for complete details.

---

## Core Scripts

| Script | Purpose |
|--------|---------|
| `smart_decision.py` | Main decision engine with API-native mode management |
| `config.py` | Configuration management from `.env` |
| `scheduler.py` | Schedule-aware task runner with peak-pinned checks |
| `generate_dashboard_data.py` | Real-time dashboard data |
| `generate_weekly_charts.py` | Performance visualization |
| `calculate_daily_savings.py` | Daily savings tracking |

---

## Web Dashboard

Real-time monitoring at `http://YOUR-SERVER-IP:8100`:

**Live Dashboard** — Battery SOC, energy flow, charging status, savings tracker, peak countdown

**Weekly Reports** — 7-day SOC timeline, daily summaries, power flow charts

**System Logs** — Intelligence log, scheduler log, monitoring data with auto-refresh

See [WEB_DASHBOARD.md](docs/WEB_DASHBOARD.md) for details.

---

## Results

### Tested Configuration
- **Battery:** Franklin WH aPower2 (2× FHP, 30 kWh total)
- **Solar:** 28.26 kW total capacity (dual-meter, house + barn)
- **Utility:** PG&E E-TOU-D with CARE discount
- **Location:** Georgetown, CA

### Performance (v3.3.0)
- **Peak Protection:** 100% success rate
- **API Reliability:** 99.5% uptime
- **Target SOC Achievement:** 91-94% before peak
- **Average Daily Savings:** ~$3.40/day

---

## Documentation

- [Docker Installation](docs/DOCKER_INSTALLATION.md) — Recommended setup path
- [Native Installation](docs/INSTALLATION.md) — For advanced users
- [Configuration Reference](docs/CONFIGURATION_REFERENCE.md) — All settings explained
- [Web Dashboard](docs/WEB_DASHBOARD.md) — Dashboard setup and features
- [Troubleshooting](docs/TROUBLESHOOTING.md) — Common issues and solutions
- [Changelog](docs/CHANGELOG.md) — Version history

---

## Contributing

Contributions welcome!

- Report bugs with log excerpts
- Share configurations for different utilities
- Submit PRs for new pricing providers
- Open discussions for feature ideas

---

## License

MIT License — See [LICENSE](LICENSE)

---

## Credits

Built using the [franklinwh](https://pypi.org/project/franklinwh/) Python library by richö butts.

**Built with ☀️ for the Franklin WH community**
