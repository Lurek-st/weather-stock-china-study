# KOSPI index inventory

`KOREA_DATA_GO_KR_SERVICE_KEY` was not present during this probe. No official
payload was requested, so this is deliberately an inventory **plan**, not an
assertion that an exact `idxNm` value exists.

With a key, the adapter first performs the official `likeIdxNm=KOSPI` precheck,
records every returned index name and identifier field actually present, then
accepts a main series only if the payload distinguishes the Korean Composite
Stock Price Index from KOSPI 200, KOSPI 100, KOSPI 50, sector, total-return,
derivative, and KOSDAQ series. It will record the portal's 2024-12-06 naming
change evidence before joining records across that date.

Current result: `not_inspected_without_credential`. Recommended main series:
unresolved until a credentialed official payload uniquely identifies the
composite KOSPI rather than a KOSPI sub-index.
