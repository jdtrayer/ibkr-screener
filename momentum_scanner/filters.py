"""
Post-scanner filters: persistence (anti-flicker), $ volume floor, spread,
float ceiling, and halt-resume transition tracking.

These are deliberately independent, small functions/classes rather than one
monolithic pipeline object, since each is tuned by its own config knobs and
the app wires them together explicitly in app.py.
"""
from __future__ import annotations

import logging
from datetime import datetime

from . import config
from .models import HaltState, PersistenceState, SymbolState
from .scanner import ScanHit
from .tunables import Tunables
from .config import TZ

log = logging.getLogger(__name__)


class PersistenceTracker:
    """
    Requires a symbol to appear in the top tunables.persistence_top_n of a
    scan refresh for tunables.persistence_required *consecutive* refreshes
    before it counts as qualified. A symbol that drops out of the top-N is
    given a grace window (tunables.persistence_reset_sec) before its streak
    resets, so a single missed refresh due to scanner jitter doesn't punish
    it -- but a symbol that's genuinely gone (not just reordered) still
    decays. All three thresholds are runtime-mutable via `tunables`.
    """

    def __init__(self, tunables: Tunables):
        self.tunables = tunables
        self._state: dict[str, PersistenceState] = {}

    def update(self, hits: dict[str, ScanHit]) -> set[str]:
        now = datetime.now(TZ)
        qualifying_symbols = {
            sym for sym, hit in hits.items() if hit.rank <= self.tunables.persistence_top_n
        }

        for sym in qualifying_symbols:
            st = self._state.setdefault(sym, PersistenceState())
            st.streak += 1
            st.last_seen = now
            if st.first_qualified_at is None:
                st.first_qualified_at = now

        for sym, st in self._state.items():
            if sym in qualifying_symbols:
                continue
            if st.last_seen is None:
                continue
            gap = (now - st.last_seen).total_seconds()
            if gap > self.tunables.persistence_reset_sec:
                st.streak = 0
                st.first_qualified_at = None

        qualified = {
            sym for sym, st in self._state.items() if st.streak >= self.tunables.persistence_required
        }
        for sym in qualified:
            self._state[sym].displayed = True
        return qualified

    def state_for(self, symbol: str) -> PersistenceState:
        return self._state.setdefault(symbol, PersistenceState())


def check_dollar_volume(state: SymbolState) -> bool:
    """True if the symbol clears the configured $ volume floor."""
    dv = state.dollar_volume
    return dv is not None and dv >= config.MIN_DOLLAR_VOLUME


def check_spread(state: SymbolState) -> tuple[bool, float | None]:
    """Returns (passes, spread_pct). `passes` is False only if SPREAD_HARD_REJECT and over threshold."""
    spread_pct = state.tick.spread_pct
    if spread_pct is None:
        return True, None
    over = spread_pct > config.MAX_SPREAD_PCT
    passes = not (over and config.SPREAD_HARD_REJECT)
    return passes, spread_pct


def check_float(state: SymbolState) -> bool:
    """Returns True (passes) unless float is known and over the ceiling and hard-reject is on."""
    if not state.float_known:
        return True
    over = (state.float_shares or 0) > config.FLOAT_CEILING_SHARES
    return not (over and config.FLOAT_HARD_REJECT)


def update_dv_floor_timer(state: SymbolState, now: datetime | None = None) -> None:
    """
    Track how long `state` has been continuously below the $ volume floor.
    A dollar_volume of None counts as below the floor -- warm-up handling
    (not judging a symbol before its first ticks arrive) lives in
    dv_evict_reason, not here, so this timer stays a pure record of fact.
    """
    now = now or datetime.now(TZ)
    if check_dollar_volume(state):
        state.dv_below_floor_since = None
    elif state.dv_below_floor_since is None:
        state.dv_below_floor_since = now


def _dv_spike_held(state: SymbolState, now: datetime) -> bool:
    last = state.spike.last_spike_at
    return last is not None and (now - last).total_seconds() < config.DV_SPIKE_HOLD_SEC


