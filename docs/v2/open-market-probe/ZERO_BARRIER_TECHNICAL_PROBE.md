# Zero-barrier technical probe

Raw responses are retained only in ignored
`.local/source-probes/zero-barrier-shortlist/`; no response payload is
versioned. The committed machine audit records only URLs, headers, hashes,
counts, and outcomes.

| Candidate | Requests | Result | Repeatability |
| --- | ---: | --- | --- |
| Bank of Canada Valet candidate | 2 | `ConnectionError` on both attempts | `request_failed` |
| Banco Central do Brasil SGS candidate | 2 | HTTP 404, JSON, 112 bytes on both attempts | `byte_identical` failure |
| RBA F1 workbook | 2 | HTTP 200, binary workbook, 467,367 bytes | `byte_identical` |

The RBA workbook's own metadata marks its observations monthly. It contains an
ASX source/proprietary-data notice, so successful transport is not evidence of
eligibility. No source was queried beyond the two bounded resource attempts.
