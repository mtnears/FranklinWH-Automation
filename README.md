# FranklinWH Battery Automation

**Intelligent solar-first battery automation for Franklin WH batteries**

Fully automated charging system that optimizes for Time-of-Use (TOU) electricity rates, dynamic hourly pricing, and solar self-consumption. Makes smart decisions every 15 minutes with comprehensive monitoring.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

---

## Key Features

- **Smart 15-Minute Decisions** - Intelligent automation runs every 15 minutes
- **Peak State Tracking** - Prevents mode changes during expensive peak periods
- **Solar-First Intelligence** - Waits for solar production before grid charging
- **Dynamic Pricing Support** - Optional ComEd hourly pricing integration
- **Configurable Everything** - All settings via `.env` file, no code edits needed
- **Web Dashboard** - Real-time monitoring with energy flow visualization
- **Weekly Reports** - Performance charts showing 7-day automation effectiveness
- **Robust API Handling** - 5-attempt retry logic for Franklin Cloud API

---

## What's New in v3.0

### Configuration-Driven Architecture
All features now controlled via environment variables:
```bash
# Enable/disable features as needed
SOLAR_ENABLED=true
TOU_ENABLED=true
DYNAMIC_PRICING_ENABLED=false
WEATHER_ENABLED=false
```

### Dynamic Pricing (ComEd)
For ComEd customers with hourly pricing:
- Automatically fetches real-time electricity prices
- Charges battery when prices are low
- Skips charging when prices spike
- Integrates with solar forecasting

### Web Dashboard
Real-time monitoring interface:
- Battery status and SOC
- Energy flow visualization
- Savings tracking
- Weekly performance charts

See [WEB_DASHBOARD.md](docs/WEB_DASHBOARD.md) for setup instructions.

---

## Quick Start

### 1. Clone and Setup
```bash
git clone https://github.com/mtnears/FranklinWH-Automation.git
cd FranklinWH-Automation

python3 -m venv venv311
source venv311/bin/activate
pip install -r requirements.txt
```

### 2. Configure
```bash
cp .env.example .env
nano .env  # Edit with your settings
```

**Required settings:**
```bash
FRANKLIN_USERNAME=your_email@example.com
FRANKLIN_PASSWORD=your_password
FRANKLIN_GATEWAY_ID=your_gateway_id
BATTERY_CAPACITY_KWH=30
```

### 3. Test
```bash
python scripts/smart_decision.py
```

### 4. Schedule
Set up to run every 15 minutes via cron or Task Scheduler.

---

## Deployment Options

| Method | Best For | Guide |
|--------|----------|-------|
| **Native** | Single system, Synology NAS | [INSTALLATION.md](docs/INSTALLATION.md) |
| **Docker** | Portability, multiple systems | [DOCKER_INSTALLATION.md](docs/DOCKER_INSTALLATION.md) |

---

## Configuration Reference

### Feature Toggles

| Feature | Default | Description |
|---------|---------|-------------|
| `SOLAR_ENABLED` | `true` | Solar-first charging logic |
| `TOU_ENABLED` | `true` | Time-of-Use peak protection |
| `DYNAMIC_PRICING_ENABLED` | `false` | Hourly pricing (ComEd) |
| `WEATHER_ENABLED` | `false` | Weather-informed decisions |
| `PVOUTPUT_ENABLED` | `false` | PVOutput solar tracking |

### TOU Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `PEAK_START_HOUR` | `17` | Peak period start (5 PM) |
| `PEAK_END_HOUR` | `20` | Peak period end (8 PM) |
| `PEAK_DAYS` | `weekdays` | Which days have peak pricing |

### Dynamic Pricing

| Setting | Default | Description |
|---------|---------|-------------|
| `PRICING_PROVIDER` | `comed` | Pricing API provider |
| `PRICE_THRESHOLD_CENTS` | `4.0` | Charge when below this price |
| `PRICE_CEILING_CENTS` | `10.0` | Never charge above this price |

See [.env.example](.env.example) for all options.

---

## System Architecture

```
Every 15 minutes:
├─ Get battery stats (with retry logic)
├─ Check enabled features
├─ Layer 1: Peak Protection (if TOU_ENABLED)
│   └─ NO ACTION during peak period
├─ Layer 2: Solar Assessment (if SOLAR_ENABLED)
│   └─ Use solar when available
├─ Layer 3: Dynamic Pricing (if DYNAMIC_PRICING_ENABLED)
│   └─ Charge when price is cheap
├─ Layer 4: Time-Based Logic
│   └─ Ensure ready for peak
└─ Execute mode switch if needed
```

### Core Scripts

| Script | Purpose |
|--------|---------|
| `smart_decision.py` | Main decision engine |
| `config.py` | Configuration management |
| `pricing.py` | Dynamic pricing integration |
| `generate_dashboard_data.py` | Dashboard data generator |
| `generate_weekly_charts.py` | Performance visualization |
| `daily_status_report.py` | Daily summary reports |

---

## Web Dashboard

Real-time monitoring of your system:

![Dashboard Preview](docs/images/dashboard-preview.png)

**Features:**
- Battery SOC and charging status
- Energy flow visualization (Franklin-style)
- Savings tracking
- Weekly performance charts

**Setup:** See [WEB_DASHBOARD.md](docs/WEB_DASHBOARD.md)

---

## Results

### Tested Configuration
- **Battery:** Franklin WH aPower2 (30 kWh)
- **Solar:** 28.26 kW total capacity
- **Utility:** PG&E E-TOU-D with CARE
- **Location:** Georgetown, CA

### Performance
- **Peak Protection:** >95% success rate
- **API Reliability:** >99% uptime
- **Estimated Savings:** ~$1,050/year

---

## Documentation

- [Installation Guide](docs/INSTALLATION.md)
- [Docker Setup](docs/DOCKER_INSTALLATION.md)
- [Web Dashboard](docs/WEB_DASHBOARD.md)
- [Configuration Reference](docs/CONFIGURATION.md)
- [Upgrade to v3.0](docs/UPGRADE_v3.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)

---

## Contributing

Contributions welcome! See [CONTRIBUTING.md](CONTRIBUTING.md).

- Report bugs with log excerpts
- Share configurations for different utilities
- Submit PRs for new pricing providers

---

## License

MIT License - See [LICENSE](LICENSE)

---

## Credits

Built using the [franklinwh](https://pypi.org/project/franklinwh/) Python library.

**Built with ☀️ for the Franklin WH community**
