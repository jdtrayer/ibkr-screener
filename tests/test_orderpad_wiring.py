"""
Coverage for app.py's order-pad wiring: the slot pin, and the arm/fire path's
effect on app state.

The pin is a NEW hold path over a live slot, which is exactly the shape that
has produced the waiting-count conflation bug three times (see memory:
waiting_count_conflation, and tests/test_app_admission.py's docstring). The
difference here is that a pinned symbol is LIVE rather than waiting for a
slot, so the counts must be untouched -- asserted below so a future change
that starts routing pins through _pending_hits gets caught.

Tk stays out of this file (see FakePad); padwindow.py is verified by
screenshotting the real window, per memory: textual_ui_verification.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

from momentum_scanner import config
from momentum_scanner.app import ScannerApp
from momentum_scanner.filters import bump_candidate
from momentum_scanner.models import SymbolState
from momentum_scanner.session import Session
from momentum_scanner.tunables import Tunables

NOW = datetime(2026, 9, 16, 10, 30, tzinfo=config.TZ)


@pytest.fixture(autouse=True)
def isolated_order_history_dir(tmp_path, monkeypatch):
    """Every arm/fire in this file writes a real order_history.log_event --
    without this, every test run pollutes the real ./logs/orders/, same
    reasoning as test_news.py's isolated_news_state_file."""
    monkeypatch.setattr(config, "ORDER_HISTORY_DIR", str(tmp_path / "orders"))


@dataclass
class FakePad:
    """Stands in for OrderPadWindow -- same surface app.py calls, no Tk."""

    snapshot: object = None
    disarm_reasons: list = field(default_factory=list)
    blocks: list = field(default_factory=list)
    results: list = field(default_factory=list)

    def arm(self, snapshot):
        self.snapshot = snapshot

    def disarm(self, reason=None):
        self.snapshot = None
        self.disarm_reasons.append(reason)

    def show_block(self, reason):
        self.blocks.append(reason)

    def show_result(self, message):
        self.results.append(message)


@dataclass
class FakePersistenceState:
    streak: int


class FakePersistence:
    def __init__(self, streak=999):
        self.streak = streak

    def state_for(self, sym):
        return FakePersistenceState(streak=self.streak)


def make_state(symbol="TEST", last=10.0, atr=0.10, subscribed_ago_sec=600) -> SymbolState:
    state = SymbolState(symbol=symbol)
    state.tick.last, state.tick.bid, state.tick.ask = last, last - 0.02, last + 0.02
    # Real clock, not NOW: app.py's _on_pad_fire/_evict_unqualified read
    # datetime.now() themselves (they're the production entry points, not
    # pure functions taking a clock), so a fixed past timestamp here would
    # trip the staleness guard in every firing test. Tests that WANT a stale
    # quote back-date this explicitly.
    state.tick.updated_at = datetime.now(config.TZ)
    state.tick.volume = 1_000_000
    state.volume_offset = 0
    state.subscribed_at = NOW - timedelta(seconds=subscribed_ago_sec)
    state.atr.true_ranges.extend([atr] * config.ATR_LOOKBACK_BARS)
    return state


def make_app(states=(), persistence_streak=999) -> ScannerApp:
    app = ScannerApp.__new__(ScannerApp)
    app.tunables = Tunables()
    app.persistence = FakePersistence(persistence_streak)
    app.states = {s.symbol: s for s in states}
    app.session = Session.REGULAR
    app._pinned = set()
    app._manual_pending = set()
    app._pending_hits = {}
    app._slot_cooldown = {}
    app._dead_hold = {}
    app._ignored_until = {}
    app._excluded_stock_types = set()
    app._logged_no_slot = set()
    app._filter_reasons = {}
    app.pad = FakePad()
    app._pad_order_ids = {}
    app.ib = None  # only the submission-path tests (test_orderpad_submission.py) touch this
    return app


# -- arming -----------------------------------------------------------------


def test_arming_pins_the_slot_and_transfers_the_snapshot():
    app = make_app([make_state("CVDK", last=4.12)])
    app._arm("CVDK")
    assert app._pinned == {"CVDK"}
    assert app.pad.snapshot.symbol == "CVDK"
    assert app.pad.snapshot.price == 4.12


def test_arming_a_second_symbol_releases_the_first_pin():
    app = make_app([make_state("AAA"), make_state("BBB")])
    app._arm("AAA")
    app._arm("BBB")
    assert app._pinned == {"BBB"}


def test_manual_arm_refuses_a_held_out_symbol():
    """A symbol on the manual non-tradable list, dead-held, or of an
    excluded instrument type is refused synchronously and without touching
    IB. A symbol that's merely unknown to the live pool instead goes through
    on-demand admission (_admit_manual) -- the same qualify/subscribe
    pipeline a scan hit uses, which is IB-integration and is verified live
    rather than here (see memory: testing_approach)."""
    app = make_app([make_state("CVDK")])
    app._ignored_until["NOPE"] = NOW + timedelta(hours=1)
    app._on_pad_manual_arm("NOPE")
    assert app._pinned == set()
    assert any("held out" in (r or "") for r in app.pad.disarm_reasons)


