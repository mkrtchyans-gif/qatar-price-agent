# Мониторинг цен на авиабилеты → Google Sheets

Версия без браузера. Playwright, Akamai и «Access Denied» больше не участвуют.

## Что делает

Раз в сутки опрашивает Travelpayouts по всем парам дат (6 туда × 4 обратно = 24 запроса на город),
находит самый дешёвый вариант по каждой паре и дописывает строки в Google Sheets.
В сводке GitHub Actions показывает топ-15 и минимальную цену — без открытия таблицы.

## Настройка (15 минут)

### 1. Токен Travelpayouts

1. Регистрация: https://www.travelpayouts.com — бесплатно, это партнёрская сеть.
2. Подключить программу **Aviasales**.
3. Профиль → раздел API token → скопировать токен.

### 2. Секреты в GitHub

Settings → Secrets and variables → Actions → New repository secret:

| Имя | Значение |
|---|---|
| `TP_TOKEN` | токен Travelpayouts |
| `SPREADSHEET_ID` | ID таблицы из URL: `docs.google.com/spreadsheets/d/`**`ЭТА_ЧАСТЬ`**`/edit` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | содержимое JSON сервисного аккаунта целиком |

Сервисный аккаунт у вас уже работает — в прошлой версии строки в таблицу записывались.
Тот же JSON и тот же доступ подойдут.

### 3. Маршруты

В `main.py`, блок `ROUTES` в самом верху:

```python
ROUTES = {
    "Boston":      {"origin": "MOW", "destination": "BOS", "sheet": "Boston"},
    "Los Angeles": {"origin": "MOW", "destination": "LAX", "sheet": "Los Angeles"},
}
```

Маршрут: Москва → Бостон и Москва → Лос-Анджелес.
`MOW` — код всех московских аэропортов сразу (SVO + DME + VKO).
Листы создадутся сами, если их нет — менять тут ничего не надо.

### 4. Запуск

Actions → Flight prices → Run workflow. Дальше каждый день в 07:00 UTC.

## Настройки в workflow

В `.github/workflows/flight-prices.yml`, блок `env`:

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `AIRLINE_FILTER` | `""` | `"QR"` — только Qatar Airways. Пусто — любые перевозчики |
| `MAX_STOPS` | `2` | Максимум пересадок в одну сторону |
| `CURRENCY` | `usd` | `rub`, `eur`, `usd` |
| `MARKET` | `ru` | Рынок поиска. Для вылета из Москвы оставить `ru` |
| `REQUEST_DELAY` | `1.5` | Пауза между запросами, сек |

## Столбцы в таблице

`checked_at · origin · destination · depart · return · price · currency · airline ·
flight · stops_out · stops_back · duration_h · found_at · link · status`

Строки только дописываются, история цен накапливается сама.
Столбец `link` — прямая ссылка на выдачу Aviasales по этому варианту.

После каждого запуска в логе и в сводке Actions появляется сравнение с прошлыми прогонами:

```
CHEAPEST: 1290 USD on 2026-12-15 -> 2027-01-09
TREND: DOWN -60 (-4.4%) vs last run 1350; all-time low in sheet 1310 over 12 runs
*** NEW LOW — cheapest price recorded so far ***
```

## Важное ограничение

Travelpayouts отдаёт **кэш**: самые дешёвые билеты, найденные пользователями Aviasales
за последние 48 часов. Это не живой запрос в систему бронирования.

Что это значит на практике:
- Динамику цены по датам видно отлично — для этого и нужно.
- Конкретная цена может не совпасть с кассой на момент покупки.
- По непопулярным парам дат кэш бывает пустой → в статусе `no data in cache`. Это не ошибка.

Когда увидите нужную цену — перепроверяйте на сайте перед покупкой.

## Если что-то не работает

| Статус / ошибка | Причина |
|---|---|
| `Missing TP_TOKEN` | секрет не добавлен или пустой |
| `API error: ...` | токен неверный или программа Aviasales не подключена |
| `no data in cache` по всем парам | проверьте IATA-коды origin/destination |
| `no offers matching filters` | слишком жёсткий `AIRLINE_FILTER` или `MAX_STOPS` |
| gspread `PermissionError` | таблица не расшарена на email сервисного аккаунта |
