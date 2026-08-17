"""Piecewise-linear time integration for hourly weather exposure windows.

The frozen primary temporal estimator for the final TCC exposure is
piecewise-linear time integration over the pre-open window
``[official core open - 120 minutes, official core open)`` — NOT the legacy
2-point arithmetic mean used by the engineering pilot.

For Taipei's 07:00-09:00 window with hourly breakpoints at 07:00 / 08:00 /
09:00, the exact mean of the piecewise-linear interpolant is::

    (TCC07 + 2*TCC08 + TCC09) / 4

This helper is generic (not Taipei-only): it supports any window boundary
(:00, :15, :30, ...) and any hourly breakpoint set, and fails closed when a
required bracket is missing or a timestamp is duplicated.

Pure and zero-network; all inputs are datetimes (naive or tz-aware, but
mutually consistent within a single call).
"""
from __future__ import annotations

from datetime import datetime

from scripts.v2.core import V2Error


def _seconds(a: datetime, b: datetime) -> float:
    """Duration in seconds between two consistent datetimes."""
    return (b - a).total_seconds()


def _value_at(x: datetime, times: list[datetime], values: list[float]) -> float:
    """Interpolate the piecewise-linear value at time ``x``.

    ``x`` must lie within ``[times[0], times[-1]]``.  Exact breakpoints return
    their stored value; otherwise linear interpolation between the bracketing
    breakpoints is used.
    """
    for i in range(len(times) - 1):
        t0, t1 = times[i], times[i + 1]
        if t0 <= x <= t1:
            if x == t0:
                return values[i]
            if x == t1:
                return values[i + 1]
            w = _seconds(t0, x) / _seconds(t0, t1)
            return values[i] + w * (values[i + 1] - values[i])
    raise V2Error(f"time {x} outside bracketing breakpoints")


def piecewise_linear_mean(
    times: list[datetime],
    values: list[float],
    start: datetime,
    end: datetime,
) -> float:
    """Exact mean of the piecewise-linear interpolant over ``[start, end)``.

    Raises V2Error when:
    - ``times`` and ``values`` differ in length or are empty;
    - a timestamp is duplicated;
    - the window is not fully bracketed (``start < times[0]`` or
      ``end > times[-1]``) — missing bracket fails closed;
    - the window has non-positive duration.
    """
    if len(times) != len(values):
        raise V2Error("piecewise-linear integration requires equal-length times and values")
    if not times:
        raise V2Error("piecewise-linear integration requires at least one breakpoint")
    pairs = sorted(zip(times, values), key=lambda p: p[0])
    ts = [p[0] for p in pairs]
    vs = [p[1] for p in pairs]
    for i in range(len(ts) - 1):
        if ts[i] == ts[i + 1]:
            raise V2Error("duplicate timestamp in piecewise-linear breakpoints")
    if start > end:
        raise V2Error("integration window start must not be after end")
    if start < ts[0] or end > ts[-1]:
        raise V2Error("missing bracket: window not fully covered by hourly breakpoints")

    # Build the evaluation nodes: window start, interior breakpoints, window end.
    nodes = [start]
    for t in ts:
        if start < t < end:
            nodes.append(t)
    if end not in nodes:
        nodes.append(end)

    node_values = [_value_at(x, ts, vs) for x in nodes]

    integral = 0.0
    for i in range(len(nodes) - 1):
        dt = _seconds(nodes[i], nodes[i + 1])
        integral += 0.5 * (node_values[i] + node_values[i + 1]) * dt
    duration = _seconds(start, end)
    if duration <= 0:
        raise V2Error("integration window has non-positive duration")
    return integral / duration
