#!/usr/bin/env python3
"""
build_setup_checklist.py — generate docs/SETUP_CHECKLIST.pdf

A fillable PDF that helps a new (or reviewing) user gather everything the
configuration review / setup wizard will ask for: required vs optional,
what each item is for, and where to find it. Fields can be typed into in
any PDF reader, or the page printed and filled by hand. Each item has a
"Matches review" box so testers can gather blind, then check the values
against the review screens.

Item order follows the review screens. Each item names the registry
key(s)/env name(s) it covers; running this script reports any
user-facing registry setting the checklist does not cover, so the two stay
in step.

    python3 tools/build_setup_checklist.py            # writes docs/SETUP_CHECKLIST.pdf
    python3 tools/build_setup_checklist.py out.pdf

Needs reportlab (pip install reportlab).
"""

import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (BaseDocTemplate, Flowable, Frame, KeepTogether, PageTemplate,
                                Paragraph, Spacer, Table, TableStyle)

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / 'VERSION').read_text().strip() if (ROOT / 'VERSION').exists() else ''
TITLE = 'FranklinWH-Automation Setup Checklist'

# ---------------------------------------------------------------------------
# Content. Tags: R = required, RC = recommended, O = optional, C = conditional
# (required only when the item above it applies).
# kind: text | secret | choice | yesno | note
# ---------------------------------------------------------------------------

