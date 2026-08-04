# TAIEX pilot runbook

1. Run the calendar builder in dry-run mode, then bounded live mode only after
   reviewing the TWSE annual resources for the pilot windows.
2. Reconcile official calendar records with the existing TAIEX sample windows;
   unresolved conflicts block the pilot.
3. Run the adapter with `--dry-run` for a bounded date range.
4. Only after calendar evidence closes, configure Taipei weather and execute
   one mature-week end-to-end acceptance.

Do not download ERA5/ERA5T or start any historical backfill in these steps.
