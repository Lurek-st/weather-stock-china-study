# TAIEX 2026 market-only pilot-window acceptance

**Status:** accepted for the 2026 pilot window only.

| Field | Value |
| --- | --- |
| Market | TAIEX (`taiex`), Taipei (`taipei`) |
| Pilot week | 2026-03-02 (Mon) to 2026-03-06 (Fri) |
| Official calendar source | TWSE annual holiday schedule |
| Calendar year verified | 2026 only |
| Full history calendar verified | false |
| Production connection | not_connected |
| Weather integration | not_run |

## Selection

Candidate range `2026-03-01` to `2026-05-31`. The earliest complete
Monday-to-Friday week with no official closure is `2026-03-02` to
`2026-03-06` (no TWSE closure in March 2026). The first day's previous close
requires one preceding valid trading day, 2026-02-26; requests therefore
covered two natural months (February and March 2026).

## Extraordinary closure check

The TWSE official news list was checked for the pilot window. Ten official
announcements fall inside 2026-03-02..06; none concerns a typhoon, earthquake,
temporary all-day closure, suspended after-hours trading, or special session.

`extraordinary_closure_evidence: none_found_in_official_sources`

## Market requests and repeatability

The candidate adapter ran twice with `--live --local-only` for the same
request plan (2026-02 and 2026-03). Raw responses are stored append-only under
ignored `.local/source-raw/taiex/`; the second run was byte-identical and
skipped by the append-only store (no new revision).

Repeatability classification: `byte_identical`.

## Calendar x market cross-validation

| Check | Result |
| --- | --- |
| Expected open days | 5 (Mon-Fri) |
| Market records in scope | 6 (2026-02-26 + Mon-Fri) |
| Matched open dates | 5 / 5 |
| Missing market dates | none |
| Unexpected market dates | none |
| Duplicate dates | none |
| Calendar match rate | 1.0 |
| Previous close match | true |
| Return recalculation match | true |
| OHLC validation | passed |

Saturday/Sunday and the official closure days (2026-02-27 and 2026-02-28)
carry no market records. 2026-03-02's previous close equals 2026-02-26's close
(crossing a weekend and an official holiday). Returns are recomputed from
normalized close and previous_close with `(close / previous_close - 1) * 100`;
the source provides no percentage field, so no source return is compared.

## Status fields

- `pilot_calendar_status`: `calendar_verified_for_2026_pilot_window`
- `adapter_status`: `market_only_pilot_accepted`
- `source_probe_status`: `open_core_candidate_conditional` (unchanged)
- `full_history_calendar_verified`: `false` (unchanged)
- `full_history_calendar_status`: `calendar_verification_partial` (unchanged)
- `historical_backfill_status`: `blocked_historical_calendar_unresolved` (unchanged)
- `historical_calendar_blocker`: `historical_twse_calendar_query_semantics_unconfirmed` (unchanged)
- `production_status`: `not_connected` (unchanged)
- `research_ready`: `false` (unchanged)
- `frozen`: `false` (unchanged)

This acceptance covers only the 2026 selected pilot week. It does not assert
that the 2020-2025 official trading calendar is verified, and it does not
start any historical backfill.

## Evidence

- `data/audits/v2/taiex-calendar/taiex-2026-pilot-window.json`
- `data/audits/v2/taiex-adapter-acceptance/taiex-2026-market-only.json`
- Raw responses: `.local/source-raw/taiex/` (ignored, append-only)
