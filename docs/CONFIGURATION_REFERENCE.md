# Configuration Reference

**Complete guide to configuring FranklinWH Battery Automation v4.3**

All configuration is done via the `.env` file. You never need to edit Python scripts directly.

---

## Quick Start

```bash
cp .env.example .env
nano .env
```

Set these required values and you're ready to go:

```bash
FRANKLIN_USERNAME=your_email@example.com
FRANKLIN_PASSWORD=your_password
FRANKLIN_GATEWAY_ID=your_gateway_id
BATTERY_CAPACITY_KWH=13.6
ADAPTIVE_ENGINE_ENABLED=true
```

---

## Required Settings

### Franklin WH Credentials

| Variable | Description | How to Find |
|----------|-------------|-------------|
| `FRANKLIN_USERNAME` | Account email | Same as mobile app login |
| `FRANKLIN_PASSWORD` | Account password | Same as mobile app login |
| `FRANKLIN_GATEWAY_ID` | Gateway identifier | App → Settings → System Info |

The Gateway ID is a 20-character alphanumeric string (e.g., `10060005A02X24470437`).

### Battery Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `BATTERY_CAPACITY_KWH` | `30` | Total battery capacity in kWh (all batteries combined) |
| `CHARGE_RATE_PER_HOUR` | `32.0` | Charge rate as % per hour (see testing instructions below) |

**Testing your charge rate:** Switch to backup mode at night, note starting SOC and time, wait 30-60 minutes, check SOC again. Calculate: `(ending_soc - starting_soc) / hours_elapsed`. Typical values: ~35-40%/hr for 13.6 kWh, ~30-35%/hr for 30 kWh, ~15-18%/hr for 60 kWh.

---

## Feature Toggles

| Variable | Default | Description |
|----------|---------|-------------|
| `ADAPTIVE_ENGINE_ENABLED` | `true` | v4 adaptive engine with P1-P8 priority stack. When disabled, falls back to v3.x time-based logic |
| `SOLAR_ENABLED` | `true` | Solar-first charging logic |
| `TOU_ENABLED` | `true` | Time-of-Use peak protection |
| `MODBUS_ENABLED` | `false` | Fast local data via Modbus TCP (100× faster than cloud API) |
| `DYNAMIC_PRICING_ENABLED` | `false` | Hourly pricing integration (ComEd, etc.) |
| `WEATHER_ENABLED` | `false` | Weather data collection from Weather Underground |
| `PVOUTPUT_ENABLED` | `false` | PVOutput solar tracking |
| `EMAIL_ENABLED` | `false` | Email notifications |

Disabled features are completely skipped in the decision logic.

---

## V4 Adaptive Engine

| Variable | Default | Description |
|----------|---------|-------------|
| `ADAPTIVE_ENGINE_ENABLED` | `true` | Enable the 8-phase priority engine |
| `BATTERY_COUNT` | `1` | Number of battery units (1 for single aPower, 2 for dual, etc.) |
| `BACKUP_RESERVE_PCT` | `20` | Reserve SOC percentage to maintain (passed to FranklinWH API) |
| `EMERGENCY_PREP_MODE` | `false` | Maintain higher reserve SOC for grid outages (storm season, PSPS events) |

---

## Solar Forecast Engine

The forecast engine predicts daily solar production to optimize grid charging decisions. It uses Open-Meteo (free, no API key required) with local weather calibration.

**How it works:** Each morning, the engine predicts how much solar will reach the battery, then limits grid charging to leave headroom for free solar. On sunny days it charges less from grid; on cloudy days it charges more to ensure the battery reaches target SOC by peak.

**Important:** Only configure the array that charges your battery. If you have solar on a separate meter (e.g., a ground-mount or barn array), don't include it here — only the array connected to the Franklin system matters for charging decisions.

| Variable | Default | Description |
|----------|---------|-------------|
| `FORECAST_ENABLED` | `false` | Enable forecast-aware charging (requires `ADAPTIVE_ENGINE_ENABLED=true`) |
| `FORECAST_LATITUDE` | — | Your location latitude (find at latlong.net) |
| `FORECAST_LONGITUDE` | — | Your location longitude |
| `FORECAST_HOUSE_TILT` | `22` | Roof pitch in degrees (0=flat, 90=vertical) |
| `FORECAST_HOUSE_AZIMUTH` | `0` | Panel facing direction: 0=South, -90=East, 90=West |
| `FORECAST_HOUSE_KWP` | `6.96` | Total panel watts ÷ 1000 (e.g., 16 × 435W = 6.96 kWp) |

