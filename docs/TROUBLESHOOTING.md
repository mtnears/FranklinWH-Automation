# Troubleshooting Guide

**Common issues and solutions for FranklinWH Battery Automation v3.3.0**

---

## Quick Diagnostics

Run these commands first to understand the current state:

```bash
# Check container is running and version
docker logs franklin-automation 2>&1 | head -5

# Check recent decisions
docker exec franklin-automation tail -20 /app/logs/solar_intelligence.log

# Check scheduler activity
docker logs --tail 20 franklin-automation

# Verify configuration loaded
docker logs franklin-automation 2>&1 | grep "Enabled features" -A 10
```

---

## Docker Issues

### Container won't start

```bash
# Check for errors
docker logs franklin-automation

# Verify .env file exists and has required values
cat .env | grep FRANKLIN

# Verify directories exist
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
# Check both containers are running
docker compose ps

# Check dashboard container
docker compose logs franklin-dashboard

# Test health endpoint
curl http://localhost:8100/health

# Check firewall allows the port
```

### Dashboard shows "Loading..."

```bash
# Check data file is being generated
docker exec franklin-automation ls -la /app/web/power_dashboard_data.json

# Check dashboard data generator is running
docker logs --tail 50 franklin-automation | grep Dashboard
```

### Logs tab shows "Unable to load log file"

```bash
# Check logs directory inside container
docker exec franklin-automation ls -la /app/logs/

# Verify intelligence log exists and has content
docker exec franklin-automation tail -5 /app/logs/solar_intelligence.log
```

### Wrong log file location

The Docker container writes logs to `/app/logs/` which maps to `./logs/` on the host (relative to your `docker-compose.yml` location). If you have an older installation, you may have logs in a different directory. Check:

```bash
# Container's actual log location
docker exec franklin-automation tail -1 /app/logs/solar_intelligence.log

# Host-side locations to check
ls -la logs/solar_intelligence.log
```

---

## API Connection Problems

### "Device response timed out" — Frequent Timeouts

The Franklin Cloud API can be slow. The system retries 5 times with 10-second delays.

```bash
# Check retry patterns
docker exec franklin-automation grep "Attempt" /app/logs/solar_intelligence.log | tail -10
```

**Good output (successful retry):**
```
Attempt 1 starting...
Attempt 1 failed: Device response timed out, retrying in 10s...
Attempt 2 starting...
Success on attempt 2
```

**If all 5 attempts consistently fail:**
1. Check Franklin WH system status in mobile app
2. Verify internet connection on your server
3. Check if Franklin is having service issues (common during updates)
4. Wait 1 hour and check if it resolves

### "Authentication failed"

```bash
# Check credentials in .env
grep "FRANKLIN_USERNAME\|FRANKLIN_PASSWORD" .env
```

- Verify you can log into the Franklin WH mobile app with the same credentials
- Check for special characters in password that may need escaping
- Try resetting your Franklin WH password

### "Gateway not found"

```bash
# Check gateway ID
grep "FRANKLIN_GATEWAY_ID" .env
```

- Find Gateway ID in Franklin WH app: Settings → System Info
- Should be exactly 20 characters
- Check for extra spaces or missing characters

---

## Mode Switching Issues

### Mode not switching when expected

v3.3.0 detects the current mode from the API's `name` field (e.g., "Emergency Backup", "Self Consumption"). This replaced `run_status` which could report incorrect values on some firmware versions. Check what the system sees:

```bash
# Check recent mode detection
docker exec franklin-automation grep "API Mode\|Mode unchanged\|Mode changed\|SWITCHING" /app/logs/solar_intelligence.log | tail -10
```

**Expected log format:**
```
API Mode: TOU (name=Time of Use, detected=tou)
Mode unchanged: tou (TOU)
```

Mode detection mapping: "Emergency Backup" → backup, "Self Consumption" → self_consumption, anything else → home mode (tou).

### Mode switch not verified

v3.3.0 verifies mode changes with a 5-second initial check and 8-second retry. A 10-minute cooldown prevents repeated switching on consecutive cycles. If you see warning messages:

```bash
docker exec franklin-automation grep "WARNING.*mode" /app/logs/solar_intelligence.log | tail -5
```

This could indicate the API accepted the command but the gateway hasn't applied it yet. The system will detect the correct mode on the next cycle.

### Battery charging during peak hours

This should never happen. Check:

```bash
# Look for any mode changes during peak (5-8 PM)
docker exec franklin-automation grep "SWITCHING" /app/logs/solar_intelligence.log | grep " 1[7-9]:\| 20:0[0-7]"
```

This should return nothing. If you see mode switches during peak:
1. Verify `PEAK_START_HOUR` and `PEAK_END_HOUR` in `.env`
2. Check system timezone: `docker exec franklin-automation date`
3. Open a GitHub issue with log excerpts

---

## Scheduling Issues

### Pre-peak check not running

```bash
# Verify it's scheduled
docker logs franklin-automation 2>&1 | grep "Pre-peak\|Post-peak"
```

