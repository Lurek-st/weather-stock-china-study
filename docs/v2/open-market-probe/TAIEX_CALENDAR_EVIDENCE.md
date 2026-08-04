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
