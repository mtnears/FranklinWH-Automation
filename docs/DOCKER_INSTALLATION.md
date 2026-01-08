# Docker Installation Guide

**Run Franklin WH Battery Automation in Docker containers**

Docker provides a portable, isolated environment that works consistently across different systems. This guide covers Docker deployment options for the automation system.

---

## Table of Contents

- [Why Docker?](#why-docker)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Deployment Options](#deployment-options)
- [Configuration](#configuration)
- [Scheduling](#scheduling)
- [Monitoring](#monitoring)
- [Troubleshooting](#troubleshooting)
- [Upgrading](#upgrading)

---

## Why Docker?

**Benefits:**
- ✅ **Portable:** Run anywhere Docker runs (NAS, Pi, cloud, home server)
- ✅ **Isolated:** No conflicts with system Python or other applications
- ✅ **Consistent:** Same environment everywhere
- ✅ **Easy Updates:** Pull new image, restart container
- ✅ **Clean:** No system-level installations required

**When to use Docker:**
- You want easy portability across systems
- You're comfortable with Docker basics
- You want isolation from system Python
- You plan to run on multiple machines

**When NOT to use Docker:**
- You're unfamiliar with Docker and prefer native installation
- You want absolute minimal overhead
- You need to frequently modify scripts (though volume mounts solve this)

---

## Prerequisites

### 1. Docker Installed

**Synology NAS:**
- Open Package Center
- Install "Container Manager" (Docker)
- Enable SSH if not already enabled

**Raspberry Pi / Linux:**
```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Add your user to docker group (optional)
sudo usermod -aG docker $USER
newgrp docker

# Verify installation
docker --version
docker compose version
```

**Docker Desktop (Mac/Windows):**
- Download from https://www.docker.com/products/docker-desktop
- Install and start Docker Desktop

### 2. Required Information

Before starting, gather the same information as native installation:
- Franklin WH credentials (username, password, gateway ID)
- Your utility's peak period hours
- Your battery's tested charge rate

See [CONFIGURATION_REFERENCE.md](CONFIGURATION_REFERENCE.md) for details.

---

## Quick Start

### 1. Clone Repository

```bash
git clone https://github.com/YOUR-USERNAME/FranklinWH-Automation.git
cd FranklinWH-Automation
```

### 2. Create Configuration File

```bash
# Copy example configuration
cp .env.example .env

# Edit with your values
nano .env
```

**Minimum required in .env:**
```env
FRANKLIN_USERNAME=your-email@example.com
FRANKLIN_PASSWORD=your-password
FRANKLIN_GATEWAY_ID=your-gateway-id
PEAK_START_HOUR=17
PEAK_END_HOUR=20
CHARGE_RATE_PER_HOUR=32.0
```

### 3. Build and Test

```bash
# Build the image
docker compose build

# Test single run
docker compose run --rm franklin-automation

# Should output: "✓ Decision made: [mode] mode ([reason])"
```

### 4. Set Up Scheduling

See [Scheduling](#scheduling) section below.

---

## Deployment Options

### Option 1: Run-Once Container (Recommended)

Container runs once per execution, controlled by external scheduler (cron/Task Scheduler).

**Advantages:**
- Simple and predictable
- Easy to debug
- Lower resource usage (container only runs when needed)
- Familiar scheduling tools

**Setup:**

```bash
# Build image
docker compose build

# Test manually
docker compose run --rm franklin-automation
```

Then schedule with cron (Linux) or Task Scheduler (Synology).

---

### Option 2: Long-Running Container with Internal Scheduler

Container runs continuously with internal Python scheduler.

**Advantages:**
- Self-contained (no external scheduler needed)
- One command to start everything
- Docker manages restarts

**Disadvantages:**
- More complex
- Always consuming resources (minimal, but present)
- Harder to debug timing issues

**Setup:**

Create `scripts/continuous_scheduler.py`:
```python
import schedule
import time
import subprocess

def run_automation():
    subprocess.run(["/opt/venv/bin/python", "/app/smart_decision.py"])

# Run every 15 minutes
schedule.every(15).minutes.do(run_automation)

# Run daily report at 10:15 PM
schedule.every().day.at("22:15").do(
    lambda: subprocess.run(["/opt/venv/bin/python", "/app/daily_status_report.py"])
)

print("Scheduler started - running every 15 minutes")
# Run immediately on start
run_automation()

while True:
    schedule.run_pending()
    time.sleep(60)
```

Update `docker-compose.yml` command:
```yaml
command: python /app/continuous_scheduler.py
```

Install schedule library in requirements.txt:
```
schedule>=1.1.0
```

---

## Configuration

### Environment Variables

All configuration via environment variables in `.env` file:

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FRANKLIN_USERNAME` | Yes | - | Franklin WH account email |
| `FRANKLIN_PASSWORD` | Yes | - | Franklin WH password |
| `FRANKLIN_GATEWAY_ID` | Yes | - | Gateway ID from mobile app |
| `PEAK_START_HOUR` | Yes | 17 | Peak period start (24-hour) |
| `PEAK_END_HOUR` | Yes | 20 | Peak period end (24-hour) |
| `CHARGE_RATE_PER_HOUR` | Yes | 32.0 | Battery charge rate (%/hour) |
| `TARGET_SOC` | No | 95.0 | Target SOC before peak (%) |
| `SAFETY_MARGIN_HOURS` | No | 0.5 | Safety margin (hours) |
| `MIN_SOLAR_FOR_WAIT` | No | 0.5 | Min solar to wait (kW) |
| `TZ` | No | America/Los_Angeles | Container timezone |

### Modifying Scripts

Scripts need to read from environment variables instead of hardcoded values.

**Example:** Update `scripts/smart_decision.py`:

```python
import os

USERNAME = os.getenv('FRANKLIN_USERNAME')
PASSWORD = os.getenv('FRANKLIN_PASSWORD')
GATEWAY_ID = os.getenv('FRANKLIN_GATEWAY_ID')
PEAK_START_HOUR = int(os.getenv('PEAK_START_HOUR', '17'))
PEAK_END_HOUR = int(os.getenv('PEAK_END_HOUR', '20'))
CHARGE_RATE_PER_HOUR = float(os.getenv('CHARGE_RATE_PER_HOUR', '32.0'))
```

This allows configuration via .env file without editing Python code.

---

## Scheduling

### Synology Task Scheduler

**Create task:**
1. Control Panel → Task Scheduler → Create → Scheduled Task → User-defined script
2. **General:**
   - Task: `Franklin Battery - Docker`
   - User: `root`
   - Enabled: ✓
3. **Schedule:**
   - Daily, every 15 minutes (00:00 to 23:45)
4. **Task Settings:**
   ```bash
   docker compose -f /path/to/FranklinWH-Automation/docker-compose.yml run --rm franklin-automation
   ```

**Daily report task (10:15 PM):**
```bash
docker compose -f /path/to/FranklinWH-Automation/docker-compose.yml run --rm franklin-automation python /app/daily_status_report.py
```

---

### Linux Cron

```bash
# Edit crontab
crontab -e

# Add these lines (adjust paths):
# Every 15 minutes - automation
*/15 * * * * cd /opt/FranklinWH-Automation && docker compose run --rm franklin-automation >> logs/docker-cron.log 2>&1

# Daily at 10:15 PM - status report
15 22 * * * cd /opt/FranklinWH-Automation && docker compose run --rm franklin-automation python /app/daily_status_report.py >> logs/daily-report.log 2>&1
```

---

### Docker Compose with Restart Policy

For Option 2 (long-running container):

```yaml
services:
  franklin-automation:
    # ... other config ...
    restart: unless-stopped
    command: python /app/continuous_scheduler.py
```

Start with:
```bash
docker compose up -d
```

---

## Monitoring

### View Logs

```bash
# Container logs (if using long-running container)
docker compose logs -f franklin-automation

# Application logs (persisted to volume)
tail -f logs/solar_intelligence.log
tail -f logs/continuous_monitoring.csv
```

### Check Container Status

```bash
# List running containers
docker compose ps

# Check health
docker compose exec franklin-automation python -c "print('Container is healthy')"
```

### Manual Test Run

```bash
# Run automation once
docker compose run --rm franklin-automation

# Get battery status
docker compose run --rm franklin-automation python /app/get_battery_status.py

# Generate status report
docker compose run --rm franklin-automation python /app/daily_status_report.py
```

---

## Troubleshooting

### Container won't start

**Check logs:**
```bash
docker compose logs franklin-automation
```

**Common issues:**
- Missing required environment variables in .env
- Typo in .env file
- Docker not running
- Insufficient permissions

**Verify environment:**
```bash
docker compose config
```

---

### "Authentication failed"

**Problem:** Incorrect Franklin WH credentials in .env

**Solution:**
1. Verify credentials in Franklin WH mobile app
2. Check .env file for typos
3. Ensure no extra spaces in values
4. Rebuild image if you changed .env: `docker compose build`

---

### Scripts not finding files

**Problem:** Path issues inside container

**Solution:**
- Use absolute paths: `/app/logs/...` not `./logs/...`
- Verify volume mounts in docker-compose.yml
- Check file permissions on host

---

### Timezone incorrect

**Problem:** Container time doesn't match your location

**Solution:**
Add to .env:
```env
TZ=America/Los_Angeles  # or your timezone
```

Find timezone: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones

---

### Logs not persisting

**Problem:** Logs disappear when container restarts

**Solution:**
Ensure logs volume is mounted in docker-compose.yml:
```yaml
volumes:
  - ./logs:/app/logs
```

Create logs directory:
```bash
mkdir -p logs
```

---

## Upgrading

### Update to New Version

```bash
# Pull latest code
cd /path/to/FranklinWH-Automation
git pull

# Rebuild image
docker compose build

# Restart (if using long-running container)
docker compose down
docker compose up -d

# Or for run-once: Next scheduled run uses new image
```

### Backup Before Upgrading

```bash
# Backup logs
tar -czf logs-backup-$(date +%Y%m%d).tar.gz logs/

# Backup configuration
cp .env .env.backup
cp docker-compose.yml docker-compose.yml.backup
```

---

## Advanced Configuration

### Custom Scripts

Mount custom scripts directory:

```yaml
volumes:
  - ./logs:/app/logs
  - ./custom-scripts:/app/custom:ro
```

### Multiple Batteries

Run separate containers for multiple battery systems:

```yaml
services:
  franklin-battery-1:
    # ... config for first battery ...
    container_name: franklin-1
    environment:
      - FRANKLIN_GATEWAY_ID=gateway-id-1
    volumes:
      - ./logs-battery-1:/app/logs

  franklin-battery-2:
    # ... config for second battery ...
    container_name: franklin-2
    environment:
      - FRANKLIN_GATEWAY_ID=gateway-id-2
    volumes:
      - ./logs-battery-2:/app/logs
```

### Resource Limits

Limit container resources:

```yaml
services:
  franklin-automation:
    # ... other config ...
    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 256M
        reservations:
          cpus: '0.1'
          memory: 64M
```

---

## Comparison: Docker vs Native

| Aspect | Docker | Native |
|--------|--------|--------|
| **Setup Time** | 10-15 min | 15-20 min |
| **Portability** | High | Medium |
| **Resource Usage** | +50-100MB | Minimal |
| **Updates** | Pull & rebuild | Git pull |
| **Isolation** | Complete | Shared Python |
| **Debugging** | Slightly harder | Easier |
| **Complexity** | Medium | Low |

**Recommendation:**
- **Choose Docker if:** You want portability, isolation, or plan to run on multiple systems
- **Choose Native if:** You want minimal overhead and simplicity

---

## Security Considerations

### .env File Permissions

```bash
# Restrict access to .env (contains credentials)
chmod 600 .env
```

### Non-Root User

Dockerfile creates non-root user `franklin` for security:
```dockerfile
USER franklin
```

### Network Isolation

Docker containers are isolated by default. No additional network configuration needed.

---

## Summary

**Minimum steps to deploy:**
1. Install Docker
2. Clone repository
3. Create .env with credentials
4. Build image: `docker compose build`
5. Test: `docker compose run --rm franklin-automation`
6. Schedule with cron or Task Scheduler
7. Monitor logs: `tail -f logs/solar_intelligence.log`

**For questions or issues:**
- Check [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
- Review Docker logs: `docker compose logs`
- Open GitHub issue with details

---

**Last Updated:** January 8, 2026  
**Version:** 2.0 (Docker support)