SECTIONS = [
  {
    'title': 'Before you start',
    'intro': 'The automation runs in Docker on a computer that stays on around the clock '
             '(a NAS, mini PC or Raspberry Pi-class machine) on the same home network as '
             'your FranklinWH aGate.',
    'items': [
      dict(id='host', label='Machine that will run the automation', tag='R', kind='text',
           what='Runs the container 24/7. It needs Docker with Compose, and it must be on the '
                'same local network as the aGate.',
           where='Any always-on computer you control. Note its name or IP so you can open the '
                 'dashboard at http://&lt;that address&gt;:8100 afterwards.'),
      dict(id='tz', label='Time zone', tag='R', kind='text', keys=['TZ'],
           what='Peak windows, schedules and daily rollups all run on local time.',
           where='Your IANA time zone name, for example America/Los_Angeles or America/Chicago. '
                 'Set in the Docker environment, not in the review.'),
    ]
  },
  {
    'title': '1. Connection',
    'intro': 'Your FranklinWH account, and how the automation talks to the aGate.',
    'items': [
      dict(id='fwh_user', label='FranklinWH account email', tag='R', kind='text',
           keys=['FRANKLIN_USERNAME'],
           what='The automation signs in to the FranklinWH cloud as a backup when the local '
                'connection is unavailable. A new sign-in ends the previous one on the same '
                'account, so if you also run Home Assistant\'s FranklinWH integration, give '
                'this tool its own FranklinWH account (or app user) or the two keep signing '
                'each other out.',
           where='The email you use to log in to the FranklinWH app, or the separate user you '
                 'added for the automation.'),
      dict(id='fwh_pass', label='FranklinWH account password', tag='R', kind='secret',
           keys=['FRANKLIN_PASSWORD'],
           what='Used with the email above. It stays in the .env file on your machine; the '
                'review only shows whether it is set.',
           where='Your FranklinWH app password.'),
      dict(id='fwh_gw', label='aGate gateway ID', tag='R', kind='text',
           keys=['FRANKLIN_GATEWAY_ID'],
           what='Identifies which aGate on your account to control. A 20-character '
                'code such as 10060005A02X24470437.',
           where='FranklinWH app: Settings, then System Info, then Gateway ID (menu names can '
                 'vary by app version). It is also the aGate serial on the unit label.'),
      dict(id='agate_ip', label='aGate IP address', tag='RC', kind='text', keys=['modbus.host'],
           what='Where to reach the aGate on your home network. Used by the local connection '
                'and by Modbus.',
           where="Your router's list of connected devices / DHCP clients; look for the FranklinWH "
                 'or aGate entry. Give it a DHCP reservation in the router so the address never '
                 'changes. A wired (Ethernet) aGate is more reliable than Wi-Fi. If you leave it '
                 'blank, the review\'s Test button searches your network for the aGate.'),
      dict(id='local_on', label='Local connection turned on?', tag='RC', kind='yesno',
           keys=['local.enabled'],
           what='The automation reads the aGate and changes modes directly over your home '
                'network. It keeps working when the FranklinWH cloud is down; the cloud is only '
                'a backup. On by default. Strongly recommended.',
           where='Nothing to set up in the FranklinWH app. In the review, press Test on the '
                 'Connection step to check it reaches your aGate.'),
      dict(id='modbus_on', label='Modbus turned on?', tag='O', kind='yesno',
           keys=['modbus.enabled'],
           what='A second local channel (port 502). It double-checks every reading and mode '
                'change from the local connection and fills in grid voltage, frequency and '
                'temperatures. Optional: the local connection works without it.',
           where='Modbus is not a setting most owners can reach in the FranklinWH app. If you '
                 'want it, ask your installer or FranklinWH support whether Modbus TCP can be '
                 'enabled on your aGate. Leave it off otherwise.'),
    ]
  },
  {
    'title': '2. Battery',
    'intro': 'Battery count and capacity are detected automatically. Write them down so you can '
             'confirm the detection is right.',
    'items': [
      dict(id='bat_count', label='Number of aPower batteries', tag='R', kind='text',
           keys=['battery.count'],
           what='Sizes every charge and discharge plan. Detected from the aGate; you only override '
                'it if detection is wrong.',
           where='FranklinWH app device list, or your installation paperwork.'),
      dict(id='bat_model', label='Battery model', tag='R', kind='choice',
           choices=['aPower (original)', 'aPower 2', 'Not sure'], keys=['battery.capacity_kwh'],
           what='Sets usable capacity per unit (aPower 2 is 13.6 kWh each). Total capacity is '
                'detected; this lets you check it.',
           where='Unit label, installer paperwork, or the FranklinWH app device details.'),
      dict(id='app_reserve', label='Backup reserve set in the FranklinWH app (%)', tag='R',
           kind='text', keys=['battery.backup_reserve_pct'],
           what='The charge kept for outages. The Franklin app is the source of truth; the engine '
                'plans never to discharge below it.',
           where='FranklinWH app, in the operating mode / reserve settings.'),
      dict(id='enforce_reserve', label='Hold a fixed reserve no matter what the app says?',
           tag='O', kind='text', keys=['battery.reserve_soc_home', 'battery.reserve_soc_backup'],
           what='Leave blank to let the app decide. Only set a value if several people change the '
                'app, or a utility / VPP program resets it; the automation then sends this value '
                'on every mode switch and overrides the app.',
           where='Your choice. Write the percentage, or leave blank.'),
    ]
  },
  {
    'title': '3. Solar arrays',
    'intro': 'Fill one block per array. The key question is whether an array is wired into the '
             'aGate (it can charge the battery) or on its own meter (it only offsets the bill). '
             'If you have no solar, skip this section.',
    'repeat': 2,
    'items': [
      dict(id='arr_name', label='Array name', tag='R', kind='text', keys=['NAME'],
           what='A short name you will recognize on the dashboard, such as House or Barn.',
           where='Your choice.'),
      dict(id='arr_brand', label='Brand / monitoring type', tag='R', kind='choice',
           choices=['Enphase', 'SolarEdge', 'Other / none'], keys=['TYPE'],
           what='Decides how production is read: Enphase from the local gateway, SolarEdge from '
                'the monitoring portal.',
           where='Inverter or gateway labels, or your installer paperwork.'),
      dict(id='arr_charges', label='Charges the battery?', tag='R', kind='yesno',
           keys=['charges_battery', 'features.solar', 'solar.capacity_kw'],
           what='Only arrays connected to the aGate solar input charge the battery. These drive the '
                'forecast and the engine. An array on a separate meter never charges the battery.',
           where='Your installer or single-line diagram: is the array connected to the aGate, or '
                 'to its own meter/panel?'),
      dict(id='arr_exports', label='Exports surplus to the grid?', tag='R', kind='yesno',
           keys=['exports'],
           what='Whether this array can send surplus power to the grid (some battery-charging '
                'arrays are non-export by design).',
           where='Interconnection agreement or installer.'),
      dict(id='arr_ac', label='AC size (kW)', tag='R', kind='text', keys=['CAPACITY_KW'],
           what='Inverter output limit. Used for sanity checks and the dashboard.',
           where='Sum of inverter or microinverter AC ratings, from the permit set, the monitoring '
                 'app, or installer paperwork.'),
      dict(id='arr_dc', label='DC size (kWp)', tag='C', kind='text', keys=['forecast.house_kwp'],
           cond='Needed for a battery-charging array.',
           what='Panel nameplate total (panel count x panel watts). This is the solar forecast '
                'input.',
           where='Permit plans or installer proposal: number of panels times the panel wattage.'),
      dict(id='arr_tilt', label='Tilt (degrees)', tag='C', kind='text', keys=['forecast.house_tilt'],
           cond='Battery-charging array.',
           what='Panel angle from flat. Shapes the forecast curve.',
           where='Permit plans, or estimate from roof pitch (a 4/12 roof is about 18 degrees, '
                 '6/12 about 27).'),
      dict(id='arr_az', label='Azimuth (degrees, 0 = south)', tag='C', kind='text',
           keys=['forecast.house_azimuth'], cond='Battery-charging array.',
           what='Direction the panels face: 0 south, -90 east, 90 west. A wrong azimuth skews '
                'the forecast toward morning or afternoon.',
           where='Permit plans, or a compass / satellite map of the roof face.'),
      dict(id='arr_ip', label='Enphase: gateway (Envoy / IQ Gateway) IP', tag='C', kind='text',
           keys=['IP'], cond='Enphase arrays.',
           what='Production is read locally from the Enphase gateway.',
           where="Router's device list (often shows as Envoy). Reserve the address in the router."),
      dict(id='arr_serial', label='Enphase: gateway serial number', tag='C', kind='text',
           keys=['SERIAL'], cond='Enphase arrays.',
           what='Needed to request the local access token.',
           where='Label on the gateway, or the Enphase app / Enlighten device list.'),
      dict(id='arr_login', label='Enphase: Enlighten login email', tag='C', kind='text',
           keys=['EMAIL'], cond='Enphase arrays.',
           what='Used once in a while to fetch the local gateway token. Stays in .env.',
           where='The account you use for the Enphase app / Enlighten.'),
      dict(id='arr_pass', label='Enphase: Enlighten password', tag='C', kind='secret',
           keys=['PASSWORD'], cond='Enphase arrays.',
           what='Used with the email above. Stays in .env.', where='Your Enlighten password.'),
      dict(id='arr_model', label='Enphase: microinverter model', tag='O', kind='text',
           keys=['MODEL'],
           what='Shown on the dashboard and used for per-panel health scaling (for example IQ8MC).',
           where='Enphase app device list or the microinverter labels.'),
      dict(id='arr_site', label='SolarEdge: site ID', tag='C', kind='text',
           keys=['SITE_ID', 'SOLAREDGE_SITE_ID'], cond='SolarEdge arrays.',
           what='Identifies your system in the SolarEdge monitoring portal.',
           where='SolarEdge monitoring portal: the number in the site details or the site URL.'),
      dict(id='arr_apikey', label='SolarEdge: API key', tag='C', kind='secret',
           keys=['API_KEY'], cond='SolarEdge arrays.',
           what='Lets the automation read production from the portal. Stays in .env.',
           where='Monitoring portal, site Admin, then Site Access, API access (site admin rights '
                 'needed; if your installer owns the site, ask them).'),
    ]
  },
  {
    'title': '4. Rates',
    'intro': 'The rate plan on the meter the battery is behind. Peak windows for the engine come '
             'from here. Most time-of-use plans have summer and winter seasons with different '
             'prices, and sometimes different windows.',
    'items': [
      dict(id='utility', label='Utility and rate plan name', tag='R', kind='text',
           what='Which plan the engine optimizes for (for example PG&amp;E EV2-A).',
           where='Your bill, usually labelled "Rate schedule" on the electric charges page.'),
      dict(id='rate_grid', label='Prices and windows by season', tag='R', kind='rategrid',
           keys=['features.tou', 'PEAK_START_HOUR', 'PEAK_END_HOUR', 'PEAK_DAYS'],
           what='Price per kWh for each tier, and the clock times each tier applies. The engine '
                'charges before peak and covers the peak window from the battery.',
           where="Your utility's published tariff sheet for the plan (search the plan name plus "
                 '"tariff"). Use total delivered price per kWh if your bill splits generation and '
                 'delivery. Note weekday-only windows.'),
      dict(id='holidays', label='Holidays treated as off-peak?', tag='O', kind='yesno',
           what='Some plans price listed holidays as off-peak all day.',
           where='Tariff sheet, usually a footnote under the time periods.'),
    ],
    # Official tariff sources. Add a utility only after checking its URL.
    'resources_title': 'Where to find your official rate schedule',
    'resources_intro': 'Your bill names the rate schedule (for example EV2-A or E-TOU-D). The '
                       "utility's tariff document for that schedule is the authority for prices, "
                       'windows and seasons; compare the review against it. PG&amp;E is listed '
                       'below; links for other utilities will be added as we find them.',
    'resources': [
      ('PG&amp;E (California)',
       'https://www.pge.com/tariffs/en.html#ELECTRIC%20RATE%20SCHEDULES',
       'Open Electric Rate Schedules. Each residential schedule is a PDF (for example EV2 or '
       'E-TOU-D); the price table and the time periods are on the first pages.'),
    ],
  },
  {
    'title': '5. Utility & billing',
    'items': [
      dict(id='discount', label='Discount program on this meter', tag='R', kind='choice',
           choices=['None', 'CARE', 'FERA', 'Other'], keys=['billing.discount_program'],
           what='Income-qualified discounts change the price per kWh. The discounted prices go in '
                'the rate plan; this records which program applies.',
           where='Your bill lists the program if you are enrolled. Programs end; check it is '
                 'still active.'),
      dict(id='export_comp', label='How exports are credited', tag='R', kind='choice',
           choices=['Net metering (1:1 retail)', 'Net billing (NEM3-style)', 'No export'],
           keys=['billing.export_compensation'],
           what='Tells the engine whether exported energy is worth close to retail or much less. '
                'In California, NEM 1 and NEM 2 are net metering; NEM 3 / NBT is net billing.',
           where='Your interconnection approval letter, or the NEM line on your bill.'),
      dict(id='multi_meter', label='Second meter billed together with this one?', tag='O',
           kind='yesno', keys=['billing.multi_meter'],
           what='For aggregate billing, such as a separately metered array whose exports offset '
                'this account.',
           where='Your bill shows multiple service IDs / meters under one account.'),
    ]
  },
  {
    'title': '6. Engine behavior',
    'intro': 'Choices rather than facts. The defaults suit most installs; note what you would like.',
    'items': [
      dict(id='strategy', label='Strategy for surplus solar', tag='R', kind='choice',
           choices=['Self-consumption (store it)', 'Export (sell it)'], keys=['solar.export'],
           what='Self-consumption keeps surplus solar in the battery and caps grid charging so '
                'solar can finish the fill. Export skips that and lets surplus go to the grid for '
                'credit. Non-export systems and net billing usually want self-consumption.',
           where='Your decision. Match it to your export credit (section 5).'),
      dict(id='target', label='Highest charge from solar (%)', tag='O', kind='text',
           keys=['engine.target_soc'], default='95',
           what='How full solar may fill the battery.', where='Leave at the default unless you '
           'have a reason.'),
      dict(id='taper', label='Highest charge from the grid (%)', tag='O', kind='text',
           keys=['engine.taper_ceiling_pct'], default='85',
           what='Grid charging stops here. Above it the charge rate tapers, so solar is left to '
                'finish. Lower it if you see solar being curtailed on full-battery days.',
           where='Leave at the default to start.'),
    ]
  },
  {
    'title': '7. Forecast, weather & site',
    'items': [
      dict(id='latlon', label='Site latitude, longitude', tag='R', kind='text',
           keys=['forecast.latitude', 'forecast.longitude', 'features.forecast'],
           what='Drives the solar forecast (free Open-Meteo service, no key needed). Two decimal '
                'places is plenty.',
           where='Right-click your home in Google Maps; the first line is latitude, longitude.'),
      dict(id='wu_station', label='Weather Underground station ID', tag='O', kind='text',
           keys=['weather.station_id', 'features.weather'],
           what='A personal weather station (PWS) near you improves forecast calibration. It can be '
                'your own station or any nearby public one; the review test shows how far it is '
                'from your site.',
           where='wunderground.com/wundermap: pick a station close to you, for example KCAGEORG2. '
                 'Without a station and key, leave this off.'),
      dict(id='wu_key', label='Weather Underground API key', tag='C', kind='secret',
           keys=['WEATHER_API_KEY'], cond='Needed if you use a station.',
           what='Lets the automation read the station. Stays in .env.',
           where='wunderground.com/member/api-keys (free keys are tied to owning a station).'),
    ]
  },
  {
    'title': '8. Integrations (all optional)',
    'items': [
      dict(id='pv_key', label='PVOutput API key', tag='O', kind='secret',
           keys=['PVOUTPUT_API_KEY', 'features.pvoutput'],
           what='Uploads daily production to PVOutput.org. Stays in .env.',
           where='pvoutput.org/account.jsp (enable API access there).'),
      dict(id='pv_ids', label='PVOutput system ID(s)', tag='C', kind='text',
           keys=['pvoutput.system_ids'], cond='If you use PVOutput.',
           what='One ID per array, in the same order as your arrays.',
           where='pvoutput.org, your systems list (the "sid" number).'),
      dict(id='se_portal', label='SolarEdge portal login (panel health)', tag='O', kind='text',
           keys=['SOLAREDGE_USERNAME', 'features.solaredge_panel_monitoring'],
           what='Per-optimizer health and degradation tracking from the SolarEdge portal. This is '
                'the portal login, not the API key.',
           where='The email you use for monitoring.solaredge.com.'),
      dict(id='se_pass', label='SolarEdge portal password', tag='C', kind='secret',
           keys=['SOLAREDGE_PASSWORD'], cond='If you use panel health.',
           what='Stays in .env.', where='Your SolarEdge portal password.'),
      dict(id='se_modbus', label='SolarEdge inverters: local Modbus IPs', tag='O', kind='text',
           keys=['solaredge.modbus_enabled', 'solaredge.modbus_inverters'],
           what='Reads SolarEdge inverters directly on your network (SunSpec Modbus TCP, usually '
                'port 1502). Only if Modbus TCP is enabled on the inverters.',
           where='Enabled with SolarEdge SetApp or by your installer; IPs from the router device list.'),
      dict(id='dyn', label='Real-time (hourly) pricing provider', tag='O', kind='text',
           keys=['features.dynamic_pricing', 'pricing.provider', 'pricing.threshold_cents',
                 'pricing.ceiling_cents', 'pricing.solar_override_cents'],
           what='Only for hourly-priced plans such as ComEd. Most time-of-use customers leave this off.',
           where='Your utility plan documents.'),
    ]
  },
  {
    'title': '9. Telemetry',
    'items': [
      dict(id='telemetry', label='Share anonymous usage telemetry?', tag='O', kind='yesno',
           keys=['telemetry.enabled', 'telemetry.region'],
           what='A daily anonymous summary that helps the project see which features and '
                'configurations are in use. No credentials, IPs, serials or exact location.',
           where='Your choice.'),
    ]
  },
]

