"""Final-primary TCC exposure: bilinear (space) + piecewise-linear (time).

Combines the frozen spatial estimator (4-point bilinear on the ERA5 0.25x0.25
grid) with the frozen temporal estimator (piecewise-linear time integration
over the pre-open window) to produce a single daily exposure in percentage
points (0-100).

Pure and zero-network.
"""
from __future__ import annotations

from datetime import datetime

from scripts.v2.climatology.temporal_integration import piecewise_linear_mean
from scripts.v2.spatial.grid_geometry import bilinear_value


def tcc_exposure_pct(
    times: list[datetime],
    corner_tcc: list[dict[str, float]],
    weights: dict[str, float],
    start: datetime,
    end: datetime,
) -> float:
    """Daily TCC exposure in percentage points (0-100).

    ``times`` and ``corner_tcc`` are aligned (one dict of SW/SE/NW/NE corner
    TCC fractions per hourly breakpoint).  Each breakpoint's bilinear estimate
    is computed, then the series is integrated piecewise-linearly over
    ``[start, end)`` and scaled by 100.
    """
    if len(times) != len(corner_tcc):
        raise ValueError("times and corner_tcc must be aligned")
    bilinear = [bilinear_value(corners, weights) for corners in corner_tcc]
    mean = piecewise_linear_mean(list(times), bilinear, start, end)
    return mean * 100.0
