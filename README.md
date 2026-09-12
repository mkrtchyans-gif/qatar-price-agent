# Qatar Airways → Google Sheets agent

## What it does
Checks Qatar Airways for two routes:
- Boston (BOS) → Doha (DOH) → Boston (BOS)
- Los Angeles (LAX) → Doha (DOH) → Los Angeles (LAX)

Passengers: 2 adults + child age 12 + child age 4, Economy.

Outbound dates checked: 13–18 December 2026.
Return dates checked: 7–10 January 2027.

The script evaluates the date combinations and writes the cheapest valid result to the matching Google Sheet tab.

## 1. Create the Google Sheet
Open `qatar_airways_price_tracker.xlsx` in Google Sheets. Keep the two tabs named exactly:
- Boston
- Los Angeles

Then copy the Google Sheet ID from the browser URL.

## 2. Google service account
Create a Google Cloud service account with Google Sheets API enabled. Download the JSON credentials as `service_account.json` into this folder. Share the Google Sheet with the service account email as Editor.

## 3. Install
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## 4. Configure
macOS/Linux:
```bash
export SPREADSHEET_ID='YOUR_GOOGLE_SHEET_ID'
export GOOGLE_SERVICE_ACCOUNT_JSON='service_account.json'
```

For the first run, use a visible browser so you can see whether Qatar Airways shows a cookie, location, login, or anti-bot screen:
```bash
export HEADLESS=0
python agent.py
```
After selectors are confirmed, you can use headless mode:
```bash
export HEADLESS=1
python agent.py
```

## Important
Qatar Airways can change its website UI and may present anti-bot or consent screens. The file isolates most UI interaction in `find_lowest_for_date()` so those selectors can be updated without changing the Google Sheets part.

For daily runs, schedule `python agent.py` with cron, launchd, GitHub Actions, or another scheduler.
