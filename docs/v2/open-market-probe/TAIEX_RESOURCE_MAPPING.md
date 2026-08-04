# TAIEX resource mapping: dataset 11755

## Official dataset record

On 2026-08-04, the official [Taiwan Government Open Data Platform dataset 11755](https://data.gov.tw/en/datasets/11755) was read directly.

| Field | Official value |
| --- | --- |
| Dataset ID | 11755 |
| Name | Weighted Stock Price Index Historical Data |
| Description | Historical data of the Taiwan Stock Exchange Weighted Index |
| Providing organisation | Securities and Futures Bureau, Financial Supervisory Commission, Executive Yuan, R.O.C. |
| Update frequency | Every day |
| Format / encoding | CSV / UTF-8 |
| Registered resource | `https://www.twse.com.tw/indicesReport/MI_5MINS_HIST?response=open_data` |
| OAS/OpenAPI documentation | `https://openapi.twse.com.tw/v1/swagger.json` |
| Licence | Open Government Data License, version 1.0 |
| Licence URL | `https://data.gov.tw/license` |

The portal describes this as **primary data** and lists five fields: date, open index, high index, low index, and close index.

## Current probe relationship

The probe endpoint is `https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date=YYYYMMDD&response=json`.

`resource_mapping_status: confirmed` under relationship **B**: both paths are TWSE's `MI_5MINS_HIST` report. The dataset's registered CSV and the RWD JSON endpoint expose the identical five fields. On 2026-08-04 they also agreed for 2026-08-03: open 42,780.42, high 43,784.19, low 42,780.42, close 43,386.41. The CSV is a registered downloadable representation; the RWD endpoint is a date-parameterized JSON representation of the same named report/data product.

## Licence conclusion

Dataset 11755 applies the named licence to its registered TWSE resource. The official licence permits use subject to attribution and includes reuse/transformation terms; the dataset page does not disclose a separate third-party restriction for this index data. This is an engineering evidence conclusion, not legal advice.

Accordingly, this isolated source probe is `open_core_candidate_conditional`, not production-approved. Its remaining gap is calendar verification; no production adapter has been enabled.
