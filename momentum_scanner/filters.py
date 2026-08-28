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
from .session import Session
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


def min_dollar_volume_for(session: Session) -> float:
    """The $ floor a symbol must clear, given its session -- premarket/afterhours
    liquidity is thin by nature, so it gets a genuinely lower bar than regular
    hours rather than the same MIN_DOLLAR_VOLUME applied to a much smaller pool."""
    return config.MIN_DOLLAR_VOLUME if session == Session.REGULAR else config.MIN_DOLLAR_VOLUME_EXTENDED_HOURS


def check_dollar_volume(state: SymbolState, session: Session) -> bool:
    """True if the symbol clears the $ volume floor for its session."""
    dv = state.dollar_volume
    return dv is not None and dv >= min_dollar_volume_for(session)


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


def _slot_warmed_up(state: SymbolState, now: datetime) -> bool:
    """Past the post-subscribe grace period, so missing/thin data can be held
    against it rather than punishing a symbol that just hasn't ticked yet."""
    return (
        state.subscribed_at is not None
        and (now - state.subscribed_at).total_seconds() >= config.SLOT_BUMP_WARMUP_SEC
    )


def _slot_spike_held(state: SymbolState, now: datetime) -> bool:
    """Exempt from bumping -- it spiked recently, so a transient dip in dollar
    volume or a momentarily wide print shouldn't cost it the slot mid-move."""
    last = state.spike.last_spike_at
    return last is not None and (now - last).total_seconds() < config.SLOT_BUMP_SPIKE_HOLD_SEC


def _dv_weakness(state: SymbolState, session: Session) -> float:
    """0 if clearing the $ floor; otherwise how many floor-multiples below it
    (no data despite warm-up is treated as maximally weak)."""
    dv = state.dollar_volume
    if dv is None:
        return float("inf")
    floor = min_dollar_volume_for(session)
    if dv >= floor:
        return 0.0
    return floor / max(dv, 1.0)


def _spread_weakness(state: SymbolState) -> float:
    """0 if within the spread ceiling or spread is simply unknown (quotes lag
    trades on some feeds -- absence isn't held against it, unlike dollar
    volume); otherwise how many ceiling-multiples over it."""
    spread_pct = state.tick.spread_pct
    if spread_pct is None or spread_pct <= config.MAX_SPREAD_PCT:
        return 0.0
    return spread_pct / config.MAX_SPREAD_PCT


def bump_reason(state: SymbolState, session: Session) -> str:
    """Human-readable reason `state` was chosen as the bump candidate --
    whichever of the two weakness signals is larger for it."""
    if _dv_weakness(state, session) >= _spread_weakness(state):
        dv = state.dollar_volume
        dv_txt = f"{dv:,.0f}" if dv is not None else "unknown"
        return f"dollar volume {dv_txt} below floor {min_dollar_volume_for(session):,}"
    return f"spread {state.tick.spread_pct:.2f}% over max {config.MAX_SPREAD_PCT}%"


def bump_candidate(
    states: dict[str, SymbolState], session: Session, now: datetime | None = None
) -> SymbolState | None:
    """
    The weakest current occupant that may be bumped to admit a newly
    qualified symbol when all slots are full -- past warm-up, not spike-held,
    and failing either the $ volume floor or the spread ceiling. This is the
    ONLY eviction path for these two signals -- deliberately no idle timer --
    so an occupant that's actually failing keeps its slot indefinitely as
    long as nothing better is waiting for it; hidden from display (see
    display_reason) but otherwise left alone.

    Weakness on each axis is normalized to "how many threshold-multiples past
    the line" so the two signals compare on equal footing rather than one
    axis silently always winning; the single worst offender across both is
    returned. None if every occupant is entitled to its slot.
    """
    now = now or datetime.now(TZ)
    best, best_score = None, 0.0
    for s in states.values():
        if not _slot_warmed_up(s, now) or _slot_spike_held(s, now):
            continue
        score = max(_dv_weakness(s, session), _spread_weakness(s))
        if score > best_score:
            best, best_score = s, score
    return best


def display_reason(state: SymbolState, session: Session) -> str | None:
    """
    None if `state` would clear display.render's row filter; otherwise a short
    human-readable reason it's being hidden. Single source of truth for that
    filter so app.py can log transitions without duplicating display.py's logic.
    """
    if state.rvol is None:
        return "no rvol (baseline missing or expected volume is 0 for this point in the session)"
    if state.rvol < config.RVOL_DISPLAY_FLOOR:
        return f"rvol {state.rvol:.2f}x below display floor {config.RVOL_DISPLAY_FLOOR}x"
    if not check_dollar_volume(state, session):
        dv = state.dollar_volume
        dv_txt = f"{dv:,.0f}" if dv is not None else "unknown"
        return f"dollar volume {dv_txt} below floor {min_dollar_volume_for(session):,}"
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