ADVANCED_NOTE = ('Advanced settings (polling and decision intervals, retries, timeouts, debug '
                 'logging, the v3 legacy options) have safe defaults and are not on this list. '
                 'The review shows them under Advanced if you want to look.')

TAG_TEXT = {'R': 'Required', 'RC': 'Recommended', 'O': 'Optional', 'C': 'If applies'}
TAG_COLOR = {'R': colors.HexColor('#b42318'), 'RC': colors.HexColor('#b54708'),
             'O': colors.HexColor('#475467'), 'C': colors.HexColor('#175cd3')}

INK = colors.HexColor('#101828')
MUTED = colors.HexColor('#475467')
RULE = colors.HexColor('#d0d5dd')
FIELD_BG = colors.HexColor('#f5f8ff')
FIELD_BORDER = colors.HexColor('#98a2b3')
HEAD_BG = colors.HexColor('#eef2f6')

sty = {
    'title': ParagraphStyle('t', fontName='Helvetica-Bold', fontSize=18, leading=22, textColor=INK),
    'sub': ParagraphStyle('s', fontName='Helvetica', fontSize=9.5, leading=13, textColor=MUTED),
    'h2': ParagraphStyle('h2', fontName='Helvetica-Bold', fontSize=13, leading=16, textColor=INK,
                         spaceBefore=6, spaceAfter=2),
    'intro': ParagraphStyle('i', fontName='Helvetica', fontSize=9, leading=12, textColor=MUTED,
                            spaceAfter=4),
    'label': ParagraphStyle('l', fontName='Helvetica-Bold', fontSize=9.5, leading=12, textColor=INK),
    'small': ParagraphStyle('sm', fontName='Helvetica', fontSize=8, leading=10.2, textColor=MUTED),
    'tag': ParagraphStyle('tag', fontName='Helvetica-Bold', fontSize=7.5, leading=9),
    'body': ParagraphStyle('b', fontName='Helvetica', fontSize=9.5, leading=13, textColor=INK,
                           alignment=TA_LEFT),
    'cell': ParagraphStyle('c', fontName='Helvetica', fontSize=8.5, leading=10.5, textColor=INK),
}


