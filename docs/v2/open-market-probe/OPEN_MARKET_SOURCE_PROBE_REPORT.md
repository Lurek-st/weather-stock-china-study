# Open Market Source Probe Report

**Scope.** This is a controlled discovery probe, not a production integration, historical backfill, candidate-list decision, or ChatGPT task migration. Candidate data are not connected to the mature-week pipeline.

## Results

| Candidate | Technical probe | Rights/quality conclusion | Recommended status |
| --- | --- | --- | --- |
| Taipei—TAIEX | Dataset 11755, its registered TWSE CSV resource, and the RWD JSON report were directly checked. The three prescribed samples and repeated requests succeeded. | Dataset 11755 applies the Open Government Data License v1.0 to the primary-data resource; previous close/return are deterministically derived from the preceding valid trading row. | `open_core_candidate_conditional` |
| Seoul—KOSPI | Public portal home page is reachable, but no verified KOSPI catalogue/API record was queried. The contemplated API needs a free Service Key, which was neither found nor requested. | Licence scope, KOSPI identifier, fields, limits, historical coverage and index rights are unverified. | `technical_access_blocked` |
| Amsterdam—AEX/DNB | The Dutch catalogue directly identifies both registered JSON resources as CC-BY 4.0. Each prescribed `resourcefile` URL was directly requested twice; the hostname could not be resolved in this environment. No cookie, alternate endpoint, or access-control bypass was used. | Resource-file content, AEX definition, and AEX/international-series provenance remain unreadable; catalogue licence does not prove all third-party index rights. | `technical_access_blocked` |
| Lima—BVL/SMV | Peru's portal is reachable, but the bounded official search did not yield a verified SMV/BVL broad-index data dictionary plus actual resource. | ODbL applicability and the distinction between locally owned BVL and S&P/BVL/other third-party series are unresolved. | `technical_access_blocked` |

## DNB index inventory

No inventory is reported. The prescribed DNB resourcefile URLs were directly requested but `statistiek.api.dnb.nl` could not be resolved in this environment. Therefore it would be misleading to claim that the resource contains (or excludes) AEX, S&P 500, FTSE 100, DAX, Nikkei, TOPIX, or any other index. Each remains `third_party_rights_unresolved` pending direct resource access and source-level provenance.

## BVL data dictionary findings

No official dictionary/resource pair for a BVL broad index was obtained. Consequently no index names, codes, field definitions, historical start date, or ownership claims are asserted. In particular, the probe does not treat “government portal” or an asserted ODbL label as proof that the provider can sublicense S&P/BVL or other branded indices. If ODbL is verified later, its attribution and Share-Alike obligations must be assessed for both canonical data and any publicly distributed database/panel.

## Repeatability and calendars

TAIEX response pairs were byte-identical and semantic-identical for each requested month. No ETag or Last-Modified header was supplied. The other candidates have no repeatability result because no authorized actual resource was obtained. Each calendar result is `calendar_verification_partial`: official entry points are retained in the machine-readable results, but no complete machine-readable holiday reconciliation was performed.

## Decision boundary

TAIEX is conditionally evidenced for source/licence purposes, but remains outside the production pipeline pending calendar verification and an explicit production-adapter decision. The other candidates remain blocked. Do not start a historical backfill from these results.
