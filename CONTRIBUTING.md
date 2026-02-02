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
- **Use the config system** - All settings should be in `.env`, not hardcoded
- **Add comments** for complex logic
- **Test on your system** before submitting
- **Sanitize credentials** in any examples or logs

### Testing Checklist

Before submitting code changes, verify:
- [ ] Scripts run without errors
- [ ] Scripts use `from config import config` for settings
- [ ] No hardcoded credentials or personal information
- [ ] Log files are in `.gitignore`
- [ ] Changes work on your Franklin WH system
- [ ] Documentation updated if needed

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
