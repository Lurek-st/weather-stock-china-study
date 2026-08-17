# Zero-barrier official market source shortlist

**Result:** `NO_TIER_1_CANDIDATE_FOUND`.

This is a bounded source screen, not a production integration or an index
selection. It excludes sources requiring registration, a key, a local phone,
payment, login cookies, or an unclear right to publish a derived panel.

| Candidate | Official institution | Access result | Rights result | Tier / score |
| --- | --- | --- | --- | --- |
| Canada — S&P/TSX Composite | Bank of Canada | Two small official Valet requests failed with `ConnectionError` through the current proxy path. | Bank of Canada terms are identified, but S&P/TSX third-party public-redistribution rights were not established. | `tier_3_technical_risk` / 0 |
| Brazil — Ibovespa | Banco Central do Brasil | Two requests to the documented bounded SGS-series candidate returned identical HTTP 404 responses. | B3/Ibovespa third-party rights remain unresolved. | `tier_3_technical_risk` / 0 |
| Australia — RBA F1 | Reserve Bank of Australia | Two requests were byte-identical and downloaded the workbook. Workbook inspection showed a monthly interest-rates table, not a qualifying daily stock-index series. | Its notes identify ASX end-of-day benchmark data as proprietary and all rights reserved. | `rejected` / 0 |

No candidate has both an accessible daily end-of-day series and an explicit,
resource-level right to publish public derived research data. The screen does
not recommend investing adapter work in any of these sources.

Official evidence: [Bank of Canada Valet](https://www.bankofcanada.ca/valet/),
[Banco Central do Brasil open data](https://dadosabertos.bcb.gov.br/), and
[RBA historical Table F1](https://www.rba.gov.au/statistics/tables/xls/f01hist.xlsx).