**Azimuth conversion from compass bearing:** Subtract 180, then negate. Examples: South (180°) = 0, SW (225°) = 45, West (270°) = 90, SE (135°) = -45, East (90°) = -90.

**Forecast sources (automatic, best-available):**
1. Open-Meteo API — hourly tilted irradiance forecast, weather-model based
2. Weather calibration — local weather history improves accuracy over time (requires `WEATHER_ENABLED=true`)
3. Learned profile — historical production averages (fallback)

---

## Solar Export & Rate Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SOLAR_EXPORT` | `false` | Does your system export surplus solar to the grid? |
| `CARE_RATE` | `false` | CARE/FERA discount program active |
| `NEM_VERSION` | `nem2` | Net metering version: `nem2` or `nem3` |

**`SOLAR_EXPORT` explained:**
- `false` (non-export): Surplus solar is curtailed when battery is full. The engine proactively drains the battery to create headroom for solar absorption. Post-peak self-consumption discharge is enabled.
- `true` (export): Surplus solar earns grid credits. Headroom management and post-peak discharge are both skipped since surplus goes to the grid.

---

## TOU Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PEAK_START_HOUR` | `17` | Peak start in 24-hour format |
| `PEAK_END_HOUR` | `20` | Peak end in 24-hour format |
| `PEAK_DAYS` | `weekdays` | `all`, `weekdays`, or `weekends` |
| `PEAK2_START_HOUR` | (disabled) | Optional second peak start |
| `PEAK2_END_HOUR` | (disabled) | Optional second peak end |

**Common TOU schedules:**
- PG&E E-TOU-D: `PEAK_START_HOUR=17`, `PEAK_END_HOUR=20`
- SCE TOU-D-4-9PM: `PEAK_START_HOUR=16`, `PEAK_END_HOUR=21`
- SDG&E TOU-DR1: `PEAK_START_HOUR=16`, `PEAK_END_HOUR=21`

**24-hour reference:** 1 PM = 13, 4 PM = 16, 5 PM = 17, 8 PM = 20, 9 PM = 21

---

## Scheduling Settings

| Variable | Default | Range | Description |
|----------|---------|-------|-------------|
| `CHECK_INTERVAL_MINUTES` | `0` (auto) | 0-60 | How often the decision engine runs. `0` = auto-calculate based on data source |
| `PEAK_TRANSITION_BUFFER_MINUTES` | `5` | 1+ | Minutes before peak to run guaranteed check |
| `HOME_MODE` | `tou` | `tou` or `self_consumption` | Your normal operating mode |

**Auto-calculated intervals** (when `CHECK_INTERVAL_MINUTES=0`):
- Cloud API only: 30 minutes (rate limit protection)
- Modbus enabled: 10 minutes (local reads are fast)
- Dynamic pricing + Modbus: 5 minutes

**How scheduling works:**
- Smart decisions run on clock-aligned intervals at :00 and :30 each hour (or more frequently with Modbus)
- A guaranteed pre-peak check runs at `PEAK_START_HOUR` minus `PEAK_TRANSITION_BUFFER_MINUTES`
- A guaranteed post-peak check runs at `PEAK_END_HOUR` + 1 minute
- Peak transitions are never missed regardless of the polling interval

**HOME_MODE:** Set this to match how your Franklin system is configured in the app. Most TOU users should leave this as `tou`. If you use Self Consumption as your normal mode, set it to `self_consumption`.

---

## Decision Tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `TARGET_SOC` | `95.0` | Target battery % before peak |
| `SAFETY_MARGIN_HOURS` | `0.5` | Buffer time for charge calculations |
| `CHARGING_STRATEGY` | `balanced` | `conservative`, `balanced`, or `aggressive` |
| `MIN_SOLAR_FOR_WAIT` | `0.5` | Minimum solar kW to delay grid charging |
| `TAPER_CEILING_PCT` | `95` | Grid charging ceiling for non-export systems (see below) |

**Taper ceiling tuning (`TAPER_CEILING_PCT`):** On non-export systems, battery charge rate tapers at high SOC, which means solar production that exceeds the reduced charge rate gets curtailed (wasted). By capping grid charging below the taper knee, the engine leaves room for solar to fill during peak production hours. Start at 95 and check your curtailment data after each sunny day. Lower by 5 until curtailment drops to near zero. Typical sweet spot is 75-90 depending on your battery size and solar output. Export systems can leave this at 95 since surplus goes to the grid for credit.

