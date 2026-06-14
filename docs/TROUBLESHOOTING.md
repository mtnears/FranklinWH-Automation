# Troubleshooting Guide

**Common issues and solutions for FranklinWH Battery Automation**

---

## Quick Diagnostics

Run these commands first to understand the current state:

```bash
docker exec -w /app/scripts franklin-automation python3 -c "from config import VERSION; print('Version:', VERSION)"

docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log ORDER BY id DESC LIMIT 20\").fetchall(); [print(r) for r in rows]"

docker exec franklin-automation cat /app/logs/data_source_health.json | python3 -m json.tool
```

---

## Docker Issues

### Container won't start

```bash
docker logs franklin-automation

cat .env | grep FRANKLIN

ls -la logs/ data/ web/
```

### Scripts not updating after git pull

Scripts are built into the Docker image, not volume-mounted. A simple `docker restart` does NOT pick up code changes.

```bash
docker compose down
docker compose build --no-cache
docker compose up -d
```

### Dashboard not accessible

```bash
docker compose ps

docker compose logs franklin-dashboard

curl http://localhost:8100/health
```

### Dashboard shows "Loading..."

```bash
docker exec franklin-automation ls -la /app/web/power_dashboard_data.json

docker logs --tail 50 franklin-automation | grep Dashboard
```

---

## Data Source Issues

### Check which data sources are active

```bash
docker exec franklin-automation cat /app/logs/data_source_health.json | python3 -m json.tool
```

This shows Modbus, cloud API, and Enphase health stats including success rates, response times, and total attempts.

### Modbus not connecting

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log WHERE message LIKE '%modbus%' OR message LIKE '%Modbus%' ORDER BY id DESC LIMIT 10\").fetchall(); [print(r) for r in rows]"
```

Common causes: aGate IP changed (check router DHCP), Modbus not enabled on aGate (contact installer), firewall blocking port 502.

### Cloud API — frequent timeouts

The Franklin cloud API can be slow (2-7 seconds typical). The system retries automatically. Check recent API activity:

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log WHERE message LIKE '%Attempt%' OR message LIKE '%timeout%' ORDER BY id DESC LIMIT 10\").fetchall(); [print(r) for r in rows]"
```

If all attempts consistently fail: check Franklin WH system status in the mobile app, verify internet connectivity, wait 1 hour and check if it resolves (Franklin service disruptions are common during firmware updates).

### Cloud API — zero attempts (expected with Modbus)

With Modbus enabled and working, the cloud API should show zero or near-zero `total_attempts` in the health stats. Cloud API calls only happen for actual mode switches (2-4 per day). This is by design — Modbus handles all monitoring.

### Cloud-only systems showing NULL data fields

If you're running with `MODBUS_ENABLED=false` and `system_readings` rows have `NULL` values for `soc_pct`, `solar_kw`, `grid_kw`, `battery_kw`, `home_load_kw`, or `mode`, you're on a pre-v4.3 release. v4.3.0 fixed a long-standing bug where the cloud collector inserted these rows with NULL primary fields. Symptoms include load profile learning operating on default 1.8 kW assumptions and SOC-target manual overrides never exiting.

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, soc_pct, solar_kw, mode, source FROM system_readings WHERE source='cloud' ORDER BY id DESC LIMIT 5\").fetchall(); [print(r) for r in rows]"
```

If recent cloud-source rows show NULL primary fields, upgrade to v4.3.0 or newer.

### Authentication failed

```bash
grep "FRANKLIN_USERNAME\|FRANKLIN_PASSWORD" .env
```

Verify you can log into the Franklin WH mobile app with the same credentials. Check for special characters in the password that may need escaping.

### Gateway not found

```bash
grep "FRANKLIN_GATEWAY_ID" .env
```

Find the Gateway ID in the Franklin WH app: Settings → System Info. It should be exactly 20 characters.

---

## Engine Decision Issues

### Check recent decisions

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log WHERE message LIKE 'Decision%' ORDER BY id DESC LIMIT 10\").fetchall(); [print(r) for r in rows]"
```

Each decision shows its priority level: `[v4 P7]` means the P7 (pre-peak charging) rule fired. Priority levels: P1 (emergency) through P8 (default TOU).