# ---------------------------------------------------------------------------
# Form field flowables (AcroForm; coordinates relative to the flowable)
# ---------------------------------------------------------------------------

class TextField(Flowable):
    def __init__(self, name, width, height=16, tooltip='', multiline=False):
        super().__init__()
        self.name, self.width, self.height = name.replace('.', '_'), width, height
        self.tooltip, self.multiline = tooltip, multiline

    def draw(self):
        self.canv.acroForm.textfield(
            name=self.name, tooltip=self.tooltip or self.name, x=0, y=0,
            width=self.width, height=self.height, relative=True, fontName='Helvetica',
            fontSize=9 if not self.multiline else 8.5, borderWidth=0.6,
            borderColor=FIELD_BORDER, fillColor=FIELD_BG, textColor=INK, forceBorder=True,
            fieldFlags='multiline' if self.multiline else '')


class CheckBox(Flowable):
    def __init__(self, name, size=10, tooltip=''):
        super().__init__()
        self.name, self.size, self.tooltip = name.replace('.', '_'), size, tooltip
        self.width = self.height = size

    def draw(self):
        self.canv.acroForm.checkbox(
            name=self.name, tooltip=self.tooltip or self.name, x=0, y=0, size=self.size,
            relative=True, buttonStyle='check', borderWidth=0.6, borderColor=FIELD_BORDER,
            fillColor=colors.white, textColor=INK, forceBorder=True)


