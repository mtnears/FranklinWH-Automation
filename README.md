

# FranklinWH Battery Automation

**Intelligent 15-minute solar-first battery automation for Franklin WH batteries**

Fully automated charging system that optimizes for Time-of-Use (TOU) electricity rates while maximizing solar usage. Makes smart decisions every 15 minutes with peak period state tracking.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

---

## 🌟 Key Features

- **🤖 Smart 15-Minute Decisions** - Intelligent automation runs every 15 minutes, not just at fixed times
- **📊 Peak State Tracking** - Prevents mode changes during expensive peak periods (5-8 PM)
- **☀️ Solar-First Intelligence** - Waits for solar production before grid charging when time permits
- **🔄 Automatic Mode Switching** - Seamlessly switches between TOU (solar-first) and BACKUP (grid charging) modes
- **🛡️ Robust API Handling** - 5-attempt retry logic for Franklin Cloud API timeouts
- **📈 Comprehensive Logging** - Detailed decision logs and CSV data for analysis
- **💰 Significant Savings** - Estimated ~$1,050/year on PG&E CARE rates
- **🔧 Zero Manual Intervention** - Set it and forget it

---

## 📋 Table of Contents

- [How It Works](#how-it-works)
- [Quick Start](#quick-start)
- [System Architecture](#system-architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Documentation](#documentation)
- [Monitoring](#monitoring)
- [Results](#results)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

---

## Deployment Options

This automation can be deployed in two ways:

### Native Installation (Recommended for single system)
- Direct installation on Synology NAS, Raspberry Pi, or Linux
- Minimal overhead, simple setup
- See [INSTALLATION.md](docs/INSTALLATION.md)

### Docker Deployment (Recommended for portability)
- Run in isolated containers
- Easy to deploy across multiple systems
- Consistent environment everywhere
- See [DOCKER_INSTALLATION.md](docs/DOCKER_INSTALLATION.md)

---

## 🔧 How It Works

### Core Automation (`smart_decision.py`)

The system runs **every 15 minutes** throughout the day, making intelligent charging decisions:

1. **Check Peak State**
   - Determines if currently in peak period (5-8 PM by default)
   - Tracks state transitions (Peak ↔ OffPeak)
   - **NEVER changes modes during peak period**

2. **Analyze Current Situation**
   - Battery State of Charge (SOC)
   - Current solar production
   - Hours until next peak period
   - Charging time required

3. **Make Decision**
   - **Wait for solar** if production is good and time permits
   - **Start grid charging** if running out of time or solar is insufficient
   - **Emergency charge** if SOC critically low (<75%) near peak

4. **Execute Action**
   - Switch to BACKUP mode (grid charging) when needed
   - Switch to TOU mode (solar-first) when appropriate
   - Log all decisions with reasoning

### Peak Period Protection

**Critical Feature:** The system uses a state file (`peak_state.txt`) to track peak periods:
- **During peak (5-8 PM):** NO mode changes, let battery discharge
- **After peak (8 PM):** Resume intelligent decision making
- **Midnight rollover:** Update state for new day

This prevents the bug where the system might think "0 hours to peak" at 8 PM and start emergency charging during the expensive peak period.

---

## 🚀 Quick Start

```bash
# Clone repository
git clone https://github.com/YOUR-USERNAME/FranklinWH-Automation.git
cd FranklinWH-Automation

# Create virtual environment
python3 -m venv venv311
source venv311/bin/activate

# Install dependencies
pip install --break-system-packages -r requirements.txt

# Configure credentials in scripts
# Edit scripts/smart_decision.py and replace:
#   USERNAME = "YOUR_EMAIL@example.com"
#   PASSWORD = "YOUR_PASSWORD"
#   GATEWAY_ID = "YOUR_GATEWAY_ID"

# Test the core automation
./scripts/smart_decision.py

# Set up scheduler (cron or Synology Task Scheduler)
# Run smart_decision.py every 15 minutes
```

---

## 🏗️ System Architecture

### Core Components

**Primary Automation:**
- `smart_decision.py` - Main 15-minute decision engine ⭐
- `run_smart_decision.sh` - Wrapper script for task schedulers
- `switch_to_backup_v2.py` - Switches to grid charging mode
- `switch_to_tou_v2.py` - Switches to solar-first mode
- `get_battery_status.py` - Quick status utility

**Data Collection (Optional but Recommended):**
- `collect_weather.py` - Weather Underground data (every 15 min)
- `collect_pvoutput.py` - Solar production tracking (hourly)

**Monitoring & Reporting (Optional):**
- `milestone_emailer.py` - Hourly status emails during testing
- `daily_status_report.py` - Daily summary at 4:30 PM
- `aggregate_data.py` - Data inventory checker (daily at 6 AM)

### Decision Logic Flow

```
Every 15 minutes:
├─ Get battery stats (with 5-attempt retry)
├─ Update peak state (Peak vs OffPeak)
├─ IF in peak period (5-8 PM):
│  └─ NO ACTION - let battery discharge
└─ ELSE (off-peak):
   ├─ Calculate hours to next peak
   ├─ Evaluate solar production
   ├─ Determine charging need
   ├─ Decide: BACKUP (grid) or TOU (solar-first)
   └─ Switch modes if needed
```

### State Management

**State Files** (in `/logs`):
- `peak_state.txt` - Current state: "Peak-YYYY-MM-DD" or "OffPeak-YYYY-MM-DD"
- `last_mode.txt` - Current battery mode: "BACKUP" or "TOU"

**Log Files** (in `/logs`):
- `solar_intelligence.log` - Timestamped decision reasoning
- `continuous_monitoring.csv` - 15-minute battery/solar/grid data
- `weather_data.csv` - Weather conditions (if enabled)

---

## 📦 Requirements

### Hardware
- **Franklin WH battery system** with cloud API access
- **24/7 Linux system** or cloud server:
  - Synology NAS (tested on DS1525+ with DSM 7.2)
  - Raspberry Pi 4 or newer
  - Any Linux server with Python 3.11+
  - Cloud VM (AWS, GCP, Azure)
- **Stable internet connection** for Franklin Cloud API
- **~100MB storage** for logs

### Software
- **Python 3.11+**
- **pip package manager**
- **Task scheduler** (cron, systemd timer, or Synology Task Scheduler)

### Optional Services
- **PVOutput account** - Free solar production tracking
- **Weather Underground** - Personal weather station data  
- **Email server** - For monitoring reports

---

## 💾 Installation

### Method 1: Synology NAS (Recommended for 24/7 Operation)

1. **Install Python 3.11** via Synology Package Center

2. **Enable SSH:**
   - Control Panel → Terminal & SNMP
   - Enable SSH service

3. **Create project directory:**
   ```bash
   ssh admin@your-nas-ip
   sudo -i
   mkdir -p /volume1/docker/franklin
   cd /volume1/docker/franklin
   ```

4. **Clone repository:**
   ```bash
   git clone https://github.com/YOUR-USERNAME/FranklinWH-Automation.git .
   ```

5. **Set up Python environment:**
   ```bash
   python3 -m venv venv311
   source venv311/bin/activate
   pip install --break-system-packages -r requirements.txt
   ```

6. **Configure credentials** in all scripts (see Configuration section)

7. **Set up Task Scheduler:**
   - Open Synology Control Panel → Task Scheduler
   - Create → Scheduled Task → User-defined script
   - **Task name:** "Smart Battery Decision - Every 15 minutes"
   - **User:** root
   - **Schedule:** Daily, every 15 minutes (00:00 to 23:45)
   - **Command:**
     ```bash
     #!/bin/bash
     cd /volume1/docker/franklin
     /volume1/docker/franklin/run_smart_decision.sh
     ```
   - Enable email notifications for errors

8. **Test the task:**
   - Select the task and click "Run"
   - Check logs in `/volume1/docker/franklin/logs/`

### Method 2: Raspberry Pi / Linux Server

1. **Install Python 3.11:**
   ```bash
   sudo apt update
   sudo apt install python3.11 python3.11-venv git
   ```

2. **Clone and set up:**
   ```bash
   git clone https://github.com/YOUR-USERNAME/FranklinWH-Automation.git
   cd FranklinWH-Automation
   python3.11 -m venv venv311
   source venv311/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure credentials** in scripts

4. **Set up cron job:**
   ```bash
   crontab -e
   ```
   
   Add this line:
   ```
   */15 * * * * cd /path/to/FranklinWH-Automation && ./scripts/run_smart_decision.sh >> /path/to/logs/cron.log 2>&1
   ```

5. **Optional: Set up systemd timer** for more robust scheduling

---

## ⚙️ Configuration

### Required: Franklin WH Credentials

Edit these files and replace placeholder values:

**Core automation scripts:**
- `scripts/smart_decision.py`
- `scripts/switch_to_backup_v2.py`
- `scripts/switch_to_tou_v2.py`
- `scripts/get_battery_status.py`

Replace:
```python
USERNAME = "YOUR_EMAIL@example.com"  # Your Franklin WH account email
PASSWORD = "YOUR_PASSWORD"            # Your Franklin WH password
GATEWAY_ID = "YOUR_GATEWAY_ID"       # From Franklin mobile app
```

**To find your Gateway ID:**
1. Open Franklin WH mobile app
2. Go to Settings → System Info
3. Copy the Gateway ID (format: `10060005A02X24470437`)

### Optional: Time-of-Use Schedule

If your utility has different peak hours, edit `smart_decision.py`:

```python
PEAK_START_HOUR = 17  # 5 PM - Change to your peak start
PEAK_END_HOUR = 20    # 8 PM - Change to your peak end
```

### Optional: Charging Thresholds

Adjust decision logic in `smart_decision.py`:

```python
TARGET_SOC = 95.0                 # Target charge before peak (%)
CHARGE_RATE_PER_HOUR = 32.0       # Your battery's charge rate
SAFETY_MARGIN_HOURS = 0.5         # Buffer time for charging
MIN_SOLAR_FOR_WAIT = 0.5          # Min solar (kW) to wait vs grid charge
```

### Optional: Weather Data Collection

Edit `scripts/collect_weather.py`:

```python
PWS_ID = "YOUR_WEATHER_STATION_ID"
API_KEY = "YOUR_WEATHER_UNDERGROUND_API_KEY"
```

### Optional: PVOutput Solar Tracking

Edit `scripts/collect_pvoutput.py`:

```python
API_KEY = "YOUR_PVOUTPUT_API_KEY"
GROUND_MOUNT_SID = "YOUR_SYSTEM_ID_1"
HOUSE_SID = "YOUR_SYSTEM_ID_2"
```

### Optional: Email Monitoring

Edit `scripts/milestone_emailer.py`:

```python
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "YOUR_EMAIL@example.com"
SENDER_PASSWORD = "YOUR_APP_PASSWORD"  # Gmail App Password
RECIPIENT_EMAIL = "YOUR_EMAIL@example.com"
MILESTONES = [8, 10, 12, 14, 16]  # Hours to send status
```

---

## 📚 Documentation

- **[Installation Guide](docs/INSTALLATION.md)** - Detailed setup instructions
- **[Peak State Logic](docs/PEAK_STATE_LOGIC.md)** - How peak period tracking works
- **[API Documentation](docs/API_REFERENCE.md)** - Franklin WH API details
- **[Troubleshooting Guide](docs/TROUBLESHOOTING.md)** - Common issues and solutions
- **[Task Scheduler Setup](docs/TASK_SCHEDULER.md)** - Synology and cron configuration
- **[DOCKER_INSTALLATION.md](docs/DOCKER_INSTALLATION.md) - Docker deployment guide

---

## 📊 Monitoring

### During Setup/Testing (Optional)

**Milestone Emailer** - Get hourly status updates:
```bash
# Set up hourly task to run milestone_emailer.py
# Sends emails at 8am, 10am, 12pm, 2pm, 4pm during testing
```

**Daily Status Report** - Summary at 4:30 PM:
```bash
# Set up daily task at 16:30 to run daily_status_report.py
# Review battery readiness before peak period
```

**Data Inventory** - Verify data collection:
```bash
# Set up daily task at 06:00 to run aggregate_data.py
# Confirms all data sources are logging properly
```

### Long-Term Monitoring

**Primary Log File:**
```bash
tail -f /volume1/docker/franklin/logs/solar_intelligence.log
```

**Check Recent Decisions:**
```bash
tail -100 /volume1/docker/franklin/logs/solar_intelligence.log | grep "Decision:"
```

**Verify Peak State Transitions:**
```bash
grep "Peak period" /volume1/docker/franklin/logs/solar_intelligence.log
```

**Current Battery Status:**
```bash
./scripts/get_battery_status.py
```

---

## 📈 Results

### Tested System Configuration
- **Platform:** Synology DS1525+, DSM 7.2.2
- **Battery:** Franklin WH aPower2 (30 kWh capacity)
- **Solar:** Dual arrays (21.3 kW ground mount + 6.9 kW roof) = 28.26 kW total
- **Utility:** PG&E E-TOU-D with CARE discount rates
- **Location:** Georgetown, CA (Sierra Nevada foothills)

### Typical Performance
- **Morning SOC:** 50-70% (after overnight home usage)
- **Target Achievement:** 95%+ SOC by 5 PM peak (consistently)
- **Grid Charging:** Minimal - only when solar insufficient or time critical
- **Peak Offset:** Full battery discharge during 5-8 PM peak period
- **System Reliability:** 99%+ uptime with retry logic

### Annual Savings Estimate
- **CARE discount:** ~$693/year (35% off standard PG&E rates)
- **Battery optimization:** ~$357/year (reduced peak usage vs no automation)
- **Total estimated savings:** ~$1,050/year

*Your results will vary based on:*
- Solar capacity and orientation
- Home energy usage patterns
- Local utility rates and TOU schedule
- Seasonal weather variations
- Battery size and degradation

---

## 🔧 Customization

### Adjust for Different TOU Schedules

**Example: Different peak times (4 PM - 9 PM):**
```python
# In smart_decision.py
PEAK_START_HOUR = 16  # 4 PM
PEAK_END_HOUR = 21    # 9 PM
```

**Example: Multiple peak periods:**

Currently the system handles one continuous peak period. For utilities with split peaks (morning + evening), you would need to modify the `update_peak_state()` function to check multiple time ranges.

### Adjust Decision Aggressiveness

**More conservative (start grid charging earlier):**
```python
SAFETY_MARGIN_HOURS = 1.0  # Increase buffer time
MIN_SOLAR_FOR_WAIT = 1.0   # Require more solar to wait
```

**More solar-optimistic (wait longer for sun):**
```python
SAFETY_MARGIN_HOURS = 0.25  # Reduce buffer
MIN_SOLAR_FOR_WAIT = 0.3    # Accept lower solar production
```

### Add More Solar Systems

For additional PVOutput systems, edit `collect_pvoutput.py`:

```python
SYSTEM_3_SID = "YOUR_THIRD_SYSTEM_ID"
SYSTEM_3_LOG = LOG_DIR / "pvoutput_system3_daily.csv"

# In main():
get_and_save_daily_output(SYSTEM_3_SID, SYSTEM_3_LOG, "System 3", yesterday)
```

---

## 🛠️ Troubleshooting

### "Cannot read battery SOC" Error

**Symptoms:** Script fails with Franklin Cloud API timeout

**Solutions:**
1. Check internet connectivity
2. Verify Franklin WH credentials in scripts
3. Confirm gateway ID is correct
4. System automatically retries 5 times with 10-second delays
5. Check Franklin WH app to confirm system is online

### Task Didn't Run on Schedule

**Synology:**
1. Control Panel → Task Scheduler → History
2. Check error messages in task history
3. Verify script has execute permissions
4. Ensure wrapper script path is correct
5. Test task manually with "Run" button

**Cron (Linux):**
1. Check cron logs: `grep CRON /var/log/syslog`
2. Verify crontab syntax: `crontab -l`
3. Check script permissions: `chmod +x run_smart_decision.sh`
4. Test script manually from same directory

### System Charged During Peak Period

**Check log for peak state transitions:**
```bash
grep "Peak period" /volume1/docker/franklin/logs/solar_intelligence.log | tail -20
```

**Verify peak state file:**
```bash
cat /volume1/docker/franklin/logs/peak_state.txt
# Should show "Peak-YYYY-MM-DD" during 5-8 PM
# Should show "OffPeak-YYYY-MM-DD" outside peak hours
```

**If peak protection not working:**
1. Confirm PEAK_START_HOUR and PEAK_END_HOUR in smart_decision.py
2. Check system time zone is correct
3. Review decision log for "IN PEAK PERIOD" messages

### API Retry Failures

**If seeing multiple retry failures:**

1. **Temporary outage:** Wait 15 minutes, system will retry
2. **Persistent issue:** Check Franklin WH service status
3. **Credentials:** Verify username/password haven't changed
4. **Network:** Confirm NAS/server can reach internet

**Check retry behavior in logs:**
```bash
grep "Attempt" /volume1/docker/franklin/logs/solar_intelligence.log | tail -20
```

Should see pattern like:
```
Attempt 1 starting...
✗ Attempt 1 failed: timeout, retrying in 10s...
Attempt 2 starting...
✓ Success on attempt 2
```

### Getting More Help

1. **Enable detailed logging:** Logs already comprehensive
2. **Check GitHub Issues:** See if others experienced similar problems
3. **Provide context when reporting:**
   - Log excerpts showing the problem
   - Your configuration (with credentials removed)
   - System platform and version
   - Franklin WH firmware version

---

## 🤝 Contributing

Contributions welcome! Here's how you can help:

### Report Bugs
- Check existing issues first
- Provide detailed reproduction steps
- Include log excerpts (sanitize credentials!)
- Specify your platform and Franklin WH model

### Suggest Features
- Explain the use case
- Describe expected behavior
- Consider compatibility with different TOU schedules

### Submit Code
- Fork the repository
- Create a feature branch
- Follow existing code style
- Add comments explaining logic
- Test thoroughly before submitting PR
- Update documentation if needed

### Share Results
- Document your savings
- Share configuration for different utilities
- Help others with setup questions
- Write guides for different platforms

---

## 🙏 Credits

### API Library
This project uses the excellent **[franklinwh](https://pypi.org/project/franklinwh/)** Python library:
- **PyPI:** https://pypi.org/project/franklinwh/
- **Source:** https://github.com/ai-joe-git/homeassistant-franklinwh
- **Author:** ai-joe-git
- **Purpose:** Provides Python interface to Franklin WH Cloud API

Thank you to the author for making this automation possible!

### Inspiration
Built to optimize battery usage for PG&E Time-of-Use rates and transition to all-electric home heating with a Bosch heat pump system.

---

## 📄 License

MIT License - See [LICENSE](LICENSE) file for details

Copyright (c) 2026 Ken Pauley

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

## ⭐ Support This Project

If this system helps you save money and optimize your Franklin WH battery:
- ⭐ **Star this repository** to increase visibility
- 🐛 **Report issues** you encounter
- 💡 **Share your results** and configuration
- 📢 **Tell other Franklin WH owners** about this automation
- 🔧 **Contribute improvements** via pull requests

---

## 📞 Contact & Community

- **Issues:** [GitHub Issues](https://github.com/YOUR-USERNAME/FranklinWH-Automation/issues)
- **Discussions:** [GitHub Discussions](https://github.com/YOUR-USERNAME/FranklinWH-Automation/discussions)
- **Questions:** Use GitHub Discussions for setup help

---

**Built with ☀️ for the Franklin WH community**

*Automating intelligent solar-first battery charging since 2026*
