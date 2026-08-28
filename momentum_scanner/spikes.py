"""
Spike detection: flags a fast intraday price move as a "spike" event and
counts how many have happened in a trailing lookback window (the SPIKE×N
flag in display.py). A symbol that's spiked at least once but has since gone
quiet -- no new spike and no new session high for spike_quiet_sec -- becomes
eligible for eviction from live tracking via ready_to_evict(), independent of
persistence streak.

Deliberately standalone functions operating on SpikeState, same style as
filters.py, since app.py already owns the wiring (call update_spike_state on
every tick, active_spike_count for display, ready_to_evict for eviction).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .models import SpikeState
from .tunables import Tunables


def update_spike_state(spike: SpikeState, price: float, now: datetime, tunables: Tunables) -> None:
    spike.price_history.append((now, price))
    cutoff = now - timedelta(seconds=tunables.spike_window_sec)
    while spike.price_history and spike.price_history[0][0] < cutoff:
        spike.price_history.popleft()

    if spike.session_high is None or price > spike.session_high:
        spike.session_high = price
        spike.last_new_high_at = now

    window_min = min(p for _, p in spike.price_history)
    if window_min <= 0:
        return

    move_pct = (price - window_min) / window_min * 100.0
    if move_pct < tunables.spike_threshold_pct:
        return

    cooldown_elapsed = (
        spike.last_spike_at is None
        or (now - spike.last_spike_at).total_seconds() >= tunables.spike_window_sec
    )
    if cooldown_elapsed:
        spike.last_spike_at = now
        spike.events.append(now)


def active_spike_count(spike: SpikeState, tunables: Tunables, now: datetime) -> int:
    cutoff = now - timedelta(seconds=tunables.spike_lookback_sec)
    spike.events = [e for e in spike.events if e >= cutoff]
    return len(spike.events)


def scalp_sizing(spike: SpikeState, price: float, tunables: Tunables) -> tuple[int, float, float] | None:
    """
    Rough, at-a-glance scalp sizing off the live spike window: the spike
    window's low is used as a simple technical stop, target is set at
    tunables.scalp_rr_ratio times that risk (reward:risk, not "assume the last
    move repeats"), and shares are however many it takes to clear
    tunables.scalp_target_usd at that per-share reward. This is meant to tell
    a quick story ("worth pulling up the chart/L2/T&S" vs. "wait") -- not a
    risk-managed trade plan, since the window low is treated as support and
    the target as reachable, neither of which is guaranteed.

    Returns (shares, target_price, stop_price), or None if there's not enough
    live data yet, no room between price and the window low, or the implied
    share count would be zero.
    """
    if not spike.price_history or price <= 0:
        return None
    window_min = min(p for _, p in spike.price_history)
    if window_min <= 0 or window_min >= price:
        return None
    risk_per_share = price - window_min
    reward_per_share = risk_per_share * tunables.scalp_rr_ratio
    if reward_per_share <= 0:
        return None
    shares = int(tunables.scalp_target_usd / reward_per_share)
    if shares <= 0:
        return None
    target_price = price + reward_per_share
    return shares, target_price, window_min


def ready_to_evict(spike: SpikeState, tunables: Tunables, now: datetime) -> bool:
    if spike.last_spike_at is None:
        return False  # never spiked -- this eviction path doesn't apply
    quiet_since_spike = (now - spike.last_spike_at).total_seconds() >= tunables.spike_quiet_sec
    quiet_since_high = (
        spike.last_new_high_at is None
        or (now - spike.last_new_high_at).total_seconds() >= tunables.spike_quiet_sec
    )
    return quiet_since_spike and quiet_since_high
