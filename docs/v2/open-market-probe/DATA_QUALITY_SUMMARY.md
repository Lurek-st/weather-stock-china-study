# Data quality summary

Only TAIEX used a live, unauthenticated official response. The three fixed windows were queried twice per request and raw responses were retained only under ignored `.local/source-probes/taiex/`.

| Market | Recent 30 | 2023-06 | 2020-10 | Fields observed | Result |
| --- | ---: | ---: | ---: | --- | --- |
| TAIEX | 30 (2026-06-22–2026-08-03) | 20 (2023-06-01–2023-06-30) | 19 (2020-10-05–2020-10-30) | date, open, high, low, close | 0 date failures, 0 duplicates, 0 weekend records, 0 OHLC-logic violations; previous close and volume are absent from this endpoint. |
| KOSPI | not fetched | not fetched | not fetched | unverified | Service Key/catalogue record not available. |
| AEX/DNB | not fetched | not fetched | not fetched | unverified | DNB direct statistical pages returned HTTP 403. |
| BVL/SMV | not fetched | not fetched | not fetched | unverified | No verified official index resource/data dictionary was identified. |

The TAIEX sample contains no inferred values. It does **not** validate a production source: licence/third-party rights and calendar verification are still unresolved, and the endpoint does not provide previous close or volume for return cross-checking.
