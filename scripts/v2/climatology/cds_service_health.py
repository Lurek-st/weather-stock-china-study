"""Stage 5E-3C-R1 dataset-specific CDS service-health checker (runtime wiring).

Implements the FROZEN controller-policy health authority as a reusable,
injectable, fail-closed runtime helper for the full-backfill controller.

Authority (frozen policy, spec 10-12):

    target dataset ONLY: reanalysis-era5-single-levels
    official Copernicus / ECMWF Data Stores structured catalogue (STAC)
    collection metadata -> ``cads:sanity_check.status``

The ONLY status that permits continuation is a normalized ``available``.
Anything else -- warning / degraded / down / unavailable / maintenance /
expired / disabled / unknown / null / missing field / HTTP failure / timeout /
JSON parse failure / unexpected schema / wrong dataset id -- is
``dataset_available = false`` (FAIL CLOSED).

Banner text / UI messages / whole-site CDS status / other dataset status are
NEVER authority.  In particular a page banner like "Degraded access to the
data" must NOT make a dataset-specific ``available`` unhealthy, and a healthy
looking banner must NOT make a structured ``warning`` healthy.

Stage 5E-3C-R1 itself performs ZERO network: every health test injects a fake
fetcher.  The real network fetch is only exercised by a future live execution
stage (5E-4A), after authorization activation.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v2.core import V2Error

# Frozen target dataset (never derived from user input / search results).
DEFAULT_DATASET_ID = "reanalysis-era5-single-levels"

# Official Data Stores STAC catalogue collection endpoint for the target
# dataset.  Stage 5E-3C-R1 never requests it (injected fetcher only); the real
# fetch belongs to the future authorized live execution stage.
HEALTH_COLLECTION_URL = (
    "https://cds.climate.copernicus.eu/stac-browser/collections/"
    "reanalysis-era5-single-levels/collection.json"
)

# The structured field carrying the dataset-specific sanity status.
SANITY_CHECK_FIELD = "cads:sanity_check"
SANITY_STATUS_FIELD = "status"
SANITY_TIMESTAMP_FIELD = "timestamp"

# Only this normalized status means the dataset is usable.
AVAILABLE_STATUS = "available"

# No automatic retry (controller policy: automatic_retry = false).
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_HEALTH_FETCHES_PER_CHECK = 1


def fetch_json(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> Any:
    """Bound, no-retry JSON fetch (default transport; injectable in tests).

    Raises on HTTP / transport / JSON errors so the caller can fail closed.
    """
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - official https endpoint
        payload = resp.read().decode("utf-8")
    return json.loads(payload)


def normalize_status(value: Any) -> str | None:
    """Normalize a raw sanity status to a canonical lowercase token.

    None / non-str / empty -> None (never healthy).  Everything else is
    lowercased + stripped; only ``available`` may continue.
    """
    if not isinstance(value, str):
        return None
    token = value.strip().lower()
    return token or None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_dataset_health(
    dataset_id: str | None = None,
    fetcher: Callable[[str, float], Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    collection_url: str | None = None,
    checked_at: str | None = None,
) -> dict[str, Any]:
    """Structured dataset-specific health check (FAIL CLOSED).

    ``fetcher`` is injected for tests (Stage 5E-3C-R1 = ZERO NETWORK); when
    None, the default ``fetch_json`` transport is used by the FUTURE live
    execution stage.  Returns a dict with at least:

        dataset_id / dataset_available / status / status_timestamp / source /
        checked_at / error_class

    Never includes credentials, cookies, or absolute paths.
    """
    dataset_id = dataset_id or DEFAULT_DATASET_ID
    fetcher = fetcher or fetch_json
    url = collection_url or HEALTH_COLLECTION_URL
    result: dict[str, Any] = {
        "dataset_id": dataset_id,
        "dataset_available": False,
        "status": None,
        "status_timestamp": None,
        "source": "official_dataset_catalogue",
        "checked_at": checked_at or _now_iso(),
        "error_class": None,
    }
    try:
        collection = fetcher(url, timeout)
    except urllib.error.HTTPError as exc:
        result["error_class"] = f"http_error_{exc.code}"
        return result
    except urllib.error.URLError as exc:
        result["error_class"] = "http_transport_error"
        return result
    except TimeoutError:
        result["error_class"] = "timeout"
        return result
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        result["error_class"] = "json_parse_failure"
        return result
    except Exception as exc:  # noqa: BLE001 - fail closed on any transport anomaly
        result["error_class"] = f"unexpected_transport_{type(exc).__name__}"
        return result

    # Structured collection must identify the target dataset.
    if not isinstance(collection, dict):
        result["error_class"] = "unexpected_schema"
        return result
    collection_id = collection.get("id")
    if collection_id != dataset_id:
        result["error_class"] = f"dataset_id_mismatch:{collection_id!r}"
        return result

    sanity = collection.get(SANITY_CHECK_FIELD)
    if not isinstance(sanity, dict):
        result["error_class"] = "missing_sanity_check"
        return result
    raw_status = sanity.get(SANITY_STATUS_FIELD)
    normalized = normalize_status(raw_status)
    result["status"] = normalized
    result["status_timestamp"] = sanity.get(SANITY_TIMESTAMP_FIELD)
    result["dataset_available"] = normalized == AVAILABLE_STATUS
    return result


def make_dataset_health_checker(
    dataset_id: str | None = None,
    fetcher: Callable[[str, float], Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    collection_url: str | None = None,
) -> Callable[[], dict[str, Any]]:
    """Build the real CLI health checker callable (construction = no network).

    The returned callable performs exactly ONE authoritative metadata fetch
    per invocation (no retry, bounded timeout).  Construction itself never
    touches the network, so the authorization kill switch (checked BEFORE this
    factory is invoked by the CLI) still guarantees 0 health HTTP when not
    authorized.
    """
    dataset_id = dataset_id or DEFAULT_DATASET_ID

    def checker() -> dict[str, Any]:
        return check_dataset_health(
            dataset_id=dataset_id,
            fetcher=fetcher,
            timeout=timeout,
            collection_url=collection_url,
        )

    return checker
