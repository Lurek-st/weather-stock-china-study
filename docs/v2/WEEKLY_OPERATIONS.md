# Weekly Operations V2

The normal Monday run selects a complete week using maturity evidence; “two weeks ago” is a scheduling default, not an unconditional rule.

1. Review official calendar exceptions and registered source incidents.
2. Run `python scripts/v2/check_source_readiness.py`.
3. Preview with `python scripts/v2/run_mature_week.py --week YYYY-Www --dry-run`.
4. Run provisional acquisition only when core weather, calendar, and market gates allow it.
5. Validate raw hashes, canonical records, window coverage, returns, row counts, and audit.
6. Retain the provisional panel for engineering/exploration.
7. After final ERA5 replaces ERA5T and official market corrections settle, rebuild from raw and freeze a new revision.

Useful modes:

- `--offline`: use only versioned fixtures/local artifacts.
- `--refresh`: re-query registered sources; changed bytes create revisions.
- `--allow-provisional`: permit ERA5T panel generation.
- `--freeze`: require final data and write the frozen tier.
- `--dry-run`: plan without downloads or panel writes.

Exit codes: 0 success; 2 validation failure; 3 validation-only station source unavailable; 4 maturity/credential/licence/input gate not satisfied.

Dependency purposes: `cdsapi` is the official CDS download client; `xarray` and `netCDF4` read ERA5 NetCDF; `pyarrow` produces interoperable Parquet; `requests` supports documented HTTPS/API adapters; NumPy/Pandas perform deterministic transforms; PyYAML/JSON Schema validate registries and records. GitHub Actions uses fixtures only and requires no production credential or large download.
