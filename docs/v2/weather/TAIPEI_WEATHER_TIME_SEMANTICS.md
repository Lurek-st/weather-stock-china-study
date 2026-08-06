# Taipei weather time semantics

Instantaneous and one-hour accumulation ERA5 variables must not share one
time mask. This document states the frozen semantics registered in
`config/v2/weather-variable-semantics.yaml` and enforced by
`scripts/v2/core.py::build_weather_windows`.

## Variable classes

A. Instantaneous — the timestamp is the valid time of the value:

- `2m_temperature`
- `2m_dewpoint_temperature`
- `total_cloud_cover`
- `10m_u_component_of_wind`
- `10m_v_component_of_wind`
- `instantaneous_10m_wind_gust`

B. One-hour accumulation — the timestamp ends the preceding full hour:

- `total_precipitation` (interval 2026-03-02T07:00 .. 08:00 Asia/Taipei is
  recorded at valid time 08:00)
- `surface_solar_radiation_downwards`

Canonical outputs keep `air_temperature_c`, `dew_point_c`,
`relative_humidity_pct`, `apparent_temperature_c`, `precipitation_mm`,
`cloud_cover_pct`, `wind_speed_mps`, `max_gust_mps`,
`solar_radiation_mj_m2`. Normalized weather rows keep `timestamp_utc` and add
`accumulation_interval_start_utc` / `accumulation_interval_end_utc` for the
one-hour interval the row timestamp ends. No single row-level
`temporal_support_type` describes a whole row, because a wide row mixes both
classes.

## Window rules

For each trading day:

- `pre_open`: local `07:00 <= t < 09:00`; instantaneous samples 07:00, 08:00
  (expected 2); whole accumulation intervals 07:00-08:00 and 08:00-09:00
  (valid times 08:00, 09:00; expected 2).
- `trading_session`: local `09:00 <= t < 13:30`; instantaneous samples
  09:00-13:00 (expected 5, never `int(4.5h)=4`); whole accumulation intervals
  09:00-10:00 .. 12:00-13:00 (valid times 10:00-13:00; expected 4). The
  13:00-14:00 interval only overlaps 30 minutes and is excluded.
- `full_day`: local `00:00 <= t < next 00:00`; instantaneous samples 00:00-23:00
  (expected 24); whole accumulation intervals 00:00-01:00 .. 23:00-24:00
  (valid times 01:00 .. next 00:00; expected 24). The 2026-03-06 full day
  therefore needs the 2026-03-07 00:00 Asia/Taipei accumulation endpoint.

Accumulation intervals are kept only when
`interval_start >= window_start and interval_end <= window_end`; partial
intervals are excluded, never split by 50%, never interpolated, and never
mixed into window values.

## Aggregation

- Instantaneous: mean (temperature, dew point, relative humidity, apparent
  temperature, cloud cover, wind speed), max (gust).
- Accumulation: sum (precipitation, solar radiation). A one-hour accumulation
  is never averaged to form a window accumulation.

## Quality flags

`missing_hours` (legacy), `missing_instantaneous_hours`,
`missing_accumulation_intervals`, `partial_accumulation_interval_excluded`,
`temporal_support_metadata_missing`. Missing hours are never silently ignored;
windows with insufficient coverage are not marked ready.