def boxed(label_text, name, tip=''):
    """A checkbox with its label, as a small inline table."""
    t = Table([[CheckBox(name, tooltip=tip or label_text), Paragraph(label_text, sty['cell'])]],
              colWidths=[13, None], hAlign='LEFT')
    t.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                           ('LEFTPADDING', (0, 0), (-1, -1), 0),
                           ('RIGHTPADDING', (0, 0), (-1, -1), 2),
                           ('TOPPADDING', (0, 0), (-1, -1), 1),
                           ('BOTTOMPADDING', (0, 0), (-1, -1), 1)]))
    return t


def choice_row(prefix, options, width):
    cells = [boxed(o, f'{prefix}.{i}', o) for i, o in enumerate(options)]
    per = width / max(len(cells), 1)
    t = Table([cells], colWidths=[per] * len(cells), hAlign='LEFT')
    t.setStyle(TableStyle([('LEFTPADDING', (0, 0), (-1, -1), 0),
                           ('RIGHTPADDING', (0, 0), (-1, -1), 2),
                           ('TOPPADDING', (0, 0), (-1, -1), 0),
                           ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
                           ('VALIGN', (0, 0), (-1, -1), 'MIDDLE')]))
    return t


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

PAGE_W, PAGE_H = letter
MARGIN = 0.6 * inch
CONTENT_W = PAGE_W - 2 * MARGIN
COL_ITEM = 2.25 * inch
COL_TAG = 1.02 * inch
COL_MATCH = 0.62 * inch
COL_VALUE = CONTENT_W - COL_ITEM - COL_TAG - COL_MATCH


