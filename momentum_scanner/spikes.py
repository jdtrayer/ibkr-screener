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


def ready_to_evict(spike: SpikeState, tunables: Tunables, now: datetime) -> bool:
    if spike.last_spike_at is None:
        return False  # never spiked -- this eviction path doesn't apply
    quiet_since_spike = (now - spike.last_spike_at).total_seconds() >= tunables.spike_quiet_sec
    quiet_since_high = (
        spike.last_new_high_at is None
        or (now - spike.last_new_high_at).total_seconds() >= tunables.spike_quiet_sec
    )
    return quiet_since_spike and quiet_since_high
