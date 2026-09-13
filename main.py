"""
Самый дешёвый перелёт туда-обратно -> Google Sheets.
Одна строка за запуск, 4 колонки.

Env: TP_TOKEN, SPREADSHEET_ID, GOOGLE_SERVICE_ACCOUNT_JSON, CITY
     ADULTS (3), CHILDREN (1), CHILD_RATIO (0.75),
     TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, ALERT_DROP_PCT (15),
     CURRENCY (usd), MARKET (ru), MAX_STOPS (2),
     AIRLINE_FILTER (""), REQUEST_DELAY (1.5)
"""

import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import gspread
import requests
from google.oauth2.service_account import Credentials

# --------------------------------------------------------------------------
# CONFIG
# --------------------------------------------------------------------------

ROUTES = {
    "Boston": {"origin": "MOW", "destination": "BOS", "sheet": "Boston"},
    "Los Angeles": {"origin": "MOW", "destination": "LAX", "sheet": "Los Angeles"},
}

OUTBOUND_DATES = [date(2026, 12, 13) + timedelta(days=i) for i in range(6)]
RETURN_DATES = [date(2027, 1, 7) + timedelta(days=i) for i in range(4)]

# --------------------------------------------------------------------------

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"

TP_TOKEN = os.environ.get("TP_TOKEN", "").strip()
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "").strip()
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "service_account.json").strip()
CITY = os.environ.get("CITY", "").strip()

# 2 взрослых + ребёнок 12 лет (с 12 лет тариф взрослый) = 3 взрослых тарифа
ADULTS = int(os.environ.get("ADULTS", "3"))
# ребёнок 2-11 лет, обычно ~75% от взрослого тарифа
CHILDREN = int(os.environ.get("CHILDREN", "1"))
CHILD_RATIO = float(os.environ.get("CHILD_RATIO", "0.75"))

PASSENGERS = ADULTS + CHILDREN
FARE_MULTIPLIER = ADULTS + CHILDREN * CHILD_RATIO
CURRENCY = os.environ.get("CURRENCY", "usd").strip().lower()
MARKET = os.environ.get("MARKET", "ru").strip().lower()
AIRLINE_FILTER = os.environ.get("AIRLINE_FILTER", "").strip().upper()
MAX_STOPS = int(os.environ.get("MAX_STOPS", "2"))
REQUEST_DELAY = float(os.environ.get("REQUEST_DELAY", "1.5"))

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ALERT_DROP_PCT = float(os.environ.get("ALERT_DROP_PCT", "15"))

HEADERS = [
    "Дата цены",
    "Дата вылета",
    "Дата прилета",
    f"Стоимость за {PASSENGERS}х человек в долларах США",
]


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fail(msg):
    log(f"FATAL: {msg}")
    sys.exit(1)


def ru(d):
    return d.strftime("%d.%m.%Y")


