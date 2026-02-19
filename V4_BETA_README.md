# v4-forecast-engine — Beta Branch

> **⚠️ BETA SOFTWARE — ACTIVELY IN DEVELOPMENT**  
> This branch is being tested by the developer right now. It works, but expect rough edges.  
> Your battery system will not be harmed — worst case is a suboptimal charging decision.  
> **If anything seems wrong, you can roll back to main in under 60 seconds (see below).**

---

## What's New in v4

The v4 engine replaces the fixed time-based decision logic with an adaptive forecast-aware system. Instead of rigid rules about when to charge, it calculates a dynamic "gap" — how much energy you need, how much solar is expected, and how much grid charging is required to hit your target SOC before peak.

**Key changes from v3.5.x:**
- New adaptive decision engine with 8 priority phases (P1-P8)
- Forecast-aware charging — calculates morning SOC ceiling based on expected solar
- Curtailment protection — leaves battery headroom so solar isn't wasted
- Rate schedule flexibility — designed for PG&E E-TOU-D, SMUD TOD, ComEd, and others
- Modbus + Enphase hybrid data (same as v3.5.x, with cloud API fallback)

**What hasn't changed:**
- Same `.env` configuration
- Same Docker deployment
- Same Franklin cloud API + Modbus data sources
- Same override system (emergency backup, etc.)
- Same web dashboard

---

## Requirements

- **Modbus TCP enabled** on your FranklinWH system  
  If you don't have this, contact your installer or Franklin and request Modbus be enabled. You can tell them it's for SPAN panel integration — that's the standard reason.
- **Everything from the main branch** (Docker, `.env` configured, etc.)

---

## How to Try It

### If you're currently running the main branch:

```bash
# 1. Go to your franklin-git directory
cd /path/to/your/franklin-git

# 2. Make sure your current work is saved
git stash

# 3. Fetch the latest branches
git fetch origin

# 4. Switch to the beta branch
git checkout v4-forecast-engine

# 5. Rebuild and restart the container
docker-compose down
docker-compose up -d --build

# 6. Watch the logs
docker logs -f franklin-automation
```

You should see `FranklinWH Smart Decision Engine v4.0 Adaptive` in the log banner.

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
# Stop the container
docker-compose down

# Switch back to main
git checkout main

# Rebuild and restart
docker-compose up -d --build
```

That's it. You're back on the stable release. No data is lost, no settings are changed.

---

## How to Toggle Without Switching Branches

If you want to keep the v4 code but temporarily disable the new engine:

**Use the override system** via the web dashboard or API:

```bash
# Put into emergency backup mode for 4 hours (battery charges, engine defers)
curl -X POST http://your-server:port/api/override \
  -H "Content-Type: application/json" \
  -d '{"mode": "emergency_backup", "duration_hours": 4}'

# Check current override status
curl http://your-server:port/api/override

# Cancel override (engine resumes normal decisions)
curl -X DELETE http://your-server:port/api/override
```

Or through the Franklin mobile app: switch to Emergency Backup or Self-Consumption manually. The engine will detect the manual override and defer until you switch back.

---

## What to Report

If you run into issues, open a GitHub issue or post in Discussions with:

1. **Your log output** — the decision engine block starting with `======` 
2. **Your rate plan** — PG&E E-TOU-D, SMUD TOD, ComEd, etc.
3. **Your setup** — number of batteries, solar array size, export enabled/disabled
4. **What happened vs what you expected**

---

## Known Limitations (Beta)

- Solar forecast integration is in progress — currently uses historical patterns
- Rate schedule is configured for PG&E E-TOU-D by default; other rate plans need `.env` adjustments
- The decision log could be more verbose — we're adding detail as we test
- No automated tests yet — this is real-world testing phase

---

## Configuration

Same `.env` file as main branch. The v4 engine reads the same variables:

```env
# Required (same as main)
FRANKLIN_USERNAME=your_email
FRANKLIN_PASSWORD=your_password
FRANKLIN_GATEWAY_ID=your_gateway_id

# Modbus (required for v4)
MODBUS_HOST=192.168.x.x
MODBUS_PORT=502

# TOU schedule (adjust to your utility)
PEAK_START_HOUR=17
PEAK_END_HOUR=20

# Target SOC
TARGET_SOC=95.0
```

---

**Questions?** Open a Discussion on the repo or comment on the v4-forecast-engine issue thread.
