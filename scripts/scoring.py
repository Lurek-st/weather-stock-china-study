"""Deterministic V1.0 scoring functions for weather and market records."""
from __future__ import annotations

import math
from typing import Any, Mapping

WEATHER_WEIGHTS = {
    "temperature_comfort": 0.25,
    "precipitation": 0.20,
    "sky_and_sunlight": 0.20,
    "air_quality": 0.20,
    "wind_and_extreme": 0.15,
}

MARKET_WEIGHTS = {
    "return": 0.60,
    "breadth": 0.25,
    "close_position": 0.10,
    "stability": 0.05,
}


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def interpolate(value: float, anchors: list[tuple[float, float]]) -> float:
    anchors = sorted(anchors)
    if value <= anchors[0][0]:
        return anchors[0][1]
    if value >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= value <= x1:
            if x1 == x0:
                return y1
            ratio = (value - x0) / (x1 - x0)
            return y0 + ratio * (y1 - y0)
    raise RuntimeError("unreachable")


def metric_value(metrics: Mapping[str, Any], key: str) -> float | None:
    raw = metrics.get(key)
    if raw is None:
        return None
    if isinstance(raw, Mapping):
        raw = raw.get("value")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def temperature_comfort_score(apparent_temperature_c: float) -> float:
    return clamp(
        interpolate(
            apparent_temperature_c,
            [(-10, 0), (0, 30), (10, 70), (18, 100), (24, 100), (30, 70), (35, 35), (40, 10), (45, 0)],
        )
    )


def precipitation_score(precipitation_mm: float) -> float:
    return clamp(
        interpolate(
            max(0.0, precipitation_mm),
            [(0, 100), (0.5, 95), (2, 80), (5, 60), (15, 35), (30, 15), (60, 0)],
        )
    )


def sky_score(cloud_cover_pct: float | None, sunshine_hours: float | None, window_hours: float | None) -> float | None:
    values: list[float] = []
    if cloud_cover_pct is not None:
        values.append(clamp(100 - cloud_cover_pct))
    if sunshine_hours is not None and window_hours and window_hours > 0:
        values.append(clamp(100 * sunshine_hours / window_hours))
    return sum(values) / len(values) if values else None


def air_quality_score(aqi: float) -> float:
    return clamp(interpolate(aqi, [(0, 100), (50, 100), (100, 70), (150, 40), (200, 15), (300, 0), (500, 0)]))


def wind_score(gust_kmh: float) -> float:
    return clamp(interpolate(max(0.0, gust_kmh), [(0, 100), (20, 100), (35, 80), (50, 55), (70, 25), (90, 0)]))


def score_to_1_10(index: float) -> int:
    return max(1, min(10, int(math.ceil(clamp(index) / 10.0))))


def weighted_available(components: Mapping[str, float | None], weights: Mapping[str, float]) -> tuple[float | None, str]:
    available = {k: v for k, v in components.items() if v is not None}
    total_weight = sum(weights[k] for k in available)
    if len(available) < 3 or total_weight < 0.60:
        return None, "score_unavailable"
    value = sum(float(available[k]) * weights[k] for k in available) / total_weight
    return clamp(value), "complete_score" if len(available) == len(weights) else "partial_score"


def weather_score(metrics: Mapping[str, Any], window_hours: float | None = None, alert_severity: str | None = None) -> dict[str, Any]:
    apparent = metric_value(metrics, "apparent_temperature_mean_c")
    precip = metric_value(metrics, "precipitation_total_mm")
    cloud = metric_value(metrics, "cloud_cover_mean_pct")
    sunshine = metric_value(metrics, "sunshine_duration_hours")
    aqi = metric_value(metrics, "aqi")
    gust = metric_value(metrics, "wind_gust_max_kmh")

    components: dict[str, float | None] = {
        "temperature_comfort": temperature_comfort_score(apparent) if apparent is not None else None,
        "precipitation": precipitation_score(precip) if precip is not None else None,
        "sky_and_sunlight": sky_score(cloud, sunshine, window_hours),
        "air_quality": air_quality_score(aqi) if aqi is not None else None,
        "wind_and_extreme": wind_score(gust) if gust is not None else None,
    }
    index, status = weighted_available(components, WEATHER_WEIGHTS)
    if index is not None:
        severity = (alert_severity or "none").lower()
        caps = {"moderate": 60.0, "severe": 40.0, "extreme": 20.0}
        if severity in caps:
            index = min(index, caps[severity])
    return {
        "components": {k: (round(v, 4) if v is not None else None) for k, v in components.items()},
        "index": round(index, 4) if index is not None else None,
        "score": score_to_1_10(index) if index is not None else None,
        "status": status,
    }


def return_component(return_pct: float) -> float:
    return clamp(
        interpolate(
            return_pct,
            [(-5, 0), (-3, 10), (-2, 25), (-1, 40), (-0.25, 48), (0, 50), (0.25, 55), (1, 68), (2, 82), (3, 92), (5, 100)],
        )
    )


def breadth_component(advance_count: float, decline_count: float) -> float | None:
    total = advance_count + decline_count
    if total <= 0:
        return None
    return clamp(100 * advance_count / total)


def close_position_component(high: float, low: float, close: float) -> float:
    if high < low:
        raise ValueError("high must be >= low")
    if high == low:
        return 50.0
    return clamp(100 * (close - low) / (high - low))


def stability_component(intraday_range_pct: float) -> float:
    return clamp(interpolate(abs(intraday_range_pct), [(0, 100), (1.5, 100), (3, 70), (5, 30), (8, 0)]))


def market_score(metrics: Mapping[str, Any]) -> dict[str, Any]:
    return_pct = metric_value(metrics, "close_to_close_return_pct")
    advances = metric_value(metrics, "advance_count")
    declines = metric_value(metrics, "decline_count")
    high = metric_value(metrics, "high")
    low = metric_value(metrics, "low")
    close = metric_value(metrics, "close")
    intraday_range = metric_value(metrics, "intraday_range_pct")

    components: dict[str, float | None] = {
        "return": return_component(return_pct) if return_pct is not None else None,
        "breadth": breadth_component(advances, declines) if advances is not None and declines is not None else None,
        "close_position": close_position_component(high, low, close) if high is not None and low is not None and close is not None else None,
        "stability": stability_component(intraday_range) if intraday_range is not None else None,
    }
    index, status = weighted_available(components, MARKET_WEIGHTS)
    # Return is mandatory because the score represents market direction.
    if components["return"] is None:
        index, status = None, "score_unavailable"
    return {
        "components": {k: (round(v, 4) if v is not None else None) for k, v in components.items()},
        "index": round(index, 4) if index is not None else None,
        "score": score_to_1_10(index) if index is not None else None,
        "status": status,
    }
