# TAIEX pilot runbook

1. Run the calendar builder in dry-run mode, then bounded live mode only after
   reviewing the TWSE annual resources for the pilot windows.
2. Reconcile official calendar records with the existing TAIEX sample windows;
   unresolved conflicts block the pilot.
3. Run the adapter with `--dry-run` for a bounded date range.
4. Only after calendar evidence closes, configure Taipei weather and execute
   one mature-week end-to-end acceptance.

Do not download ERA5/ERA5T or start any historical backfill in these steps.

## Status: 2026 market-only pilot accepted (2026-03-02 .. 2026-03-06)

Steps 1--3 completed for the 2026 pilot window:

- Official 2026 calendar used to select the earliest eligible week
  (`2026-03-02 .. 2026-03-06`); no extraordinary closure announcement found in
  official TWSE sources.
- Candidate adapter run twice with `--live --local-only` (months 202602/202603);
  raw responses append-only under `.local/source-raw/taiex/`, byte-identical
  across runs.
- Calendar x market cross-validation passed: 5/5 expected open days matched,
  previous close and close-to-close returns recomputed independently, OHLC
  checks passed, no duplicates or unexpected dates.

Resulting state: `pilot_calendar_status = calendar_verified_for_2026_pilot_window`,
`adapter_status = market_only_pilot_accepted`. Step 4 (weather end-to-end) is
not started. Full-history calendar (2020--2025) remains unresolved and
historical backfill remains blocked.

Audit repair (2026-08-05): the acceptance audit was strengthened (duplicate
detection before de-dup; full validation interval 2026-02-26..03-08 including
closures and weekends; two independent run evidence files; closure conclusion
derived from stored TWSE announcement evidence). Run the tools in this order:

```text
python scripts/v2/taiex_extraordinary_closures.py
python scripts/v2/taiex_repeatability.py
python scripts/v2/taiex_pilot_acceptance.py
```

## Weather side: Taipei ERA5 pilot contract (2026-08-06, dry-run only)

The weather contract for the accepted market pilot week is frozen but no data
was downloaded:

1. `config/v2/locations.yaml` registers `taipei` (municipal reference point,
   25.0375, 121.5646, Asia/Taipei).
2. `config/v2/markets.yaml` registers `taiex` (09:00-13:30, no break, TWD)
   referencing `twse_official_holiday_schedule`, registered in
   `config/v2/calendar-registry.yaml`.
3. `config/v2/weather-variable-semantics.yaml` separates instantaneous from
   one-hour accumulation variables.
4. `scripts/v2/fetch_era5.py` is zero-network by default; `--pilot-only`
   plans only the primary city; UTC coverage is computed from the local
   window (3 segments, 121 hours).
5. Windows: pre_open 2/2, trading_session 5/4 (partial 13:00-14:00 interval
   excluded), full_day 24/24.

Generate the offline dry-run audit:

```text
python scripts/v2/fetch_era5.py --pilot-only --final --audit-output data/audits/v2/weather-pilot/taipei-era5-dry-run.json
```

Live readiness repair (2026-08-06): multi-date request segments use the full
`utc_dates` list; the finality gate is a date threshold (target month + 3
months + 1 day; `2026-07-01` for the 2026-03 window) so `--final` alone never
grants finality; `expected_hour_count` equals the instantaneous count (TAIEX
trading session 5, not 4); staging uses a unique temporary directory; live
output contains only relative paths; dry-run writes no files unless
`--audit-output` is given.

Exact UTC timestamp validation (2026-08-06): before persistence every staged
NetCDF is checked against the request's date x time cartesian product —
missing, unexpected, duplicate or non-hourly timestamps, or an observed count
mismatch, fail validation (bounded listing, no absolute paths) and block
persistence. A correct date with missing or wrong hours is still a failure.

stepType ZIP container support (2026-08-06): the pilot variable set mixes GRIB
`stepType`s, so CDS returns several NetCDF members inside a ZIP even when
`unarchived` is requested (official behaviour since the 2024-11 NetCDF
conversion update). The contract now uses `download_format: zip` with
`expected_download_container: zip` (`mixed_grib_step_types_produce_multiple_netcdf_members`);
staging uses neutral `*.download` names; magic-byte detection identifies ZIP
vs NetCDF; safe ZIP inspection rejects corrupt/empty/encrypted/path-traversal/
absolute/drive/symlink/nested-archive/duplicate/oversize/zip-bomb members;
every NetCDF member is validated separately (exact UTC timestamps, spatial
grid inside the area, identical grid across members) and the member variable
union must cover all eight requested variables with no duplicates; the raw
ZIP as returned by CDS is the persisted artifact (suffix `.zip`, manifest
carries a non-sensitive container summary). The first real attempt
(run `20260806T090232Z`) returned the expected ZIP; the old single-NetCDF
client rejected it, 0 artifacts persisted, no retry. This round only fixed
code and tests; **no CDS API was called**.

The next step is one controlled live retry of the identical Taipei pilot-week
contract (3 segments, 121 UTC times, unchanged dates/hours/variables/area;
only the expected container is corrected from single NetCDF to a ZIP of
NetCDF members), after the control plane approves. Do not run `--live` before
that approval.
