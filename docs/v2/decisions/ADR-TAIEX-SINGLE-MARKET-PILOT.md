# ADR: TAIEX single-market pilot

## Decision

V2 proceeds as a **Taipei—TAIEX single-market pilot**. The intended three to
five publicly usable core markets are not frozen as an achieved result.

## Evidence

- TAIEX is `open_core_candidate_conditional`: its TWSE source mapping,
  licence evidence, repeated official requests, and sample quality checks pass.
- AEX remains `technical_access_blocked` because the official legacy resource
  hostname returns public DNS NXDOMAIN.
- KOSPI remains `credential_required` because the overseas individual
  registration path requires a Korean local mobile number.
- A bounded zero-barrier screen of ten official routes found no Tier 1 source.

The project will not lower official-source, rights, or reproducibility standards
merely to add markets. A single-market result can support only Taiwan-market
internal observations; it cannot be represented as globally general.

## Consequences

The pilot's next gates are the TWSE calendar contract, a candidate market
adapter, Taipei weather configuration, and one real mature-week acceptance.
There is no weather download, production connection, or historical backfill in
this decision. Expansion is reconsidered only when a new official source meets
the existing Tier 1 conditions.
