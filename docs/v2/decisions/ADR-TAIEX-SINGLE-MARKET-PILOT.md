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

## Update: 2026 market-only pilot accepted

On 2026-08-05 the 2026 market-only pilot window (`2026-03-02 .. 2026-03-06`)
passed calendar x market cross-validation using the official 2026 TWSE
calendar: 5/5 expected open days matched, previous close and close-to-close
returns recomputed independently, OHLC checks passed, repeatability
`byte_identical`. Statuses moved to `pilot_calendar_status =
calendar_verified_for_2026_pilot_window` and `adapter_status =
market_only_pilot_accepted`.

This update does not change the single-market pilot decision. Production
status remains `not_connected`, `research_ready`/`frozen` remain false, weather
is not integrated, the 2020--2025 calendar is still unresolved, and historical
backfill remains blocked. Evidence:
`docs/v2/open-market-probe/TAIEX_2026_PILOT_ACCEPTANCE.md`.
