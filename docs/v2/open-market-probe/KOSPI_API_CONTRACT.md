# KOSPI official API contract

This is an engineering and data-governance record, not legal advice.

## Confirmed portal metadata

- **Dataset ID:** `15094807`
- **Dataset name:** Financial Services Commission_Index Price Information
- **Portal provider:** Financial Services Commission (FSC)
- **Underlying data described by the portal:** prices for major Korea Exchange
  indices, including KOSPI and KOSDAQ
- **Format:** JSON and XML REST OpenAPI
- **Service:** `GetMarketIndexInfoService/getStockMarketIndex`
- **Coverage claim:** available from 2020-01-01
- **Frequency and delay:** daily; after 13:00 on the business day following the
  reference date
- **Name-change warning:** some index names changed after 2024-12-06; consult
  the official user guide before stitching a live series
- **Metadata registration:** 2021-11-16; metadata last modified 2026-01-02
- **Contact:** FSC Financial Public Data Team, as identified in the portal
- **Declared permission scope:** `이용허락범위 제한 없음` (portal metadata says no
  stated use-permission restriction)

## Access contract

The official endpoint is:

`https://apis.data.go.kr/1160100/service/GetMarketIndexInfoService/getStockMarketIndex`

The controlled adapter sends `serviceKey`, `resultType=json`, paging fields,
and only documented index/date filters. The key is URL-encoded by the HTTP
client and redacted from errors and audit output. No request occurs until a
locally supplied `KOREA_DATA_GO_KR_SERVICE_KEY` is present.

The public page identifies the service as free and its development/operation
use applications as automatic approval. However, the attempted overseas
individual registration path requires a Korean local mobile number. The attempt
was stopped without using a virtual or borrowed number. This is an access
credential blocker, not evidence of a paid service, API failure, or licence
failure. It does not supply a public payload without a Service Key, so actual
fields, pagination, inclusion semantics, and the exact KOSPI series are
intentionally not asserted.

## Rights boundary

“No stated use-permission restriction” applies to the FSC portal metadata. The
same official description says the FSC collects and opens linked data from
data-holding institutions and describes the underlying index provider as the
Korea Exchange. The consulted public pages do not expressly resolve whether
KRX retains separate rights for raw KOSPI responses or public redistributed
canonical/derived datasets. Those fields remain `unclear`; no legal conclusion
or public-data release is inferred.

## Evidence

- [English dataset page](https://www.data.go.kr/en/data/15094807/openapi.do)
- [Official dataset metadata](https://www.data.go.kr/catalog/15094807/openapi.json)
- [Official stock-index operation](https://apis.data.go.kr/1160100/service/GetMarketIndexInfoService/getStockMarketIndex)

Accessed 2026-08-04.
