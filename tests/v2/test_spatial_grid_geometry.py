"""Zero-network tests for Stage 5C spatial grid geometry and bilinear."""
from __future__ import annotations

import pytest

from scripts.v2.spatial.grid_geometry import (
    GridPoint,
    bilinear_value,
    bilinear_weights,
    describe_cell,
    nearest_grid_point,
    surrounding_cell,
)

EXCHANGE_ANCHOR = (25.0338352, 121.5644995)
LEGACY_ANCHOR = (25.0375, 121.5646)


# ---------------------------------------------------------------------------
# Grid geometry
# ---------------------------------------------------------------------------


def test_interior_point_cell():
    c = surrounding_cell(25.0338352, 121.5644995)
    assert c["SW"].latitude == 25.0
    assert c["SW"].longitude == 121.5
    assert c["NE"].latitude == 25.25
    assert c["NE"].longitude == 121.75
    assert c["NW"].latitude == 25.25 and c["NW"].longitude == 121.5
    assert c["SE"].latitude == 25.0 and c["SE"].longitude == 121.75


def test_exact_grid_point_on_south_west_edge():
    # Anchor exactly on a grid point: floor keeps it on the SW edge of the cell
    # whose SW corner is that point.
    c = surrounding_cell(25.0, 121.5)
    assert c["SW"].latitude == 25.0 and c["SW"].longitude == 121.5
    assert c["NE"].latitude == 25.25 and c["NE"].longitude == 121.75


def test_cell_resolution_is_quarter_degree():
    c = surrounding_cell(25.1, 121.6)
    assert describe_cell(c)["north_latitude"] - describe_cell(c)["south_latitude"] == 0.25
    assert describe_cell(c)["east_longitude"] - describe_cell(c)["west_longitude"] == 0.25


def test_negative_coordinates_floor_correctly():
    # Southern hemisphere / western hemisphere use floor (not truncation).
    c = surrounding_cell(-33.9, -70.6)
    assert c["SW"].latitude == -34.0
    assert c["SW"].longitude == -70.75
    assert c["NE"].latitude == -33.75
    assert c["NE"].longitude == -70.5


def test_anchor_between_grid_lines():
    c = surrounding_cell(25.125, 121.625)
    assert c["SW"].latitude == 25.0 and c["NW"].latitude == 25.25
    assert c["SW"].longitude == 121.5 and c["SE"].longitude == 121.75


# ---------------------------------------------------------------------------
# Bilinear weights
# ---------------------------------------------------------------------------


def test_weights_sum_to_one():
    w = bilinear_weights(*EXCHANGE_ANCHOR, surrounding_cell(*EXCHANGE_ANCHOR))
    assert abs(sum(w.values()) - 1.0) < 1e-12


def test_weights_non_negative():
    w = bilinear_weights(*EXCHANGE_ANCHOR, surrounding_cell(*EXCHANGE_ANCHOR))
    assert all(v >= 0.0 for v in w.values())


def test_center_weight_equal_four_quarters():
    c = surrounding_cell(25.125, 121.625)
    w = bilinear_weights(25.125, 121.625, c)
    for k in ("SW", "SE", "NW", "NE"):
        assert abs(w[k] - 0.25) < 1e-12


def test_weights_constant_field():
    # A constant field yields the same constant regardless of anchor.
    c = surrounding_cell(*EXCHANGE_ANCHOR)
    w = bilinear_weights(*EXCHANGE_ANCHOR, c)
    val = bilinear_value({"SW": 50.0, "SE": 50.0, "NW": 50.0, "NE": 50.0}, w)
    assert val == pytest.approx(50.0)


def test_weights_linear_field():
    # Linear field f(lat, lon) = lat + lon should interpolate exactly.
    c = surrounding_cell(*EXCHANGE_ANCHOR)
    w = bilinear_weights(*EXCHANGE_ANCHOR, c)
    corners = {
        "SW": 25.0 + 121.5,
        "SE": 25.0 + 121.75,
        "NW": 25.25 + 121.5,
        "NE": 25.25 + 121.75,
    }
    val = bilinear_value(corners, w)
    assert val == pytest.approx(25.0338352 + 121.5644995, abs=1e-9)


def test_known_numeric_fixture():
    c = {"SW": GridPoint(0.0, 0.0), "SE": GridPoint(0.0, 1.0), "NW": GridPoint(1.0, 0.0), "NE": GridPoint(1.0, 1.0)}
    w = bilinear_weights(0.5, 0.5, c)
    val = bilinear_value({"SW": 0.0, "SE": 1.0, "NW": 1.0, "NE": 2.0}, w)
    assert val == pytest.approx(1.0)


def test_anchor_outside_cell_fails():
    c = surrounding_cell(*EXCHANGE_ANCHOR)
    with pytest.raises(ValueError):
        bilinear_weights(26.0, 121.5644995, c)  # latitude outside cell


def test_missing_corner_fails():
    c = surrounding_cell(*EXCHANGE_ANCHOR)
    w = bilinear_weights(*EXCHANGE_ANCHOR, c)
    with pytest.raises(KeyError):
        bilinear_value({"SW": 1.0, "SE": 1.0, "NW": 1.0}, w)  # missing NE


def test_weights_invariant_across_timestamps():
    # Same anchor -> same weights (geometry does not depend on time).
    c = surrounding_cell(*EXCHANGE_ANCHOR)
    w1 = bilinear_weights(*EXCHANGE_ANCHOR, c)
    w2 = bilinear_weights(*EXCHANGE_ANCHOR, c)
    assert w1 == w2


# ---------------------------------------------------------------------------
# Nearest grid point
# ---------------------------------------------------------------------------


def test_nearest_obvious():
    c = surrounding_cell(25.03, 121.52)
    name, point = nearest_grid_point(25.03, 121.52, c)
    assert name == "SW"
    assert point.latitude == 25.0 and point.longitude == 121.5


def test_nearest_deterministic_tie():
    # Anchor exactly at cell center: all four equidistant; tie-break is SW.
    c = surrounding_cell(25.125, 121.625)
    name, _ = nearest_grid_point(25.125, 121.625, c)
    assert name == "SW"


def test_exchange_nearest_is_sw():
    c = surrounding_cell(*EXCHANGE_ANCHOR)
    name, _ = nearest_grid_point(*EXCHANGE_ANCHOR, c)
    assert name == "SW"