def value_cell(item, fid):
    k = item['kind']
    w = COL_VALUE - 8
    if k in ('text', 'secret'):
        parts = [TextField(fid, w, tooltip=item['label'])]
        if k == 'secret':
            parts.append(Spacer(1, 2))
            parts.append(boxed('I have this (safer: tick this instead of writing it down)',
                               fid + '.have'))
        elif item.get('default'):
            parts.append(Paragraph(f"Default {item['default']}", sty['small']))
        return parts
    if k == 'yesno':
        return [choice_row(fid, ['Yes', 'No', 'Not sure'], min(w, 2.4 * inch))]
    if k == 'choice':
        return [choice_row(fid, item['choices'], w)]
    return []


def rate_grid(fid):
    """Two seasons x tiers with price and window fields."""
    head = ['Season (months)', 'Tier', 'Price (cents/kWh)', 'Window(s), e.g. 16:00-21:00 weekdays']
    widths = [1.35 * inch, 1.0 * inch, 1.05 * inch, CONTENT_W - 3.4 * inch - 12]
    rows = [[Paragraph(h, sty['tag']) for h in head]]
    tiers = ['Peak', 'Partial-peak', 'Off-peak']
    for s in range(2):
        for i, tier in enumerate(tiers):
            rows.append([
                TextField(f'{fid}.s{s}.name', widths[0] - 6, tooltip='Season name and months')
                if i == 0 else '',
                Paragraph(tier, sty['cell']),
                TextField(f'{fid}.s{s}.{i}.price', widths[2] - 6, tooltip=f'{tier} price'),
                TextField(f'{fid}.s{s}.{i}.win', widths[3] - 6, tooltip=f'{tier} windows'),
            ])
    t = Table(rows, colWidths=widths, hAlign='LEFT')
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), HEAD_BG),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LINEBELOW', (0, 3), (-1, 3), 0.8, RULE),
        ('SPAN', (0, 1), (0, 3)), ('SPAN', (0, 4), (0, 6)),
        ('VALIGN', (0, 1), (0, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ('LEFTPADDING', (0, 0), (-1, -1), 3), ('RIGHTPADDING', (0, 0), (-1, -1), 3),
        ('BOX', (0, 0), (-1, -1), 0.5, RULE),
    ]))
    return t


