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
from .config import TZ

log = logging.getLogger(__name__)


class PersistenceTracker:
    """
    Requires a symbol to appear in the top PERSISTENCE_TOP_N of a scan
    refresh for PERSISTENCE_REQUIRED *consecutive* refreshes before it
    counts as qualified. A symbol that drops out of the top-N is given a
    grace window (PERSISTENCE_STREAK_RESET_SEC) before its streak resets,
    so a single missed refresh due to scanner jitter doesn't punish it --
    but a symbol that's genuinely gone (not just reordered) still decays.
    """

    def __init__(self):
        self._state: dict[str, PersistenceState] = {}

    def update(self, hits: dict[str, ScanHit]) -> set[str]:
        now = datetime.now(TZ)
        qualifying_symbols = {
            sym for sym, hit in hits.items() if hit.rank <= config.PERSISTENCE_TOP_N
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
            if gap > config.PERSISTENCE_STREAK_RESET_SEC:
                st.streak = 0
                st.first_qualified_at = None

        qualified = {
            sym for sym, st in self._state.items() if st.streak >= config.PERSISTENCE_REQUIRED
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
