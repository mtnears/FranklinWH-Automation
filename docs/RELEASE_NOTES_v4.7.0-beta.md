# v4.7.0-beta — Configuration review (beta)

**Who this is for:** existing installs that want to move their settings out of `.env` and into the app. New installs should stay on `main` (v4.6.x) until this beta is merged.

v4.7 makes the configuration store the source of truth. You go through a guided **configuration review** once: every setting is shown with what `.env` says, what the store says and what is in effect now, you confirm or adjust each one, and committing switches the system to run on the reviewed configuration. After that, `.env` only holds credentials, API keys and deployment values (ports, paths, timezone).

Nothing changes until you commit, and switching back to `.env` is one button.

## Upgrading to the beta

```bash
git fetch
git checkout v4.7-config-review
git pull
docker compose build --no-cache
docker compose down
docker compose up -d
```

Then open the dashboard. A banner asks you to take the review; **Settings → Configuration review** opens it too. Plan on about 10 minutes.

You don't need to run `migrate_v46.py` first. If your install never ran it, the review imports `data/rate_schedule.json` when you commit.

### Before you start
- **Print or fill in the Setup Checklist.** It lists every setting the review asks about, what each is for and where to find it, with room to note your values. It ships with the install as `docs/SETUP_CHECKLIST.pdf` and is linked from the review page, the dashboard banner and Settings (served at `/SETUP_CHECKLIST.pdf`). Fill it in on screen in any PDF reader and save a copy, or print it.
- **If your rate file was set up for CARE (or another discount), check the Rates and Utility & billing steps carefully.** The review shows your plan's prices and windows exactly as the engine will use them. If you have left the discount program, or your utility's prices changed since you wrote `rate_schedule.json`, fix it here. The Billing step warns you when the discount setting disagrees with your latest rate history.
- **Commit outside the peak window.** Commits are refused from 20 minutes before peak until 5 minutes after it ends, so a restart can never land on a peak transition.

## What the review does
- **11 steps:** Connection, Battery, Solar arrays, Rates, Utility & billing, Engine behavior, Forecast/weather/site, Integrations, Telemetry, Advanced, Summary.
- **Pre-filled from your current setup.** Where `.env` and the store disagree, `.env` is pre-selected and both values are shown.
- **Every value is checked on the server** before anything is written: type, allowed choices and ranges, and every solar array must be classified (does it charge the battery, does it export).
- **Live Test buttons** for the aGate (Modbus), the Envoy, the Franklin cloud login, SolarEdge, PVOutput and Weather Underground. Tests run on the server using the secrets in `.env`, and report pass or fail with a short message. They never send a secret to the browser.
- **Secrets stay in `.env`.** The review shows whether each one is set; it never displays them. To change one, edit `.env`, run `docker compose up -d`, then come back and press Test.
- **Commit:**
  1. saves a backup of the current configuration to `data/backups/config-snapshot-*.json`;
  2. writes everything in one transaction;
  3. checks that the store now holds exactly what you reviewed and that your rate plan resolves today's prices and peak window. If either check fails, the backup is restored and nothing changes.
  4. switches the system to the store and restarts the automation in place, which takes about 10–30 seconds. The container keeps running and no decision or collector run is cut off.
- **Values left at their defaults are not stored**, so a future default improvement reaches you automatically.
- **Switch back to `.env`:** a button on the review's last step, or `docker exec franklin-automation python3 /app/scripts/config_admin.py authority env` followed by a restart. The store is kept, so you can commit again later. To restore an earlier configuration from a backup: `config_admin.py restore <config-snapshot-file> --yes`.

## Also on this branch
- **Learned solar day (replaces `SUN_SCHEDULE`).** The engine's fixed month table put the end of solar production about 55 minutes late on average, which affected evening and pre-peak decisions. Each install now learns its own production start, ramp and end relative to the real sunrise and sunset, from its own data over the last 30 productive days. That handles terrain, trees and clock changes automatically. The solar cap used to bound forecasts is learned the same way, and never exceeds the configured AC capacity.
- **Forecast history.** Evening-before and morning forecasts, the actual production, and the corrections applied are now kept in a permanent `forecast_history` table. The decision log is pruned at 30 days, which made forecast accuracy hard to review. This history is the groundwork for a forecast calibration fix in a later release.
- **Install values come from settings.** The forecast array, site location, battery capacity and solar cap now come from your settings and detected hardware instead of constants in the code.
- **`rate_schedule.json` errors are reported.** A JSON syntax error in the file is logged with its line and column, instead of the rate schedule failing silently.
- **Update badge.** The dashboard no longer offers an older release as an "update" when you run a newer or beta version.
- **Telemetry** (opt-in installs only) adds three fields: `config_authority`, `review_completed` and `review_completed_version`. These show how the beta rollout is going.

## Known limitations
- **The rate plan can't be restructured in the review.** You can change prices and window times, but adding or removing tiers, windows or seasons waits for the setup wizard. Window times are shared across seasons when your plan defines them that way, and the page says when that applies.
- **Rate history overrides peak and off-peak prices.** If a `rate_history` row is in effect, its peak and off-peak prices take precedence over the plan's. When they differ, the Rates step says so.
- **`config_admin.py sync` overwrites some reviewed edits.** It keeps settings and rate plans you edited in the review, but refreshes array fields from `.env`. After committing the review there is normally no reason to run `sync`.

## Feedback
Please report problems on the issue tracker and include the **System Logs** lines around `REVIEW COMMITTED` or `Review commit refused`.
