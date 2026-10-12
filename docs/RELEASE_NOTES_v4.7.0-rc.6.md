# v4.7.0-rc.6 (pre-release)

**For testers already on rc.5.** A fix-up release before v4.7.0. Nothing changes in how the engine decides when to charge, hold or discharge.

## What changes for you
- **Big grid or battery flows read correctly.** Above 32.7 kW, Modbus power readings used to wrap around (a heavy import could show as export). Mostly matters for three-plus aPowers, EV charging or a large generator.
- **Daily energy totals are right after midnight.** The first reading of a day no longer carries the previous day's totals into it.
- **Battery health (SoH) shows as SoH** instead of being stored as battery voltage.
- **PVOutput fills in missed days by itself** if uploads stopped for a while.
- **Setup checklist updated** for the local connection. Modbus is optional; you don't need your installer to turn anything on.
- **Fresh builds work again.** pymodbus 3.16.0 came out and fails to import on our image (it needs `pyserial`), so a `--no-cache` build took the engine down. rc.6 pins pymodbus to 3.15.0.
- **Using Home Assistant's FranklinWH integration with the same Franklin account?** The two sign each other out. See Troubleshooting: give the automation its own Franklin account or app user.

## Upgrade
```
cd <your FranklinWH-Automation folder>
git fetch
git checkout v4.7.0-rc.6
docker compose build --no-cache
docker compose down
docker compose up -d
```
Your configuration review and settings carry over; there's nothing to redo.

## Afterwards
- The dashboard footer shows **4.7.0-rc.6**.
- The next morning, yesterday's row in the daily energy summary should match the totals in the Franklin app closely.

## Telemetry
If you've opted in, two things change in the daily report:
- **Peak protection** now measures grid energy used during your peak window. Earlier versions counted grid *charging* days, which made the figure meaningless for installs that grid-charge on purpose.
- **Guided review edits:** how many settings you changed in the review compared with your `.env` or the defaults, by category, and whether you edited rates. Counts only, no values.

## Going back
rc.5 doesn't pin pymodbus, so a fresh build of it pulls 3.16.0 and won't start. Pin it while you build, then put the file back:
```
git checkout v4.7.0-rc.5
sed -i.bak 's/^pymodbus>=3.5.0/pymodbus==3.15.0/' requirements.txt
docker compose build --no-cache
docker compose down
docker compose up -d
git checkout -- requirements.txt
rm requirements.txt.bak
```