---

## Modbus TCP Integration

| Variable | Default | Description |
|----------|---------|-------------|
| `MODBUS_ENABLED` | `false` | Enable local Modbus TCP data collection |
| `MODBUS_HOST` | `192.168.5.149` | aGate IP address on your local network |
| `MODBUS_PORT` | `502` | Modbus TCP port (rarely needs changing) |
| `MODBUS_TIMEOUT` | `5.0` | Connection timeout in seconds |
| `MODBUS_RETRY_ATTEMPTS` | `3` | Retry attempts before falling back to cloud API |

When Modbus is enabled, it provides 100× faster data collection (26ms vs 5,000ms cloud API) and is used for SOC monitoring, grid power tracking, grid disconnect detection, temperature, voltage/frequency, and mode verification. The cloud API is reserved for mode switching only.

To enable Modbus: contact your installer or Franklin support and request Modbus be enabled for SPAN panel integration. Then set `MODBUS_ENABLED=true` and `MODBUS_HOST` to your aGate's IP address.

See [MODBUS_REGISTER_MAP.md](MODBUS_REGISTER_MAP.md) for the full register reference.

---

## Solar Array Monitoring

For local solar production monitoring. Each array needs its own configuration block.

| Variable | Default | Description |
|----------|---------|-------------|
| `SOLAR_ARRAYS` | (empty) | Comma-separated list of array names (e.g., `house` or `house,barn`) |
| `SOLAR_CAPACITY_KW` | `28.26` | Total solar capacity in kW across all arrays |

**Per-array settings** use the pattern `SOLAR_ARRAY_{NAME}_{SETTING}` with name in uppercase:

### Enphase Array

```bash
SOLAR_ARRAY_HOUSE_NAME=House
SOLAR_ARRAY_HOUSE_TYPE=enphase
SOLAR_ARRAY_HOUSE_IP=192.168.4.93
SOLAR_ARRAY_HOUSE_SERIAL=your_gateway_serial
SOLAR_ARRAY_HOUSE_EMAIL=your_enphase_email
SOLAR_ARRAY_HOUSE_PASSWORD=your_enphase_password
SOLAR_ARRAY_HOUSE_MODEL=IQ8MC
```

### SolarEdge Array

```bash
SOLAR_ARRAY_BARN_NAME=Barn
SOLAR_ARRAY_BARN_TYPE=solaredge
SOLAR_ARRAY_BARN_SITE_ID=your_site_id
SOLAR_ARRAY_BARN_API_KEY=your_api_key
SOLAR_ARRAY_BARN_MODEL=SolarEdge
```

---

## SolarEdge Panel-Level Monitoring

Optional per-optimizer health monitoring using the SolarEdge monitoring portal.

| Variable | Default | Description |
|----------|---------|-------------|
| `SOLAREDGE_PANEL_MONITORING` | `false` | Enable panel-level data collection |
| `SOLAREDGE_SITE_ID` | — | SolarEdge site ID |
| `SOLAREDGE_USERNAME` | — | Portal email (monitoring.solaredge.com login) |
| `SOLAREDGE_PASSWORD` | — | Portal password (NOT the API key) |

Data collected every 15 minutes: per-panel energy (daily/weekly/monthly/lifetime), inverter and string assignment, real hardware serial numbers. Stored in `solaredge_readings` and `solaredge_inverter_readings` SQLite tables.

---

## Dynamic Pricing (ComEd)

| Variable | Default | Description |
|----------|---------|-------------|
| `PRICING_PROVIDER` | `comed` | Pricing API provider |
| `PRICE_THRESHOLD_CENTS` | `4.0` | Charge from grid at or below this price |
| `PRICE_CEILING_CENTS` | `10.0` | Never charge above this price |
| `SOLAR_OVERRIDE_PRICE_CENTS` | (disabled) | Override solar-first when price is at or below this |

All pricing thresholds support negative values for markets with negative pricing.

**Solar override:** When the grid price drops to or below `SOLAR_OVERRIDE_PRICE_CENTS`, the system charges from grid even when solar is producing, even during peak periods. Use for utilities like ComEd that can have negative pricing where they pay you to consume.

**Threshold ordering (most aggressive → least aggressive):**
```
SOLAR_OVERRIDE_PRICE_CENTS  ≤  PRICE_THRESHOLD_CENTS  <  PRICE_CEILING_CENTS
```

---

## Telemetry

