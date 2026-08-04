# V2 Source Feasibility Audit

Audit date: 2026-08-04. Protocol/schema: 2.0.0.

## Decision

Weather is technically feasible as a deterministic global pipeline. ERA5T is the provisional core, final ERA5 is the frozen core, and GHCNh is a non-blocking station check. The market requirement is only partly automatable under the stricter combination of official/high-quality, stable, lawful, free, and long-term. V2 therefore ships production-ready acquisition, immutability, normalization, revision, maturity, panel, and audit interfaces, but does **not** pretend that unverified web endpoints are production feeds.

The machine-readable record is `config/v2/source-registry.yaml`; that registry is normative for provider, official/third-party status, dataset/interface, documentation/access URL, account and local-credential requirements, cost, fields, historical coverage, resolution, latency, provisional/final status, automation boundary, licence, stability, role, and fallback.

## Weather

### ERA5T and ERA5

Primary official documentation:

- [ECMWF ERA5 data documentation](https://confluence.ecmwf.int/display/CKB/ERA5%3A+data+documentation)
- [CDS ERA5 single-level hourly catalogue](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels)
- [CDS API setup](https://cds.climate.copernicus.eu/how-to-api)

ECMWF states that ERA5T daily updates are available about five days behind real time, at no guaranteed time of day, and that CDS replaces ERA5T for a month with final ERA5 about two months after the month concerned. The catalogue warns that preliminary data can differ from the final release two to three months later. Access is free but requires a CDS account, acceptance of dataset terms, and a local CDS API token. The catalogue currently identifies a CC-BY licence. Requests can be automated through `cdsapi`; rate and queue limits still apply.

Fields frozen for V2: 2 m temperature, 2 m dew point, total precipitation, total cloud cover, 10 m u/v wind, instantaneous 10 m gust, and surface solar radiation downwards. ERA5T records are `provisional_reanalysis`; ERA5 records are `final_reanalysis`. Neither is a ground-station observation.

Failure rule: wait and retain the prior provisional revision. Never interpolate a missing core hour silently. ERA5T is sufficient for a provisional panel; only final ERA5 can satisfy the weather side of `frozen`.

### NOAA GHCNh

Primary documentation:

- [NOAA NCEI GHCNh product page](https://www.ncei.noaa.gov/products/global-historical-climatology-network-hourly)
- [NOAA bulk hourly access](https://www.ncei.noaa.gov/data/global-hourly/)
- [NOAA station inventory](https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv)

NOAA describes GHCNh as hourly/synoptic observations from fixed land stations and recommends web-accessible ASCII folders for large volumes. It is public, free, and can be downloaded without a credential. Availability and fields vary by contributing station; therefore GHCNh is `station_observation`, used to estimate reanalysis bias, and never blocks core panel completeness.

Eight representative coordinates and two frozen station candidates each are in `config/v2/locations.yaml` and `config/v2/stations.yaml`. Distances were calculated from the 2026-08-04 NOAA inventory snapshot. Shenzhen's closest candidates are Hong Kong stations, so it is explicitly high-risk/cross-jurisdiction rather than silently treated as a Shenzhen station.

## Market-by-market finding

| Market | Registered primary | Official | Stable free automation decision | Core field | Fallback |
|---|---|---:|---|---|---|
| Beijing—BSE 50 | Beijing Stock Exchange controlled export | yes | `manual_source_required`; no stable documented bulk contract was frozen | official close only | unavailable; never news |
| Shanghai—SSE Composite | SSE controlled export | yes | `manual_source_required`; public page is evidence, not an API contract | official close only | unavailable; never news |
| Shenzhen—SZSE Component | CNI/SZSE controlled export | yes | `manual_source_required`; dynamic page endpoint/terms are not frozen | official close only | unavailable; never news |
| Tokyo—TOPIX | JPX Historical Index Value | yes | `official_manual_import`; JPX publishes historical values, but direct file URL/version contract remains to be frozen | official close only | unavailable; never news |
| Mumbai—NIFTY 50 | NSE Historical Index Data | yes | `official_manual_import`; the official report exists but session/cookie behavior and automated-use terms require review | official close only | unavailable; never news |
| London—FTSE 100 | FTSE Russell/LSEG licensed data | yes | `manual_source_required`; historical redistribution/automation is proprietary and likely licensed | official close only | licensed vendor/controlled file |
| Frankfurt—DAX | STOXX licensed data | yes | `manual_source_required`; STOXX offers index data/licensing, not a confirmed free bulk contract | official close only | licensed vendor/controlled file |
| New York—S&P 500 | S&P DJI licensed data | yes | `manual_source_required`; official history is proprietary | official close only | FRED is registered but disabled pending series-terms review |

Primary pages checked:

- [SSE Composite](https://www.sse.com.cn/market/sseindex/indexlist/basic/index.shtml?COMPANY_CODE=000001)
- [CNI SZSE Component](https://www.cnindex.com.cn/module/index-detail.html?act_menu=1&indexCode=399001)
- [JPX TOPIX](https://www.jpx.co.jp/english/markets/indices/topix/index.html)
- [NSE Historical Index Data](https://www.nseindia.com/reports-indices-historical-index-data)
- [FTSE UK Series](https://www.lseg.com/en/ftse-russell/indices/uk)
- [STOXX DAX](https://www.stoxx.com/index-details?symbol=DAX)
- [S&P 500](https://www.spglobal.com/spdji/en/indices/equity/sp-500/)
- [FRED SP500 series](https://fred.stlouisfed.org/series/SP500)

Public visibility of a value does not itself grant automated bulk collection or redistribution. V2 therefore accepts controlled official/vendor files through an immutable import path, stores the licence identifier in each manifest, and refuses to elevate news or an unregistered scraper.

## Calendars

Each market configuration freezes timezone, regular cash-session boundaries, and lunch breaks. `config/v2/calendar-registry.yaml` separately freezes the official evidence source and access boundary for all eight markets. The calendar source ID points to official exchange evidence that must be stored append-only for the target period. A homepage is not calendar verification. Normal days, holidays, weekends, early closes, special sessions, temporary closures, auction changes, and rule changes are distinct statuses. Fixture calendars test the engine only; a live week cannot become research-ready until official evidence or an approved machine-readable calendar is registered and hashed.

## Remaining feasibility gates

1. Create and configure a free CDS account locally.
2. Complete legal/terms review and freeze direct, stable official file contracts for JPX and NSE.
3. Decide whether controlled manual official exports are acceptable for the three Chinese indices.
4. Obtain licensed history or an explicitly permitted research feed for FTSE 100, DAX, and S&P 500.
5. Validate GHCNh candidate continuity against the current GHCNh inventory during the first live week.

Until those gates are cleared, the eight-market synthetic fixture is engineering evidence only, not evidence that all eight live market sources are automated.
