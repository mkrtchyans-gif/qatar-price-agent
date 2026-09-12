import os, re, json, time
from datetime import date, datetime, timedelta
from pathlib import Path
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

SPREADSHEET_ID=os.environ.get('SPREADSHEET_ID','').strip()
SERVICE_ACCOUNT_JSON=os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON','service_account.json').strip()
CITY=os.environ.get('CITY','').strip()
HEADLESS=os.environ.get('HEADLESS','1')!='0'
SEARCH_TIMEOUT_MS=max(15000,int(os.environ.get('SEARCH_TIMEOUT_MS','45000')))
DIAGNOSTIC_ONLY=os.environ.get('DIAGNOSTIC_ONLY','1')=='1'
TRIPS={'Boston':{'origin':'BOS','sheet':'Boston'},'Los Angeles':{'origin':'LAX','sheet':'Los Angeles'}}
OUTBOUND_DATES=[date(2026,12,13)+timedelta(days=i) for i in range(6)]
RETURN_DATES=[date(2027,1,7)+timedelta(days=i) for i in range(4)]
SEARCH_URL='https://booking.qatarairways.com/nsp/cug/views/cugSearch.xhtml'
ARTIFACT_DIR=Path(os.environ.get('GITHUB_WORKSPACE','.')).resolve()/'artifacts'
ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)

def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}",flush=True)

def sheets_client():
    creds=Credentials.from_service_account_file(SERVICE_ACCOUNT_JSON,scopes=['https://www.googleapis.com/auth/spreadsheets'])
    return gspread.authorize(creds)

def append_result(ws,res):
    ws.append_row([res.get('checked_at',''),res.get('outbound',''),res.get('return',''),res.get('price',''),res.get('currency',''),res.get('outbound_checked',''),res.get('return_checked',''),res.get('status','')],value_input_option='USER_ENTERED')

def save_debug(page,origin,outbound_dt,return_dt,reason):
    ARTIFACT_DIR.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    base=f'{stamp}_{origin}_{outbound_dt}_{return_dt}_{reason}'
    results={}
    try:
        p=ARTIFACT_DIR/f'{base}.png'; page.screenshot(path=str(p),full_page=True); results['png']=str(p)
    except Exception as e:
        (ARTIFACT_DIR/f'{base}_screenshot_error.txt').write_text(str(e),encoding='utf-8'); results['png_error']=str(e)
    try:
        p=ARTIFACT_DIR/f'{base}.html'; p.write_text(page.content(),encoding='utf-8'); results['html']=str(p)
    except Exception as e:
        (ARTIFACT_DIR/f'{base}_html_error.txt').write_text(str(e),encoding='utf-8'); results['html_error']=str(e)
    try:
        p=ARTIFACT_DIR/f'{base}.txt'; p.write_text(page.locator('body').inner_text(timeout=5000),encoding='utf-8'); results['txt']=str(p)
    except Exception as e:
        (ARTIFACT_DIR/f'{base}_body_error.txt').write_text(str(e),encoding='utf-8'); results['body_error']=str(e)
    try:
        info=[]
        for frame in page.frames:
            entry={'url':frame.url,'inputs':[],'buttons':[]}
            try:
                for i in range(min(frame.locator('input').count(),200)):
                    el=frame.locator('input').nth(i)
                    entry['inputs'].append({k:el.get_attribute(k) for k in ['id','name','placeholder','aria-label','type','value']})
            except Exception as e: entry['inputs_error']=str(e)
            try:
                count=frame.get_by_role('button').count()
                for i in range(min(count,100)):
                    b=frame.get_by_role('button').nth(i)
                    try: txt=b.inner_text(timeout=500) or ''
                    except Exception: txt=''
                    entry['buttons'].append(txt[:200])
            except Exception as e: entry['buttons_error']=str(e)
            info.append(entry)
        p=ARTIFACT_DIR/f'{base}_frames.json'; p.write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8'); results['frames']=str(p)
    except Exception as e:
        (ARTIFACT_DIR/f'{base}_frames_error.txt').write_text(str(e),encoding='utf-8'); results['frames_error']=str(e)
    manifest=ARTIFACT_DIR/f'{base}_manifest.json'
    manifest.write_text(json.dumps({'base':base,'reason':reason,'url':page.url,'results':results},ensure_ascii=False,indent=2),encoding='utf-8')
    files=sorted(str(p) for p in ARTIFACT_DIR.glob(f'{base}*') if p.is_file())
    log(f'DEBUG SAVED {len(files)} files in {ARTIFACT_DIR}: {files}')

