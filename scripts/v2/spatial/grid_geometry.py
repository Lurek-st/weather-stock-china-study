"""Spatial geometry and interpolation for ERA5 regular grid.

Pure, zero-network helpers (unit-testable) that map a fixed weather anchor to
the four surrounding ERA5 regular-grid points and compute bilinear / nearest
point estimates.

Grid contract: ERA5 atmospheric regular grid is 0.25 x 0.25 degrees.  A
bilinear estimate at an anchor uses the four corners of the surrounding cell
(SW/SE/NW/NE); the nearest estimate uses the single closest grid point.  The
bilinear estimator is the frozen PRIMARY spatial estimator; nearest is a
robustness estimator only.

All functions are generic (not Taipei-specific) so they can serve other
markets.  Longitude wrap-around is NOT handled here (anchors for the current
study are far from the anti-meridian); callers must not pass anchors near
+/-180.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

GRID_RESOLUTION_DEGREES = 0.25


@dataclass(frozen=True)
class GridPoint:
    latitude: float
    longitude: float


def _floor_to_resolution(value: float, resolution: float) -> float:
    """Floor a coordinate to the nearest multiple of resolution (grid line)."""
    return math.floor(value / resolution) * resolution


def surrounding_cell(anchor_lat: float, anchor_lon: float, resolution: float = GRID_RESOLUTION_DEGREES) -> dict[str, GridPoint]:
    """Return the four grid points surrounding an anchor.

    Uses floor (not round) so an anchor exactly on a grid line is always on the
    south/west edge of a cell, keeping the four corners unique and the anchor
    inside [south, north] x [west, east].
    """
    south = _floor_to_resolution(anchor_lat, resolution)
    north = south + resolution
    west = _floor_to_resolution(anchor_lon, resolution)
    east = west + resolution
    return {
        "SW": GridPoint(south, west),
        "SE": GridPoint(south, east),
        "NW": GridPoint(north, west),
        "NE": GridPoint(north, east),
    }


def bilinear_weights(anchor_lat: float, anchor_lon: float, corners: dict[str, GridPoint]) -> dict[str, float]:
    """Bilinear weights for the four corners at an anchor.

    Weights are the standard bilinear basis: u = fractional latitude position,
    v = fractional longitude position.  Sum is exactly 1.0 and each weight is
    in [0, 1] when the anchor lies inside the cell.
    """
    south = corners["SW"].latitude
    north = corners["NW"].latitude
    west = corners["SW"].longitude
    east = corners["SE"].longitude
    if not (south <= anchor_lat <= north):
        raise ValueError(f"anchor latitude {anchor_lat} outside cell [{south}, {north}]")
    if not (west <= anchor_lon <= east):
        raise ValueError(f"anchor longitude {anchor_lon} outside cell [{west}, {east}]")
    u = (anchor_lat - south) / (north - south)
    v = (anchor_lon - west) / (east - west)
    return {
        "SW": (1.0 - u) * (1.0 - v),
        "SE": (1.0 - u) * v,
        "NW": u * (1.0 - v),
        "NE": u * v,
    }


def bilinear_value(corner_values: dict[str, float], weights: dict[str, float]) -> float:
    """Weighted sum of four corner values."""
    return sum(corner_values[k] * weights[k] for k in ("SW", "SE", "NW", "NE"))


def nearest_grid_point(anchor_lat: float, anchor_lon: float, corners: dict[str, GridPoint]) -> tuple[str, GridPoint]:
    """Return the nearest corner (deterministic tie-break).

    Uses the squared great-circle-equivalent angular distance on a unit sphere;
    for a single 0.25-degree cell this is effectively planar.  Ties are broken
    deterministically by latitude then longitude (SW before SE before NW before
    NE), never at random.
    """
    def sqdist(point: GridPoint) -> float:
        # Angular distance squared; fine for a single cell.
        dlat = point.latitude - anchor_lat
        dlon = point.longitude - anchor_lon
        return dlat * dlat + dlon * dlon

    ordered = ["SW", "SE", "NW", "NE"]
    return min(ordered, key=lambda k: (sqdist(corners[k]), corners[k].latitude, corners[k].longitude)), corners[min(
        ordered, key=lambda k: (sqdist(corners[k]), corners[k].latitude, corners[k].longitude)
    )]


def describe_cell(corners: dict[str, GridPoint]) -> dict[str, Any]:
    """Machine-readable cell description for audits."""
    return {
        "north_latitude": corners["NW"].latitude,
        "south_latitude": corners["SW"].latitude,
        "west_longitude": corners["SW"].longitude,
        "east_longitude": corners["SE"].longitude,
        "resolution_degrees": GRID_RESOLUTION_DEGREES,
        "corners": {k: {"latitude": v.latitude, "longitude": v.longitude} for k, v in corners.items()},
    }
