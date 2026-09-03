"""Regression coverage for app.py's waiting/cooldown/held slot-status counts.

This exact bug has recurred three times: a new non-capacity admission hold
gets added to _try_admit's early-return checks, but _waiting_for_slot_count
isn't updated to exclude it, so a genuinely non-capacity-blocked symbol gets
reported as "waiting for a slot" -- see memory: waiting_count_conflation.
- 2026-08-31: _slot_cooldown wasn't excluded.
- 2026-09-03 (1st): _dead_hold and _ignored_until weren't excluded (reported
  live: 22/35 slots used, 10 "waiting" -- all 10 were actually held, not
  capacity-blocked).
- 2026-09-03 (2nd, same day): the ETF/ETN exclusion added to _add_symbol
  never held the symbol out at all -- an excluded symbol got re-admitted,
  re-checked, and re-dropped every ~1-3s indefinitely (real IB round trips
  each cycle), and while cycling out of self.states it counted as "waiting"
  -- reported live: 7 ETF symbols cycling this way showed as "7 waiting for
  a slot". Fixed with _excluded_stock_types, a permanent (never-expiring,
  unlike every other hold) hold-out set.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from momentum_scanner import config
from momentum_scanner.app import ScannerApp
from momentum_scanner.tunables import Tunables

NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=config.TZ)


@dataclass
class FakePersistenceState:
    streak: int


class FakePersistence:
    """Every symbol is treated as persistence-qualified -- these tests are
    about the capacity/hold bookkeeping downstream of persistence, not
    persistence itself (see test_filters.py for that)."""

    def state_for(self, sym):
        return FakePersistenceState(streak=999)


@dataclass
class FakeHit:
    symbol: str
    con_id: int
    rank: int
    source: str


def make_app(pending_symbols, dead_hold=(), ignored=(), cooldown=(), excluded=()):
    app = ScannerApp.__new__(ScannerApp)
    app.tunables = Tunables()
    app.persistence = FakePersistence()
    app.states = {}
    app._slot_cooldown = {s: NOW for s in cooldown}
    app._dead_hold = {s: NOW + timedelta(minutes=45) for s in dead_hold}
    app._ignored_until = {s: NOW + timedelta(hours=10) for s in ignored}
    app._excluded_stock_types = set(excluded)
    app._pending_hits = {s: FakeHit(symbol=s, con_id=0, rank=1, source="TEST") for s in pending_symbols}
    return app


def test_dead_held_symbols_do_not_count_as_waiting():
    app = make_app(pending_symbols=[f"DEAD{i}" for i in range(7)], dead_hold=[f"DEAD{i}" for i in range(7)])
    assert app._waiting_for_slot_count() == 0
    assert app._held_count() == 7


def test_non_tradable_symbols_do_not_count_as_waiting():
    app = make_app(pending_symbols=["NVD", "SPY", "TQQQ"], ignored=["NVD", "SPY", "TQQQ"])
    assert app._waiting_for_slot_count() == 0
    assert app._held_count() == 3


def test_cooldown_symbols_still_excluded_from_waiting():
    app = make_app(pending_symbols=["BUMPED"], cooldown=["BUMPED"])
    assert app._waiting_for_slot_count() == 0
    assert app._cooldown_wait_count() == 1
    assert app._held_count() == 0


def test_genuinely_unblocked_symbol_counts_as_waiting():
    app = make_app(pending_symbols=["REAL"])
    assert app._waiting_for_slot_count() == 1
    assert app._held_count() == 0
    assert app._cooldown_wait_count() == 0


def test_mixed_pending_symbols_are_bucketed_correctly():
    # Reproduces the exact reported shape: some dead-held, some non-tradable,
    # some genuinely waiting -- each must land in exactly one bucket.
    app = make_app(
        pending_symbols=["D1", "D2", "I1", "R1", "R2"],
        dead_hold=["D1", "D2"],
        ignored=["I1"],
    )
    assert app._waiting_for_slot_count() == 2  # R1, R2
    assert app._held_count() == 3  # D1, D2, I1
    assert app._cooldown_wait_count() == 0


def test_excluded_stock_type_symbols_do_not_count_as_waiting():
    # Reproduces the live 2026-09-03 report exactly: 7 ETF symbols cycling
    # in and out of self.states (admitted, dropped, re-admitted, ...) every
    # tick, none of them ever actually capacity-blocked.
    etfs = ["LULG", "CONX", "CIFU", "CIFG", "CLSX", "MSTW", "CONL"]
    app = make_app(pending_symbols=etfs, excluded=etfs)
    assert app._waiting_for_slot_count() == 0
    assert app._held_count() == 7
