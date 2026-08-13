"""Stage 5C-R1: layered ERA5 timestamp contract (pure, no I/O).

Separates three distinct timestamp layers so a Cartesian-product transport
result is never silently conflated with the scientific exposure target:

1. transport_requested  — the CDS ``date x time`` cartesian product actually
                          sent in the request payload.
2. raw_observed         — the timestamps present in the returned NetCDF.
3. scientific_target    — the pre-open (07:00-08:59 Asia/Taipei) exposure
                          timestamps that are the analysis target.
4. analysis_consumed    — the timestamps actually entering bilinear / nearest /
                          legacy output.

All values are normalized to hour-granularity UTC strings "YYYY-MM-DDTHH:MM".
"""
from __future__ import annotations

from datetime import datetime


def canonical(value) -> str:
    """Normalize a datetime or ISO string to hour-granularity UTC "YYYY-MM-DDTHH:MM"."""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%dT%H:%M")
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1]
    elif "+" in s[10:]:
        s = s.split("+", 1)[0]
    return s[:16]


def transport_cartesian_set(request: dict) -> set[str]:
    """The exact ``date x time`` cartesian product implied by a CDS request."""
    from scripts.v2.fetch_era5 import expected_timestamps

    return {canonical(dt) for dt in expected_timestamps(request)}


def scientific_target_set() -> set[str]:
    """The frozen pre-open scientific targets (07:00-08:59 Asia/Taipei)."""
    from scripts.v2.spatial.build_taiex_spatial_acceptance import pre_open_timestamps

    return {canonical(t) for t in pre_open_timestamps()}


def extra_transport(transport: set[str], scientific: set[str]) -> list[str]:
    """Transport timestamps that are NOT scientific targets (sorted)."""
    return sorted(transport - scientific)


def assess_contract(
    transport: set[str],
    raw_observed: set[str],
    scientific: set[str],
    analysis_consumed: set[str],
) -> dict:
    """Validate the four timestamp-set relations; returns booleans, never guesses."""
    extras = extra_transport(transport, scientific)
    return {
        "scientific_target_count": len(scientific),
        "transport_requested_count": len(transport),
        "raw_observed_count": len(raw_observed),
        "analysis_consumed_count": len(analysis_consumed),
        "transport_exact_match": raw_observed == transport,
        "scientific_subset_match": scientific <= raw_observed,
        "analysis_exact_target_match": analysis_consumed == scientific,
        "extra_transport_timestamps": extras,
        "no_extra_leakage_into_analysis": not (set(extras) & analysis_consumed),
    }
