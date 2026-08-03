#!/usr/bin/env python3
"""Run preregistered exploratory correlations and a Newey-West regression baseline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from scipy.stats import pearsonr, spearmanr
import statsmodels.api as sm

ROOT = Path(__file__).resolve().parents[1]


def safe_corr(frame: pd.DataFrame, x: str, y: str) -> dict:
    data = frame[[x, y]].dropna()
    if len(data) < 3 or data[x].nunique() < 2 or data[y].nunique() < 2:
        return {"n": len(data), "pearson_r": None, "pearson_p": None, "spearman_rho": None, "spearman_p": None}
    pr = pearsonr(data[x], data[y])
    sr = spearmanr(data[x], data[y])
    return {"n": len(data), "pearson_r": pr.statistic, "pearson_p": pr.pvalue, "spearman_rho": sr.statistic, "spearman_p": sr.pvalue}


def nw_regression(frame: pd.DataFrame, weather_col: str, return_col: str) -> dict:
    data = frame[[weather_col, return_col]].dropna()
    if len(data) < 20 or data[weather_col].nunique() < 2:
        return {"n": len(data), "status": "insufficient_data"}
    X = sm.add_constant(data[[weather_col]])
    model = sm.OLS(data[return_col], X).fit(cov_type="HAC", cov_kwds={"maxlags": 5})
    return {
        "n": len(data),
        "status": "ok",
        "beta": float(model.params[weather_col]),
        "p_value": float(model.pvalues[weather_col]),
        "r_squared": float(model.rsquared),
        "hac_maxlags": 5
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=str(ROOT / "data" / "derived" / "daily_panel.csv"))
    parser.add_argument("--output", default=str(ROOT / "analysis" / "results" / "baseline.json"))
    args = parser.parse_args()
    frame = pd.read_csv(args.input)
    results = {"markets": {}}
    for market_id, group in frame.groupby("market_id"):
        results["markets"][market_id] = {
            "pre_open_score_vs_return": safe_corr(group, "weather_pre_open_score", "market_close_to_close_return_pct"),
            "trading_weather_score_vs_market_score": safe_corr(group, "weather_trading_session_score", "market_score"),
            "pre_open_index_newey_west": nw_regression(group, "weather_pre_open_index", "market_close_to_close_return_pct")
        }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
