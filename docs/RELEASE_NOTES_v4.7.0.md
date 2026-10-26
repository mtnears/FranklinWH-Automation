# v4.7.0

v4.7 leaves beta. **New installs are set up in the dashboard** (the guided setup), and existing installs keep the configuration review they already completed. Nothing changes in how the engine decides when to charge, hold or discharge.

## New install
1. Copy `.env.minimal.example` to `.env` and fill in your FranklinWH login, gateway ID and time zone (plus solar array logins or API keys if you have solar).
2. `docker compose build --no-cache && docker compose up -d`, then open `http://<host>:8100`.
3. Follow the **Setup is not complete** banner. The guided setup asks for your site, battery, solar arrays, rate plan and the features you want, with a Test button for each connection. Until you commit it the engine observes only and changes no battery modes.

Have your electricity bill to hand: the setup refuses to save until every required item is entered, including your full rate plan (seasons, each season's prices, and the time windows when each price applies). The Setup Checklist (`docs/SETUP_CHECKLIST.pdf`, also linked from the dashboard) lists each item and where to find it.

## Upgrading from v4.7.0-rc.x
```
cd <your FranklinWH-Automation folder>
git fetch
git checkout v4.7.0
docker compose build --no-cache
docker compose down
docker compose up -d
```
Your review and settings carry over; there is nothing to redo. If your plan was imported from `rate_schedule.json`, the Rates step now marks any season price that was copied from the plan's base prices as **inherited**; check those against your tariff once.

## Upgrading from v4.6.x
Same commands. The dashboard then asks you to take the configuration review (about 10 minutes): every setting is checked once and then managed by the app instead of `.env`. Your `.env` and `rate_schedule.json` are imported; nothing is lost.

## What changed since rc.6
- **Rate plan editor.** Seasons, tiers and time windows can be added and removed in the review, with templates (flat, two-tier, three-tier with summer and winter). The plan must cover every month, price every tier it uses in every season, and have no overlapping windows; the review says exactly what is missing. A plan built here has no base prices, so nothing is inherited between seasons.
- **The engine reads rates from the store.** Prices and windows edited in the review are what the engine runs on; `rate_schedule.json` is only a fallback for installs that never migrated.
- **Required settings are enforced.** Site coordinates, battery count and capacity (when they cannot be detected), export compensation, each array's name, type, size and connection details, and the key for any feature you switch on. The commit lists what is missing.
- **Observe-only until setup is complete.** A new install never runs on default peak hours.
- **Dependencies pinned** to the versions the rc.6 image resolved to, and the base image pinned to Python 3.11.17, so a fresh build reproduces the tested image.
- `.env.minimal.example`, install docs and the Setup Checklist updated for new installs.

## How this release was tested
The clean-install path was tested in simulation: a throwaway container with a fresh database was fed the maintainer's production configuration through the guided setup's commit endpoint, and the resulting store, solar arrays, rate plan and effective configuration were diffed against production (identical). A second profile (two batteries, no solar, no Modbus, one weekday peak window) was run the same way. The engine code is the same code the maintainer has run since rc.6, with the rate source change above verified against the production plan. The guided setup screens were walked once by hand as a new user. Please report anything the setup refuses or gets wrong as a GitHub issue with the message it showed.

## Going back
`git checkout v4.7.0-rc.6`, rebuild as above. The store is kept; the review can be committed again later.
