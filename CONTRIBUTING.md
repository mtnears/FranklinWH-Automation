# Contributing to FranklinWH-Automation

Thank you for your interest in contributing! This project aims to help Franklin WH battery owners optimize their systems.

## Ways to Contribute

### 🐛 Report Bugs

Found a bug? Please open an issue with:
- Description of the problem
- Steps to reproduce
- Expected vs actual behavior
- Log snippets (sanitize any credentials!)
- Your platform (Synology DSM version, Python version, etc.)

### 💡 Suggest Features

Have an idea? Open an issue tagged "enhancement" with:
- Clear description of the feature
- Use case / why it's valuable
- Example of how it would work

### 📝 Improve Documentation

Documentation improvements are always welcome:
- Fix typos or unclear instructions
- Add examples
- Improve installation guides
- Add screenshots or diagrams

### 🔧 Submit Code

Want to contribute code? Great!

1. **Fork the repository**
2. **Create a feature branch** (`git checkout -b feature/amazing-feature`)
3. **Make your changes**
4. **Test thoroughly** on your system
5. **Commit with clear messages** (`git commit -m 'Add amazing feature'`)
6. **Push to your fork** (`git push origin feature/amazing-feature`)
7. **Open a Pull Request**

### Code Guidelines

- **Maintain Python 3.11+ compatibility**
- **Follow existing code style** (similar formatting to current scripts)
- **Use the config system** — see "Configuration model (v4.6+)" below. Settings belong in `.env` / `rate_schedule.json` (the user-facing config surfaces), never hardcoded
- **Add comments** for complex logic
- **Test on your system** before submitting
- **Sanitize credentials** in any examples or logs

### Configuration model (v4.6+)

As of v4.6 there is a DB-resident configuration store (`config_store.py`) and a single canonical rate resolver (`rate_config.py`). `.env` is still how users configure the system, but new code should be aware of the consolidation so it doesn't reintroduce the divergence v4.6 exists to eliminate:

- **Reading a setting:** existing scripts read via `from config import config` (which reads `.env`). That continues to work and is fine for now. Prefer not to add *new* `os.getenv()` calls scattered across scripts — that pattern is exactly what v4.6 consolidated away.
- **Reading rates or the peak window:** use `rate_config.resolve_rates(date)` and `rate_config.peak_window_for_date(date)`. Do **not** add a new bespoke rate reader, hardcode rates, or read `PEAK_START_HOUR`/`PEAK_END_HOUR` directly for peak detection — the engine follows the rate schedule now, and a second reader is how #26 and the savings-calc rate bug happened.
- **Battery / array facts:** capacity comes from `app_config` (`battery.capacity_kwh`); which array charges the battery comes from `solar_arrays.charges_battery`. Don't hardcode capacity or assume a single array.
- If you're unsure where a value should come from, open an issue before writing the PR — the v4.6 consolidation is recent and we'd rather discuss it than untangle a new divergent path later.

### Testing Checklist

Before submitting code changes, verify:
- [ ] Scripts run without errors
- [ ] Settings are read through the config system, not hardcoded — see "Configuration model (v4.6+)" above
- [ ] No new bespoke rate readers or direct peak-hour reads — use `rate_config` for rates and the peak window
- [ ] No hardcoded credentials or personal information
- [ ] Log files and the DB are in `.gitignore` (`*.db`, `.env`, `data/rate_schedule.json`)
- [ ] Changes work on your Franklin WH system
- [ ] Documentation updated if needed (CHANGELOG.md at minimum)

## 🔐 Security

**NEVER commit:**
- Franklin WH credentials
- API keys (PVOutput, Weather Underground)
- Gateway IDs
- Personal email addresses
- IP addresses or hostnames
- Your `.env` file (it's in `.gitignore`)

**Always use the config system:**
```python
# ✓ CORRECT - use config
from config import config
client = Client(fetcher, config.FRANKLIN_GATEWAY_ID)

# ✗ WRONG - hardcoded values
GATEWAY_ID = "10060005A02X24470437"
```

**In documentation, use placeholders:**
- `your_email@example.com`
- `your_password`
- `your_api_key`
- `your_gateway_id`

## 📋 Feature Request Priority

High priority features:
- Support for additional utility rate schedules
- Additional pricing providers (ERCOT, CAISO, etc.)
- Solar forecasting integrations
- Home Assistant integration
- Enhanced web dashboard features

## 🌍 Platform Testing

Help test on different platforms:
- Different Synology NAS models
- Raspberry Pi (various models)
- Ubuntu/Debian servers
- Other Linux distributions
- Docker deployments

Report your results to help others!

## 💬 Communication

- **Questions:** Use GitHub Discussions
- **Bugs:** Open an issue
- **Features:** Open an issue tagged "enhancement"
- **Code:** Submit a Pull Request

## 📜 License

By contributing, you agree that your contributions will be licensed under the MIT License.

## 🙏 Recognition

Contributors will be recognized in the project README.

Thank you for helping improve FranklinWH-Automation!