def find_booking_scope(page, timeout_ms=20000):
    deadline=time.monotonic()+timeout_ms/1000
    while time.monotonic()<deadline:
        for frame in page.frames:
            try:
                inputs=frame.locator('input')
                for i in range(min(inputs.count(),100)):
                    el=inputs.nth(i)
                    meta=' '.join(filter(None,[el.get_attribute('placeholder'),el.get_attribute('aria-label'),el.get_attribute('name'),el.get_attribute('id')]))
                    if re.search(r'departure|origin|from|place of departure',meta,re.I): return frame
            except Exception: pass
        page.wait_for_timeout(500)
    return None

def run_diagnostic(origin,pair):
    out_dt,ret_dt=pair
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=HEADLESS,args=['--disable-blink-features=AutomationControlled'])
        context=browser.new_context(viewport={'width':1440,'height':1000},locale='en-US',timezone_id='Europe/Moscow')
        page=context.new_page(); page.set_default_timeout(4000); page.set_default_navigation_timeout(SEARCH_TIMEOUT_MS)
        try:
            log(f'Opening Qatar Airways: {SEARCH_URL}')
            page.goto(SEARCH_URL,wait_until='domcontentloaded',timeout=SEARCH_TIMEOUT_MS)
            page.wait_for_timeout(5000)
            log(f'Loaded URL: {page.url}')
            log(f'Frames: {[f.url for f in page.frames]}')
            save_debug(page,origin,out_dt,ret_dt,'initial')
            scope=find_booking_scope(page,20000)
            if scope is None:
                save_debug(page,origin,out_dt,ret_dt,'no_booking_scope')
                raise RuntimeError('Booking form/scope not detected')
            return {'ok':True,'outbound':out_dt.isoformat(),'return':ret_dt.isoformat(),'price':'','currency':'','status':'DIAGNOSTIC OK: booking scope found'}
        finally:
            context.close(); browser.close()

def main():
    if not SPREADSHEET_ID: raise SystemExit('Missing SPREADSHEET_ID')
    if CITY not in TRIPS: raise SystemExit(f"CITY must be one of: {', '.join(TRIPS)}")
    log(f'Starting diagnostic {CITY}; diagnostic_only={DIAGNOSTIC_ONLY}; artifact_dir={ARTIFACT_DIR}')
    gc=sheets_client(); ws=gc.open_by_key(SPREADSHEET_ID).worksheet(TRIPS[CITY]['sheet'])
    pair=(OUTBOUND_DATES[0],RETURN_DATES[0])
    try: res=run_diagnostic(TRIPS[CITY]['origin'],pair)
    except Exception as e:
        log(f'FAILED {CITY}: {e}')
        res={'ok':False,'outbound':'','return':'','price':'','currency':'','status':f'DIAGNOSTIC ERROR: {e}','outbound_checked':f'{OUTBOUND_DATES[0]}..{OUTBOUND_DATES[-1]}','return_checked':f'{RETURN_DATES[0]}..{RETURN_DATES[-1]}'}
    res['checked_at']=datetime.now().strftime('%Y-%m-%d %H:%M:%S'); append_result(ws,res)
    # Always create a plain manifest so artifact upload can never be empty.
    (ARTIFACT_DIR/'run_manifest.txt').write_text(f'city={CITY}\nstatus={res.get("status")}\nurl={SEARCH_URL}\n',encoding='utf-8')
    if not res.get('ok'): raise SystemExit(1)

if __name__=='__main__': main()
