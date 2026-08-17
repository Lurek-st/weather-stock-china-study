# TAIEX calendar evidence contract

**Official source:** Taiwan Stock Exchange annual holiday schedule,
[resource page](https://www.twse.com.tw/holidaySchedule/holidaySchedule).
The controlled source template and 2020--2026 year list are versioned in
`config/v2/calendars/taiex-calendar-sources.yaml`.

The parser preserves official holiday dates, derives Saturday/Sunday closures,
and leaves ordinary weekday sessions `unresolved` until an official open-session
contract has been verified. It can express extraordinary closures, special
opens, and early closes, but does not invent them. A make-up workday is not
assumed to be a trading day. Typhoon-related closures require an official TWSE
revision/evidence record.

Raw annual responses remain under ignored `.local/source-probes/taiex-calendar/`.
Normalized records contain their source URL, year, revision hash, verification
time, reason and quality flags. This repository does not claim all 2020--2026
calendar years are fully verified yet.

## 2026 pilot window (2026-03-02 .. 2026-03-06)

The official 2026 schedule (27 rows: 3 named trading sessions, 24 closures)
was used to select the earliest complete Monday-to-Friday pilot week inside
`2026-03-01 .. 2026-05-31`, giving `2026-03-02 .. 2026-03-06` with no closure.
The TWSE official news list shows no extraordinary closure announcement for
that window. Consequently:

- `pilot_calendar_status`: `calendar_verified_for_2026_pilot_window`
- `full_history_calendar_verified`: `false` — 2020--2025 remain
  `calendar_year_unresolved` (`historical_twse_calendar_query_semantics_unconfirmed`)
- `historical_backfill_status`: `blocked_historical_calendar_unresolved`

See `docs/v2/open-market-probe/TAIEX_2026_PILOT_ACCEPTANCE.md` and
`data/audits/v2/taiex-calendar/taiex-2026-pilot-window.json`.

Audit repair (2026-08-05): the pilot acceptance now validates the full
`2026-02-26 .. 2026-03-08` interval (previous trading day, official closure,
weekends), counts duplicate dates before any de-duplication, consumes two
independent live-run evidence files, and derives the extraordinary-closure
conclusion from stored, hashed TWSE announcement evidence. This is an audit
implementation repair; it changes no market data and no frozen status.
