# v4-forecast-engine — Beta Branch

> **⚠️ BETA SOFTWARE — ACTIVELY IN DEVELOPMENT**  
> This branch is being tested by the developer and early community members. It works, but expect rough edges.  
> Your battery system will not be harmed — worst case is a suboptimal charging decision.  
> **If anything seems wrong, you can roll back to main in under 60 seconds (see below).**

---

## What's New in v4

The v4 engine replaces the fixed time-based decision logic with an adaptive forecast-aware system. Instead of rigid rules about when to charge, it continuously evaluates "what is the optimal mode right now?" every cycle — calculating a dynamic charging gap based on current SOC, expected solar production, and time until peak.

**Key changes from v3.5.x:**
- New adaptive decision engine with 8 priority phases (P1-P8)
- Forecast-aware charging — calculates morning SOC ceiling based on expected solar to prevent curtailment
- Curtailment protection — leaves battery headroom so free solar energy isn't wasted
- Rate schedule flexibility — supports PG&E E-TOU-D, SMUD TOD, ComEd dynamic pricing, and others
- Optional anonymous telemetry — opt-in usage stats to help guide development
- One-click diagnostic reporting from the dashboard

**What hasn't changed:**
- Same `.env` configuration approach
- Same Docker deployment
- Same Franklin cloud API + optional Modbus data sources
- Same override system (self-consumption and emergency backup with auto-expiring timers)
- Same web dashboard (with new features added)

---

## Requirements

- **Docker** on an always-on device (Synology NAS, Raspberry Pi, mini PC, etc.)
- **FranklinWH account credentials** (same as your mobile app login)
- **A configured `.env` file** — see `.env.example` for all options

### Modbus TCP (Recommended, Not Required)

Modbus TCP gives you 100x faster local data collection (26ms vs 5,000ms cloud API) and works during Franklin cloud outages. If you have it enabled, v4 uses it automatically for monitoring while the cloud API handles mode switching.

**If you don't have Modbus**, v4 works fine — it uses the Franklin cloud API for everything, same as v3.5.x. All the same decisions are made; you just don't get the speed benefits.

To enable Modbus: contact your installer or Franklin support and request it be enabled for SPAN panel integration. Then add to your `.env`:
```env
MODBUS_ENABLED=true
MODBUS_HOST=192.168.x.x   # Your aGate's IP address
MODBUS_PORT=502
```

---

## Quick Start (New Install)

```bash
# 1. Clone the repo and switch to the beta branch
git clone https://github.com/mtnears/FranklinWH-Automation.git franklin-git
cd franklin-git
git checkout v4-forecast-engine

# 2. Copy and configure your .env
cp .env.example .env
nano .env   # Fill in your credentials, battery config, TOU schedule

# 3. Build and start
docker-compose up -d --build

# 4. Open the dashboard
# http://your-server-ip:8100

# 5. Watch the logs
docker logs -f franklin-automation
```

You should see `FranklinWH Automation Scheduler v4.0` in the startup banner.

---

## Upgrading from Main Branch

```bash
# 1. Go to your franklin-git directory
cd /path/to/your/franklin-git

# 2. Save any local changes
git stash

# 3. Fetch and switch to the beta branch
git fetch origin
git checkout v4-forecast-engine

# 4. Rebuild (no-cache recommended for major version changes)
docker-compose down
docker-compose build --no-cache
docker-compose up -d

# 5. Watch the logs
docker logs -f franklin-automation
```

### Verify it's working:

Look for decision lines like:
```
Decision: [v4 P8] No peak approaching — self-consumption
Decision: [v4 P5] Charging gap: 12.3 kWh, grid charging needed
Decision: [v4 P3] Peak imminent — ensuring target SOC
```

The `[v4 Px]` prefix tells you which priority phase made the decision.

---

## How to Roll Back to Main (60 seconds)

If anything seems off:

```bash
docker-compose down
git checkout main
docker-compose up -d --build
```

No data is lost, no settings are changed. Your `.env` is compatible with both branches.

---

## Anonymous Telemetry (Opt-In)

v4 includes optional anonymous telemetry to help guide development. On first dashboard load, a one-time popup asks if you'd like to opt in. No `.env` changes required.

**What is collected:** system size (battery kWh, panel count), engine version, config flags, aggregate performance metrics, and country (you select from a dropdown).

**What is NOT collected:** IP addresses, credentials, gateway IDs, serial numbers, exact location, raw energy usage data, or anything personally identifiable.

- Decline the popup and no data is ever sent — you won't be asked again
- Disable anytime by adding `TELEMETRY_ENABLED=false` to your `.env`
- The collection repo is public for full transparency: [mtnears/franklin-telemetry](https://github.com/mtnears/franklin-telemetry)

---

## Dashboard Override System

The dashboard includes quick-access buttons for Self-Consumption and Emergency Backup modes with auto-expiring timers — click the mode, set a duration, and the engine automatically resumes normal operation when the timer expires.

You can also use the API directly:

```bash
# Emergency backup mode for 4 hours
curl -X POST http://your-server:8100/api/override \
  -H "Content-Type: application/json" \
  -d '{"mode": "emergency_backup", "duration": "4h"}'

# Check override status
curl http://your-server:8100/api/override

# Cancel override (engine resumes)
curl -X DELETE http://your-server:8100/api/override
```

---

## Reporting Issues

On the **System Logs** tab of the dashboard, click the **🐛 Report Issue** button in the upper right. This generates a sanitized diagnostic bundle with credentials automatically stripped.

You can also:
- Open a [GitHub Issue](https://github.com/mtnears/FranklinWH-Automation/issues/new) directly
- Post in [GitHub Discussions](https://github.com/mtnears/FranklinWH-Automation/discussions)

Please include:
1. Your log output (the decision engine block)
2. Your rate plan (PG&E, SMUD, ComEd, etc.)
3. Your setup (battery count, solar size, Modbus enabled?)
4. What happened vs. what you expected

---

## Known Limitations (Beta)

- Solar forecast integration is in progress — currently uses historical patterns and weather data
- Performance stats in telemetry are still being calibrated
- Rate schedule defaults to PG&E E-TOU-D patterns; other utilities need `.env` adjustments
- Engine version display in telemetry shows "unknown" (cosmetic — will be fixed)

---

## Configuration

Same `.env` file as the main branch. Key settings:

```env
# Required
FRANKLIN_USERNAME=your_email
FRANKLIN_PASSWORD=your_password
FRANKLIN_GATEWAY_ID=your_gateway_id
BATTERY_CAPACITY_KWH=13.6     # Your battery capacity

# TOU schedule (adjust to your utility)
PEAK_START_HOUR=17
PEAK_END_HOUR=20
PEAK_DAYS=weekdays

# Modbus (recommended, not required)
MODBUS_ENABLED=true
MODBUS_HOST=192.168.x.x
MODBUS_PORT=502

# V4 engine (enabled by default on this branch)
ADAPTIVE_ENGINE_ENABLED=true
```

See `.env.example` for all available options including weather, solar arrays, SolarEdge panel monitoring, dynamic pricing, and more.

---

**Questions?** Open a [Discussion](https://github.com/mtnears/FranklinWH-Automation/discussions) on the repo or comment on the beta announcement thread.
