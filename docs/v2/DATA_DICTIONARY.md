# V2 Data Dictionary

The generated `data_dictionary.json` is authoritative for each concrete panel revision.

Core identifiers are `schema_version`, `panel_tier`, `city_id`, `market_id`, and local `trading_date`. Market fields are `official_close`, `previous_official_close`, `close_to_close_return_pct`, `source_timestamp`, `value_status`, `calendar_status`, `calendar_source_id`, `source_id`, `revision`, `is_final`, `currency`, and `quality_flags`.

Each core weather variable is emitted with `pre_open_`, `trading_session_`, and `full_day_` prefixes:

| Variable | Unit | Window aggregation |
|---|---|---|
| `air_temperature_c` | °C | mean |
| `dew_point_c` | °C | mean |
| `relative_humidity_pct` | % | mean; Magnus-derived |
| `apparent_temperature_c` | °C | mean; versioned Steadman shaded formula (`steadman_v1`) |
| `precipitation_mm` | mm | sum |
| `cloud_cover_pct` | % | mean |
| `wind_speed_mps` | m/s | mean vector magnitude |
| `max_gust_mps` | m/s | maximum |
| `solar_radiation_mj_m2` | MJ/m² | sum |

`pre_open_temperature_anomaly_c` is the difference from the city/month sample mean; `pre_open_temperature_z` divides that anomaly by its sample standard deviation. Production analysis should use a longer frozen climatological baseline and record its version.
