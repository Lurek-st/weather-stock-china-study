"""Core-open regime resolver — the research temporal authority for primary exposure.

Loads config/v2/core-open-regimes.yaml and resolves a (market_id, trading_date)
to exactly one primary core cash-market open clock, fail-closed on missing,
overlapping, or ambiguous regimes.

This is deliberately SEPARATE from config/v2/markets.yaml; the resolver never
falls back to the static ``markets.yaml.session.open`` clock.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scripts.v2.core import V2Error, load_yaml, repo_root

REGISTRY_PATH = "config/v2/core-open-regimes.yaml"
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def load_registry(root: Path | None = None) -> dict[str, Any]:
    root = root or repo_root()
    return load_yaml(root / REGISTRY_PATH)


def _parse_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def resolve_core_open(market_id: str, trading_date: str | date, root: Path | None = None) -> dict[str, Any]:
    """Resolve the single core-open regime for a market on a trading date.

    Returns {market_id, timezone, core_open_local, core_close_local, regime_id,
    effective_from, effective_to, evidence_ids}.  Raises V2Error when the
    market is unknown, or when the date maps to zero or more than one regime.
    """
    registry = load_registry(root)
    markets = {m["market_id"]: m for m in registry.get("markets", [])}
    if market_id not in markets:
        raise V2Error(f"market {market_id} not in core-open regime registry")
    market = markets[market_id]
    day = _parse_date(trading_date)
    matches = []
    for regime in market.get("regimes", []):
        eff_from = _parse_date(regime["effective_from"])
        eff_to = _parse_date(regime["effective_to"])
        if eff_from <= day <= eff_to:
            matches.append(regime)
    if not matches:
        raise V2Error(f"no core-open regime for {market_id} on {day.isoformat()}")
    if len(matches) > 1:
        raise V2Error(f"overlapping core-open regimes for {market_id} on {day.isoformat()}")
    regime = matches[0]
    return {
        "market_id": market_id,
        "timezone": market["timezone"],
        "core_open_local": regime["core_open_local"],
        "core_close_local": regime.get("core_close_local"),
        "regime_id": regime["regime_id"],
        "effective_from": regime["effective_from"],
        "effective_to": regime["effective_to"],
        "evidence_ids": regime.get("evidence_ids", []),
        "qualification_status": regime.get("qualification_status"),
    }


def validate_registry(root: Path | None = None, registry: dict[str, Any] | None = None) -> list[str]:
    """Return a list of registry problems (empty == valid).

    Checks: valid IANA timezone, exact HH:MM clocks, non-overlapping and
    non-gapped regimes within each market's own horizon, resolvable evidence
    ids, and qualification_status values.  ``registry`` (for tests) overrides
    the on-disk file.
    """
    registry = registry if registry is not None else load_registry(root)
    errors: list[str] = []
    evidence = registry.get("evidence", {})
    valid_statuses = {
        "qualified_single_regime",
        "qualified_multiple_regimes",
        "partial_evidence",
        "unresolved",
    }
    seen_markets: set[str] = set()
    for market in registry.get("markets", []):
        mid = market["market_id"]
        if mid in seen_markets:
            errors.append(f"duplicate market_id {mid}")
        seen_markets.add(mid)
        try:
            ZoneInfo(market["timezone"])
        except ZoneInfoNotFoundError:
            errors.append(f"{mid}: invalid IANA timezone {market['timezone']}")
        regimes = market.get("regimes", [])
        if not regimes:
            errors.append(f"{mid}: no regimes")
            continue
        intervals = []
        for regime in regimes:
            if regime.get("qualification_status") not in valid_statuses:
                errors.append(f"{mid}: invalid qualification_status {regime.get('qualification_status')}")
            for clock_key in ("core_open_local",):
                clock = regime.get(clock_key, "")
                if not _HHMM.match(clock):
                    errors.append(f"{mid}/{regime.get('regime_id')}: invalid {clock_key} {clock!r}")
            for eid in regime.get("evidence_ids", []):
                if eid not in evidence:
                    errors.append(f"{mid}/{regime.get('regime_id')}: unresolved evidence_id {eid}")
            eff_from = _parse_date(regime["effective_from"])
            eff_to = _parse_date(regime["effective_to"])
            if eff_to < eff_from:
                errors.append(f"{mid}/{regime.get('regime_id')}: effective_to before effective_from")
            intervals.append((eff_from, eff_to, regime.get("regime_id")))
        intervals.sort()
        for i in range(len(intervals) - 1):
            if intervals[i][1] >= intervals[i + 1][0]:
                errors.append(f"{mid}: overlapping regimes {intervals[i][2]} and {intervals[i + 1][2]}")
    return errors


def all_market_ids(root: Path | None = None) -> list[str]:
    registry = load_registry(root)
    return [m["market_id"] for m in registry.get("markets", [])]