def item_block(item, fid):
    tag = item['tag']
    tag_p = Paragraph(f'<font color="{TAG_COLOR[tag].hexval()}">{TAG_TEXT[tag].upper()}</font>',
                      sty['tag'])
    label = [Paragraph(item['label'], sty['label'])]
    if item.get('cond'):
        label.append(Paragraph(item['cond'], sty['small']))
    match = CheckBox(fid + '.match', tooltip='Matches what the review shows')
    top_value = value_cell(item, fid) if item['kind'] != 'rategrid' else \
        [Paragraph('Use the grid below.', sty['small'])]
    top = Table([[label, tag_p, top_value, match]],
                colWidths=[COL_ITEM, COL_TAG, COL_VALUE, COL_MATCH], hAlign='LEFT')
    top.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (3, 0), (3, 0), 'CENTER'),
        ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 5), ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
    ]))
    detail = Paragraph(f"<b>What it's for:</b> {item['what']}<br/>"
                       f"<b>Where to find it:</b> {item['where']}", sty['small'])
    dt = Table([[detail]], colWidths=[CONTENT_W], hAlign='LEFT')
    dt.setStyle(TableStyle([('LEFTPADDING', (0, 0), (-1, -1), 4),
                            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                            ('TOPPADDING', (0, 0), (-1, -1), 0),
                            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                            ('LINEBELOW', (0, 0), (-1, -1), 0.5, RULE)]))
    parts = [top]
    if item['kind'] == 'rategrid':
        parts += [Spacer(1, 2), rate_grid(fid), Spacer(1, 4)]
    parts.append(dt)
    return parts


def column_header():
    t = Table([[Paragraph('Item', sty['tag']), Paragraph('Need', sty['tag']),
                Paragraph('Your information', sty['tag']),
                Paragraph('Matches review', sty['tag'])]],
              colWidths=[COL_ITEM, COL_TAG, COL_VALUE, COL_MATCH], hAlign='LEFT')
    t.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), HEAD_BG),
                           ('ALIGN', (3, 0), (3, 0), 'CENTER'),
                           ('LEFTPADDING', (0, 0), (-1, -1), 4),
                           ('TOPPADDING', (0, 0), (-1, -1), 3),
                           ('BOTTOMPADDING', (0, 0), (-1, -1), 3)]))
    return t


