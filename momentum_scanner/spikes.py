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
    """
    Retains price_history for the LONGER of spike_window_sec (fast detection)
    and scalp_stop_lookback_sec (a separate, longer window scalp_sizing uses
    for its stop level) -- these two need different lengths: detection must
    stay short to catch a fast move, but a stop based on that same short
    window collapses to near-zero risk the moment price stops moving for even
    a few seconds mid-spike (constantly, in practice), which is what made
    scalp_sizing's target/stop come out a penny apart. Spike DETECTION below
    still only looks at the spike_window_sec-recent slice of this history.
    """
    spike.price_history.append((now, price))
    retain_sec = max(tunables.spike_window_sec, tunables.scalp_stop_lookback_sec)
    cutoff = now - timedelta(seconds=retain_sec)
    while spike.price_history and spike.price_history[0][0] < cutoff:
        spike.price_history.popleft()

    if spike.session_high is None or price > spike.session_high:
        spike.session_high = price
        spike.last_new_high_at = now

    detect_cutoff = now - timedelta(seconds=tunables.spike_window_sec)
    window_min = min(p for t, p in spike.price_history if t >= detect_cutoff)
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
    Rough, at-a-glance scalp sizing: the low over the trailing
    scalp_stop_lookback_sec (NOT the short spike-detection window -- that
    collapses to near-zero risk the moment price pauses even briefly, which
    happens constantly mid-spike) is used as a simple technical stop, target
    is set at tunables.scalp_rr_ratio times that risk (reward:risk, not
    "assume the last move repeats"), and shares are however many it takes to
    clear tunables.scalp_target_usd at that per-share reward -- capped so
    shares * price never exceeds tunables.scalp_max_position_usd. That cap is
    what actually keeps share counts realistic: the theoretical stop-loss risk
    is already fixed at scalp_target_usd / scalp_rr_ratio regardless of price,
    but share count alone can still balloon into unrealistic buying power when
    the stop distance is small in dollar terms, and that's what the position
    cap bounds. This is meant to tell a quick story ("worth pulling up the
    chart/L2/T&S" vs. "wait") -- not a risk-managed trade plan, since the
    window low is treated as support and the target as reachable, neither of
    which is guaranteed. When the position cap binds, the actual profit if
    target is hit is less than scalp_target_usd -- shares * (target - price).

    Returns (shares, target_price, stop_price), or None if there's not enough
    live data yet, no room between price and the window low, or the implied
    share count would be zero.
    """
    if not spike.price_history or price <= 0:
        return None
    stop_basis = min(p for _, p in spike.price_history)
    if stop_basis <= 0 or stop_basis >= price:
        return None
    risk_per_share = price - stop_basis
    reward_per_share = risk_per_share * tunables.scalp_rr_ratio
    if reward_per_share <= 0:
        return None
    shares_for_target = tunables.scalp_target_usd / reward_per_share
    shares_for_position_cap = tunables.scalp_max_position_usd / price
    shares = int(min(shares_for_target, shares_for_position_cap))
    if shares <= 0:
        return None
    target_price = price + reward_per_share
    return shares, target_price, stop_basis


def ready_to_evict(spike: SpikeState, tunables: Tunables, now: datetime) -> bool:
    if spike.last_spike_at is None:
        return False  # never spiked -- this eviction path doesn't apply
    quiet_since_spike = (now - spike.last_spike_at).total_seconds() >= tunables.spike_quiet_sec
    quiet_since_high = (
        spike.last_new_high_at is None
        or (now - spike.last_new_high_at).total_seconds() >= tunables.spike_quiet_sec
    )
    return quiet_since_spike and quiet_since_high
