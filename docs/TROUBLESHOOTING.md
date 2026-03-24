# Troubleshooting Guide

**Common issues and solutions for FranklinWH Battery Automation v4.1**

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

This should never happen. Check for any mode issues during peak (5-8 PM for E-TOU-D):

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, soc_pct, mode, battery_kw FROM system_readings WHERE CAST(strftime('%H',timestamp) AS INT) BETWEEN 17 AND 19 AND date(timestamp)=date('now','-1 day') ORDER BY timestamp\").fetchall(); [print(r) for r in rows[-10:]]"
```

If you see grid charging during peak: verify `PEAK_START_HOUR` and `PEAK_END_HOUR` in `.env`, check system timezone with `docker exec franklin-automation date`.

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

### Understanding v4.1 decision cycle

A complete engine cycle in the intelligence log looks like:

```
AdaptiveEngine initialized: target_soc=95.0%, rate_schedule=PG&E E-TOU-D...
TOU drift loaded from DB: X.XX%/hr...
Loaded hourly load profile from DB: night=X.XkW, morning=X.XkW...
======================================================================
FranklinWH Smart Decision Engine v4.0 Adaptive
======================================================================
Data source: modbus+enphase
Features: Solar, TOU (17:00-20:00), Modbus TCP...
API Mode: TOU-Idle (detected=time_of_use)
Environment: Temp: XXF/XX.XC, Freq: 59.90Hz
SOC: XX.X%, Solar: X.XXXkW, Grid: X.XXXkW, Battery: -X.XXXkW
Charging: Grid→Bat: X.XXkW, Solar→Bat: X.XXkW
Status: X.Xh to peak
Decision: [v4 PX] ...
Action: TIME_OF_USE mode (time_of_use)
Mode unchanged: time_of_use (TIME_OF_USE)
Engine metrics: ct_target_soc=XX.X, ct_floor_pct=XX.X...
```

### Finding specific events

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log WHERE message LIKE '%SWITCHING%' ORDER BY id DESC LIMIT 10\").fetchall(); [print(r) for r in rows]"
```

```bash
docker exec -w /app/scripts franklin-automation python3 -c "import sqlite3; conn=sqlite3.connect('/app/data/franklin.db'); rows=conn.execute(\"SELECT timestamp, message FROM intelligence_log WHERE message LIKE '%error%' OR message LIKE '%failed%' ORDER BY id DESC LIMIT 10\").fetchall(); [print(r) for r in rows]"
```

---

## Known Limitations

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

**Last Updated:** March 2026
**Version:** 4.1.0
