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
