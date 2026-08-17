# Historical Backfill Plan

Start with:

```bash
python scripts/v2/backfill_history.py --start 2023-01-01 --end 2025-12-31 --dry-run
```

The plan estimates requests, hourly observations, market records, data size, credentials, blocked sources, and licence risks. Do not start the live download until the CDS account works, market-source licences are approved, target disk space is accepted, and a one-week live run passes.

Execute by calendar year and source, hash each raw artifact, normalize independently, then rebuild the entire canonical and panel layers. Re-run an overlapping month to demonstrate idempotency. Expand to five years only after the three-year audit is closed.