def test_manual_arm_works_for_a_live_symbol():
    app = make_app([make_state("CVDK")])
    app._on_pad_manual_arm("CVDK")
    assert app._pinned == {"CVDK"}


def test_arming_a_symbol_with_no_price_is_refused():
    state = make_state("QUIET")
    state.tick.last = None
    app = make_app([state])
    app._arm("QUIET")
    assert app._pinned == set()
    assert app.pad.snapshot is None


# -- the pin holds against every eviction path ------------------------------


def test_pinned_symbol_is_never_the_bump_candidate():
    """A symbol failing both the $ floor and the spread ceiling is normally
    the first thing bumped -- pinned, it must be passed over entirely."""
    weak = make_state("WEAK", last=10.0)
    weak.tick.volume = 1  # far below any $ floor
    states = {"WEAK": weak}
    assert bump_candidate(states, Session.REGULAR, NOW) is weak
    assert bump_candidate(states, Session.REGULAR, NOW, pinned={"WEAK"}) is None


def test_pin_survives_persistence_decay():
    app = make_app([make_state("CVDK")], persistence_streak=0)
    app._arm("CVDK")
    app._evict_unqualified()
    assert "CVDK" in app.states
    assert app.pad.snapshot is not None


def test_pin_survives_spike_quiet_eviction():
    """The likeliest real case: a symbol pops, you arm it, and it goes quiet
    for a minute while you wait for an entry."""
    state = make_state("CVDK")
    state.spike.last_spike_at = NOW - timedelta(seconds=10_000)
    state.spike.last_new_high_at = NOW - timedelta(seconds=10_000)
    app = make_app([state], persistence_streak=999)
    app._arm("CVDK")
    app._evict_unqualified()
    assert "CVDK" in app.states


def test_unpinned_symbol_still_evicts_on_persistence_decay():
    """The pin must not accidentally disable eviction for everything else."""
    app = make_app([make_state("CVDK")], persistence_streak=0)
    app._evict_unqualified()
    assert "CVDK" not in app.states


# -- involuntary disarm -----------------------------------------------------


def test_removing_a_pinned_symbol_disarms_the_pad():
    """_remove_symbol is the choke point every removal reason routes through
    (session rollover, manual non-tradable mark, a contract that won't
    qualify) -- none of which a pin should override."""
    app = make_app([make_state("CVDK")])
    app._arm("CVDK")
    app._remove_symbol("CVDK")
    assert app._pinned == set()
    assert app.pad.snapshot is None
    assert any("left the live pool" in (r or "") for r in app.pad.disarm_reasons)


def test_disarm_releases_the_pin():
    app = make_app([make_state("CVDK")])
    app._arm("CVDK")
    app._on_pad_disarm()
    assert app._pinned == set()
    assert app.pad.snapshot is None


# -- firing -----------------------------------------------------------------


def test_fire_dry_runs_while_submission_is_disabled():
    app = make_app([make_state("CVDK", last=4.12)])
    assert app.tunables.order_pad_dry_run, "phase 1: dry run must default on"
    app._arm("CVDK")
    app._on_pad_fire()
    assert app.pad.results and app.pad.results[0].startswith("DRY RUN:")
    assert not app.pad.blocks
    assert app._pinned == {"CVDK"}  # a dry run doesn't disarm


def test_fire_is_refused_after_the_price_drifts():
    app = make_app([make_state("CVDK", last=4.12)])
    app._arm("CVDK")
    snapshot = app.pad.snapshot
    app.states["CVDK"].tick.last = snapshot.price + snapshot.drift_allowance * 2
    app._on_pad_fire()
    assert app.pad.blocks and "re-arm" in app.pad.blocks[0]
    assert not app.pad.results


def test_fire_is_refused_on_a_stale_quote():
    app = make_app([make_state("CVDK")])
    app._arm("CVDK")
    app.states["CVDK"].tick.updated_at = NOW - timedelta(days=1)
    app._on_pad_fire()
    assert app.pad.blocks and "stale" in app.pad.blocks[0]


def test_fire_is_refused_for_a_non_tradable_listed_symbol():
    app = make_app([make_state("CVDK")])
    app._arm("CVDK")
    app._ignored_until["CVDK"] = NOW + timedelta(hours=10)
    app._on_pad_fire()
    assert app.pad.blocks and "non-tradable" in app.pad.blocks[0]


def test_fire_does_nothing_when_disarmed():
    app = make_app([make_state("CVDK")])
    app._on_pad_fire()
    assert not app.pad.blocks and not app.pad.results


# -- slot-status counts (see module docstring) ------------------------------


def test_pinning_does_not_inflate_the_waiting_count():
    """A pinned symbol is live, not queued -- it must not show up in any of
    the three slot-status buckets."""
    app = make_app([make_state("CVDK")])
    app._pending_hits = {}
    app._arm("CVDK")
    assert app._waiting_for_slot_count() == 0
    assert app._cooldown_wait_count() == 0
    assert app._held_count() == 0