Should show:
```
Pre-peak check: Daily at 16:55 (5min before peak)
Post-peak check: Daily at 20:01 (1min after peak ends)
```

If not visible, check that `PEAK_TRANSITION_BUFFER_MINUTES` is set in `.env` and rebuild the container.

### Decision running at wrong intervals

```bash
# Check configured interval
docker logs franklin-automation 2>&1 | grep "Smart Decision"
```

Should show `Smart Decision: Every XX minutes` matching your `CHECK_INTERVAL_MINUTES` setting.

### Wrong HOME_MODE

If the system returns to the wrong mode after peak:

```bash
grep "HOME_MODE" .env
```

Set to `tou` if you use Time-of-Use mode, or `self_consumption` if you use Self Consumption mode. This must match what you've configured in the Franklin app.

---

## Per-Battery Monitoring

### Battery data not showing

The system automatically detects the number of batteries. Check:

```bash
docker exec franklin-automation grep "Per-battery" /app/logs/solar_intelligence.log | tail -3
```

Expected:
```
Per-battery SOC: Bat1: 65.1%, Bat2: 65.2% (combined: 63.3%)
```

If missing, the system may be running an older version. Check:

```bash
docker logs franklin-automation 2>&1 | head -3
```

Should show `Scheduler v3.3.0`.

### Large SOC difference between batteries

A small difference (< 2%) is normal. If batteries diverge significantly, this could indicate a cell issue in one battery. Monitor over several days — the BMS should balance them over time.

---

## Log Analysis

### Understanding v3.3.0 log entries

A complete decision cycle looks like:

```
2026-02-02 14:15:23 - Attempting to get battery stats (max 5 attempts)...
2026-02-02 14:15:23 - Attempt 1 starting...
2026-02-02 14:15:26 - Success on first attempt
2026-02-02 14:15:26 - ======================================================================
2026-02-02 14:15:26 - Features: Solar, TOU (17:00-20:00), PVOutput
2026-02-02 14:15:26 - API Mode: TOU (name=Time of Use, detected=tou)
2026-02-02 14:15:26 - Per-battery SOC: Bat1: 78.3%, Bat2: 78.4% (combined: 76.1%)
2026-02-02 14:15:26 - Environment: Temp: 55F/12.8C, Signal: 30
2026-02-02 14:15:26 - SOC: 76.1%, Solar: 4.2kW, Grid->Bat: 0.0kW, Solar->Bat: 4.2kW
2026-02-02 14:15:26 - Status: 2.7h to peak
2026-02-02 14:15:26 - Decision: Solar can provide ~18.7% (need 18.9%), looking promising
2026-02-02 14:15:26 - Action: Solar-first (tou mode)
2026-02-02 14:15:26 - Mode unchanged: tou (TOU)
```

### Finding specific events

```bash
# Mode changes
docker exec franklin-automation grep "SWITCHING\|Mode changed" /app/logs/solar_intelligence.log | tail -10

# API errors
docker exec franklin-automation grep -i "error\|failed\|timeout" /app/logs/solar_intelligence.log | tail -10

# Peak transitions
docker exec franklin-automation grep "Peak period\|IN PEAK" /app/logs/solar_intelligence.log | tail -10

# Emergency charging events
docker exec franklin-automation grep -i "emergency\|out of time\|Must start" /app/logs/solar_intelligence.log | tail -10
```

### Log file growing too large

```bash
# Check log size
docker exec franklin-automation du -h /app/logs/*.log

# Logs can be archived from the host side
cd logs/
tar -czf archive-$(date +%Y%m%d).tar.gz *.log
> solar_intelligence.log   # Truncate (system recreates)
```

---

## Known Limitations

### Franklin Cloud API
- Timeout frequency varies — the 5-retry mechanism handles most cases
- Rate limiting is undocumented — the 15-minute default interval avoids issues
- Service disruptions can occur during Franklin firmware updates

### System Limitations
- TOU schedule cannot be queried from the API — must be set manually in `.env`
- Mode IDs are firmware-specific — the system uses `run_status` codes instead for universal detection
- Cell-level battery data is not available through the cloud API (requires local Modbus access)

---

## Getting Help

### Before opening an issue

1. Check this troubleshooting guide
2. Review the container logs: `docker logs franklin-automation`
3. Review the intelligence log: `docker exec franklin-automation tail -50 /app/logs/solar_intelligence.log`
4. Check your `.env` configuration

### Include this info in bug reports

```bash
# Version
docker logs franklin-automation 2>&1 | head -3

# Recent decisions
docker exec franklin-automation tail -30 /app/logs/solar_intelligence.log

# Configuration (remove credentials!)
grep -v "PASSWORD\|USERNAME" .env
```

- GitHub Issues: https://github.com/mtnears/FranklinWH-Automation/issues
- GitHub Discussions: https://github.com/mtnears/FranklinWH-Automation/discussions

---

**Last Updated:** February 2026
**Version:** 3.3.0
