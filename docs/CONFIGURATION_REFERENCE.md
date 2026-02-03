# Configuration Reference

**Complete guide to configuring FranklinWH Battery Automation v3.2.0**

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
BATTERY_CAPACITY_KWH=30
CHARGE_RATE_PER_HOUR=32
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
| `BATTERY_CAPACITY_KWH` | `30` | Total battery capacity in kWh |
| `CHARGE_RATE_PER_HOUR` | `32.0` | Charge rate as % per hour (must test) |

**Testing your charge rate:** Switch to backup mode at night, note starting SOC and time, wait 30-60 minutes, check SOC again. Calculate: `(ending_soc - starting_soc) / hours_elapsed`. Typical values: ~35-40%/hr for 13.6 kWh, ~30-35%/hr for 30 kWh, ~15-18%/hr for 60 kWh.

---

## Feature Toggles

| Variable | Default | Description |
|----------|---------|-------------|
| `SOLAR_ENABLED` | `true` | Solar-first charging logic |
| `TOU_ENABLED` | `true` | Time-of-Use peak protection |
| `DYNAMIC_PRICING_ENABLED` | `false` | Hourly pricing integration |
| `WEATHER_ENABLED` | `false` | Weather data collection |
| `PVOUTPUT_ENABLED` | `false` | PVOutput solar tracking |
| `EMAIL_ENABLED` | `false` | Email notifications |

Disabled features are completely skipped in the decision logic.

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

These control when and how often the automation runs.

| Variable | Default | Range | Description |
|----------|---------|-------|-------------|
| `CHECK_INTERVAL_MINUTES` | `15` | 1-60 | How often smart decisions run |
| `PEAK_TRANSITION_BUFFER_MINUTES` | `5` | 1+ | Minutes before peak to run guaranteed check |
| `HOME_MODE` | `tou` | `tou` or `self_consumption` | Your normal operating mode |

**How scheduling works:**
- The smart decision runs every `CHECK_INTERVAL_MINUTES` (default: every 15 minutes)
- A guaranteed pre-peak check is pinned at `PEAK_START_HOUR` minus `PEAK_TRANSITION_BUFFER_MINUTES` (e.g., 16:55)
- A guaranteed post-peak check is pinned at `PEAK_END_HOUR` + 1 minute (e.g., 20:01)
- This ensures peak transitions are never missed regardless of the polling interval

**HOME_MODE:** Set this to match how your Franklin system is configured in the app. Most TOU users should leave this as `tou`. If you use Self Consumption as your normal mode, set it to `self_consumption`.

---

## Decision Tuning

| Variable | Default | Description |
|----------|---------|-------------|
| `TARGET_SOC` | `95.0` | Target battery % before peak |
| `SAFETY_MARGIN_HOURS` | `0.5` | Buffer time for charge calculations |
| `CHARGING_STRATEGY` | `balanced` | `conservative`, `balanced`, or `aggressive` |
| `MIN_SOLAR_FOR_WAIT` | `0.5` | Minimum solar kW to delay grid charging |

**Adjusting TARGET_SOC:** Lower to 90% if you consistently reach 95% well before peak. Keep at 95% if you sometimes fall short on cloudy days.

**Adjusting SAFETY_MARGIN_HOURS:** Increase to 1.0 for more conservative charging. Decrease to 0.25 to maximize solar waiting time (riskier).

---

## Solar Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `SOLAR_CAPACITY_KW` | `28.26` | Total solar array capacity |
| `MIN_SOLAR_FOR_WAIT` | `0.5` | Minimum kW to consider "useful solar" |

---

## Dynamic Pricing (ComEd)

| Variable | Default | Description |
|----------|---------|-------------|
| `PRICING_PROVIDER` | `comed` | Pricing API provider |
| `PRICE_THRESHOLD_CENTS` | `4.0` | Charge from grid below this price |
| `PRICE_CEILING_CENTS` | `10.0` | Never charge above this price |

When enabled, the system checks current electricity prices and charges from the grid during cheap periods, regardless of solar availability.

---

## Weather Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `WEATHER_PROVIDER` | `wunderground` | Weather data source |
| `WEATHER_STATION_ID` | (required) | Personal Weather Station ID |
| `WEATHER_API_KEY` | (required) | Weather Underground API key |
| `CLOUDY_THRESHOLD_PERCENT` | `50` | Cloud cover % to trigger grid charging |

Get a free API key at [wunderground.com/member/api-keys](https://www.wunderground.com/member/api-keys).

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
| `DATA_DIR` | `BASE_DIR/data` | Savings data directory |
| `WEB_DIR` | `/volume1/web` | Web dashboard directory |

**Docker users:** These paths are mapped via volume mounts in `docker-compose.yml`. The container uses `/app/logs`, `/app/data`, `/app/web` internally.

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

The startup banner shows all enabled features and scheduled tasks.

### Check the intelligence log for enriched data

```bash
docker exec franklin-automation tail -15 /app/logs/solar_intelligence.log
```

v3.2.0 entries include `API Mode:`, `Per-battery SOC:`, and `Environment:` lines.

---

## Troubleshooting Configuration

| Problem | Likely Cause | Solution |
|---------|-------------|----------|
| "Authentication failed" | Wrong credentials | Verify in Franklin WH app, check `.env` |
| "Gateway not found" | Wrong gateway ID | Check App → Settings → System Info |
| Battery charges during peak | Wrong peak hours | Verify `PEAK_START_HOUR` / `PEAK_END_HOUR` |
| Battery not ready for peak | Charge rate too high | Re-test your charge rate, lower by 10-20% |
| Charges too early (wastes solar) | Charge rate too low | Re-test, or reduce `SAFETY_MARGIN_HOURS` |
| Wrong mode after peak | Wrong `HOME_MODE` | Set to `tou` or `self_consumption` to match your setup |

---

**Last Updated:** February 2026
**Version:** 3.2.0
