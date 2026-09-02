"""
Runtime-mutable copies of the persistence + spike thresholds.

Everything in here starts seeded from config.py, but nothing downstream
(PersistenceTracker, spikes.py, the Textual control panel) reads config.py
directly for these values anymore -- they hold a reference to one shared
Tunables instance and read/write it live, so the scanner's behavior can be
adjusted mid-session without a restart.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import config


@dataclass
class Tunables:
    persistence_required: int = config.PERSISTENCE_REQUIRED
    persistence_top_n: int = config.PERSISTENCE_TOP_N
    persistence_reset_sec: float = config.PERSISTENCE_STREAK_RESET_SEC
    spike_threshold_pct: float = config.SPIKE_THRESHOLD_PCT
    spike_window_sec: float = config.SPIKE_WINDOW_SEC
    spike_lookback_sec: float = config.SPIKE_LOOKBACK_SEC
    spike_quiet_sec: float = config.SPIKE_QUIET_SEC
    scalp_position_usd: float = config.SCALP_POSITION_USD
    scalp_target_usd: float = config.SCALP_TARGET_USD
    scalp_rr_ratio: float = config.SCALP_RR_RATIO
    slot_reentry_cooldown_sec: float = config.SLOT_REENTRY_COOLDOWN_SEC
    max_live_symbols: int = config.MAX_LIVE_SYMBOLS
    scorer_reserved_slots: int = config.SCORER_RESERVED_SLOTS


@dataclass(frozen=True)
class TunableSpec:
    attr: str
    label: str
    step: float
    min: float
    max: float
    is_int: bool
    fmt: Callable[[float], str]


TUNABLE_SPECS: list[TunableSpec] = [
    TunableSpec("persistence_required", "Persist req", 1, 1, 10, True, str),
    TunableSpec("persistence_top_n", "Persist top-N", 1, 5, 50, True, str),
    TunableSpec("persistence_reset_sec", "Persist reset", 5, 10, 300, False, lambda v: f"{v:.0f}s"),
    TunableSpec("spike_threshold_pct", "Spike thresh", 0.5, 0.5, 20, False, lambda v: f"{v:.1f}%"),
    TunableSpec("spike_window_sec", "Spike window", 5, 5, 120, False, lambda v: f"{v:.0f}s"),
    TunableSpec("spike_lookback_sec", "Spike lookback", 60, 60, 3600, False, lambda v: f"{v / 60:.0f}m"),
    TunableSpec("spike_quiet_sec", "Spike quiet", 60, 60, 1800, False, lambda v: f"{v / 60:.0f}m"),
    TunableSpec("scalp_position_usd", "Scalp size $", 50, 50, 5000, False, lambda v: f"${v:.0f}"),
    TunableSpec("scalp_target_usd", "Scalp target $", 5, 5, 200, False, lambda v: f"${v:.0f}"),
    TunableSpec("scalp_rr_ratio", "Scalp R:R", 0.5, 1.0, 5.0, False, lambda v: f"{v:.1f}:1"),
    TunableSpec("slot_reentry_cooldown_sec", "Slot cooldown", 60, 0, 1800, False, lambda v: f"{v:.0f}s"),
    TunableSpec("max_live_symbols", "Live slots", 5, 5, 100, True, str),
    TunableSpec("scorer_reserved_slots", "Scorer slots", 1, 0, 10, True, str),
]


def bump(tunables: Tunables, spec: TunableSpec, direction: int) -> None:
    """Adjust one field on `tunables` by one step in `direction` (+1/-1), clamped."""
    current = getattr(tunables, spec.attr)
    new_value = current + spec.step * direction
    new_value = max(spec.min, min(spec.max, new_value))
    if spec.is_int:
        new_value = int(round(new_value))
    setattr(tunables, spec.attr, new_value)
