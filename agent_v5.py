import os
import re
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json").strip()
CITY = os.environ.get("CITY", "").strip()
HEADLESS = os.environ.get("HEADLESS", "1") != "0"
SEARCH_TIMEOUT_MS = max(15_000, int(os.environ.get("SEARCH_TIMEOUT_MS", "45_000")))
DIAGNOSTIC_ONLY = os.environ.get("DIAGNOSTIC_ONLY", "1") == "1"

TRIPS = {
    "Boston": {"origin": "BOS", "sheet": "Boston"},
    "Los Angeles": {"origin": "LAX", "sheet": "Los Angeles"},
}
OUTBOUND_DATES = [date(2026, 12, 13) + timedelta(days=i) for i in range(6)]
RETURN_DATES = [date(2027, 1, 7) + timedelta(days=i) for i in range(4)]
SEARCH_URL = "https://booking.qatarairways.com/nsp/cug/views/cugSearch.xhtml"

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def sheets_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON, scopes=scopes)
    return gspread.authorize(creds)

def append_result(ws, result):
    ws.append_row([result.get("checked_at",""), result.get("outbound",""), result.get("return",""), result.get("price",""), result.get("currency",""), result.get("outbound_checked",""), result.get("return_checked",""), result.get("status","")], value_input_option="USER_ENTERED")

def save_debug(page, origin, outbound_dt, return_dt, reason="debug"):
    out = Path("artifacts"); out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = f"{stamp}_{origin}_{outbound_dt}_{return_dt}_{reason}"
    try: page.screenshot(path=str(out / f"{base}.png"), full_page=True)
    except Exception as e: log(f"DEBUG screenshot failed: {e}")
    try: (out / f"{base}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e: log(f"DEBUG html failed: {e}")
    try: (out / f"{base}.txt").write_text(page.locator("body").inner_text(timeout=5000), encoding="utf-8")
    except Exception as e: log(f"DEBUG body text failed: {e}")
    try:
        info=[]
        for frame in page.frames:
            entry={"url":frame.url,"inputs":[],"buttons":[]}
            try:
                for i in range(frame.locator("input").count()):
                    el=frame.locator("input").nth(i)
                    entry["inputs"].append({k:el.get_attribute(k) for k in ["id","name","placeholder","aria-label","type","value"]})
            except Exception: pass
            try:
                for i in range(min(frame.get_by_role("button").count(),80)):
                    b=frame.get_by_role("button").nth(i)
                    entry["buttons"].append((b.inner_text(timeout=500) or "")[:150])
            except Exception: pass
            info.append(entry)
        (out / f"{base}_frames.json").write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding="utf-8")
    except Exception as e: log(f"DEBUG frame dump failed: {e}")
    log(f"DEBUG SAVED: {base}.*")

def first_visible(scope, locators):
    for loc in locators:
        try:
            if loc.count() and loc.first.is_visible(timeout=700): return loc.first
        except Exception: pass
    return None

def find_booking_scope(page, timeout_ms=20000):
    deadline=time.monotonic()+timeout_ms/1000
    needles=[r"Your place of departure", r"Your destination", r"From", r"To", r"origin", r"destination"]
    while time.monotonic()<deadline:
        for frame in page.frames:
            try:
                texts=[]
                for n in needles:
                    if frame.get_by_placeholder(re.compile(n,re.I)).count(): return frame
                for i in range(min(frame.locator("input").count(),50)):
                    el=frame.locator("input").nth(i)
                    meta=" ".join(filter(None,[el.get_attribute("placeholder"),el.get_attribute("aria-label"),el.get_attribute("name"),el.get_attribute("id")]))
                    if re.search(r"departure|origin|from|place of departure",meta,re.I): return frame
            except Exception: pass
        page.wait_for_timeout(500)
    return None

def locate_airport(scope, labels):
    patterns=[]
    for x in labels:
        patterns += [
            scope.get_by_placeholder(re.compile(re.escape(x),re.I)),
            scope.locator(f'input[aria-label*="{x}"]'),
            scope.locator(f'input[name*="{x}"]'),
            scope.locator(f'input[id*="{x}"]'),
        ]
    return first_visible(scope, patterns)