def _dv_judgeable(state: SymbolState, now: datetime) -> bool:
    """Past warm-up, so its dollar volume (or lack of data) can be held against it."""
    return (
        state.subscribed_at is not None
        and (now - state.subscribed_at).total_seconds() >= config.DV_EVICT_WARMUP_SEC
    )


def dv_evict_reason(state: SymbolState, tunables: Tunables, now: datetime | None = None) -> str | None:
    """
    None if `state` may keep its live-symbol slot; otherwise a short reason it
    should be evicted as a dollar-volume squatter. Never evicts a symbol
    clearing the $ floor, one still in its post-subscribe warm-up, or one
    that spiked within the last DV_SPIKE_HOLD_SEC.
    """
    now = now or datetime.now(TZ)
    if state.dv_below_floor_since is None:
        return None
    if not _dv_judgeable(state, now):
        return None
    if _dv_spike_held(state, now):
        return None
    below_sec = (now - state.dv_below_floor_since).total_seconds()
    if below_sec < tunables.dv_evict_sec:
        return None
    dv = state.dollar_volume
    dv_txt = f"{dv:,.0f}" if dv is not None else "unknown"
    return (
        f"dollar volume {dv_txt} below floor {config.MIN_DOLLAR_VOLUME:,} "
        f"for {below_sec:.0f}s (limit {tunables.dv_evict_sec:.0f}s)"
    )


def bump_candidate(
    states: dict[str, SymbolState], tunables: Tunables, now: datetime | None = None
) -> SymbolState | None:
    """
    The weakest current occupant that may be bumped to admit a newly
    qualified symbol when all slots are full: past warm-up, currently below
    the $ volume floor, and not spike-held. Lowest dollar volume wins the
    eviction (None -- no data despite warm-up -- sorts as weakest of all).
    Returns None if every occupant is entitled to its slot.
    """
    now = now or datetime.now(TZ)
    # Re-check the floor live rather than trusting dv_below_floor_since alone:
    # the timer is only refreshed on the tick cadence, and a scan callback can
    # land between ticks, right after a symbol crossed the floor.
    candidates = [
        s for s in states.values()
        if s.dv_below_floor_since is not None
        and not check_dollar_volume(s)
        and _dv_judgeable(s, now)
        and not _dv_spike_held(s, now)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda s: s.dollar_volume if s.dollar_volume is not None else -1.0)


def display_reason(state: SymbolState) -> str | None:
    """
    None if `state` would clear display.render's row filter; otherwise a short
    human-readable reason it's being hidden. Single source of truth for that
    filter so app.py can log transitions without duplicating display.py's logic.
    """
    if state.rvol is None:
        return "no rvol (baseline missing or expected volume is 0 for this point in the session)"
    if state.rvol < config.RVOL_DISPLAY_FLOOR:
        return f"rvol {state.rvol:.2f}x below display floor {config.RVOL_DISPLAY_FLOOR}x"
    if not check_dollar_volume(state):
        dv = state.dollar_volume
        dv_txt = f"{dv:,.0f}" if dv is not None else "unknown"
        return f"dollar volume {dv_txt} below floor {config.MIN_DOLLAR_VOLUME:,}"
    spread_ok, spread_pct = check_spread(state)
    if not spread_ok:
        return f"spread {spread_pct:.2f}% over hard-reject threshold {config.MAX_SPREAD_PCT}%"
    if not check_float(state):
        return f"float {state.float_shares:,.0f} over hard-reject ceiling {config.FLOAT_CEILING_SHARES:,}"
    return None


def update_halt_state(halt: HaltState, halted_value: float, now: datetime | None = None) -> None:
    """
    Feed the latest generic tick 49 ("Halted") value in: 0 not halted,
    1 general halt, 2 volatility halt. Tracks the halted -> not-halted
    transition so `recently_resumed()` can flag the real catalyst window.
    """
    now = now or datetime.now(TZ)
    is_halted_now = halted_value in (1, 2)

    if is_halted_now and not halt.is_halted:
        halt.last_transition_at = now
    elif not is_halted_now and halt.is_halted:
        halt.last_transition_at = now
        halt.last_resume_at = now

    halt.is_halted = is_halted_now
