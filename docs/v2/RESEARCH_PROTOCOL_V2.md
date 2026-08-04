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
