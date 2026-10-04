# v4.7.0-rc.4 (pre-release)

**For testers.** Franklin's cloud login has been failing since October 3. Installs that switch modes through the cloud can't change the battery mode while it's down. This version runs readings and mode changes over a **local connection** to the aGate on your home network, so it keeps working without the cloud.

## What changes for you
- **Readings and mode changes go directly to the aGate.** The engine checks every mode change took effect. It falls back to the cloud only if the local connection fails.
- **If you have Modbus on, it checks the local connection.** Every reading is compared with Modbus, and every mode change is confirmed by the mode the aGate reports over Modbus.
- **No Modbus? That's fine.** The local connection doesn't need your installer to turn anything on.
- **Your reserves are kept.** Mode changes select the aGate's own Self-Consumption / Time of Use / Emergency Backup modes, each with the reserve you set in the Franklin app.
- **If a mode change fails on every connection, the dashboard shows a red banner** and the engine retries on its next cycle.
- **Grid charging runs to its target.** Fixes a v4.6.2+ bug that stopped Emergency Backup grid charging after one 30-minute cycle.
- **New in v4.7: guided configuration review.** Your settings move from `.env` into the app's configuration store once you review and commit them. Until you do, everything keeps running on `.env` as before.

## Upgrade
```
cd <your FranklinWH-Automation folder>
git fetch
git checkout v4.7.0-rc.4
docker compose build --no-cache
docker compose down
docker compose up -d
```
Then open the dashboard. A banner offers the **configuration review**. At minimum, do the **Connection** step:
1. Enter the **aGate IP address** (the same one Modbus uses, if you have Modbus). Use the wired address if your aGate has both wired and Wi-Fi.
2. Press **Test** on **Local connection**. You should see your battery %, current mode and the modes available.
3. If you have Modbus, press **Test** on **Modbus connection** too.

If you leave the IP blank, the Test searches your network for the aGate.

The rest of the review is worth doing too, since it checks your rates and solar setup, but it isn't required for the local connection.

## Telemetry
If you've opted in, the daily report adds counts and timings for each connection: success rates, how long mode changes took to confirm, and how often local and Modbus readings agreed. It contains no addresses or error text. These numbers tell us how reliable the local connection is across installs.

## Going back
`git checkout v4.6.3`, then build and restart as above. Your `.env` is unchanged by v4.7.
