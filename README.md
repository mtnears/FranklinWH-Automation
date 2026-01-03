# FranklinWH-Automation

**Solar-first battery automation system for Franklin WH batteries**

Fully automated intelligent charging that optimizes for Time-of-Use (TOU) electricity rates while maximizing solar usage. No manual intervention required.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

---

## 🌟 Features

- **🌞 Solar-First Intelligence** - Waits for solar production before grid charging when possible
- **📊 Three-Tier Decision System** - Morning analysis, midday check, final safety verification
- **📈 Automated Data Collection** - PVOutput solar data, Weather Underground conditions, continuous battery monitoring
- **📧 Daily Email Reports** - Comprehensive summaries of automation decisions and performance
- **💰 Significant Cost Savings** - ~$1,050/year on PG&E CARE rates (results may vary)
- **🔄 Zero Manual Intervention** - Fully automated 24/7 operation

---

## 📋 Table of Contents

- [How It Works](#how-it-works)
- [Quick Start](#quick-start)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Documentation](#documentation)
- [Results](#results)
- [Customization](#customization)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [Credits](#credits)
- [License](#license)

---

## 🔧 How It Works

The system runs on a schedule optimized for PG&E E-TOU-D rates (5 PM - 8 PM peak period):

1. **8:00 AM - Morning Solar Intelligence**
   - Analyzes overnight battery state of charge (SOC)
   - Checks recent solar production trends
   - Decides: wait for solar, or start grid charging immediately
   - Target: 95% SOC by 5 PM

2. **2:00 PM - Midday Progress Check**
   - Evaluates charging progress
   - Assesses current solar production
   - Starts grid charge if needed to ensure peak readiness

3. **3:30 PM - Final Safety Check**
   - Last chance verification (90 minutes before peak)
   - Emergency grid charge if SOC < 75%
   - Ensures minimum acceptable charge level

4. **5:00-8:00 PM - Peak Period**
   - Battery discharges to offset expensive grid power
   - System saves money on TOU rates

5. **Continuous Throughout the Day**
   - 15-minute battery monitoring
   - 15-minute weather data collection  
   - Hourly PVOutput solar production logging
   - 4:30 PM daily email status report

---

## 🚀 Quick Start

```bash
# Clone repository
git clone https://github.com/mtnears/FranklinWH-Automation.git
cd FranklinWH-Automation

# Create virtual environment
python3 -m venv venv311
source venv311/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure your credentials
# Edit scripts and replace:
#   - Franklin WH username/password/gateway ID
#   - PVOutput API key and system IDs (optional)
#   - Weather Underground station ID and API key (optional)

# Test scripts
./scripts/get_battery_status.py

# Set up Task Scheduler (see INSTALLATION.md)
```

---

## 📦 Requirements

### Hardware
- **Franklin WH battery** with cloud access enabled
- **24/7 Linux system** (tested on Synology DS1525+ DSM 7.2)
  - Synology NAS
  - Raspberry Pi
  - Any Linux server with Python support
- **100MB+ storage** for logs

### Software
- **Python 3.11+**
- **Network access** to:
  - Franklin WH Cloud API
  - PVOutput.org (optional)
  - Weather Underground (optional)
  - Email server (for notifications)

### Accounts (Optional but Recommended)
- **PVOutput account** - Free solar production tracking
- **Weather Underground** - Personal weather station data
- **Email** - For daily status reports

---

## 💾 Installation

### Method 1: Synology NAS

1. **Install Python 3.11** via Package Center
2. **Enable SSH** in Control Panel → Terminal & SNMP
3. **Follow detailed guide:** See [INSTALLATION.md](INSTALLATION.md)

### Method 2: Raspberry Pi / Linux

1. **Install Python 3.11:**
   ```bash
   sudo apt update
   sudo apt install python3.11 python3.11-venv
   ```

2. **Clone and setup:**
   ```bash
   git clone https://github.com/YOUR-USERNAME/FranklinWH-Automation.git
   cd FranklinWH-Automation
   python3.11 -m venv venv311
   source venv311/bin/activate
   pip install -r requirements.txt
   ```

3. **Configure scripts** with your credentials

4. **Set up cron jobs** (equivalent to Synology Task Scheduler)

---

## ⚙️ Configuration

All scripts require your personal credentials. Replace these placeholders:

### Franklin WH Credentials
```python
USERNAME = "YOUR_EMAIL@example.com"
PASSWORD = "YOUR_PASSWORD"
GATEWAY_ID = "YOUR_GATEWAY_ID"  # From Franklin mobile app
```

### PVOutput (Optional)
```python
API_KEY = "YOUR_PVOUTPUT_API_KEY"
SYSTEM_ID = "YOUR_SYSTEM_ID"
```

### Weather Underground (Optional)
```python
PWS_ID = "YOUR_WEATHER_STATION_ID"
API_KEY = "YOUR_WEATHER_UNDERGROUND_API_KEY"
```

**See [docs/FRANKLIN_BATTERY_SCRIPTS.pdf](docs/FRANKLIN_BATTERY_SCRIPTS.pdf) for complete script reference**

---

## 📚 Documentation

- **📘 [Complete Installation Guide](docs/FRANKLIN_BATTERY_AUTOMATION_GUIDE.pdf)** - Step-by-step setup
- **📗 [Script Reference](docs/FRANKLIN_BATTERY_SCRIPTS.pdf)** - All scripts with sanitized templates
- **📄 [INSTALLATION.md](INSTALLATION.md)** - Detailed installation instructions

---

## 📊 Results

### Tested System Performance
- **Platform:** Synology DS1525+, DSM 7.2.2
- **Battery:** Franklin WH aPower (30 kWh)
- **Solar:** Two arrays (21.3 kW ground mount + 6.9 kW roof)
- **Utility:** PG&E E-TOU-D with CARE rates

### Typical Day Results
- **Morning SOC:** 50-70% (after overnight usage)
- **Peak Period SOC:** 95%+ (consistently charged and ready)
- **Grid Charging:** Minimal, only when solar insufficient
- **Peak Period Offset:** Full battery discharge during 5-8 PM

### Annual Savings Breakdown
- **CARE discount:** ~$693/year (35% off standard rates)
- **Battery optimization:** ~$357/year (reduced peak usage)
- **Heat pump integration:** Additional savings when replacing propane
- **Total estimated savings:** ~$1,050/year

*Your results may vary based on solar capacity, usage patterns, and local rates*

---

## 🎨 Customization

### Adjust Charge Thresholds

Edit the intelligence scripts to match your needs:

```python
# morning_solar_intelligence.py
GOOD_SOLAR_THRESHOLD = 1.5  # kW - Increase if you have more solar
LOW_SOC_THRESHOLD = 50      # % - Lower to wait longer for solar
TARGET_SOC = 95             # % - Maximum charge target

# final_safety_check.py
MINIMUM_ACCEPTABLE = 75     # % - Minimum SOC before peak period
```

### Change Peak Period Times

Current system assumes 5:00-8:00 PM peak. To change:

```python
# Update in morning_solar_intelligence.py
peak_start = datetime.now().replace(hour=17, minute=0, ...)  # Change hour=17
```

Then adjust task schedule times:
- Morning check: 9+ hours before peak
- Midday check: 3 hours before peak
- Final safety: 90 minutes before peak

### Add More Solar Systems

Modify `collect_pvoutput.py` to add additional PVOutput systems:

```python
SYSTEM_3_SID = "YOUR_THIRD_SYSTEM_ID"
SYSTEM_3_LOG = LOG_DIR / "pvoutput_system3_daily.csv"

get_and_save_daily_output(SYSTEM_3_SID, SYSTEM_3_LOG, "System 3", yesterday)
```

---

## 🐛 Troubleshooting

### Common Issues

**"Cannot read battery SOC" Error**
- Franklin Cloud API timeout (temporary)
- Check network connectivity
- Verify credentials are correct

**Task Didn't Run**
- Check Task Scheduler is enabled
- Verify script has execute permissions (`chmod +x`)
- Check Task Scheduler logs for errors

**Continuous Monitoring Stopped**
- Process may have died (check with `ps aux | grep continuous_monitor`)
- Restart: `nohup ./continuous_monitor.py > /dev/null 2>&1 &`
- Ensure boot-up task is configured

**No PVOutput Data**
- Verify API key and system IDs
- Check rate limits (60 requests/hour)
- Test manually: `./collect_pvoutput.py`

### Getting Help

1. **Check logs:** `/volume1/docker/franklin/logs/`
2. **Test scripts manually** to see error messages
3. **Open an issue** on GitHub with logs and error details

---

## 🤝 Contributing

Contributions welcome! Ways to help:

- **Report bugs** - Open an issue with details
- **Share improvements** - Submit a pull request
- **Documentation** - Fix typos, add examples
- **Testing** - Try on different platforms and report results
- **Rate schedules** - Adapt for other utility TOU schedules

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## 🙏 Credits

### API Library
This project uses the excellent [franklinwh](https://pypi.org/project/franklinwh/) Python library for Franklin WH API access.

- **Library:** https://pypi.org/project/franklinwh/
- **Source:** https://github.com/ai-joe-git/homeassistant-franklinwh
- **Author:** ai-joe-git

### Inspiration
Built to optimize battery usage for PG&E Time-of-Use rates and maximize solar-first charging strategies.

---

## 📄 License

MIT License - See [LICENSE](LICENSE) file

Copyright (c) 2026 Ken Pauley

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

---

## ⭐ Support

If this project helps you save money and optimize your Franklin WH battery, please consider:
- ⭐ **Starring this repository**
- 🐛 **Reporting issues** you encounter
- 💡 **Sharing your results** and improvements
- 📢 **Spreading the word** to other Franklin WH owners

---

## 📞 Contact

- **GitHub Issues:** [Report bugs or request features](https://github.com/YOUR-USERNAME/FranklinWH-Automation/issues)
- **Discussions:** Share your setup and results

---

**Built with ☀️ for the Franklin WH community**