### Mode not switching when expected

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log WHERE message LIKE '%SWITCHING%' OR message LIKE '%Mode changed%' OR message LIKE '%Mode unchanged%' ORDER BY id DESC LIMIT 10\").fetchall(); [print(r) for r in rows]"
```

The v4 engine verifies every mode switch against the actual hardware state. If the hardware doesn't confirm the change, it retries up to 3 times.

### Mode verification — Modbus vs cloud

With Modbus enabled, mode verification uses Modbus register 15507 (instant, local) instead of the cloud API. Check current mode:

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, mode, mode_detail, source FROM system_readings ORDER BY id DESC LIMIT 5\").fetchall(); [print(r) for r in rows]"
```

### Battery charging during peak hours

This should never happen. Check for any mode issues during peak. The peak window depends on your rate plan:
- PG&E E-TOU-D: 5-8 PM weekdays (17-20)
- PG&E EV2-A: 4-9 PM every day (16-21), plus 3-4 PM and 9 PM-midnight partial-peak
- SCE TOU-D-PRIME: 4-9 PM weekdays (16-21)

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, soc_pct, mode, battery_kw FROM system_readings WHERE CAST(strftime('%H',timestamp) AS INT) BETWEEN 16 AND 20 AND date(timestamp)=date('now','-1 day') ORDER BY timestamp\").fetchall(); [print(r) for r in rows[-10:]]"
```

If you see grid charging during peak:
- For users on legacy `PEAK_START_HOUR`/`PEAK_END_HOUR` config: verify these match your tariff
- For `rate_schedule.json` users: check that the active window covers the expected hours (see Rate Schedule Issues below)
- Check system timezone with `docker exec franklin-automation date` — container should match your local time zone for the rate plan to apply correctly

> **v4.6 (#26):** the engine now resolves its peak window from your rate schedule, not the legacy `.env` peak hours, so a stale `PEAK_START_HOUR`/`PEAK_END_HOUR` no longer silently drives behavior. If the two disagree you'll see a `CONFIG CONFLICT` line in the intelligence log and a flag on **Settings → Configuration Health**. Check there first — it tells you the engine's active window, the schedule's window, and which is being used:
> ```bash
> docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log WHERE message LIKE 'CONFIG CONFLICT%' ORDER BY id DESC LIMIT 5\").fetchall(); [print(r) for r in rows] or print('no conflicts')"
> ```
> The fix is to align `PEAK_START_HOUR`/`PEAK_END_HOUR` in `.env` with your schedule (then re-run `migrate_v46.py`), or rely on the schedule and treat the env vars as a fallback.

### Excessive mode switching (flapping)

Some mode switching before peak is normal — the engine evaluates conditions each cycle. Check today's switch count:

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT COUNT(*) FROM intelligence_log WHERE message LIKE '%SWITCHING%' AND date(timestamp)=date('now')\").fetchone(); print('Mode switches today:', rows[0])"
```

More than 10-15 switches in a day may warrant investigation.

---

## Forecast Issues

### Check forecast status

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log WHERE message LIKE '%Open-Meteo%' OR message LIKE '%Morning plan%' OR message LIKE '%Calibration%' ORDER BY id DESC LIMIT 10\").fetchall(); [print(r) for r in rows]"
```

You should see `Open-Meteo: house (X.XXkWp)` with a kWh total, followed by a morning plan with forecast source `[open_meteo]`.

### Forecast shows unrealistic values

The raw Open-Meteo forecast is calibrated using local weather data and yesterday's actual production. If values seem off, check that weather collection is working:

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT date, temp_high, temp_low, humidity_avg, precip_total FROM weather_daily ORDER BY date DESC LIMIT 5\").fetchall(); [print(r) for r in rows]"
```

### No forecast available

If the forecast falls back to `clear_sky` or `profile_fallback`, Open-Meteo may be unreachable. Check:

```bash
docker exec franklin-automation python3 -c "import urllib.request, json; r=urllib.request.urlopen('https://api.open-meteo.com/v1/forecast?latitude=38.91&longitude=-120.84&hourly=global_tilted_irradiance&tilt=22&azimuth=0&forecast_days=1', timeout=10); print(json.loads(r.read().decode()).get('hourly',{}).get('time',['none'])[:3])"
```

---

## Database Issues

### Check database size and table counts

