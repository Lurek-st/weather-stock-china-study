# Taipei ERA5 pilot contract (2026-03-02 .. 2026-03-06)

**Status:** `dry_run_ready` — configuration, request planning, time semantics,
code and tests are frozen; **no weather data was downloaded** and no CDS API
was contacted.

| Field | Value |
| --- | --- |
| City | taipei (Asia/Taipei) |
| Market | taiex (TWSE TAIEX) |
| Pilot trading dates | 2026-03-02 .. 2026-03-06 |
| Target dataset | `reanalysis-era5-single-levels` |
| Expected data class | `final_reanalysis` (dry-run expectation only) |
| Live requests | not_run |
| Weather data | not_downloaded |

## Coordinate

- `city_id: taipei`, `latitude: 25.0375`, `longitude: 121.5646`
- `coordinate_basis: municipal_reference_point`
- Source: Taipei City Government (`https://www.gov.taipei/`); the point is the
  municipal reference location at Taipei City Hall, No.1 City Hall Road,
  Xinyi District.
- Verified: 2026-08-06. Single frozen point; request area is the point plus
  and minus 0.13 degrees (north/west/south/east), following the existing
  repository small-area convention. No whole-island or multi-point request.

## TAIEX session

TWSE official regular session 09:00-13:30 Asia/Taipei, no midday break
(evidence: TWSE official trading-hours publication; the 08:30 order-input
time is not the open). `config/v2/markets.yaml` registers `taiex` with
`session: {open: "09:00", close: "13:30", breaks: []}` and
`calendar_source_id: twse_official_holiday_schedule`, which is registered in
`config/v2/calendar-registry.yaml` (TWSE official annual holiday schedule).

## UTC request range

Local window `2026-03-02T00:00:00+08:00 .. 2026-03-07T00:00:00+08:00`
translates to UTC `2026-03-01T16:00:00Z .. 2026-03-06T16:00:00Z` (inclusive,
the final full-day accumulation endpoint). Minimal full coverage yields three
request segments and 121 distinct UTC valid times:

| Segment | UTC dates | Times | Hours |
| --- | --- | --- | --- |
| 1 | 2026-03-01 | 16:00-23:00 | 8 |
| 2 | 2026-03-02 .. 2026-03-05 | 00:00-23:00 | 96 |
| 3 | 2026-03-06 | 00:00-16:00 | 17 |

`left_padding_reason: convert_first_local_midnight_to_utc`,
`right_padding_reason: include_final_full_day_accumulation_endpoint`.

## Time semantics

Instantaneous and one-hour accumulation variables are separated per
`config/v2/weather-variable-semantics.yaml`; see
`docs/v2/weather/TAIPEI_WEATHER_TIME_SEMANTICS.md`. For the TAIEX 4.5-hour
session the window expected counts are:

| Window | Instantaneous | Accumulation |
| --- | --- | --- |
| pre_open 07:00-09:00 | 2 | 2 |
| trading_session 09:00-13:30 | 5 | 4 |
| full_day 00:00-24:00 | 24 | 24 |

The 13:00-14:00 accumulation interval only overlaps the session by 30 minutes
and is excluded (`partial_interval_policy: partial_accumulation_interval_excluded`).

## Finality

The 2026-03 window is well beyond the official ERA5 finalization latency
(about two to three months), so the dry-run records
`expected_data_class: final_reanalysis` and
`finality_status: expected_final_by_official_latency`. This is an expectation
based on the official latency policy only: no file was downloaded, no NetCDF
metadata was read, and a future live task must verify the dataset, request
parameters, file metadata and manifest before marking anything final. Days
that have not passed the finalization threshold must not be labelled
`final_reanalysis`.

## CDS credentials

Read-only local readiness is recorded in the dry-run audit. On this machine
`cdsapi 0.7.7` is installed but no `.cdsapirc` exists
(`cdsapirc_status: missing`, `credential_readiness_status:
credential_file_missing`), and dataset terms acceptance is
`acceptance_unverified`. A future live request requires a present,
shape-valid `.cdsapirc` and a user-confirmed terms acceptance recorded outside
the task; the web terms must be accepted manually.

## Dry-run audit

`data/audits/v2/weather-pilot/taipei-era5-dry-run.json` records the full
request plan, variable semantics, area, finality expectation and credential
booleans with `live_requests_run: false`, `weather_data_downloaded: false`,
`historical_backfill_run: false`.

## Next step (not started)

One controlled live download of the Taipei pilot week, after credential and
terms gates are met and the control plane approves.
