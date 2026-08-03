from scripts.scoring import market_score, weather_score


def metric(value):
    return {"value": value}


def test_weather_score_is_deterministic():
    result = weather_score({
        "apparent_temperature_mean_c": metric(21),
        "precipitation_total_mm": metric(0),
        "cloud_cover_mean_pct": metric(20),
        "sunshine_duration_hours": metric(4),
        "aqi": metric(40),
        "wind_gust_max_kmh": metric(15),
    }, window_hours=5.5, alert_severity="none")
    assert result["index"] == 95.2727
    assert result["score"] == 10
    assert result["status"] == "complete_score"


def test_extreme_alert_caps_weather():
    result = weather_score({
        "apparent_temperature_mean_c": metric(21),
        "precipitation_total_mm": metric(0),
        "cloud_cover_mean_pct": metric(0),
        "aqi": metric(10),
        "wind_gust_max_kmh": metric(5),
    }, window_hours=2, alert_severity="extreme")
    assert result["index"] == 20
    assert result["score"] == 2


def test_market_score_requires_return():
    result = market_score({"high": metric(101), "low": metric(99), "close": metric(100)})
    assert result["status"] == "score_unavailable"
    assert result["score"] is None


def test_market_score_fixture():
    result = market_score({
        "close_to_close_return_pct": metric(0.8),
        "advance_count": metric(600),
        "decline_count": metric(400),
        "high": metric(101),
        "low": metric(99),
        "close": metric(100.6),
        "intraday_range_pct": metric(1.2),
    })
    assert result["index"] == 66.72
    assert result["score"] == 7
