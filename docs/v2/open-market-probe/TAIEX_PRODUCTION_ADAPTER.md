# TAIEX production-adapter candidate

`scripts/v2/adapters/market/taiex.py` is a candidate adapter, not a production
connection. Its default and `--dry-run` modes send no network request. `--live`
also requires `--local-only`, performs explicit monthly TWSE requests, stores
raw responses append-only under ignored `.local/source-raw/taiex/`, and derives
previous close and return from preceding valid trading records.

The adapter is not connected to weather windows, `run_mature_week.py`, a panel,
or a backfill.

## 2026 market-only pilot acceptance

On 2026-03-02 .. 2026-03-06 the candidate adapter ran twice with
`--live --local-only` (months 202602 and 202603). Both runs returned 6
normalized records (2026-02-26 + Monday to Friday); the second run was
byte-identical and skipped by the append-only store. Calendar x market
cross-validation passed with no issues (previous close, return
recalculation, and OHLC checks all pass), so:

- `adapter_status`: `market_only_pilot_accepted`
- `production_status`: `not_connected` (unchanged)

See `docs/v2/open-market-probe/TAIEX_2026_PILOT_ACCEPTANCE.md` and
`data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json`.