def telegram_send(text):
    """Отправка в Telegram. Никогда не роняет запуск."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram не настроен — уведомление пропущено")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=15,
        )
        if r.status_code == 200:
            log("Уведомление в Telegram отправлено")
        else:
            log(f"Telegram вернул {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        log(f"Telegram недоступен: {e}")


def fetch_pair(session, origin, destination, depart, ret):
    params = {
        "origin": origin, "destination": destination,
        "departure_at": depart.isoformat(), "return_at": ret.isoformat(),
        "one_way": "false", "direct": "false",
        "currency": CURRENCY, "market": MARKET,
        "sorting": "price", "limit": 30, "page": 1,
    }
    for attempt in range(1, 4):
        try:
            r = session.get(API_URL, params=params, timeout=30)
        except requests.RequestException as e:
            log(f"  сеть недоступна ({e}); попытка {attempt}/3")
            time.sleep(3 * attempt)
            continue
        if r.status_code == 429:
            time.sleep(10 * attempt)
            continue
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        try:
            body = r.json()
        except ValueError:
            return None, "некорректный JSON"
        if not body.get("success", False):
            return None, f"ошибка API: {body.get('error')}"
        return body.get("data") or [], None
    return None, "3 попытки не удались"


def acceptable(offer):
    if AIRLINE_FILTER and (offer.get("airline") or "").upper() != AIRLINE_FILTER:
        return False
    if (offer.get("transfers") or 0) > MAX_STOPS:
        return False
    if (offer.get("return_transfers") or 0) > MAX_STOPS:
        return False
    return offer.get("price") is not None


def open_sheet(sheet_name):
    creds = Credentials.from_service_account_file(
        SA_JSON, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    book = gspread.authorize(creds).open_by_key(SPREADSHEET_ID)
    try:
        ws = book.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        log(f"Создаю лист '{sheet_name}'")
        ws = book.add_worksheet(title=sheet_name, rows=1000, cols=len(HEADERS))
    if ws.row_values(1) != HEADERS:
        ws.update(range_name="A1", values=[HEADERS])
        ws.freeze(rows=1)
        ws.format("A1:D1", {"textFormat": {"bold": True}})
    return ws


def previous_total(ws):
    for row in reversed(ws.get_all_values()[1:]):
        if len(row) > 3 and row[3]:
            try:
                return float(str(row[3]).replace(",", ".").replace(" ", ""))
            except ValueError:
                continue
    return None


def main():
    if not TP_TOKEN:
        fail("Не задан TP_TOKEN")
    if not SPREADSHEET_ID:
        fail("Не задан SPREADSHEET_ID")
    if CITY not in ROUTES:
        fail(f"CITY должен быть одним из: {', '.join(ROUTES)}")

    route = ROUTES[CITY]
    origin, destination = route["origin"], route["destination"]
    pairs = [(o, r) for o in OUTBOUND_DATES for r in RETURN_DATES]

    log(f"{CITY}: {origin} -> {destination}, {len(pairs)} пар дат, "
        f"пассажиров: {PASSENGERS} (множитель тарифа {FARE_MULTIPLIER:g})")

    ws = open_sheet(route["sheet"])
    prev = previous_total(ws)

    session = requests.Session()
    session.headers.update({"X-Access-Token": TP_TOKEN})

    best, best_pair, priced = None, None, 0

    for i, (depart, ret) in enumerate(pairs, 1):
        offers, err = fetch_pair(session, origin, destination, depart, ret)
        if err:
            log(f"  [{i}/{len(pairs)}] {ru(depart)} / {ru(ret)}: {err}")
        else:
            candidates = [o for o in offers if acceptable(o)]
            if candidates:
                priced += 1
                cheapest = min(candidates, key=lambda o: o["price"])
                mark = ""
                if best is None or cheapest["price"] < best["price"]:
                    best, best_pair, mark = cheapest, (depart, ret), "   <-- минимум"
                log(f"  [{i}/{len(pairs)}] {ru(depart)} / {ru(ret)}: "
                    f"{cheapest['price']} x{FARE_MULTIPLIER:g} = "
                    f"{round(cheapest['price'] * FARE_MULTIPLIER)} "
                    f"({cheapest.get('airline')}){mark}")
            else:
                log(f"  [{i}/{len(pairs)}] {ru(depart)} / {ru(ret)}: нет данных")
        time.sleep(REQUEST_DELAY)

    if best is None:
        fail(f"Цен нет ни по одной паре дат (0 из {len(pairs)})")

    depart, ret = best_pair
    total = round(best["price"] * FARE_MULTIPLIER)

    ws.append_row(
        [datetime.now(timezone.utc).strftime("%d.%m.%Y"), ru(depart), ru(ret), total],
        value_input_option="USER_ENTERED",
    )

    link = best.get("link") or ""
    if link.startswith("/"):
        link = "https://www.aviasales.com" + link

    if prev is None:
        change = "первый запуск"
    else:
        diff = total - prev
        pct = diff / prev * 100 if prev else 0
        change = f"{'ДЕШЕВЛЕ' if diff < 0 else ('ДОРОЖЕ' if diff > 0 else 'без изменений')} {diff:+.0f} ({pct:+.1f}%)"

    if prev is not None and prev > 0:
        drop_pct = (prev - total) / prev * 100
        if drop_pct >= ALERT_DROP_PCT:
            telegram_send(
                f"\u2708\ufe0f <b>Цена упала на {drop_pct:.1f}%</b>\n\n"
                f"<b>{CITY}</b>: {origin} \u2192 {destination}\n"
                f"Было: {prev:.0f} USD\n"
                f"Стало: <b>{total} USD</b> за {PASSENGERS} чел.\n\n"
                f"Вылет {ru(depart)}, обратно {ru(ret)}\n"
                f"{best.get('airline')}, пересадок "
                f"{best.get('transfers')}/{best.get('return_transfers')}\n\n"
                + (link if link else "")
            )
        else:
            log(f"Порог не пройден: изменение {-drop_pct:+.1f}%, "
                f"уведомление при падении от {ALERT_DROP_PCT:g}%")

    log("=" * 60)
    log(f"{total} USD за {PASSENGERS} чел. — вылет {ru(depart)}, обратно {ru(ret)}")
    log(f"{best['price']} USD за взрослого x {FARE_MULTIPLIER:g} "
        f"({ADULTS} взр. + {CHILDREN} дет. по {CHILD_RATIO:g}), {best.get('airline')}, "
        f"{best.get('transfers')}/{best.get('return_transfers')} пересадок")
    log(f"Относительно прошлого запуска: {change}")
    log(f"Данные найдены по {priced} из {len(pairs)} пар дат")
    if link:
        log(f"Ссылка: {link}")
    log("=" * 60)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(
                f"## {CITY}: {total} USD за {PASSENGERS} чел.\n\n"
                f"**Вылет {ru(depart)} → обратно {ru(ret)}**\n\n"
                f"{best['price']} USD за взрослого · множитель {FARE_MULTIPLIER:g} · "
                f"{best.get('airline')} · "
                f"{best.get('transfers')}/{best.get('return_transfers')} пересадок\n\n"
                f"{change} · данные по {priced}/{len(pairs)} парам дат\n\n"
                + (f"[Открыть на Aviasales]({link})\n" if link else "")
            )


if __name__ == "__main__":
    main()
