# Data quality summary

Only TAIEX used a live, unauthenticated official response. The three fixed windows were queried twice per request and raw responses were retained only under ignored `.local/source-probes/taiex/`.

| Market | Recent 30 | 2023-06 | 2020-10 | Fields observed | Result |
| --- | ---: | ---: | ---: | --- | --- |
| TAIEX | 30 (2026-06-22–2026-08-03) | 20 (2023-06-01–2023-06-30) | 19 (2020-10-05–2020-10-30) | date, open, high, low, close | 0 date failures, 0 duplicates, 0 weekend records, 0 OHLC-logic violations; source previous close/volume are absent, but previous close and close-to-close return are derived from prior valid trading rows. |
| KOSPI | not fetched | not fetched | not fetched | unverified | Service Key/catalogue record not available. |
| AEX/DNB | not fetched | not fetched | not fetched | unverified | Both specified DNB resourcefile URLs were directly requested but the hostname could not be resolved; no values were inferred. |
| BVL/SMV | not fetched | not fetched | not fetched | unverified | No verified official index resource/data dictionary was identified. |

The TAIEX sample contains no inferred source values. Dataset 11755 supplies a resource-level Open Government Data License v1.0 mapping; the remaining limitation is partial calendar verification, not the absence of a source previous-close field.