Anonymous, opt-in usage telemetry to help guide development.

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEMETRY_ENABLED` | (popup) | Force on/off, or leave unset for dashboard popup |
| `TELEMETRY_REGION` | — | Your state/region for aggregate stats (e.g., CA, IL, TX) |
| `MULTI_METER` | `false` | System has separate meters for different solar arrays |

On first dashboard load, a one-time popup asks if you'd like to opt in. Your choice is saved and the popup never appears again. The `.env` overrides let you skip the popup entirely.

---

## Weather Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `WEATHER_PROVIDER` | `wunderground` | Weather data source |
| `WEATHER_STATION_ID` | (required) | Personal Weather Station ID |
| `WEATHER_API_KEY` | (required) | Weather Underground API key |
| `CLOUDY_THRESHOLD_PERCENT` | `50` | Cloud cover % to trigger grid charging |

Get a free API key at [wunderground.com/member/api-keys](https://www.wunderground.com/member/api-keys). Weather data is stored in SQLite and used by the solar forecast calibration model.

---

## PVOutput Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `PVOUTPUT_API_KEY` | (required) | PVOutput API key |
| `PVOUTPUT_SYSTEM_IDS` | (required) | Comma-separated system IDs |

Get your API key at [pvoutput.org/account.jsp](https://pvoutput.org/account.jsp).

---

## System Paths

| Variable | Default | Description |
|----------|---------|-------------|
| `BASE_DIR` | `/volume1/docker/franklin` | Base installation directory |
| `LOG_DIR` | `BASE_DIR/logs` | Log file directory |
| `DATA_DIR` | `BASE_DIR/data` | Database and data directory |
| `WEB_DIR` | `/volume1/web` | Web dashboard directory |

**Docker users:** These paths are mapped via volume mounts in `docker-compose.yml`. The container uses `/app/logs`, `/app/data`, `/app/web` internally. The SQLite database (`franklin.db`) lives in the data directory.

---

## Docker Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `DASHBOARD_PORT` | `8100` | Port for the web dashboard |
| `TZ` | `America/Los_Angeles` | Container timezone |

---

## Notification Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `EMAIL_ENABLED` | `false` | Enable email reports |
| `SMTP_SERVER` | `smtp.gmail.com` | SMTP server |
| `SMTP_PORT` | `587` | SMTP port |
| `SENDER_EMAIL` | (required) | From address |
| `SENDER_PASSWORD` | (required) | App password (not account password) |
| `RECIPIENT_EMAIL` | (required) | To address |

For Gmail, use an [App Password](https://myaccount.google.com/apppasswords), not your regular password.

---

## Advanced Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `DEBUG_MODE` | `false` | Verbose logging |
| `API_MAX_RETRIES` | `5` | API call retry attempts |
| `API_RETRY_DELAY` | `10` | Seconds between retries |

---

## Validating Your Configuration

### Docker

```bash
docker logs franklin-automation 2>&1 | head -25
```

The startup banner shows the version, all enabled features, and scheduled tasks.

### Check the intelligence log for engine decisions

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log ORDER BY id DESC LIMIT 15\").fetchall(); [print(r) for r in rows]"
```

v4.1 entries include `Data source:`, `API Mode:`, `Environment:`, `SOC:`, `Decision:`, and `Engine metrics:` lines.

### Check version

```bash
docker exec -w /app/scripts franklin-automation python3 -c "from config import VERSION; print(VERSION)"
```

---

## Troubleshooting Configuration

| Problem | Likely Cause | Solution |
|---------|-------------|----------|
| "Authentication failed" | Wrong credentials | Verify in Franklin WH app, check `.env` |
| "Gateway not found" | Wrong gateway ID | Check App → Settings → System Info |
| Battery charges during peak | Wrong peak hours | Verify `PEAK_START_HOUR` / `PEAK_END_HOUR` |
| Battery not ready for peak | Charge rate too high | Re-test your charge rate, lower by 10-20% |
| Charges too early (wastes solar) | Taper ceiling too high | Lower `TAPER_CEILING_PCT` by 5 until curtailment clears |
| Wrong mode after peak | Wrong `HOME_MODE` | Set to `tou` or `self_consumption` to match your setup |
| Forecast shows wrong kWh | Wrong array params | Check `FORECAST_HOUSE_TILT`, `AZIMUTH`, `KWP` |
| No weather calibration | Missing weather config | Set `WEATHER_ENABLED=true` with WU station and API key |

---

**Last Updated:** May 2026
**Version:** 4.3.0