```bash
docker exec franklin-automation ls -lh /app/data/franklin.db

docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); tables=conn.execute(\"SELECT name FROM sqlite_master WHERE type='table' ORDER BY name\").fetchall(); print(f'{len(tables)} tables:'); [print(f'  {t[0]}') for t in tables]"
```

### Check recent readings

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, soc_pct, solar_kw, grid_kw, mode, source FROM system_readings ORDER BY id DESC LIMIT 5\").fetchall(); [print(r) for r in rows]"
```

### Daily energy summary

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT date, solar_kwh, grid_import_kwh, battery_charge_kwh, battery_discharge_kwh, home_load_kwh FROM daily_energy_summary ORDER BY date DESC LIMIT 7\").fetchall(); [print(r) for r in rows]"
```

---

## Per-Battery Monitoring

### Battery data

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); r=conn.execute(\"SELECT timestamp, soc_pct, per_battery_soc_json, per_battery_power_json FROM system_readings ORDER BY id DESC LIMIT 1\").fetchone(); print(r)"
```

### Large SOC difference between batteries

A small difference (< 2%) is normal. If batteries diverge significantly, this could indicate a cell issue in one battery. Monitor over several days — the BMS should balance them over time.

---

## Log Analysis

### Understanding a v4 decision cycle

A complete engine cycle in the intelligence log looks like:

```
Profile loaded from /app/data/system_profile.json (rebuilt ...)
Active season: winter (month 5) — tier overrides: [peak=26.714¢, partial_peak=25.628¢, off_peak=14.663¢]
Rates from DB: EV2-A CARE Winter 2026 (CARE, effective 2026-05-16) — peak=26.714¢ off_peak=14.663¢
Loaded rate schedule: PG&E EV2-A with CARE (3 tiers, 3 windows, season=winter)
Loaded hourly load profile from DB: night=1.3kW, morning=1.5kW, afternoon=1.9kW, evening=2.1kW
AdaptiveEngine initialized: target_soc=95.0%, rate_schedule=PG&E EV2-A with CARE, ...
======================================================================
FranklinWH Smart Decision Engine v4.4.1 Adaptive
======================================================================
Data source: modbus+enphase
Features: Solar, TOU (16:00-21:00), Modbus TCP...
API Mode: Self Consumption (detected=self_consumption)
Environment: Temp: XXF/XX.XC, Freq: 59.90Hz
SOC: XX.X%, Solar: X.XXXkW, Grid: X.XXXkW, Battery: -X.XXXkW
Charging: Grid→Bat: X.XXkW, Solar→Bat: X.XXkW
Status: X.Xh to peak
Decision: [v4 PX] ...
Action: SELF_CONSUMPTION mode (self_consumption)
Mode unchanged: self_consumption (SELF_CONSUMPTION)
Engine metrics: ct_target_soc=XX.X, ct_floor_pct=XX.X, ct_sc_commit_threshold=XX.X...
```

Key lines to watch:
- **Active season** — appears when `rate_schedule.json` has a `seasons` block; tells you which season's `tier_rates` were applied
- **Rates from DB** — appears when `rate_history` table has a row for today; DB values override JSON for peak/off_peak
- **Loaded rate schedule** — final loaded state with tier count, window count, active season name
- **Smart Decision Engine vX.X.X Adaptive** — version banner pulled from `VERSION` file (v4.4.1+)
- **Decision [v4 PX]** — priority level that fired (P1-P8, plus P4.5 for partial-peak on three-tier plans)

### Finding specific events

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log WHERE message LIKE '%SWITCHING%' ORDER BY id DESC LIMIT 10\").fetchall(); [print(r) for r in rows]"
```

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log WHERE message LIKE '%error%' OR message LIKE '%failed%' ORDER BY id DESC LIMIT 10\").fetchall(); [print(r) for r in rows]"
```

---

## Rate Schedule Issues

### Verify the active rate schedule

```bash
docker exec -w /app/scripts franklin-automation python3 -c "
import sqlite3
conn = sqlite3.connect('/app/data/franklin.db')
conn.row_factory = sqlite3.Row
rows = conn.execute(\"SELECT timestamp, message FROM intelligence_log WHERE message LIKE 'Loaded rate schedule%' OR message LIKE 'Active season%' OR message LIKE 'Rates from DB%' ORDER BY id DESC LIMIT 6\").fetchall()
for r in rows: print(r['timestamp'], '|', r['message'])
"
```

You should see (most recent first):
- `Loaded rate schedule: <name> (N tiers, N windows, season=<name>)`
- `Rates from DB: <name> (<type>, effective <date>) — peak=XX.XXX¢ off_peak=XX.XXX¢` (if `rate_history` populated)
- `Active season: <name> (month N) — tier overrides: [peak=XX.XXX¢, ...]` (if `seasons` block configured)

### Wrong tier rates in dashboard or decisions

If displayed rates don't match your bill:
1. Check JSON config: `docker exec franklin-automation python3 -c "import json; d=json.load(open('/app/data/rate_schedule.json')); print(json.dumps(d['rate_schedule']['tiers'], indent=2))"`
2. Check active season override (if applicable): look for `Active season:` line in intelligence_log
3. Check `rate_history` DB override (if applicable): look for `Rates from DB:` line; query the table directly: `docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT effective_date, rate_name, peak_rate, off_peak_rate, care_peak_rate, care_off_peak_rate FROM rate_history WHERE effective_date <= date('now') ORDER BY effective_date DESC LIMIT 3\").fetchall(); [print(r) for r in rows]"`

**Precedence reminder:** JSON base → JSON `seasons` override → DB `rate_history` override (peak/off_peak only). For 3-tier plans, `partial_peak` comes from JSON only since `rate_history` has no `partial_peak` column.

### Seasons configuration warnings

If `rate_schedule.json` has a `seasons` block but startup logs show warnings about overlapping months, missing month coverage, invalid month values, or unknown tier names, validation has flagged a misconfig. The config still loads (warnings are advisory), but the result may not be what you expect.

```bash
docker exec -w /app/scripts franklin-automation python3 -c "
import sqlite3
conn = sqlite3.connect('/app/data/franklin.db')
rows = conn.execute(\"SELECT timestamp, message FROM intelligence_log WHERE level='WARNING' AND message LIKE '%Season%' AND timestamp > datetime('now','-7 day') ORDER BY id DESC LIMIT 10\").fetchall()
for r in rows: print(r)
"
```

Common fixes:
- Overlap: Move one month to a single season (first match wins, so a month in both seasons silently uses the first match in the array)
- Missing coverage: Add missing months to a season, or accept that those months fall back to the JSON base config
- Unknown tier name: Match the spelling in `seasons[].tier_rates` to the tier names defined in the top-level `tiers` object

### No partial-peak decisions firing

If you're on a three-tier plan but never see `[P4.5]` entries during partial-peak windows:
1. Confirm `rate_schedule.json` has a `partial_peak` tier defined under `tiers`
2. Confirm windows include a `"tier": "partial_peak"` entry covering the expected hours
3. Test the tier resolution: `docker exec -w /app/scripts franklin-automation python3 -c "
import sys; sys.path.insert(0, '/app/scripts')
from rate_schedule import load_rate_schedule
from datetime import datetime
s = load_rate_schedule('/app/data/rate_schedule.json')
now = datetime.now()
print(f'Current tier: {s.current_tier(now)}')
print(f'Is partial peak: {s.is_partial_peak(now)}')
print(f'Tiers loaded: {s.tiers}')
"`

---

## Configuration Store & Settings (v4.6)

### Settings tab shows stale or wrong values

The Settings tab reads the SQLite config store, which is refreshed by the migration — not live from `.env`. After editing `.env` or `rate_schedule.json`, re-run the migration to refresh it:

```bash
docker exec franklin-automation python3 /app/scripts/migrate_v46.py --battery-array <your-array-id>
```

The migration preserves any values you've edited directly in the store (`source=user`); it only refreshes `env`/`default` rows and re-imports the rate plan.

### Settings tab / health checks show "not migrated"

The v4.6 migration hasn't been run on this install. Run it once (preview with `--dry-run` first):

```bash
docker exec franklin-automation python3 /app/scripts/migrate_v46.py --dry-run --battery-array <your-array-id>
docker exec franklin-automation python3 /app/scripts/migrate_v46.py --battery-array <your-array-id>
```

### Migration warns about a `months` key on a window

Your `rate_schedule.json` has a per-window `months` key, which the old parser silently ignored (seasons would never switch). Move seasonal window layouts into per-season `windows` blocks — see `data/rate_schedule.example.json` (`pepco_r_tou_p_seasonal`). The migration rejects this by default; `--allow-legacy-months` downgrades it to a warning that drops the key and imports the window as year-round.

### Confirm the migration landed

```bash
docker exec franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT key, value FROM app_state ORDER BY key\").fetchall(); [print(r) for r in rows]"
```

Look for `schema_version = 4.6.0-phase1` and a `migration.phase1_completed_at` timestamp.

---

## aGate Mode State Issues

### Mode verification disagrees with observed battery behavior

In rare cases the aGate can report one operating mode (e.g., Emergency Backup) while the battery actually behaves as another (e.g., Self-Consumption). The system reads mode from Modbus register 15507 and from the cloud API, which normally agree, but the gateway can hold stale state across firmware events or transient communication issues.

Symptoms:
- Dashboard and intelligence_log report a mode (`API Mode: Emergency Backup`) that doesn't match observed battery_kw or grid_kw behavior
- Battery is charging from solar when it should be discharging (or vice versa) given the reported mode
- Mode switches issued by the engine "succeed" per the cloud API but the hardware doesn't actually transition

Resolution: rebooting the aGate clears the stale mode state. From the Franklin mobile app: Settings → System → Reboot Gateway (or contact your installer for remote restart). Allow 5-10 minutes for the gateway to come back online and Modbus to resume.

This is uncommon and not a recurring issue, but worth knowing about as a recovery step before deeper investigation.

---

## Synology — Docker Bridge Subnet Blocked by DSM Firewall

Symptoms during install on a Synology NAS:
- `docker compose build` fails with package install errors or network unreachable
- Container starts but cannot reach external APIs (Open-Meteo, Franklin cloud, weather)
- `docker logs franklin-automation` shows connection timeouts or DNS resolution failures
- Pinging from inside the container fails: `docker exec franklin-automation ping -c 2 google.com`

Cause: DSM Firewall's default "Allow LAN only" rule blocks Docker's bridge network subnets (172.16.0.0/12 and 172.17.0.0/16). Docker containers cannot reach the host or the internet when this rule is active.

Resolution:
1. Control Panel → Security → Firewall → Edit Rules
2. Add a new rule **above** any "Allow LAN only" or deny rules:
   - Ports: All
   - Source IP: Specific IP / Subnet
   - IP: `172.16.0.0`
   - Subnet mask: `255.240.0.0`
   - Action: Allow
3. Save and apply

This allows all Docker bridge subnets (172.16.0.0 through 172.31.255.255) to reach the host. Reported by community user **WaywardWilderness** (Issue #19).

### Related: dockerd.json DNS config for Synology build failures

If `docker compose build --no-cache` fails specifically at the pip/apt install stage with DNS errors, configure Docker daemon DNS:

1. Edit `/etc/docker/daemon.json` (create if missing):
   ```json
   {
     "dns": ["8.8.8.8", "1.1.1.1"]
   }
   ```
2. Restart Docker: Package Center → Container Manager → Action → Restart
3. Rebuild: `cd /volume1/docker/franklin-git && docker compose build --no-cache`

---

### Franklin Cloud API
- Timeout frequency varies — retry mechanism handles most cases
- Rate limiting is undocumented — Modbus-first monitoring minimizes cloud calls
- Service disruptions can occur during Franklin firmware updates

### Modbus
- Read-only interface — mode switching still requires cloud API
- Per-battery SOC not available via Modbus (only aggregate SOC)
- Solar production registers (Model 502) return zeros — use Enphase/SolarEdge directly

### System Limitations
- TOU schedule cannot be queried from the API — must be set manually in `.env`
- Cell-level battery data is not available through any current interface
- `PEAK_END_HOUR=24` has a known bug — use `PEAK_END_HOUR=0` as a workaround

---

## Getting Help

### Before opening an issue

1. Check this troubleshooting guide
2. Review the container logs: `docker logs franklin-automation`
3. Check the System Logs tab in the dashboard
4. Use the **🐛 Report Issue** button in the dashboard to generate a sanitized diagnostic bundle

### Include this info in bug reports

- Version (from dashboard About card or startup banner)
- Recent engine decisions (last 20 lines from intelligence log)
- Data source health stats
- Your rate plan and setup details (battery count, solar size, Modbus enabled)
- What happened vs. what you expected

GitHub Issues: https://github.com/mtnears/FranklinWH-Automation/issues

---

**Last Updated:** June 2026
**Version:** 4.6.0
