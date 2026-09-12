import os
import re
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------- CONFIG ----------------
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json").strip()
CITY = os.environ.get("CITY", "").strip()
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
MAX_WORKERS = max(1, int(os.environ.get("MAX_WORKERS", "2")))
SEARCH_TIMEOUT_MS = max(15_000, int(os.environ.get("SEARCH_TIMEOUT_MS", "45_000")))

TRIPS = {
    "Boston": {"origin": "BOS", "sheet": "Boston"},
    "Los Angeles": {"origin": "LAX", "sheet": "Los Angeles"},
}

OUTBOUND_DATES = [date(2026, 12, 13) + timedelta(days=i) for i in range(6)]
RETURN_DATES = [date(2027, 1, 7) + timedelta(days=i) for i in range(4)]
SEARCH_URL = "https://booking.qatarairways.com/nsp/cug/views/cugSearch.xhtml"

MONTHS = {
    1: ("January", "Jan"), 2: ("February", "Feb"), 3: ("March", "Mar"),
    4: ("April", "Apr"), 5: ("May", "May"), 6: ("June", "Jun"),
    7: ("July", "Jul"), 8: ("August", "Aug"), 9: ("September", "Sep"),
    10: ("October", "Oct"), 11: ("November", "Nov"), 12: ("December", "Dec"),
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------- GOOGLE SHEETS ----------------
def sheets_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=scopes)
    return gspread.authorize(creds)


def append_result(ws, result: dict) -> None:
    ws.append_row([
        result["checked_at"], result["outbound"], result["return"],
        result["price"], result["currency"], result["outbound_checked"],
        result["return_checked"], result["status"]
    ], value_input_option="USER_ENTERED")


# ---------------- QATAR HELPERS ----------------
def visible(locator) -> bool:
    try:
        return locator.is_visible(timeout=800)
    except Exception:
        return False


def first_visible(candidates):
    for loc in candidates:
        try:
            if loc.count() and visible(loc.first):
                return loc.first
        except Exception:
            pass
    return None


def click_first(page, candidates, timeout=2500) -> bool:
    for loc in candidates:
        try:
            target = loc.first
            if target.is_visible(timeout=700):
                target.click(timeout=timeout)
                return True
        except Exception:
            pass
    return False


def fill_airport(scope, labels, value):
    field = first_visible([
        scope.get_by_placeholder(re.compile("|".join(labels), re.I)),
        scope.locator("input[aria-label]"),
    ])
    if field is None:
        # More direct fallback based on all visible textboxes.
        for i in range(scope.get_by_role("textbox").count()):
            tb = scope.get_by_role("textbox").nth(i)
            try:
                meta = " ".join(filter(None, [tb.get_attribute("placeholder"), tb.get_attribute("aria-label"), tb.get_attribute("name")])).lower()
                if any(x.lower() in meta for x in labels):
                    field = tb
                    break
            except Exception:
                pass
    if field is None:
        raise RuntimeError(f"Could not locate airport field for {labels}")

    field.click()
    field.fill(value)
    scope.wait_for_timeout(700)
    # Autocomplete option: prefer exact code/text, then first visible option.
    if not click_first(scope, [
        scope.get_by_text(re.compile(fr"\b{re.escape(value)}\b", re.I)),
        scope.get_by_role("option", name=re.compile(re.escape(value), re.I)),
    ], timeout=2500):
        # Some Qatar autocomplete widgets use a listbox without option roles.
        click_first(scope, [scope.locator('[role="listbox"] >> text=' + value)], timeout=1500)
    scope.wait_for_timeout(300)


def set_passengers_and_cabin(scope):
    # Open passenger/class editor.
    opened = click_first(scope, [
        scope.get_by_role("button", name=re.compile(r"Passengers / Class|Passengers", re.I)),
        scope.get_by_text(re.compile(r"^Passengers / Class$|^Passengers$", re.I)),
    ], timeout=2000)

    # If the editor is already open, or opening succeeded, set counts.
    if opened or visible(scope.get_by_text(re.compile(r"Adults", re.I)).first):
        # Default is typically 1 adult; add one adult.
        click_first(page, [
            scope.get_by_role("button", name=re.compile(r"increase adult count", re.I)),
            scope.get_by_text(re.compile(r"\+increase adult count", re.I)),
        ], timeout=1500)
        # Age 12 => Teenager (12-15) on Qatar's current booking form.
        click_first(page, [
            scope.get_by_role("button", name=re.compile(r"increase teenager count", re.I)),
            scope.get_by_text(re.compile(r"\+increase teenager count", re.I)),
        ], timeout=1500)
        # Age 4 => Child (2-11).
        click_first(page, [
            scope.get_by_role("button", name=re.compile(r"increase child count", re.I)),
            scope.get_by_text(re.compile(r"\+increase child count", re.I)),
        ], timeout=1500)

        click_first(page, [
            scope.get_by_text(re.compile(r"^Economy$", re.I)),
            scope.get_by_role("button", name=re.compile(r"Economy", re.I)),
        ], timeout=1200)

        # Confirm passenger editor if such button exists.
        click_first(page, [
            scope.get_by_role("button", name=re.compile(r"^Confirm$", re.I)),
            scope.get_by_text(re.compile(r"^Confirm$", re.I)),
        ], timeout=1200)


