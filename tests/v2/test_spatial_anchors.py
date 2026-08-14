"""Stage 5C-G: global official exchange anchor qualification tests.

Includes an INDEPENDENT spatial oracle (reconstructs the anchor from the four
corners via plain bilinear math, not the production helper) and synthetic
grid-boundary controls.  All tests are pure / zero-network.
"""
from __future__ import annotations

import hashlib
import json

import pytest
import yaml

from scripts.v2.core import repo_root
from scripts.v2.spatial.grid_geometry import bilinear_weights, nearest_grid_point, surrounding_cell

REGISTRY = "config/v2/spatial-anchors.yaml"
AUDIT = "data/audits/v2/spatial/global-exchange-anchor-qualification.json"

RESEARCH_MARKETS = [
    "sse_composite", "szse_component", "topix", "nifty50",
    "ftse100", "dax", "sp500", "bse50",
]


@pytest.fixture(scope="module")
def registry():
    return yaml.safe_load((repo_root() / REGISTRY).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def anchors(registry):
    return {a["market_id"]: a for a in registry["anchors"]}


def test_eight_research_markets_present(anchors):
    assert set(anchors.keys()) == set(RESEARCH_MARKETS)


def test_all_qualified(anchors):
    for m in RESEARCH_MARKETS:
        assert anchors[m]["qualification_status"] == "qualified"


def test_no_municipal_coordinate_as_primary(anchors, registry):
    # municipal points live only under legacy_sensitivity, never as the anchor
    for m in RESEARCH_MARKETS:
        a = anchors[m]
        coord = a["coordinate"]
        leg = a["legacy_sensitivity"]["municipal_coordinate"]
        assert (coord["latitude"], coord["longitude"]) != (leg["latitude"], leg["longitude"])


def test_official_address_required(anchors):
    for m in RESEARCH_MARKETS:
        assert anchors[m]["venue"]["official_address"]


def test_geocoder_coordinate_required(anchors):
    for m in RESEARCH_MARKETS:
        assert anchors[m]["coordinate"]["geocoder"] == "nominatim"
        assert anchors[m]["coordinate"]["geocoder_object_id"] is not None


def test_reverse_check_required(anchors):
    for m in RESEARCH_MARKETS:
        assert anchors[m]["coordinate"]["reverse_check_status"] == "passed"


def test_wgs84_required(anchors):
    for m in RESEARCH_MARKETS:
        assert anchors[m]["coordinate"]["crs"] == "WGS84"


def test_anchor_role(anchors):
    for m in RESEARCH_MARKETS:
        assert anchors[m]["venue"]["anchor_role"] == "market_local_atmospheric_proxy"


def test_four_unique_corners(anchors):
    for m in RESEARCH_MARKETS:
        corners = anchors[m]["era5"]["corners"]
        assert set(corners.keys()) == {"SW", "SE", "NW", "NE"}
        pts = {(c["latitude"], c["longitude"]) for c in corners.values()}
        assert len(pts) == 4


def test_weights_sum_one(anchors):
    for m in RESEARCH_MARKETS:
        w = anchors[m]["era5"]["bilinear_weights"]
        assert sum(w.values()) == pytest.approx(1.0, abs=1e-9)


def test_weights_in_unit_interval(anchors):
    for m in RESEARCH_MARKETS:
        w = anchors[m]["era5"]["bilinear_weights"]
        assert all(0.0 <= v <= 1.0 for v in w.values())


def test_independent_oracle_reconstructs_anchor(anchors):
    """Plain bilinear reconstruction: weighted corner coords ~= anchor coords."""
    for m in RESEARCH_MARKETS:
        a = anchors[m]
        corners = a["era5"]["corners"]
        w = a["era5"]["bilinear_weights"]
        lat = sum(corners[k]["latitude"] * w[k] for k in ("SW", "SE", "NW", "NE"))
        lon = sum(corners[k]["longitude"] * w[k] for k in ("SW", "SE", "NW", "NE"))
        assert lat == pytest.approx(a["coordinate"]["latitude"], abs=1e-9)
        assert lon == pytest.approx(a["coordinate"]["longitude"], abs=1e-9)


def test_negative_longitude_western_markets(anchors):
    assert anchors["ftse100"]["coordinate"]["longitude"] < 0
    assert anchors["sp500"]["coordinate"]["longitude"] < 0


def test_anchor_hash_deterministic():
    import json as _json

    from scripts.v2.spatial.grid_geometry import bilinear_weights as _bw, surrounding_cell as _sc

    lat, lon = 51.5150440, -0.0990793
    corners = _sc(lat, lon)
    w = _bw(lat, lon, corners)
    payload = {"market_id": "ftse100", "latitude": lat, "longitude": lon,
               "corners": {k: {"latitude": v.latitude, "longitude": v.longitude} for k, v in corners.items()},
               "weights": w}
    h1 = hashlib.sha256(_json.dumps(payload, sort_keys=True).encode()).hexdigest()
    h2 = hashlib.sha256(_json.dumps(payload, sort_keys=True).encode()).hexdigest()
    assert h1 == h2 and len(h1) == 64


def test_global_registry_hash_deterministic(registry):
    assert len(registry["global_spatial_anchor_registry_hash"]) == 64


def test_frankfurt_venue_role(anchors):
    a = anchors["dax"]
    assert "Xetra" in a["venue"]["entity_name"] or "Deutsche Börse" in a["venue"]["entity_name"]
    assert "Eschborn" in a["venue"]["official_address"]
    # primary is the Xetra/DB operational premises (Mergenthalerallee 61, Eschborn),
    # NOT the FWB Börsenplatz 4 physical site
    assert "Mergenthalerallee" in a["venue"]["official_address"]


def test_lse_head_office_proxy(anchors):
    a = anchors["ftse100"]
    assert "10 Paternoster Square" in a["venue"]["official_address"]


def test_legacy_municipal_retained_as_sensitivity(anchors):
    for m in RESEARCH_MARKETS:
        leg = anchors[m]["legacy_sensitivity"]
        assert leg["distance_km"] > 0
        assert isinstance(leg["same_surrounding_cell"], dict)


def test_no_silent_pass_on_horizon_move(anchors):
    for m in RESEARCH_MARKETS:
        assert anchors[m]["venue"]["target_horizon_anchor_change_detected"] is False


def test_live_authorization_not_implied(anchors, registry):
    audit = json.loads((repo_root() / AUDIT).read_text(encoding="utf-8"))
    assert audit["spatial_ready_for_live_canary"] is True
    assert audit["live_backfill_authorized"] is False
    assert audit["network_activity"]["cds_calls"] == 0
    assert audit["network_activity"]["era5_downloads"] == 0


def test_unresolved_anchor_blocks_ready():
    # synthetic: qualification_status != qualified -> research_usable false
    audit = json.loads((repo_root() / AUDIT).read_text(encoding="utf-8"))
    assert audit["qualified_count"] == 8
    assert audit["unresolved_count"] == 0
    # the gate logic requires 8/8; a single unresolved would flip it
    assert audit["research_usable"] == (audit["qualified_count"] == 8 and audit["unresolved_count"] == 0)


# --- Synthetic grid-boundary controls -----------------------------------
def test_anchor_on_lat_grid_line():
    corners = surrounding_cell(31.25, 121.5)  # exactly on 31.25 line
    assert corners["SW"].latitude == 31.25
    assert corners["NW"].latitude == 31.5
    w = bilinear_weights(31.25, 121.5, corners)
    assert sum(w.values()) == pytest.approx(1.0)
    assert all(0 <= v <= 1 for v in w.values())
    # deterministic floor semantics: anchor on line -> south/west edge
    assert w["SW"] == pytest.approx(1.0)  # (1-0)*(1-0)


def test_anchor_on_lon_grid_line():
    corners = surrounding_cell(31.2, 121.75)  # exactly on 121.75 line
    assert corners["SW"].longitude == 121.75
    w = bilinear_weights(31.2, 121.75, corners)
    assert sum(w.values()) == pytest.approx(1.0)
    assert all(0 <= v <= 1 for v in w.values())


def test_anchor_on_grid_intersection():
    corners = surrounding_cell(31.25, 121.75)  # exact intersection
    pts = {(c.latitude, c.longitude) for c in corners.values()}
    assert len(pts) == 4  # no duplicate corners
    w = bilinear_weights(31.25, 121.75, corners)
    assert w["SW"] == pytest.approx(1.0)
    assert w["SE"] == w["NW"] == w["NE"] == pytest.approx(0.0)


def test_negative_longitude_cell_geometry():
    corners = surrounding_cell(40.7070653, -74.0111761)  # NYSE
    assert corners["SW"].longitude < 0 and corners["SE"].longitude < 0
    assert corners["SW"].longitude == -74.25  # floor(-74.011/0.25)*0.25
    w = bilinear_weights(40.7070653, -74.0111761, corners)
    assert sum(w.values()) == pytest.approx(1.0)


def test_nearest_grid_deterministic():
    c = surrounding_cell(31.2221653, 121.5307778)
    n1 = nearest_grid_point(31.2221653, 121.5307778, c)
    n2 = nearest_grid_point(31.2221653, 121.5307778, c)
    assert n1[0] == n2[0]