def fill_airport(scope, labels, value, page, origin, out_dt, ret_dt):
    field=locate_airport(scope, labels)
    if field is None:
        save_debug(page, origin, out_dt, ret_dt, "no_airport_field")
        raise RuntimeError(f"Could not locate airport field for {labels}")
    field.click(); field.fill(value); scope.wait_for_timeout(1000)
    # Try exact option/code in any listbox or visible text.
    clicked=False
    candidates=[
        scope.get_by_role("option", name=re.compile(re.escape(value),re.I)),
        scope.get_by_text(re.compile(fr"\b{re.escape(value)}\b",re.I)),
        scope.locator("[role=listbox] [role=option]")
    ]
    for c in candidates:
        try:
            if c.count() and c.first.is_visible(timeout=700): c.first.click(timeout=2000); clicked=True; break
        except Exception: pass
    if not clicked: scope.keyboard.press("ArrowDown"); scope.keyboard.press("Enter")
    scope.wait_for_timeout(400)

def run_diagnostic(origin, city, pair):
    out_dt, ret_dt = pair
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=HEADLESS, args=["--disable-blink-features=AutomationControlled"])
        try:
            context=browser.new_context(viewport={"width":1440,"height":1000},locale="en-US",timezone_id="Europe/Moscow")
            page=context.new_page(); page.set_default_timeout(4000); page.set_default_navigation_timeout(SEARCH_TIMEOUT_MS)
            try:
                log(f"Opening Qatar Airways: {SEARCH_URL}")
                page.goto(SEARCH_URL,wait_until="domcontentloaded",timeout=SEARCH_TIMEOUT_MS)
                page.wait_for_timeout(5000)
                log(f"Loaded URL: {page.url}")
                log(f"Frames: {[f.url for f in page.frames]}")
                # Capture immediately so we have evidence even if form detection fails.
                save_debug(page,origin,out_dt,ret_dt,"initial")
                scope=find_booking_scope(page,20000)
                if scope is None:
                    save_debug(page,origin,out_dt,ret_dt,"no_booking_scope")
                    raise RuntimeError("Booking form/scope not detected")
                log(f"Booking scope found in frame: {scope.url}")
                fill_airport(scope,["Your place of departure","From","origin","departure"],origin,page,origin,out_dt,ret_dt)
                fill_airport(scope,["Your destination","To","destination"],"DOH",page,origin,out_dt,ret_dt)
                save_debug(page,origin,out_dt,ret_dt,"after_airports")
                return {"ok":True,"outbound":out_dt.isoformat(),"return":ret_dt.isoformat(),"status":"DIAGNOSTIC OK: airport fields found"}
            finally:
                context.close()
        finally:
            browser.close()

def main():
    if not SPREADSHEET_ID: raise SystemExit("Missing SPREADSHEET_ID")
    if CITY not in TRIPS: raise SystemExit(f"CITY must be one of: {', '.join(TRIPS)}")
    log(f"Starting diagnostic {CITY}; diagnostic_only={DIAGNOSTIC_ONLY}")
    gc=sheets_client(); ws=gc.open_by_key(SPREADSHEET_ID).worksheet(TRIPS[CITY]["sheet"])
    pairs=[(OUTBOUND_DATES[0],RETURN_DATES[0])] if DIAGNOSTIC_ONLY else [(o,r) for o in OUTBOUND_DATES for r in RETURN_DATES]
    try:
        res=run_diagnostic(TRIPS[CITY]["origin"],CITY,pairs[0])
    except Exception as e:
        log(f"FAILED {CITY}: {e}")
        res={"ok":False,"outbound":"","return":"","price":"","currency":"","status":f"DIAGNOSTIC ERROR: {e}","outbound_checked":f"{OUTBOUND_DATES[0]}..{OUTBOUND_DATES[-1]}","return_checked":f"{RETURN_DATES[0]}..{RETURN_DATES[-1]}"}
    res["checked_at"]=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    append_result(ws,res)
    if not res.get("ok"): raise SystemExit(1)

if __name__=="__main__": main()
