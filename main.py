"""
Flight price tracker -> Google Sheets.

Data source: Travelpayouts (Aviasales Data API).
No browser, no scraping, no anti-bot problems.

Required env vars:
    TP_TOKEN                      Travelpayouts API token
    SPREADSHEET_ID                Google Sheets ID
    GOOGLE_SERVICE_ACCOUNT_JSON   path to service account json (default: service_account.json)
    CITY                          one of ROUTES keys

Optional env vars:
    CURRENCY        default "usd"
    MARKET          default "us"
    AIRLINE_FILTER  e.g. "QR" to keep only Qatar Airways. Empty = all carriers.
    MAX_STOPS       default "2"
    REQUEST_DELAY   seconds between API calls, default "1.5"
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials

# --------------------------------------------------------------------------
# CONFIG — edit this block
# --------------------------------------------------------------------------

ROUTES = {
    "Boston": {
        "origin": "MOW",        # MOW = SVO + DME + VKO together
        "destination": "BOS",
        "sheet": "Boston",
    },
    "Los Angeles": {
        "origin": "MOW",
        "destination": "LAX",
        "sheet": "Los Angeles",
    },
}

OUTBOUND_DATES = [date(2026, 12, 13) + timedelta(days=i) for i in range(6)]
RETURN_DATES = [date(2027, 1, 7) + timedelta(days=i) for i in range(4)]

# --------------------------------------------------------------------------

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"

TP_TOKEN = os.environ.get("TP_TOKEN", "").strip()
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json").strip()
CITY = os.environ.get("CITY", "").strip()

CURRENCY = os.environ.get("CURRENCY", "usd").strip().lower()
MARKET = os.environ.get("MARKET", "ru").strip().lower()
AIRLINE_FILTER = os.environ.get("AIRLINE_FILTER", "").strip().upper()
MAX_STOPS = int(os.environ.get("MAX_STOPS", "2"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "1.5"))

HEADERS = [
    "checked_at", "origin", "destination", "depart", "return",
    "price", "currency", "airline", "flight", "stops_out", "stops_back",
    "duration_h", "found_at", "link", "status",
]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fail(msg):
    log(f"FATAL: {msg}")
    sys.exit(1)


# --------------------------------------------------------------------------
# Travelpayouts
# --------------------------------------------------------------------------

def fetch_pair(session, origin, destination, depart, ret):
    """Return list of offers for one (depart, return) pair. Never raises."""
    params = {
        "origin": origin,
        "destination": destination,
        "departure_at": depart.isoformat(),
        "return_at": ret.isoformat(),
        "one_way": "false",
        "direct": "false",
        "currency": CURRENCY,
        "market": MARKET,
        "sorting": "price",
        "limit": 30,
        "page": 1,
    }

    for attempt in range(1, 4):
        try:
            r = session.get(API_URL, params=params, timeout=30)
        except requests.RequestException as e:
            log(f"  network error ({e}); retry {attempt}/3")
            time.sleep(3 * attempt)
            continue

        if r.status_code == 429:
            wait = 10 * attempt
            log(f"  rate limited; sleeping {wait}s")
            time.sleep(wait)
            continue

        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:200]}"

        try:
            body = r.json()
        except ValueError:
            return None, f"bad JSON: {r.text[:200]}"

        if not body.get("success", False):
            return None, f"API error: {body.get('error')}"

        return body.get("data") or [], None

    return None, "gave up after 3 attempts"


def pick_best(offers):
    """Cheapest offer matching the airline / stops filters."""
    best = None
    for o in offers:
        if AIRLINE_FILTER and (o.get("airline") or "").upper() != AIRLINE_FILTER:
            continue
        if (o.get("transfers") or 0) > MAX_STOPS:
            continue
        if (o.get("return_transfers") or 0) > MAX_STOPS:
            continue
        price = o.get("price")
        if price is None:
            continue
        if best is None or price < best["price"]:
            best = o
    return best


def to_row(checked_at, origin, destination, depart, ret, offer, status):
    if offer is None:
        return [checked_at, origin, destination, depart.isoformat(), ret.isoformat(),
                "", CURRENCY.upper(), "", "", "", "", "", "", "", status]

    link = offer.get("link") or ""
    if link.startswith("/"):
        link = "https://www.aviasales.com" + link

    duration = offer.get("duration")
    duration_h = round(duration / 60, 1) if isinstance(duration, (int, float)) else ""

    return [
        checked_at,
        origin,
        destination,
        depart.isoformat(),
        ret.isoformat(),
        offer.get("price", ""),
        CURRENCY.upper(),
        offer.get("airline", ""),
        offer.get("flight_number", ""),
        offer.get("transfers", ""),
        offer.get("return_transfers", ""),
        duration_h,
        offer.get("found_at", ""),
        link,
        status,
    ]


# --------------------------------------------------------------------------
# Google Sheets
# --------------------------------------------------------------------------

def open_sheet(sheet_name):
    creds = Credentials.from_service_account_file(
        SA_JSON, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    book = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
    try:
        ws = book.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        log(f"Sheet '{sheet_name}' not found, creating it")
        ws = book.add_worksheet(title=sheet_name, rows=2000, cols=len(HEADERS))

    first = ws.row_values(1)
    if first[:1] != HEADERS[:1]:
        ws.update(range_name="A1", values=[HEADERS])
        ws.freeze(rows=1)
    return ws


def previous_minimums(ws):
    """{checked_at: min price} from history already in the sheet."""
    try:
        records = ws.get_all_values()[1:]
    except Exception as e:
        log(f"Could not read history: {e}")
        return {}
    runs = {}
    for r in records:
        if len(r) < 6 or not r[5]:
            continue
        try:
            price = float(str(r[5]).replace(",", "."))
        except ValueError:
            continue
        stamp = r[0]
        runs[stamp] = min(runs.get(stamp, price), price)
    return runs


def trend_report(history, today_min):
    """Compare today's cheapest against previous runs."""
    if not history:
        return "first run — no history to compare yet", None
    stamps = sorted(history)
    prev = history[stamps[-1]]
    all_time = min(history.values())
    delta = today_min - prev
    pct = (delta / prev * 100) if prev else 0
    arrow = "DOWN" if delta < 0 else ("UP" if delta > 0 else "FLAT")
    line = (f"{arrow} {delta:+.0f} ({pct:+.1f}%) vs last run {prev:.0f}; "
            f"all-time low in sheet {all_time:.0f} over {len(stamps)} runs")
    return line, (today_min <= all_time)


