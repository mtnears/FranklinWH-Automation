# FranklinWH Battery Automation — Roadmap

Planned features and improvements. Items are listed in rough priority order. Contributions, feedback, and discussion are welcome — open an issue or start a discussion.

---

## 🔧 In Progress

### Energy Source Tracking & Post-Peak Optimization
**Priority: High**

Track how much stored battery energy came from solar vs grid on a daily basis to enable smarter post-peak decisions. If the battery holds surplus *solar* energy after peak, stay in Self-Consumption overnight to burn free energy instead of importing from the grid. If stored energy came from grid charging, return to TOU — discharging paid energy just to re-buy it tomorrow gains nothing.

This same data answers a bigger question: **is the solar installation sized correctly for the battery automation strategy?** Seasonal trends will show whether solar consistently fills the battery (high-value, free peak avoidance) or whether the system is primarily doing rate arbitrage (still valuable, but lower savings).

The metric must account for multiple user profiles:
- **Solar + battery:** Track solar vs grid contribution. Best case: solar fills battery, surplus burns overnight free.
- **Battery only, no solar:** Pure rate arbitrage — buy off-peak, discharge during peak. Baseline/floor value, still worth optimizing.
- **Dynamic/demand pricing:** Price signals create opportunities including negative pricing events. Track cost-of-charge vs value-of-discharge per cycle.
- **Solar + battery + NEM export:** Surplus solar earns export credits — overnight self-consumption may not be optimal vs exporting.

Reporting tiers: daily/weekly metrics (solar contribution %, grid charge cost, peak avoidance value), seasonal/true-up trends (system sizing adequacy, cumulative savings breakdown), and opt-in anonymous telemetry aggregates for community benchmarking.

`solar_to_bat_kw` is already logged each cycle. Implementation adds `grid_to_bat_kw` tracking, daily source accumulation, post-peak surplus decision logic, and `GRID_EXPORT_ENABLED` config flag.

### Telemetry Expansion
**Priority: Medium**

Initial telemetry deployment is complete — opt-in anonymous collection with GitHub-based storage and dashboard consent flow. Next steps: expand the telemetry payload to include energy source breakdown (solar vs grid contribution), build `scripts/aggregate.py` for the [franklin-telemetry](https://github.com/mtnears/franklin-telemetry) collection repo to generate `summary.json` and a community dashboard with anonymous fleet statistics (system sizes, utility distribution, engine versions, aggregate performance). Expand the collection repo README with full privacy policy, data schema documentation, and opt-out instructions.

### Solar Health Monitor
**Priority: Medium**

Comprehensive panel performance tracking for users whose solar installer is defunct or unavailable. Portal scraping for historical per-panel production data, cross-array anomaly detection (Enphase house array vs SolarEdge barn array), and weather-aware failure alerts leveraging 13+ years of local weather history. Most of the core logic is built — needs wiring into the scheduler and dashboard.

### Interactive Dynamic Charts
**Priority: Medium**

Replace static weekly PNG chart generation with client-side interactive charts using Plotly.js. Users select date range, charts render dynamically with zoom, pan, hover tooltips, and touch support for tablet. Static PNGs remain for archive and email use cases.

Phased approach:
1. Add SQLite logging alongside CSV (write to both, no disruption)
2. Lightweight API endpoint serving date-range queries as JSON
3. New "Interactive Charts" dashboard tab consuming the API
4. Optionally deprecate static PNG generation or keep as weekly export

### Open Issue Resolution
**Priority: Medium**

Beta tester on ComEd (Illinois) dynamic pricing encountering mode detection issues (`detected=unknown`) and P7 gap charging behavior that may not suit dynamic hourly pricing without fixed peak periods. Enhanced diagnostic bundle deployed for data collection — pending further verification and log analysis to determine whether ComEd requires a dynamic pricing path distinct from fixed TOU schedules.

---

## 📋 Planned

### Script Status Dashboard — Description Updates
**Priority: Low**

The Script Status dashboard tab is live and functional (shipped in v3.5.1). Needs updated or added descriptions for newer v4 scripts and scheduled tasks to keep the status page informative as the system grows.

### Home Load & Battery Power via Modbus
**Priority: Medium**

`home_load_kw` and `battery_kw` are currently hardcoded to `0.0` in the Modbus data path. Quick win: energy balance derivation after Modbus+Enphase merge. Proper fix: validate Modbus registers 82 (battery power) and 83 (home load) during active charging and peak discharge. Resolves flat zero lines in Power Flow charts.

### Multi-Gateway Management
**Priority: Low**

Support for users with multiple FranklinWH aGate systems. Coordinated management of multiple battery systems with independent configurations.

---

## 💡 Future Ideas

- **Weekend strategy optimization** — Pure solar self-consumption on non-peak days with dynamic off-grid duration
- **Holiday schedule support** — Rate schedule awareness for utility holidays
- **Modbus write exploration** — Investigate direct Modbus register control for mode switching (currently requires cloud API; DIY orchestration is possible but fragile and unsupported)
- **SolarEdge local API** — Direct panel monitoring if/when local API access becomes available
- **Home Assistant integration** — MQTT discovery for HA dashboards alongside the built-in web dashboard

---

## ✅ Recently Completed

### v4.0.2 — Three-Mode Strategy & Weekly Charts (Feb 2026)
- **Three-mode strategy**: TOU (default resting state) / Self-Consumption (peak hours only) / Emergency Backup (gap-fill bursts only). Replaces two-mode system with clearer separation of concerns.
- **Peak transition**: Proactive switch to Self-Consumption from any non-SC mode at peak start, verified via cloud API
- **Post-peak return**: Automatic return to TOU when peak ends, with `need_return_to_tou` detection
- **Startup grace period**: First decision cycle collects baseline data without mode switching, preventing aggressive Emergency Backup on container restart
- **P7 small gap guard**: Gaps < 1 kWh always skip grid charging; gaps < 2 kWh with active solar and 4+ hours to peak defer to solar
- **Enhanced diagnostic bundle**: `.env` now shows actual values for non-sensitive config keys; new `cloud_mode_debug.txt` captures raw Franklin cloud API mode fields
- **Weekly charts rewrite**: 5 charts (up from 3) — SOC Timeline with mode markers, 3-panel Daily Summary, Power Flow with filled areas, Decision Engine Activity (v4), Solar Curtailment Tracker (v4). Backward compatible with v3.5 log formats.
- **Dashboard carousel & lightbox**: Full-width chart carousel with dot navigation, swipe support, and fullscreen lightbox. Dynamic chart discovery adapts to v3.5 (3 charts) and v4 (5 charts) reports automatically.

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
