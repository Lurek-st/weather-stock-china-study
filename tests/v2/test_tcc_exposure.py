"""Stage 5E-1: TCC exposure (bilinear + temporal integration) unit tests."""
from __future__ import annotations

from datetime import datetime

import pytest

from scripts.v2.climatology.tcc_exposure import tcc_exposure_pct
from scripts.v2.spatial.grid_geometry import bilinear_weights, surrounding_cell

ANCHOR = (25.0338352, 121.5644995)
CORNERS = surrounding_cell(*ANCHOR)
WEIGHTS = bilinear_weights(*ANCHOR, CORNERS)


def _t(hour: int) -> datetime:
    return datetime(2020, 3, 4, hour, 0)


def _corner(fraction: float) -> dict[str, float]:
    return {"SW": fraction, "SE": fraction, "NW": fraction, "NE": fraction}


def test_exact_hour_two_hour_exposure_simpson():
    # constant corner field 0.5 -> exposure 50.0
    times = [_t(7), _t(8), _t(9)]
    corners = [_corner(0.5), _corner(0.5), _corner(0.5)]
    assert tcc_exposure_pct(times, corners, WEIGHTS, _t(7), _t(9)) == pytest.approx(50.0)


def test_exposure_uses_bilinear_weights():
    # non-uniform corner field exercises the bilinear step
    times = [_t(7), _t(8), _t(9)]
    corners = [
        {"SW": 0.0, "SE": 1.0, "NW": 0.0, "NE": 0.0},
        {"SW": 0.0, "SE": 1.0, "NW": 0.0, "NE": 0.0},
        {"SW": 0.0, "SE": 1.0, "NW": 0.0, "NE": 0.0},
    ]
    # bilinear value = SE weight (only SE non-zero)
    expected_fraction = WEIGHTS["SE"]
    assert tcc_exposure_pct(times, corners, WEIGHTS, _t(7), _t(9)) == pytest.approx(
        expected_fraction * 100.0
    )


def test_exposure_remains_in_0_to_100_when_valid():
    # valid inputs (0..1 fractions) -> output within [0, 100]
    times = [_t(7), _t(8), _t(9)]
    corners = [_corner(0.0), _corner(1.0), _corner(0.3)]
    value = tcc_exposure_pct(times, corners, WEIGHTS, _t(7), _t(9))
    assert 0.0 <= value <= 100.0


def test_length_mismatch_fails():
    with pytest.raises(ValueError):
        tcc_exposure_pct([_t(7), _t(8)], [_corner(0.5)], WEIGHTS, _t(7), _t(8))