def write_summary(city, rows, best_row, trend="", is_new_low=None):
    """GitHub Actions job summary — readable result without opening the sheet."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    priced = [r for r in rows if r[5] != ""]
    lines = [
        f"## {city} — {len(priced)}/{len(rows)} pairs priced",
        "",
    ]
    if best_row:
        lines += [
            f"**Cheapest: {best_row[5]} {best_row[6]}** — "
            f"{best_row[3]} → {best_row[4]}, {best_row[7]}, "
            f"{best_row[9]}/{best_row[10]} stops",
            "",
            ("### NEW LOW" if is_new_low else "Trend"),
            "",
            f"`{trend}`",
            "",
        ]
    lines += ["| Depart | Return | Price | Airline | Stops |", "|---|---|---|---|---|"]
    for r in sorted(priced, key=lambda x: x[5])[:15]:
        lines.append(f"| {r[3]} | {r[4]} | {r[5]} {r[6]} | {r[7]} | {r[9]}/{r[10]} |")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------

def main():
    if not TP_TOKEN:
        fail("Missing TP_TOKEN")
    if not SPREADSHEET_ID:
        fail("Missing SPREADSHEET_ID")
    if CITY not in ROUTES:
        fail(f"CITY must be one of: {', '.join(ROUTES)}")
    if not os.path.exists(SA_JSON):
        fail(f"Service account file not found: {SA_JSON}")

    route = ROUTES[CITY]
    origin, destination = route["origin"], route["destination"]
    pairs = [(o, r) for o in OUTBOUND_DATES for r in RETURN_DATES]

    log(f"{CITY}: {origin} -> {destination}, {len(pairs)} date pairs, "
        f"currency={CURRENCY}, airline_filter={AIRLINE_FILTER or 'ANY'}")

    ws = open_sheet(route["sheet"])
    history = previous_minimums(ws)
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    session = requests.Session()
    session.headers.update({"X-Access-Token": TP_TOKEN})

    rows, errors = [], 0
    for i, (depart, ret) in enumerate(pairs, 1):
        offers, err = fetch_pair(session, origin, destination, depart, ret)
        if err:
            errors += 1
            log(f"  [{i}/{len(pairs)}] {depart} / {ret}: {err}")
            rows.append(to_row(checked_at, origin, destination, depart, ret, None, err))
        else:
            best = pick_best(offers)
            if best:
                log(f"  [{i}/{len(pairs)}] {depart} / {ret}: "
                    f"{best['price']} {CURRENCY.upper()} ({best.get('airline')})")
                rows.append(to_row(checked_at, origin, destination, depart, ret, best, "OK"))
            else:
                note = "no offers matching filters" if offers else "no data in cache"
                log(f"  [{i}/{len(pairs)}] {depart} / {ret}: {note}")
                rows.append(to_row(checked_at, origin, destination, depart, ret, None, note))
        time.sleep(REQUEST_DELAY)

    ws.append_rows(rows, value_input_option="USER_ENTERED")
    log(f"Wrote {len(rows)} rows to '{route['sheet']}' ({errors} errors)")

    priced = [r for r in rows if r[5] != ""]
    best_row = min(priced, key=lambda r: r[5]) if priced else None
    if best_row:
        log(f"CHEAPEST: {best_row[5]} {best_row[6]} on {best_row[3]} -> {best_row[4]}")
        trend, is_new_low = trend_report(history, float(best_row[5]))
        log(f"TREND: {trend}")
        if is_new_low:
            log("*** NEW LOW — cheapest price recorded so far ***")
    else:
        trend, is_new_low = "no prices this run", None
    write_summary(CITY, rows, best_row, trend, is_new_low)

    if not priced:
        fail("No prices returned for any date pair — check token, route and dates")


if __name__ == "__main__":
    main()
