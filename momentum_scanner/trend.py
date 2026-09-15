"""
Trend direction: a slower, zoomed-out companion to spikes.py's fast 20s
SPIKE×N detector. Compares the oldest vs newest price in a longer rolling
window (tunables.trend_window_sec) to classify direction as up/down/flat for
the Trend column.

Deliberately does not gate or suppress SPIKE×N -- a fast 20s pop and a
falling multi-minute trend aren't mutually exclusive (that's exactly the
case that prompted this module: a brief bounce inside a selloff still spikes
off its own local low, and a genuine reversal looks identical to that bounce
at the moment it starts). This module only adds context, same style as
spikes.py operating on SpikeState.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import TrendState
from .tunables import Tunables


def update_trend_state(trend: TrendState, price: float, now: datetime, tunables: Tunables) -> None:
    trend.price_history.append((now, price))
    cutoff = now - timedelta(seconds=tunables.trend_window_sec)
    while trend.price_history and trend.price_history[0][0] < cutoff:
        trend.price_history.popleft()


def trend_direction(trend: TrendState, tunables: Tunables) -> str | None:
    """
    "up" / "down" / "flat", or None if there isn't yet enough history to
    speak to the full window -- a freshly-subscribed symbol with only a few
    ticks shouldn't flash a direction based on a near-zero time span.
    """
    if len(trend.price_history) < 2:
        return None
    oldest_ts, oldest_price = trend.price_history[0]
    newest_ts, newest_price = trend.price_history[-1]
    if oldest_price <= 0:
        return None
    span_sec = (newest_ts - oldest_ts).total_seconds()
    if span_sec < tunables.trend_window_sec * 0.5:
        return None

    move_pct = (newest_price - oldest_price) / oldest_price * 100.0
    if move_pct > tunables.trend_flat_pct:
        return "up"
    if move_pct < -tunables.trend_flat_pct:
        return "down"
    return "flat"