def date_input_by_role(scope, return_field=False):
    patterns = [
        "When do you want to return" if return_field else "When do you want to go",
        "return" if return_field else "departure",
    ]
    # aria-label / placeholder first
    for p in patterns:
        for loc in [
            scope.get_by_placeholder(re.compile(p, re.I)),
            scope.locator(f'input[aria-label*="{p}"]'),
        ]:
            try:
                if loc.count() and loc.first.is_visible(timeout=600):
                    return loc.first
            except Exception:
                pass
    # scan visible textboxes
    for i in range(scope.get_by_role("textbox").count()):
        tb = scope.get_by_role("textbox").nth(i)
        try:
            if not tb.is_visible(timeout=400):
                continue
            meta = " ".join(filter(None, [tb.get_attribute("placeholder"), tb.get_attribute("aria-label"), tb.get_attribute("name")])).lower()
            if (return_field and ("return" in meta or "return date" in meta)) or ((not return_field) and ("departure" in meta or "go" in meta)):
                return tb
        except Exception:
            pass
    return None


def select_calendar_day(scope, target: date):
    # Fallback calendar picker. Navigate until target month is visible.
    full, short = MONTHS[target.month]
    target_month_re = re.compile(fr"{full}\s+{target.year}|{short}\s+{target.year}", re.I)
    for _ in range(6):
        body = scope.locator("body").inner_text(timeout=3000)
        if target_month_re.search(body):
            break
        clicked = click_first(page, [
            scope.get_by_role("button", name=re.compile(r"^Next$|Next month|next", re.I)),
            scope.get_by_text(re.compile(r"^Next$", re.I)),
        ], timeout=1200)
        if not clicked:
            raise RuntimeError(f"Cannot navigate calendar to {target}")
        scope.wait_for_timeout(250)

    # Prefer date buttons with exact day and an accessible date/name containing target month/year.
    iso = target.strftime("%Y-%m-%d")
    candidates = [
        scope.locator(f'[aria-label*="{iso}"]'),
        scope.locator(f'[data-date="{iso}"]'),
        scope.get_by_role("button", name=re.compile(fr"^{target.day}$")),
        scope.get_by_text(re.compile(fr"^{target.day}$")),
    ]
    if not click_first(page, candidates, timeout=1800):
        raise RuntimeError(f"Could not select calendar date {target}")
    scope.wait_for_timeout(350)


def set_date(scope, target: date, return_field=False):
    field = date_input_by_role(page, return_field=return_field)
    target_strings = [
        target.strftime("%d-%b-%Y"),
        target.strftime("%d %b %Y"),
        target.strftime("%Y-%m-%d"),
        target.strftime("%d/%m/%Y"),
    ]
    if field is not None:
        try:
            field.click(timeout=1500)
            try:
                field.fill(target_strings[0], timeout=1500)
                scope.keyboard.press("Tab")
                return
            except Exception:
                pass
        except Exception:
            pass
    select_calendar_day(page, target)


def parse_price(text: str):
    # Prefer the explicitly displayed total for the whole party.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    total_markers = re.compile(r"total trip price|total price for all passengers|total price", re.I)
    currency_patterns = [
        ("USD", r"USD\s*([0-9][0-9,]*(?:\.\d{1,2})?)"),
        ("USD", r"\$\s*([0-9][0-9,]*(?:\.\d{1,2})?)"),
        ("EUR", r"EUR\s*([0-9][0-9,]*(?:\.\d{1,2})?)"),
        ("EUR", r"€\s*([0-9][0-9,]*(?:\.\d{1,2})?)"),
        ("GBP", r"GBP\s*([0-9][0-9,]*(?:\.\d{1,2})?)"),
        ("GBP", r"£\s*([0-9][0-9,]*(?:\.\d{1,2})?)"),
        ("QAR", r"QAR\s*([0-9][0-9,]*(?:\.\d{1,2})?)"),
    ]
    for idx, line in enumerate(lines):
        if total_markers.search(line):
            window = " ".join(lines[idx:idx + 4])
            for cur, pat in currency_patterns:
                m = re.search(pat, window, re.I)
                if m:
                    val = float(m.group(1).replace(",", ""))
                    if val > 0:
                        return val, cur

    # Fallback: collect currency-marked values, but reject 0 and extremely small UI numbers.
    vals = []
    for cur, pat in currency_patterns:
        for m in re.finditer(pat, text, re.I):
            try:
                val = float(m.group(1).replace(",", ""))
                if 100 <= val <= 100000:
                    vals.append((val, cur))
            except Exception:
                pass
    return min(vals, key=lambda x: x[0]) if vals else None


