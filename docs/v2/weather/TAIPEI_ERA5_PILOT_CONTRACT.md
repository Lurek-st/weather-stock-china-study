# Taipei ERA5 pilot contract (2026-03-02 .. 2026-03-06)

**Status:** `live_code_ready_step_type_zip_supported` — configuration, request
planning, time semantics, code and tests are frozen for the ZIP container
contract. **No validated weather raw artifact exists yet**: the first real CDS
attempt (2026-08-06) returned the expected ZIP container, the pre-ZIP client
rejected it (single-NetCDF assumption), 0 artifacts were persisted, and no
retry was made. The corrected ZIP pipeline is fully tested offline.

| Field | Value |
| --- | --- |
| City | taipei (Asia/Taipei) |
| Market | taiex (TWSE TAIEX) |
| Pilot trading dates | 2026-03-02 .. 2026-03-06 |
| Target dataset | `reanalysis-era5-single-levels` |
| Expected data class | `final_reanalysis` (expectation only; verified after download) |
| Download container | `zip` (multiple NetCDF members) |
| Live requests | not_run (one attempt failed container validation; retry approved but not run) |
| Weather data | no_validated_raw_artifact |

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

## Download container contract (stepType-split ZIP)

The pilot variable set mixes GRIB `stepType`s: instantaneous variables
(`2m_temperature`, `2m_dewpoint_temperature`, `total_cloud_cover`,
`10m_u/v_component_of_wind`, `instantaneous_10m_wind_gust`) and one-hour
accumulations (`total_precipitation`, `surface_solar_radiation_downwards`).
Since the 2024-11 ECMWF NetCDF conversion update, CDS splits NetCDF output by
`stepType`; multiple NetCDF members are returned inside a **ZIP**, even when
`download_format: unarchived` was requested. The project therefore treats ZIP
as the formal transfer container:

- `data_format: netcdf`, `download_format: zip`
- `expected_download_container: zip`
- `container_reason: mixed_grib_step_types_produce_multiple_netcdf_members`
- ZIP is only a transport container; every inner member must still be NetCDF;
  the weather data semantics are unchanged.
- The request plan hash includes `download_format` (it is part of the request
  contract); current value
  `73824d434f115e045209c26b7ad3f6ea9608997b531704fbb2084268719384f1`.

Container validation (`validate_download_container`): magic-byte detection
(ZIP `PK\\x03\\x04`/`PK\\x05\\x06`, NetCDF classic `CDF\\x01`/`CDF\\x02`,
NetCDF4/HDF5 `\\x89HDF\\r\\n\\x1a\\n`; never the file extension), safe ZIP
inspection (corrupt/empty/encrypted/path-traversal/absolute/drive/symlink/
nested-archive/duplicate/case-insensitive/oversize/zip-bomb members rejected),
per-member exact UTC timestamp-set validation, spatial grid checks (each
member inside the requested area, identical grid across members — latitude
sort direction may differ), and a variable-union check (all eight requested
variables present, each in exactly one member). Only
`container_validation_passed = true` lets the raw bytes persist; the **raw
ZIP as returned by CDS** is the immutable artifact (suffix `.zip`), and the
manifest records a non-sensitive container summary
(`container_type`, `member_count`, `member_names`, `member_sha256`,
`observed_variable_union`, `container_validation_passed`).

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
`cdsapi 0.7.7` is installed; the user configured `~/.cdsapirc`
(`cdsapirc_status: present_shape_valid`, `url_field_present: true`,
`key_field_present: true`, non-empty values confirmed by boolean check only),
and explicitly accepted the dataset terms in the browser; the local
confirmation file `.local/agreements/cds-era5-single-levels.json` exists
(`dataset_terms_status: user_confirmed_outside_task`).
`credential_readiness_status: ready_for_future_live_request`. Credential
values are never read back, stored or printed; readiness exposes booleans
only.

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
downloaded files are validated before the append-only raw store. Live results
report only relative manifest paths, artifact ids, revisions, SHA-256 and
skipped flags. A live `--final` request is refused unless the finality date
gate passes. No CDS API was called in this round.

### Exact UTC timestamp-set validation (2026-08-06)

Before any file enters the raw store, `validate_netcdf` performs an exact
UTC timestamp-set check against the request's `date x time` cartesian product
(e.g. 8 timestamps for the single-day segment, 96 for the four-day middle
segment). Observed timestamps are normalized to whole hours — timezone-naive
treated as UTC, timezone-aware converted to UTC. Any missing, unexpected,
duplicate or non-hourly timestamp, or an observed count different from the
expected count, fails validation (bounded listing of at most 20 values with
total counts, no local absolute paths) and blocks persistence. Live segment
results carry a `timestamp_validation` summary
(`expected_timestamp_count`, `observed_timestamp_count`,
`timestamp_set_match`, `netcdf_validation_passed`).

### stepType ZIP container support (2026-08-06)

The first real CDS request (run `20260806T090232Z`) succeeded
(accepted -> running -> successful) but returned a ZIP archive, not a single
unarchived NetCDF. The then-current client assumed one NetCDF per download,
so `validate_netcdf` rejected the staged file; 0 raw artifacts were persisted
and no retry was made (validation failures are not retried). The evidence is
preserved under `.local/runs/taipei-era5-live/20260806T090232Z/` (local only,
not committed). This round adds the corrected pipeline: `download_format:
zip`, neutral staging names (`*.download`), magic-byte container detection,
safe ZIP inspection, per-member exact UTC timestamp-set + spatial + variable
union validation, and persistence of the raw ZIP. All tests are offline; no
CDS API was called in this round.

## Next step (not started)

One controlled live retry of the identical Taipei pilot-week contract (3
segments, 121 UTC times, same dates/hours/variables/area; only the expected
container is corrected from single NetCDF to a ZIP of NetCDF members), after
control-plane approval.
