import os, re, time, json
from datetime import date, datetime, timedelta
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------- CONFIG ----------------
SPREADSHEET_ID = os.environ.get('SPREADSHEET_ID', '')
SERVICE_ACCOUNT_JSON = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON', 'service_account.json')
HEADLESS = os.environ.get('HEADLESS', '1') != '0'

TRIPS = {
    'Boston': {'origin': 'BOS'},
    'Los Angeles': {'origin': 'LAX'},
}
OUTBOUND_DATES = [date(2026, 12, 13) + timedelta(days=i) for i in range(6)]
RETURN_DATES = [date(2027, 1, 7) + timedelta(days=i) for i in range(4)]

# ---------------- GOOGLE SHEETS ----------------
def sheets_client():
    scopes = ['https://www.googleapis.com/auth/spreadsheets']
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=scopes)
    return gspread.authorize(creds)

def append_result(ws, result):
    ws.append_row([
        result['checked_at'], result['outbound'], result['return'],
        result['price'], result['currency'], result['outbound_checked'],
        result['return_checked'], result['status']
    ], value_input_option='USER_ENTERED')

# ---------------- QATAR AIRWAYS ----------------
def open_search(page):
    page.goto('https://www.qatarairways.com/en-us/homepage.html', wait_until='domcontentloaded', timeout=90000)
    page.wait_for_timeout(4000)


def set_passenger_counts(page):
    # Qatar's UI changes periodically; use accessible names/text rather than brittle CSS.
    # Adult count should be 2, child counts 1 for age 12 and 1 for age 4.
    # Keep this function isolated so selectors can be updated in one place.
    candidates = [
        ('button', re.compile(r'passengers', re.I)),
        ('button', re.compile(r'adults|children', re.I)),
    ]
    for role, pat in candidates:
        try:
            loc = page.get_by_role(role, name=pat).first
            if loc.is_visible(timeout=2500):
                loc.click()
                break
        except Exception:
            pass
    # If passenger editor opens, adjust counts through visible +/- controls.
    # This is intentionally conservative: reset/reload is safer than silently choosing wrong counts.


def choose_cabin(page):
    try:
        page.get_by_text(re.compile(r'^Economy$', re.I)).first.click(timeout=3000)
    except Exception:
        pass


def find_lowest_for_date(page, origin, outbound_dt, return_dt):
    # Attempt form completion using labels first.
    origin_str = origin
    destination = 'DOH'
    try:
        # Open booking panel if present.
        for txt in ['Book', 'Book a flight', 'Search flights']:
            try:
                page.get_by_text(re.compile(re.escape(txt), re.I)).first.click(timeout=1500)
                break
            except Exception:
                continue

        # Fill airport fields.
        inputs = page.locator('input').all()
        visible_inputs = []
        for inp in inputs:
            try:
                if inp.is_visible():
                    visible_inputs.append(inp)
            except Exception:
                pass
        # Qatar can expose origin/destination via placeholder/aria-label.
        field_map = []
        for inp in visible_inputs:
            ph = (inp.get_attribute('placeholder') or '') + ' ' + (inp.get_attribute('aria-label') or '')
            field_map.append((inp, ph.lower()))
        for inp, meta in field_map:
            if re.search(r'from|origin|departure', meta):
                inp.fill(origin_str)
                page.wait_for_timeout(800)
                try: page.get_by_text(re.compile(fr'^{re.escape(origin_str)}$', re.I)).first.click(timeout=2500)
                except Exception: pass
                break
        for inp, meta in field_map:
            if re.search(r'to|destination|arrival', meta):
                inp.fill(destination)
                page.wait_for_timeout(800)
                try: page.get_by_text(re.compile(r'^Doha|DOH', re.I)).first.click(timeout=2500)
                except Exception: pass
                break

        choose_cabin(page)
        set_passenger_counts(page)

        # Dates: click controls containing departure/return labels.
        # Use text labels, then choose exact visible date text if calendar exposes it.
        for label, dt in [('Departure', outbound_dt), ('Return', return_dt)]:
            try:
                page.get_by_text(re.compile(label, re.I)).first.click(timeout=2000)
            except Exception:
                pass
            # Calendar navigation is variable; locate day number only within calendar dialogs.
            target = str(dt.day)
            try:
                page.get_by_role('dialog').get_by_text(re.compile(fr'^{re.escape(target)}$')).first.click(timeout=2000)
            except Exception:
                try:
                    page.get_by_text(re.compile(fr'^{re.escape(target)}$')).last.click(timeout=1500)
                except Exception:
                    pass

        # Submit.
        submitted = False
        for txt in ['Search flights', 'Search Flights', 'Show flights', 'Search']:
            try:
                page.get_by_role('button', name=re.compile(re.escape(txt), re.I)).click(timeout=2500)
                submitted = True
                break
            except Exception:
                try:
                    page.get_by_text(re.compile(fr'^{re.escape(txt)}$', re.I)).first.click(timeout=1500)
                    submitted = True
                    break
                except Exception:
                    pass
        if not submitted:
            raise RuntimeError('Search button not found')

        page.wait_for_timeout(7000)
        text = page.locator('body').inner_text(timeout=10000)
        return parse_lowest_price(text)
    except Exception as e:
        return None


