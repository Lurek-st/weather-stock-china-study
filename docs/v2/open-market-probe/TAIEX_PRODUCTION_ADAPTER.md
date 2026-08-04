# TAIEX production-adapter candidate

`scripts/v2/adapters/market/taiex.py` is a candidate adapter, not a production
connection. Its default and `--dry-run` modes send no network request. `--live`
also requires `--local-only`, performs explicit monthly TWSE requests, stores
raw responses append-only under ignored `.local/source-raw/taiex/`, and derives
previous close and return from preceding valid trading records.

The adapter is not connected to weather windows, `run_mature_week.py`, a panel,
or a backfill. Its status is `production_adapter_candidate`; production status
remains `not_connected` until the calendar and real mature-week gates close.