def intro_page():
    s = []
    s.append(Paragraph(TITLE, sty['title']))
    s.append(Paragraph(f'For the configuration review and first-time setup · version {VERSION}',
                       sty['sub']))
    s.append(Spacer(1, 10))
    body = [
        'Use this list to gather everything the setup asks for before you start. Each item says '
        'whether it is required, what it is for, and where to find it. The same explanations '
        'appear in the (i) pop-ups on the review screens.',
        '<b>Filling it in.</b> Type into the boxes in any PDF reader (Adobe Reader, Preview, a '
        'browser) and save a copy, or print it and write by hand. Everything is optional to write '
        'down; the point is to have it at hand.',
        '<b>Passwords and keys.</b> Items marked with an "I have this" box are credentials. They '
        'stay in the .env file on your machine and the review never displays them. It is safer to '
        'tick the box than to write the value here. If you do write them, keep this file private.',
        '<b>Have this ready before you commit.</b> The setup refuses to save until every '
        'required item is entered, because the engine cannot run safely on a guess: your '
        'FranklinWH login and gateway ID; the site latitude and longitude; the number of '
        'batteries and their total capacity (detected when the aGate is reachable); how your '
        'exports are credited; for each solar array its name, type, AC size and connection '
        'details; and your rate plan with every month of the year in a season, each season\'s '
        'prices, and the time windows when each price applies. A feature you turn on (weather, '
        'PVOutput, SolarEdge portal, solar forecasting) needs its own key or details too.',
        '<b>Checking against the review.</b> After you gather the information, open the dashboard, '
        'go to Settings and choose <i>Configuration review</i>. Step through the screens and tick '
        '<i>Matches review</i> for each item that shows what you expected. Nothing is saved until '
        'you press Commit on the last step (<i>Preview</i> never saves).',
    ]
    for b in body:
        s.append(Paragraph(b, sty['body']))
        s.append(Spacer(1, 6))
    legend = Table([[Paragraph(f'<font color="{TAG_COLOR[k].hexval()}">{TAG_TEXT[k].upper()}</font>',
                               sty['tag']), Paragraph(v, sty['cell'])] for k, v in (
        ('R', 'The system cannot be set up without it.'),
        ('RC', 'Works without it, but strongly advised.'),
        ('O', 'Adds a feature; skip if you do not use it.'),
        ('C', 'Required only when the condition shown under the item applies.'))],
        colWidths=[1.1 * inch, 4.5 * inch], hAlign='LEFT')
    legend.setStyle(TableStyle([('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                                ('TOPPADDING', (0, 0), (-1, -1), 2),
                                ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
                                ('LEFTPADDING', (0, 0), (-1, -1), 0)]))
    s.append(Spacer(1, 4))
    s.append(legend)
    s.append(Spacer(1, 10))
    return s


def build(out_path: Path):
    doc = BaseDocTemplate(str(out_path), pagesize=letter, leftMargin=MARGIN, rightMargin=MARGIN,
                          topMargin=MARGIN, bottomMargin=0.7 * inch, title=TITLE,
                          author='FranklinWH-Automation', subject='Setup checklist')
    frame = Frame(MARGIN, 0.7 * inch, CONTENT_W, PAGE_H - MARGIN - 0.7 * inch, id='f',
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)

    def footer(canv, d):
        canv.saveState()
        canv.setFont('Helvetica', 7.5)
        canv.setFillColor(MUTED)
        canv.drawString(MARGIN, 0.45 * inch, f'{TITLE} · v{VERSION}')
        canv.drawRightString(PAGE_W - MARGIN, 0.45 * inch, f'Page {d.page}')
        canv.restoreState()

    doc.addPageTemplates([PageTemplate(id='p', frames=[frame], onPage=footer)])
    story = intro_page()
    for sec in SECTIONS:
        reps = sec.get('repeat', 1)
        for r in range(reps):
            title = sec['title'] + (f' - array {r + 1}' if reps > 1 else '')
            head = [Paragraph(title, sty['h2'])]
            if sec.get('intro') and r == 0:
                head.append(Paragraph(sec['intro'], sty['intro']))
            head.append(column_header())
            first = sec['items'][0]
            prefix = f"{sec['title'].split('.')[0].strip().lower().replace(' ', '_')}"
            fid0 = f'{prefix}.{r}.{first["id"]}' if reps > 1 else f'{prefix}.{first["id"]}'
            story.append(KeepTogether(head + item_block(first, fid0)))
            for it in sec['items'][1:]:
                fid = f'{prefix}.{r}.{it["id"]}' if reps > 1 else f'{prefix}.{it["id"]}'
                story.append(KeepTogether(item_block(it, fid)))
            if sec.get('resources') and r == 0:
                res = [Spacer(1, 4), Paragraph(sec['resources_title'], sty['label']),
                       Paragraph(sec['resources_intro'], sty['small'])]
                for name, url, note in sec['resources']:
                    res.append(Paragraph(f'<b>{name}:</b> <link href="{url}" color="blue">'
                                         f'<u>{url}</u></link><br/>{note}', sty['small']))
                story.append(KeepTogether(res))
            story.append(Spacer(1, 6))
    story.append(Paragraph('Advanced settings', sty['h2']))
    story.append(Paragraph(ADVANCED_NOTE, sty['intro']))
    story.append(Spacer(1, 6))
    story.append(KeepTogether([
        Paragraph('Notes (testers: anything missing, unclear, or different in the review)',
                  sty['h2']),
        TextField('notes', CONTENT_W, height=140, tooltip='Notes', multiline=True)]))
    doc.build(story)


# ---------------------------------------------------------------------------
# Coverage check against the settings registry
# ---------------------------------------------------------------------------

def coverage():
    sys.path.insert(0, str(ROOT / 'scripts'))
    try:
        import settings
    except Exception as e:
        return f'(registry not importable: {e})'
    covered = set()
    for sec in SECTIONS:
        for it in sec['items']:
            covered.update(it.get('keys', []))
    missing = []
    for e in settings.REGISTRY:
        if e.cls in (settings.SETTING, settings.DETECTED) and not (e.advanced or e.legacy):
            if e.key not in covered:
                missing.append(e.key)
        elif e.cls == settings.SECRET and e.env[0] not in covered:
            missing.append(e.env[0])
    for fld, (cls, _, _) in settings.ARRAY_FIELDS.items():
        if cls in (settings.SETTING, settings.SECRET) and fld not in covered:
            missing.append(f'SOLAR_ARRAY_*_{fld}')
    return missing


if __name__ == '__main__':
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / 'docs' / 'SETUP_CHECKLIST.pdf'
    build(out)
    miss = coverage()
    print(f'wrote {out}')
    if isinstance(miss, str):
        print(miss)
    elif miss:
        print('registry items not covered by the checklist: ' + ', '.join(miss))
    else:
        print('coverage: every user-facing registry setting and secret is on the checklist')