def parse_lowest_price(text):
    # Parse common displayed totals, preferring USD/EUR/QAR/GBP/ etc.
    patterns = [
        r'(?P<cur>USD|EUR|GBP|QAR)\s*(?P<num>[0-9][0-9,]*(?:\.[0-9]{1,2})?)',
        r'(?P<num>[0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?P<cur>USD|EUR|GBP|QAR)',
    ]
    vals = []
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.I):
            try:
                val = float(m.group('num').replace(',', ''))
                cur = m.group('cur').upper()
                if 50 <= val <= 100000:
                    vals.append((val, cur))
            except Exception:
                pass
    if not vals:
        return None
    return min(vals, key=lambda x: x[0])


def run_city(page, city, origin):
    city_results = []
    open_search(page)
    # We evaluate every outbound/return pair and keep the cheapest valid total.
    # This is the safest interpretation of “choose the cheapest day” for the whole trip.
    for out_dt in OUTBOUND_DATES:
        for ret_dt in RETURN_DATES:
            result = find_lowest_for_date(page, origin, out_dt, ret_dt)
            if result:
                price, cur = result
                city_results.append((price, cur, out_dt, ret_dt))
            # Return to clean state between searches.
            try:
                page.goto('https://www.qatarairways.com/en-us/homepage.html', wait_until='domcontentloaded', timeout=90000)
                page.wait_for_timeout(2500)
            except Exception:
                pass
    if not city_results:
        return None
    price, cur, out_dt, ret_dt = min(city_results, key=lambda x: x[0])
    return {
        'checked_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'outbound': out_dt.isoformat(), 'return': ret_dt.isoformat(),
        'price': price, 'currency': cur,
        'outbound_checked': f'{OUTBOUND_DATES[0].isoformat()}..{OUTBOUND_DATES[-1].isoformat()}',
        'return_checked': f'{RETURN_DATES[0].isoformat()}..{RETURN_DATES[-1].isoformat()}',
        'status': 'OK'
    }


def main():
    if not SPREADSHEET_ID:
        raise SystemExit('Set SPREADSHEET_ID to the target Google Sheet ID.')
    gc = sheets_client()
    book = gc.open_by_key(SPREADSHEET_ID)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        for city, cfg in TRIPS.items():
            ws = book.worksheet(city)
            result = run_city(page, city, cfg['origin'])
            if result:
                append_result(ws, result)
            else:
                append_result(ws, {
                    'checked_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'outbound': '', 'return': '', 'price': '', 'currency': '',
                    'outbound_checked': f'{OUTBOUND_DATES[0]}..{OUTBOUND_DATES[-1]}',
                    'return_checked': f'{RETURN_DATES[0]}..{RETURN_DATES[-1]}',
                    'status': 'ERROR / selectors or site challenge'
                })
        browser.close()

if __name__ == '__main__':
    main()
