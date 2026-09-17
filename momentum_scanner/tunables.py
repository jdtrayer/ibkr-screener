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
    trend_window_sec: float = config.TREND_WINDOW_SEC
    trend_flat_pct: float = config.TREND_FLAT_PCT
    risk_usd: float = config.RISK_USD
    atr_multiplier: float = config.ATR_MULTIPLIER
    min_spreads: int = config.MIN_SPREADS
    r_multiple: float = config.R_MULTIPLE
    max_position_usd: float = config.MAX_POSITION_USD
    min_shares: int = config.MIN_SHARES
    pass_through_per_share: float = config.PASS_THROUGH_PER_SHARE
    stop_trigger_lead_pct: float = config.STOP_TRIGGER_LEAD_PCT
    slot_reentry_cooldown_sec: float = config.SLOT_REENTRY_COOLDOWN_SEC
    max_live_symbols: int = config.MAX_LIVE_SYMBOLS
    scorer_reserved_slots: int = config.SCORER_RESERVED_SLOTS
    scorer_admit_min_score: float = config.SCORER_ADMIT_MIN_SCORE
    dead_hold_sec: float = config.DEAD_HOLD_SEC
    # Not a TunableSpec/bump() field -- boolean, toggled by its own button in
    # TunablesPanel rather than +/-. Defaults on: a fresh session should
    # never fire live without someone deliberately switching it off.
    order_pad_dry_run: bool = True


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
    TunableSpec("trend_window_sec", "Trend window", 30, 30, 900, False, lambda v: f"{v / 60:.1f}m"),
    TunableSpec("trend_flat_pct", "Trend flat", 0.25, 0.25, 10, False, lambda v: f"{v:.2f}%"),
    TunableSpec("risk_usd", "Risk $", 5, 5, 500, False, lambda v: f"${v:.0f}"),
    TunableSpec("atr_multiplier", "ATR x", 0.1, 0.1, 5.0, False, lambda v: f"{v:.1f}x"),
    TunableSpec("min_spreads", "Min Spreads", 1, 1, 30, True, str),
    TunableSpec("r_multiple", "R Multiple", 0.25, 0.5, 5.0, False, lambda v: f"{v:.2f}"),
    TunableSpec("max_position_usd", "Max Position $", 50, 100, 10000, False, lambda v: f"${v:.0f}"),
    TunableSpec("min_shares", "Min Shares", 5, 1, 200, True, str),
    TunableSpec("pass_through_per_share", "Pass-thru $/sh", 0.0001, 0.0, 0.01, False, lambda v: f"${v:.4f}"),
    TunableSpec("stop_trigger_lead_pct", "Stop trig lead", 0.05, 0.0, 2.0, False, lambda v: f"{v:.2f}x"),
    TunableSpec("slot_reentry_cooldown_sec", "Slot cooldown", 60, 0, 1800, False, lambda v: f"{v:.0f}s"),
    TunableSpec("max_live_symbols", "Live slots", 5, 5, 100, True, str),
    TunableSpec("scorer_reserved_slots", "Scorer slots", 1, 0, 10, True, str),
    TunableSpec("scorer_admit_min_score", "Scorer min score", 0.25, -5.0, 5.0, False, lambda v: f"{v:+.2f}"),
    TunableSpec("dead_hold_sec", "Dead hold", 300, 300, 7200, False, lambda v: f"{v / 60:.0f}m"),
]


def bump(tunables: Tunables, spec: TunableSpec, direction: int) -> None:
    """Adjust one field on `tunables` by one step in `direction` (+1/-1), clamped."""
    current = getattr(tunables, spec.attr)
    new_value = current + spec.step * direction
    new_value = max(spec.min, min(spec.max, new_value))
    if spec.is_int:
        new_value = int(round(new_value))
    setattr(tunables, spec.attr, new_value)
