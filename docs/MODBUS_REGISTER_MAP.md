# FranklinWH aGate — Modbus TCP Register Reference

> **Last updated:** May 3, 2026
> **Project:** [FranklinWH Automation](https://github.com/mtnears/FranklinWH-Automation)

---

## Overview

This document maps the Modbus TCP registers available on the FranklinWH aGate gateway. These mappings were developed through systematic testing — polling registers via pymodbus while simultaneously reading the Franklin cloud API, then correlating values across varying conditions (nighttime idle, solar production, battery charge/discharge, peak periods, and a real grid disconnect event).

The aGate exposes a read-only Modbus TCP interface using the SunSpec 700-series DER models. This project uses these registers for fast local monitoring (26-50ms response times vs 2-7 seconds for cloud API calls), while reserving cloud API calls for mode switching operations that Modbus does not support.

### Disclaimer

These mappings are based on independent testing against a retail 2023+ aGate unit and are not officially documented or endorsed by FranklinWH. Register behavior may vary across firmware versions. **This interface should be used for read-only monitoring only.** Writing to Modbus registers is unsupported, undocumented, and risks conflicting with the cloud API's mode orchestration. Use at your own risk.

### How to Enable

Modbus TCP can be enabled in the FranklinWH mobile app via the SPAN panel toggle. No SPAN hardware is required — the toggle simply enables the Modbus TCP listener on port 502. Once enabled, any Modbus TCP client on the local network can connect.

---

## Connection Details

| Parameter | Value |
|-----------|-------|
| Protocol | Modbus TCP |
| Port | 502 |
| Unit ID | 1 (default) |
| Authentication | None |
| Access | Read-only (by default) |
| Typical response time | 26-50ms |

---

## SunSpec Model Map

The aGate uses 700-series DER models (newer SunSpec standard), not the older 101-103 inverter models.

| Model | Base Address | Length | Content |
|-------|-------------|--------|---------|
| 1 (Common) | 4 | 66 | Device identification |
| 701 (AC Measurement) | 72 | 153 | AC power, voltage, current, frequency, energy, temperatures |
| 702 (DC/Nameplate) | 227 | 50 | Inverter nameplate/capacity ratings (static) |
| 703 (Watt-Hours) | 279 | 17 | Lifetime energy accumulators |
| 713 (DER Status) | 1035 | 7 | SOC, battery DC voltage, rated power |
| 502 (Solar/PV) | 1098 | 28 | PV/solar data (mostly non-functional — see notes) |
| Extended (vendor) | 15500+ | — | Operating mode (15507), other vendor-specific registers |

---

## Confirmed Register Mappings

These registers have been validated against simultaneous cloud API reads across multiple operating conditions.

### Model 713 — DER Storage Status (Base: 1035)

| Register | Offset | Field | Scale | Unit | Notes |
|----------|--------|-------|-------|------|-------|
| 1035-1036 | 0-1 | Rated power | — | W | System rated power (e.g., 30000 = 30kW). Static. |
| **1037** | **2** | **State of Charge** | **÷ 10** | **%** | **Primary SOC reading. Value of 730 = 73.0%. Correlation r=1.000 vs cloud API, ±0.22% accuracy.** |
| 1038 | 3 | Battery DC voltage | ÷ 10 | V | e.g., 992 = 99.2V. May update less frequently than SOC. |

### Model 714 — DER DC Measurement (Base: 1042)

| Register | Offset | Field | Scale | Unit | Notes |
|----------|--------|-------|-------|------|-------|
| 1048 | 4 | Battery Watts | signed int16 | W | Negative=charging, positive=discharging. Validated within 0.3% of cloud charge totals |

### Model 701 — AC Measurement (Base: 72)

#### System State

| Register | Offset | Field | Values | Notes |
|----------|--------|-------|--------|-------|
| 73 | 1 | Operating state | `1` = operating | Only value `1` observed across all conditions |
| 74 | 2 | Inverter state | `3`, `7` | Toggles frequently during normal operation. Not a reliable state indicator on its own. See Inverter State section. |
| **75** | **3** | **Connection state** | **`1` = connected, `0` = disconnected** | **Primary grid detection register. Clean binary signal confirmed during real grid outage.** |
| **79** | **7** | **DER connect status** | **`1` = connected, `2` = islanded, `6` = transitioning** | **Secondary grid detection register. Provides transition state granularity.** |

#### Power

| Register | Offset | Field | Scale | Unit | Notes |
|----------|--------|-------|-------|------|-------|
| **80** | **8** | **Grid active power** | **direct** | **W** | **Signed 16-bit. Positive = importing from grid, negative = exporting. Correlation r=0.998 vs cloud API, ±84W accuracy.** |
| 81 | 9 | Apparent power | direct | VA | Total apparent power |
| 82 | 10 | Reactive power | direct | VAr | Signed |

> **Note (v4.2.0+):** Some register values occasionally return near-`0xFFFF` numbers (e.g., 65531, 65532) instead of the exact `0xFFFF` sentinel. These slip through naive sentinel checks and produce nonsensical readings (65 MW solar, 65 MW load). The collector now applies sanity bounds — `MAX_PLAUSIBLE_SOLAR_W = 25,000` and `MAX_PLAUSIBLE_LOAD_W = 50,000` — and discards values above these thresholds as Modbus errors.

#### Voltage & Frequency

| Register | Offset | Field | Scale | Unit | Notes |
|----------|--------|-------|-------|------|-------|
| 85 | 13 | Voltage L-L | ÷ 10 | V | Line-to-line. e.g., 2509 = 250.9V |
| 86 | 14 | Voltage L-N | ÷ 10 | V | Same as L-L on single split-phase systems |
| **88** | **16** | **Frequency** | **÷ 1000** | **Hz** | **e.g., 59900 = 59.9Hz, 60000 = 60.0Hz** |

#### Temperature

| Register | Offset | Field | Scale | Unit | Notes |
|----------|--------|-------|-------|------|-------|
| **105** | **33** | **Ambient temp** | **÷ 10** | **°C** | **Outdoor/ambient. e.g., 36 = 3.6°C** |
| **106** | **34** | **Cabinet temp** | **÷ 10** | **°C** | **Internal cabinet. Varies with load and ambient.** |
| 107 | 35 | Heatsink temp | ÷ 10 | °C | May not be populated on all units |
| 108 | 36 | Transformer temp | ÷ 10 | °C | May not be populated on all units |

#### Per-Phase Data (Split-Phase 240V)

| Register | Offset | Field | Scale | Unit |
|----------|--------|-------|-------|------|
| 109 | 37 | Watts L1 | direct | W |
| 110 | 38 | VA L1 | direct | VA |
| 111 | 39 | VAr L1 | direct | VAr |
| 113 | 41 | Amps L1 | raw | — |
| 114 | 42 | Voltage L1-L2 | ÷ 10 | V |
| 115 | 43 | Voltage L1-N | ÷ 10 | V |

### Model 702 — DC Nameplate/Ratings (Base: 227)

Static values representing inverter capacity. These do not change during operation.

| Field | Observed Value | Notes |
|-------|---------------|-------|
| DC rated power | 20,000 W | 20kW inverter capacity |
| DC rated VA | 20,000 VA | |
| DC voltage max | 16,000 | Scale factor uncertain |
| DC current rated | 23,000 | Scale factor uncertain |

### Model 703 — Energy Accumulators (Base: 279)

| Register | Offset | Field | Notes |
|----------|--------|-------|-------|
| 280 | 1 | Wh total injected | Lifetime energy exported. Did not increment during 10h test — may need longer observation or different scale factor |
| 281 | 2 | Wh total absorbed | Lifetime energy imported |
| 283 | 4 | Wh injected L1 | Per-phase |
| 285 | 6 | Wh absorbed L1 | Per-phase |
| 287 | 8 | Wh injected L2 | Per-phase |
| 289 | 10 | Wh absorbed L2 | Returns 0xFFFF — likely not implemented |

### Model 502 — Solar/PV (Base: 1098)

**Status: Largely non-functional.** Most registers return zero even during active solar production. The aGate does not appear to expose PV data through this model. Solar production data should be sourced from the solar inverter directly (e.g., Enphase local API, SolarEdge API) or from a service like PVOutput.

### Extended Registers — Operating Mode (15500 Block)

These registers are outside the standard SunSpec model chain and appear to be vendor-specific FranklinWH extensions.

| Register | Offset | Field | Scale | Unit | Notes |
|----------|--------|-------|-------|------|-------|
| 15502 | 2 | Solar Production | raw | W | ~43% of readings return 0xFFFF (65535) — spike filter >25000W required. Validated <0.1% of cloud solar |
| 15506 | 6 | Home Consumption | raw | W | Validated within 1–5% of cloud home load. Spike filter <25000W |
| 15507 | 7 | OnGrid Mode | raw | 0=Never observed<br>1=Backup<br>2=Self Consumption<br>3=TOU | Current operating mode. Confirmed matching mode selected/displayed in Franklin app. Used for local mode verification to eliminate routine cloud API polling. |
| 15508 | 8 | Active mode backup reserve | raw | % | Reflects current active mode's reserve setting — automatically goes to 100% in backup/storm hedge mode |
| 15509 | 9 | Active mode backup reserve | raw | % | 	Identical to 15508 in all observed conditions — purpose distinction unknown |

**Discovery note:** Register 15507 was identified through systematic polling of the 15500-15600 range while switching modes via the cloud API. Values change within seconds of a mode switch command. This register is used by `data_sources.py` for mode verification, reducing cloud API calls from every 30 minutes to only on actual mode switches (~2-4/day).

---

## Grid Disconnect Detection

Validated during a real grid outage on February 17, 2026, captured with 10-second Modbus polling.

### Detection Registers

| Priority | Register | Offset | Connected | Disconnected | Notes |
|----------|----------|--------|-----------|-------------|-------|
| **Primary** | 75 | 3 | `1` | `0` | Clean binary. Use this. |
| Secondary | 79 | 7 | `1` | `2` (island), `6` (transition) | Extra state detail |
| Supporting | 80 | 8 | Normal watts | 5-7W (noise) | Not reliable alone |

### Observed Event Sequence

```
Time       State       Voltage   Grid Power  conn_state  off7
─────────────────────────────────────────────────────────────────
19:09:13   NORMAL      250.9V    74W         1           1
19:09:24   COLLAPSE    14.4V     -32W        0           6      ← Grid lost
19:09:34   BLACKOUT    0.0V      0W          0           6
19:09:44   PROBING     72.0V     0W          0           6      ← System testing
19:09:54   ISLANDED    253.6V    6W          0           2      ← Battery inverter active
  ...      ISLAND      251-256V  5-7W        0           2      ← Stable (~5.5 min)
19:15:04   RECONNECT   250.5V    5412W       1           1      ← Grid returns
19:15:24   SETTLED     252.5V    79W         1           1      ← Normal (~20s settling)
```

### Key Observations

- **Anti-islanding:** Even when the battery is actively powering the house (e.g., during TOU peak scheduling with zero grid draw), the system remains grid-tied (`conn_state=1`). A grid outage still causes a ~30-second blackout while the system detects the loss, opens the transfer switch, and restarts the inverter in standalone mode. This is standard IEEE 1547 anti-islanding behavior, not specific to FranklinWH.
- **Island mode voltage:** Battery inverter generates slightly elevated voltage (253-256V vs normal ~250V) and locks frequency to exactly 60.000Hz.
- **Reconnection surge:** ~5,400W inrush peak, decaying to normal over ~20 seconds.

### Inverter State Values

| Value | Interpretation | Notes |
|-------|---------------|-------|
| 7 | Grid-tied active | Common during normal operation |
| 3 | Standalone / reduced | Seen in island mode, also common during normal operation |

The inverter state (register 74) toggles between `3` and `7` frequently during normal grid-connected operation (32 transitions observed across 10 hours of logging). **It is not a reliable grid detection indicator.** Use `conn_state` (register 75) instead.

---

## Registers Not Yet Mapped

| Target | Status | Notes |
|--------|--------|-------|
| ~~Battery power (charge/discharge kW)~~ | **Found** | Register 1048 - see Sunspec Model 714 section above; confirmed with ~300k modbus readings compared to cloud API values |
| ~~Home load (kW)~~ | **Found** | Register 15506 (offset 6) — see Extended Registers section above; confirmed with ~300k modbus readings compared to cloud API values |
| ~~Operating mode~~ | **Found** | Register 15507 — see Extended Registers section above. |
| ~~Reserve SOC setting~~ | **Found** | Registers 15508/9 (offset 8/9) — see Extended Registers section above; confirmed with ~300k modbus readings compared to cloud API values |
| ~~Solar production~~ | **Found** | Register 15502 (offset 2) — see Extended Registers section above; confirmed with ~300k modbus readings compared to cloud API values and inverter reported values |
| Per-battery SOC | Not available | Only aggregate SOC at register 1037. Individual battery SOC requires cloud API. |

---

## How This Project Uses Modbus

This project uses a hybrid architecture: Modbus for fast, frequent monitoring and the Franklin cloud API for mode switching only.

```
Modbus TCP (local, 26-50ms)           Cloud API (remote, 2-7s)
├── SOC monitoring                     ├── Mode switching (TOU/SC/EB)
├── Grid power tracking                ├── Per-battery SOC
├── Grid disconnect detection          ├── Reserve SOC changes
├── Temperature monitoring             └── (fallback mode verification)
├── Voltage / frequency
├── Mode verification (reg 15507)
└── Real-time dashboard updates
```

Before issuing any cloud API mode switch, the automation checks grid status via Modbus:

```python
conn_state = read_register(75)  # Model 701, offset 3
if conn_state == 0:
    log("Grid disconnected — skipping mode switch")
    return
# Safe to proceed with cloud API mode switch
```

Mode verification uses register 15507 to confirm the current operating mode locally, eliminating the need for routine cloud API polling. The cloud API is now used only for actual mode switch commands (~2-4/day) and as a fallback if extended registers are unavailable.

---

## Quick Start

```python
from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient('AGATE_IP', port=502)
client.connect()

# SOC
regs = client.read_holding_registers(1035, count=7)
soc = regs.registers[2] / 10.0  # percent

# Grid status and power
regs = client.read_holding_registers(72, count=50)
grid_connected = regs.registers[3] == 1
grid_watts = regs.registers[8]
if grid_watts > 32767:
    grid_watts -= 65536  # signed 16-bit
frequency = regs.registers[16] / 1000.0
voltage = regs.registers[13] / 10.0
ambient_c = regs.registers[33] / 10.0
cabinet_c = regs.registers[34] / 10.0

# Operating mode (extended register)
regs = client.read_holding_registers(15507, count=1, device_id=1)
mode_map = {0: 'backup', 1: 'tou', 2: 'self_consumption', 3: 'manual'}
mode = mode_map.get(regs.registers[0], 'unknown')

client.close()
```

---

## References

- [SunSpec Alliance](https://sunspec.org/) — Modbus model specifications
- [Franklin PICS Spreadsheet](https://certifications.sunspec.org/PICS/Franklin_PICS_SM000028_1.xlsx) — Protocol Implementation Conformance Statement
- [pymodbus](https://pymodbus.readthedocs.io/) — Python Modbus library
- [HA SunSpec Integration](https://github.com/CJNE/ha-sunspec) — Home Assistant SunSpec HACS integration
