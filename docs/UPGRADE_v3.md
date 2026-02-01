# Upgrading to v3.0 - Configuration-Driven Architecture

This guide covers upgrading from v2.x to v3.0 of FranklinWH Battery Automation.

## What's New in v3.0

### Configuration-Driven Features
All features are now controlled via environment variables in `.env`:
- **TOU_ENABLED** - Time-of-Use peak protection
- **SOLAR_ENABLED** - Solar-first charging logic
- **DYNAMIC_PRICING_ENABLED** - ComEd/hourly pricing integration
- **WEATHER_ENABLED** - Weather-informed decisions
- **PVOUTPUT_ENABLED** - PVOutput solar tracking

### New Capabilities
- **Dynamic Pricing** - Integrates with ComEd hourly pricing API
- **Configurable Peak Hours** - Set your utility's peak window
- **Configurable Peak Days** - Weekdays, weekends, or all days
- **Multiple Peak Periods** - Support for split-peak utilities
- **Charging Strategy** - Conservative, balanced, or aggressive modes
- **Daily Report Config Summary** - Shows enabled features at top of report

### Backward Compatibility
- If you don't create a `.env` file, defaults match v2.x behavior
- Existing log files and data are preserved
- No changes to Franklin WH API interaction

---

## Upgrade Path

### Option A: Fresh Install (Recommended for Docker users)

1. **Backup your current setup:**
   ```bash
   cd /volume1/docker/franklin
   tar -czf franklin-backup-$(date +%Y%m%d).tar.gz logs/ data/ *.py
   ```

2. **Pull the new version:**
   ```bash
   git pull origin main
   # Or re-clone if needed
   ```

3. **Create your .env file:**
   ```bash
   cp .env.example .env
   nano .env  # Edit with your settings
   ```

4. **Migrate your credentials:**
   - Copy USERNAME, PASSWORD, GATEWAY_ID from your old scripts
   - Add them to .env as FRANKLIN_USERNAME, FRANKLIN_PASSWORD, FRANKLIN_GATEWAY_ID

5. **Test:**
   ```bash
   # For Docker:
   docker compose run --rm franklin-automation
   
   # For native:
   ./scripts/smart_decision.py
   ```

### Option B: In-Place Upgrade (For Native Installations)

1. **Backup:**
   ```bash
   cp smart_decision.py smart_decision.py.v2.backup
   cp daily_status_report.py daily_status_report.py.v2.backup
   ```

2. **Create .env file:**
   ```bash
   cat > .env << 'EOF'
   # Your existing credentials
   FRANKLIN_USERNAME=your_email@example.com
   FRANKLIN_PASSWORD=your_password
   FRANKLIN_GATEWAY_ID=your_gateway_id
   
   # Your existing settings (these are the defaults)
   BATTERY_CAPACITY_KWH=30
   CHARGE_RATE_PER_HOUR=32.0
   TARGET_SOC=95.0
   PEAK_START_HOUR=17
   PEAK_END_HOUR=20
   
   # Feature toggles (match your v2 setup)
   SOLAR_ENABLED=true
   TOU_ENABLED=true
   DYNAMIC_PRICING_ENABLED=false
   WEATHER_ENABLED=false
   PVOUTPUT_ENABLED=false
   
   # Paths
   BASE_DIR=/volume1/docker/franklin
   LOG_DIR=/volume1/docker/franklin/logs
   DATA_DIR=/volume1/docker/franklin/data
   WEB_DIR=/volume1/web
   EOF
   ```

3. **Install python-dotenv:**
   ```bash
   pip install python-dotenv --break-system-packages
   ```

4. **Update scripts:**
   - Replace `smart_decision.py` with new version
   - Replace `daily_status_report.py` with new version
   - Add new files: `config.py`, `pricing.py`

5. **Test:**
   ```bash
   ./scripts/smart_decision.py
   ```

---

## Configuration Reference

### Required Settings

| Variable | Description | Example |
|----------|-------------|---------|
| `FRANKLIN_USERNAME` | Franklin WH account email | `user@example.com` |
| `FRANKLIN_PASSWORD` | Franklin WH password | `yourpassword` |
| `FRANKLIN_GATEWAY_ID` | Gateway ID from app | `10060005A02X24470437` |
| `BATTERY_CAPACITY_KWH` | Battery size in kWh | `30` |
| `CHARGE_RATE_PER_HOUR` | Charge rate %/hour | `32.0` |

### Feature Toggles

| Variable | Default | Description |
|----------|---------|-------------|
| `SOLAR_ENABLED` | `true` | Enable solar-first logic |
| `TOU_ENABLED` | `true` | Enable TOU peak protection |
| `DYNAMIC_PRICING_ENABLED` | `false` | Enable hourly pricing |
| `WEATHER_ENABLED` | `false` | Enable weather forecasts |
| `PVOUTPUT_ENABLED` | `false` | Enable PVOutput tracking |

### TOU Settings (when TOU_ENABLED=true)

| Variable | Default | Description |
|----------|---------|-------------|
| `PEAK_START_HOUR` | `17` | Peak period start (5 PM) |
| `PEAK_END_HOUR` | `20` | Peak period end (8 PM) |
| `PEAK_DAYS` | `weekdays` | Which days: `all`, `weekdays`, `weekends` |

### Dynamic Pricing (when DYNAMIC_PRICING_ENABLED=true)

| Variable | Default | Description |
|----------|---------|-------------|
| `PRICING_PROVIDER` | `comed` | API provider |
| `PRICE_THRESHOLD_CENTS` | `4.0` | Charge below this price |
| `PRICE_CEILING_CENTS` | `10.0` | Never charge above this |

---

## Enabling Dynamic Pricing (ComEd)

If you're a ComEd customer and want to enable hourly pricing:

1. **Update .env:**
   ```env
   DYNAMIC_PRICING_ENABLED=true
   PRICING_PROVIDER=comed
   PRICE_THRESHOLD_CENTS=4.0
   PRICE_CEILING_CENTS=10.0
   ```

2. **Decision Logic:**
   - Price < threshold + no solar → Charge from grid
   - Price < 2 cents → Always charge (even with solar coming)
   - Price > ceiling → Wait for solar or cheaper prices
   - Good solar available → Use solar regardless of price

3. **Overnight Charging:**
   With weather forecasting enabled, the system can decide:
   - Sunny forecast → Skip overnight cheap grid, wait for free solar
   - Cloudy forecast → Take advantage of cheap overnight rates

---

## Troubleshooting

### "ModuleNotFoundError: No module named 'dotenv'"
```bash
pip install python-dotenv --break-system-packages
```

### "CONFIG ERROR: FRANKLIN_USERNAME is required"
Create a `.env` file with your credentials. See `.env.example`.

### Scripts still using hardcoded values
The new scripts import from `config.py`. Make sure:
1. `config.py` is in the `scripts/` directory
2. `.env` file exists and is readable
3. You're running the new version of `smart_decision.py`

### Dynamic pricing not working
1. Check `DYNAMIC_PRICING_ENABLED=true` in `.env`
2. Test the API: `python scripts/pricing.py`
3. Check network connectivity to `hourlypricing.comed.com`

---

## Rollback

If you need to rollback to v2.x:

```bash
# Restore backup
cp smart_decision.py.v2.backup smart_decision.py
cp daily_status_report.py.v2.backup daily_status_report.py

# Remove new files (optional)
rm config.py pricing.py .env
```

---

## Questions?

- GitHub Issues: https://github.com/mtnears/FranklinWH-Automation/issues
- GitHub Discussions: https://github.com/mtnears/FranklinWH-Automation/discussions
