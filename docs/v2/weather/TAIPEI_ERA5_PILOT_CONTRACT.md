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
- `coordinate_derivation: derived_from_official_taipei_city_hall_address`
- `coordinate_source`: Taipei City Government official City Hall page
  (`https://www.gov.taipei/cp.aspx?n=8E989D7C8B359DF3&s=C5C52F5B391356DB`,
  "市府大樓"; 11008 No.1 City Hall Road, Xinyi District; phone 02-27208889).
- `coordinate_evidence_status: official_address_with_derived_coordinate` —
  the coordinates are a reproducible reference point derived from the
  official City Hall address; the page does not itself publish the exact
  latitude/longitude.
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

The 2026-03 window is beyond the official ERA5 finalization latency, and the
conservative eligibility gate (target month + 3 full months + 1 day) gives
`final_eligibility_date: 2026-07-01`, which is before the retrieval date, so
the dry-run records `expected_data_class: final_reanalysis` and
`finality_status: expected_final_by_official_latency`. This is an expectation
based on the official latency policy and the date gate only: no file was
downloaded, no NetCDF metadata was read, and the live task must verify the
dataset, request parameters, file metadata and manifest before marking
anything final. `--final` alone never grants finality; requests whose target
month has not passed the gate are labelled `not_yet_eligible_for_final` and
stay `provisional_reanalysis`, and a live `--final` request is refused.

## CDS credentials

Read-only local readiness is recorded in the dry-run audit. On this machine
`cdsapi 0.7.7` is installed but no `.cdsapirc` exists
(`cdsapirc_status: missing`, `url_field_present: false`,
`key_field_present: false`, `credential_readiness_status:
credential_file_missing`), and dataset terms acceptance is
`acceptance_unverified` (no local confirmation file exists under
`.local/agreements/`). A future live request requires a present,
shape-valid `.cdsapirc` and a local, non-sensitive terms-confirmation file
(`.local/agreements/cds-era5-single-levels.json`, created only after the user
explicitly accepts the dataset terms in the browser). Credential values are
never read back, stored or printed.

## Dry-run audit

`data/audits/v2/weather-pilot/taipei-era5-dry-run.json` records the full
request plan (3 segments with stable ids), variable semantics, area,
coordinate evidence, finality gate, credential booleans with
`live_requests_run: false`, `weather_data_downloaded: false`,
`historical_backfill_run: false`. Offline commands write this file only when
`--audit-output` is given.

## Live readiness (2026-08-06 repair)

The live branch is implemented and exercised offline by an injected fake CDS
client in tests (3 segments, multi-date list for segment 2, no
`segment["utc_date"]` dependency). Staging uses a unique temporary directory;
downloaded files are validated (exists, non-empty, openable NetCDF, all
requested variables, non-empty timestamps within the request plan) before the
append-only raw store. Live results report only relative manifest paths,
artifact ids, revisions, SHA-256 and skipped flags. A live `--final` request
is refused unless the finality date gate passes. No CDS API was called in
this round.

## Next step (not started)

The user must configure the CDS account/API key in `~/.cdsapirc` and accept
the ERA5 dataset terms in the browser (dataset licensed CC BY); then one
controlled live Taipei pilot-week download can proceed after control-plane
approval.
