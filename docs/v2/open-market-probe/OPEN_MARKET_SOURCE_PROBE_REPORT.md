# Open Market Source Probe Report

**Scope.** This is a controlled discovery probe, not a production integration, historical backfill, candidate-list decision, or ChatGPT task migration. Candidate data are not connected to the mature-week pipeline.

## Results

| Candidate | Technical probe | Rights/quality conclusion | Recommended status |
| --- | --- | --- | --- |
| Taipei—TAIEX | Dataset 11755, its registered TWSE CSV resource, and the RWD JSON report were directly checked. The three prescribed samples and repeated requests succeeded. | Dataset 11755 applies the Open Government Data License v1.0 to the primary-data resource; previous close/return are deterministically derived from the preceding valid trading row. | `open_core_candidate_conditional` |
| Seoul—KOSPI | FSC dataset 15094807 and its `getStockMarketIndex` operation were directly verified. The local Service Key is absent, so no API payload was requested. | FSC metadata declares unrestricted use-permission scope, but the public contract does not expressly settle KRX raw/canonical/derived redistribution rights. KOSPI series identity and data quality are unverified. | `credential_required` |
| Amsterdam—AEX/DNB | The Dutch catalogue still lists both registered JSON resources as CC-BY 4.0. A bounded Windows diagnostic returned explicit NXDOMAIN for `statistiek.api.dnb.nl` through the default resolver, Cloudflare, and Google; direct no-proxy curl could not resolve the host. No cookie, alternate endpoint, or access-control bypass was used. | Resource-file content, AEX definition, and AEX/international-series provenance remain unreadable; catalogue licence does not prove all third-party index rights. DNB's newer API needs a My DNB Public subscription for available datasets, but this dataset's migration is unconfirmed. | `technical_access_blocked` |
| Lima—BVL/SMV | Peru's portal is reachable, but the bounded official search did not yield a verified SMV/BVL broad-index data dictionary plus actual resource. | ODbL applicability and the distinction between locally owned BVL and S&P/BVL/other third-party series are unresolved. | `technical_access_blocked` |

## DNB index inventory

No inventory is reported. The prescribed DNB resourcefile hostname returns public-DNS NXDOMAIN, while the official catalogue still lists both exact URLs. Therefore it would be misleading to claim that the resource contains (or excludes) AEX, S&P 500, FTSE 100, DAX, Nikkei, TOPIX, or any other index. Each remains `third_party_rights_unresolved` pending direct resource access and source-level provenance.

## KOSPI credential boundary

The KOSPI registration attempt is closed: the overseas individual registration
path requires a Korean local mobile number, with no foreign-member or
international-number alternative observed. No virtual number, borrowed number,
or other workaround was used. KOSPI remains `credential_required` with
`foreign_user_registration_requires_korean_mobile` as the access blocker.

No registration was completed, no Service Key was issued, no API request or
data download ran, and no cost was incurred. This is not a paid-access,
API-quality, or licence-failure finding. The adapter, official contract,
redaction tests, and local-only raw-response policy remain prepared, but no
credentialed inventory, fixed-window result, or historical-coverage assertion
is made. This is not a production integration or historical backfill. TAIEX
and AEX conclusions are unchanged.

## BVL data dictionary findings

No official dictionary/resource pair for a BVL broad index was obtained. Consequently no index names, codes, field definitions, historical start date, or ownership claims are asserted. In particular, the probe does not treat “government portal” or an asserted ODbL label as proof that the provider can sublicense S&P/BVL or other branded indices. If ODbL is verified later, its attribution and Share-Alike obligations must be assessed for both canonical data and any publicly distributed database/panel.

## Repeatability and calendars

TAIEX response pairs were byte-identical and semantic-identical for each requested month. No ETag or Last-Modified header was supplied. The other candidates have no repeatability result because no authorized actual resource was obtained. Each calendar result is `calendar_verification_partial`: official entry points are retained in the machine-readable results, but no complete machine-readable holiday reconciliation was performed.

## Decision boundary

TAIEX is conditionally evidenced for source/licence purposes, but remains outside the production pipeline pending calendar verification and an explicit production-adapter decision. The other candidates remain blocked. Do not start a historical backfill from these results.
