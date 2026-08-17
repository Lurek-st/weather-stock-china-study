# Research Protocol V2

Version 2.0.0 defines an observational, reproducible weather–market association study. It does not support causal language.

- Twenty trading days are an engineering acceptance sample, not an inferential sample.
- Historical backfill must cover at least three years; extend to five when licence, coverage, and storage permit.
- The primary weather exposure is the local two hours before official cash-market open (`pre_open`).
- `trading_session` is a concurrent association exposure. `full_day` is descriptive/robustness-only because it includes post-close weather.
- The primary outcome is official close-to-previous-official-close return. OHLC, range, volume, breadth, turnover, and auxiliary indices are optional.
- Continuous weather variables are retained. Scores are visualization aids, never primary model inputs.
- Temperature enters as local seasonal anomaly or standardized exposure, with the transformation/version recorded.
- Models control weekday, month/season, and a justified market-wide factor.
- Use Newey–West or another justified time-series robust covariance estimator.
- Control false discovery with Benjamini–Hochberg and report the full tested family.
- Missing values remain missing. Correlation is not causation.
- Provisional and frozen observations cannot be pooled for final inference. Final conclusions use only a single documented frozen revision.

The model plan, hypotheses, exclusions, correction family, and revision ID must be fixed before final estimation.

## Weather data time semantics (2026-08-06)

- Instantaneous ERA5 variables (2m temperature, 2m dew point, total cloud
  cover, 10m u/v wind, instantaneous gust) are valid at their timestamp.
- One-hour accumulation variables (total precipitation, surface solar
  radiation) represent the accumulation ending at their timestamp over the
  preceding hour. Windows aggregate them only when the whole interval lies
  inside the window; partial intervals are excluded and flagged, never
  split or interpolated.
- The variable registry is `config/v2/weather-variable-semantics.yaml`;
  normalized rows carry `accumulation_interval_start_utc` /
  `accumulation_interval_end_utc`.
- The Taipei pilot contract (`docs/v2/weather/TAIPEI_ERA5_PILOT_CONTRACT.md`)
  freezes the 2026-03-02..06 pilot week request plan (3 segments, 121 UTC
  hours). Only a dry-run audit exists; no ERA5 data has been downloaded and
  no CDS API has been called. A future live download must verify dataset,
  parameters, file metadata and manifest before any `final_reanalysis`
  labelling, and `--final` is gated by a conservative finality date
  (target month + 3 months + 1 day).
