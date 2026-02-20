# FranklinWH Battery Automation — Roadmap

Planned features and improvements. Items are listed in rough priority order. Contributions, feedback, and discussion are welcome — open an issue or start a discussion.

---

## 🔧 In Progress

### Telemetry Aggregation & Community Dashboard
**Priority: High**

Build `scripts/aggregate.py` for the [franklin-telemetry](https://github.com/mtnears/franklin-telemetry) collection repo — generates `summary.json` and a community dashboard HTML page showing anonymous fleet statistics (system sizes, utility distribution, engine versions, aggregate performance). Expand the collection repo README with full privacy policy, data schema documentation, and opt-out instructions.

### Solar Health Monitor
**Priority: Medium**

Comprehensive panel performance tracking for users whose solar installer is defunct or unavailable. Portal scraping for historical per-panel production data, cross-array anomaly detection (Enphase house array vs SolarEdge barn array), and weather-aware failure alerts. Most of the core logic is built — needs wiring into the scheduler and dashboard.

### Mode Switch Reliability Hardening
**Priority: High**

Following the v4.0.1 fix for mode switch verification, continue improving reliability:
- Track mode switch success rates over time in dashboard
- Alert on repeated switch failures
- Investigate Franklin cloud API rate limiting behavior after container restarts
- Consider exponential backoff for rapid restart recovery

---

## 📋 Planned

### Script Status Dashboard Tab
**Priority: Medium**

Dedicated dashboard tab showing all scheduled scripts, run frequency, last execution status, success/fail counts since container start, and clickable error history. System health dot on the Live Dashboard links to this tab and turns red on active failures.

### Adaptive Post-Peak Optimization
**Priority: Medium**

Use solar generation data to determine how long to stay off-grid past peak. High-solar days: stay off-grid to consume stored solar energy (it's free). Low-solar days: return to grid at peak end to preserve SOC for overnight. Dynamic duration based on SOC at peak end and daily solar generation.

Core principle: *use what you generated that day for free while maintaining a safety reserve, but don't stay off-grid longer than the solar energy justifies.*

### Multi-Gateway Management
**Priority: Low**

Support for users with multiple FranklinWH aGate systems. Coordinated management of multiple battery systems with independent configurations.

### Commercialization Pathway
**Priority: Low**

Explore SaaS model for solar installers. Historical savings modeling tools for installer sales presentations. Cloud-based architecture for multi-customer management. First target customer identified (developer's installer).

---

## 💡 Future Ideas

- **GitHub Sponsors / Buy Me a Coffee** — Funding setup for the project
- **Weekend strategy optimization** — Pure solar self-consumption on non-peak days with dynamic off-grid duration
- **Holiday schedule support** — Rate schedule awareness for utility holidays
- **Modbus write exploration** — Investigate direct Modbus register control for mode switching (currently requires cloud API; DIY orchestration is possible but fragile and unsupported)
- **SolarEdge local API** — Direct panel monitoring if/when local API access becomes available
- **Home Assistant integration** — MQTT discovery for HA dashboards alongside the built-in web dashboard

---

## ✅ Recently Completed

### v4.0.1 — Mode Switch Verification & Peak Safety Net (Feb 2026)
- **Root cause fix**: Mode detection was reading cached local state instead of actual hardware mode, allowing failed switches to go undetected for hours
- **Switch verification**: Every mode switch now retries up to 3x with hardware confirmation via cloud API (5s/8s/12s delays)
- **Peak safety net**: If hardware is in Emergency Backup during peak hours, forces an immediate switch to self-consumption regardless of engine state
- **Tiered cloud verification**: Every-cycle hardware checks during peak and pre-peak hours, 15-minute intervals otherwise (~130 API calls/day vs ~30 before)
- **Cooldown bypass**: Within 30 minutes of peak, mode switch cooldown is bypassed to ensure critical transitions succeed

### v4.0 — Adaptive Forecast Engine (Feb 2026)
- **Adaptive decision engine**: 8-phase priority system (P1-P8) replaces fixed time-based rules with continuous "what is optimal right now?" evaluation
- **Forecast-aware charging**: Calculates morning SOC ceiling based on expected solar to prevent curtailment
- **Curtailment protection**: Detects battery full + solar producing and switches modes to avoid wasting free energy
- **Rate schedule flexibility**: Supports PG&E E-TOU-D, SMUD TOD, ComEd dynamic pricing, and custom schedules
- **Anonymous telemetry**: Opt-in system with dashboard consent flow, public collection repo for transparency
- **Diagnostic reporting**: One-click sanitized diagnostic bundle from dashboard
- **`.env.example` overhaul**: New v4 sections for adaptive engine, telemetry, NEM version, CARE rate, solar array capacities

### v3.5.1 — Script Status Dashboard & Daily Savings Fix (Feb 2026)
- Script Status dashboard tab with real-time health monitoring of all scheduled scripts
- System Health indicator on Live Dashboard
- Daily Savings calculation fix (argument handling, schedule timing, CSV format evolution)
- Improved error logging with stdout capture on script failures

### v3.5.0 — Modbus TCP + Enphase Local Integration (Feb 2026)
- Local-first data collection: SOC and grid power via Modbus TCP (26ms, was 5000ms via cloud API)
- Enphase local solar production reads
- SOC Trend Tracker for deriving solar-to-battery charging rate
- Mode State Tracker with periodic cloud verification
- Hybrid architecture: Modbus for monitoring, cloud API for mode switching
- Grid disconnect detection via Modbus registers with mode switch guard

### v3.4.0 — Clock-Aligned Scheduling & Solar Override (Jan 2026)
- Clock-aligned 30-minute scheduling at :00 and :30
- Solar charging override for grid-charge deadlines
- Stale API value correction during discharge
- Manual override API with auto-expiring timers
- PVOutput configuration integration