def make_search_page(browser, city):
    context = browser.new_context(
        viewport={"width": 1440, "height": 1000},
        locale="en-US",
        timezone_id="Europe/Amsterdam",
    )
    page = context.new_page()
    page.set_default_timeout(4000)
    page.set_default_navigation_timeout(SEARCH_TIMEOUT_MS)
    return context, page


def find_booking_scope(page, timeout_ms=20000):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        frames = page.frames
        for frame in frames:
            try:
                if frame.get_by_placeholder(re.compile(r"Your place of departure", re.I)).count():
                    return frame
            except Exception:
                pass
            try:
                if frame.get_by_placeholder(re.compile(r"Your destination", re.I)).count():
                    return frame
            except Exception:
                pass
            try:
                if frame.get_by_text(re.compile(r"Flight Route", re.I)).count():
                    return frame
            except Exception:
                pass
        page.wait_for_timeout(500)
    return page


def dump_form_debug(page, origin, outbound_dt, return_dt):
    Path("artifacts").mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    try:
        page.screenshot(path=f"artifacts/{stamp}_{origin}_{outbound_dt}_{return_dt}_form.png", full_page=True)
    except Exception:
        pass
    try:
        Path(f"artifacts/{stamp}_{origin}_{outbound_dt}_{return_dt}_form.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        rows = []
        for i in range(page.locator("input").count()):
            el = page.locator("input").nth(i)
            rows.append({k: el.get_attribute(k) for k in ["id","name","placeholder","aria-label","type"]})
        Path(f"artifacts/{stamp}_{origin}_{outbound_dt}_{return_dt}_inputs.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def make_search_page(browser, city):
    context = browser.new_context(
        viewport={"width": 1440, "height": 1000},
        locale="en-US",
        timezone_id="Europe/Amsterdam",
    )
    page = context.new_page()
    page.set_default_timeout(4000)
    page.set_default_navigation_timeout(SEARCH_TIMEOUT_MS)
    return context, page


def find_booking_scope(page, timeout_ms=20000):
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        frames = page.frames
        for frame in frames:
            try:
                if frame.get_by_placeholder(re.compile(r"Your place of departure", re.I)).count():
                    return frame
            except Exception:
                pass
            try:
                if frame.get_by_placeholder(re.compile(r"Your destination", re.I)).count():
                    return frame
            except Exception:
                pass
            try:
                if frame.get_by_text(re.compile(r"Flight Route", re.I)).count():
                    return frame
            except Exception:
                pass
        page.wait_for_timeout(500)
    return page


def dump_form_debug(page, origin, outbound_dt, return_dt):
    Path("artifacts").mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    try:
        page.screenshot(path=f"artifacts/{stamp}_{origin}_{outbound_dt}_{return_dt}_form.png", full_page=True)
    except Exception:
        pass
    try:
        Path(f"artifacts/{stamp}_{origin}_{outbound_dt}_{return_dt}_form.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    try:
        rows = []
        for i in range(page.locator("input").count()):
            el = page.locator("input").nth(i)
            rows.append({k: el.get_attribute(k) for k in ["id","name","placeholder","aria-label","type"]})
        Path(f"artifacts/{stamp}_{origin}_{outbound_dt}_{return_dt}_inputs.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def search_one(page, origin, outbound_dt, return_dt):
    start = time.monotonic()
    page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=SEARCH_TIMEOUT_MS)
    page.wait_for_timeout(1200)

    # Close common cookie/overlay buttons if present.
    click_first(page, [
        page.get_by_role("button", name=re.compile(r"Accept|Agree|Allow all|Close", re.I)),
        page.get_by_text(re.compile(r"^Accept$|^Agree$|^Close$", re.I)),
    ], timeout=1200)

    fill_airport(page, ["Your place of departure", "From", "origin"], origin)
    fill_airport(page, ["Your destination", "To", "destination"], "DOH")
    set_passengers_and_cabin(page)
    set_date(page, outbound_dt, return_field=False)
    set_date(page, return_dt, return_field=True)

    if not click_first(page, [
        page.get_by_role("button", name=re.compile(r"^Search$|Search flights|Show flights", re.I)),
        page.get_by_text(re.compile(r"^Search$|Search flights|Show flights", re.I)),
    ], timeout=2500):
        raise RuntimeError("Search button not found")

    # Wait for the fare-selection URL/page or a useful result marker.
    try:
        page.wait_for_url(re.compile(r"fareSelection|booking|flight", re.I), timeout=SEARCH_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        # Still inspect the page — Qatar sometimes updates in-place.
        page.wait_for_timeout(2500)

    text = page.locator("body").inner_text(timeout=8000)
    parsed = parse_price(text)
    elapsed = time.monotonic() - start
    if not parsed:
        # Save debug artifacts for failed individual searches.
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        Path("artifacts").mkdir(exist_ok=True)
        try:
            page.screenshot(path=f"artifacts/{stamp}_{origin}_{outbound_dt}_{return_dt}.png", full_page=True)
        except Exception:
            pass
        try:
            Path(f"artifacts/{stamp}_{origin}_{outbound_dt}_{return_dt}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        raise RuntimeError("Price not found on results page")
    log(f"{origin} {outbound_dt} -> {return_dt}: {parsed[0]:,.2f} {parsed[1]} ({elapsed:.1f}s)")
    return parsed


def worker_search(pair, origin, city):
    """Run one Playwright search entirely inside one OS thread.

    The Playwright sync API is thread-affine: a sync_playwright instance/browser
    created in the main thread cannot safely be used from a worker thread.
    Therefore each worker creates and closes its own Playwright + browser stack.
    """
    o, r = pair
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        try:
            context, page = make_search_page(browser, city)
            try:
                return o, r, search_one(page, origin, o, r)
            finally:
                context.close()
        finally:
            browser.close()


def run_city(city, origin):
    pairs = [(o, r) for o in OUTBOUND_DATES for r in RETURN_DATES]
    best = None
    failures = 0

    # IMPORTANT: each worker owns its own Playwright instance/browser.
    # This avoids the 'Cannot switch to a different thread' greenlet error.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(worker_search, pair, origin, city): pair for pair in pairs}
        for fut in as_completed(futures):
            o, r = futures[fut]
            try:
                price, cur = fut.result()
                candidate = (price, cur, o, r)
                if best is None or candidate[0] < best[0]:
                    best = candidate
            except Exception as exc:
                failures += 1
                log(f"FAILED {origin} {o} -> {r}: {exc}")

    checked = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = len(pairs)
    if best:
        price, cur, out_dt, ret_dt = best
        return {
            "checked_at": checked,
            "outbound": out_dt.isoformat(),
            "return": ret_dt.isoformat(),
            "price": price,
            "currency": cur,
            "outbound_checked": f"{OUTBOUND_DATES[0].isoformat()}..{OUTBOUND_DATES[-1].isoformat()}",
            "return_checked": f"{RETURN_DATES[0].isoformat()}..{RETURN_DATES[-1].isoformat()}",
            "status": f"OK ({total - failures}/{total} searches)",
        }

    return {
        "checked_at": checked,
        "outbound": "",
        "return": "",
        "price": "",
        "currency": "",
        "outbound_checked": f"{OUTBOUND_DATES[0].isoformat()}..{OUTBOUND_DATES[-1].isoformat()}",
        "return_checked": f"{RETURN_DATES[0].isoformat()}..{RETURN_DATES[-1].isoformat()}",
        "status": f"ERROR: no valid price ({failures}/{total} searches failed)",
    }


def main():
    if not SPREADSHEET_ID:
        raise SystemExit("Missing SPREADSHEET_ID")
    if CITY not in TRIPS:
        raise SystemExit(f"CITY must be one of: {', '.join(TRIPS)}")

    log(f"Starting {CITY}; {len(OUTBOUND_DATES)} outbound x {len(RETURN_DATES)} return dates; workers={MAX_WORKERS}")
    gc = sheets_client()
    book = gc.open_by_key(SPREADSHEET_ID)
    ws = book.worksheet(TRIPS[CITY]["sheet"])

    result = run_city(CITY, TRIPS[CITY]["origin"])

    append_result(ws, result)
    log(f"Recorded {CITY}: {json.dumps(result, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
