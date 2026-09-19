"""
Coverage for app.py's order-pad wiring: load / arm / fire, who owns the pad's
market-data feed, and the borrowed-slot pin.

The pin is a hold path over a live slot, which is exactly the shape that has
produced the waiting-count conflation bug three times (see memory:
waiting_count_conflation, and tests/test_app_admission.py's docstring). A
pad-OWNED feed is deliberately outside self.states altogether, so it can't
touch the slot counts by construction -- asserted below so a future change
that routes it through the pool gets caught.

Tk stays out of this file (see FakePad); padwindow.py is verified by
screenshotting the real window, per memory: textual_ui_verification.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from momentum_scanner import config
from momentum_scanner.app import ScannerApp, _PadFeed
from momentum_scanner.filters import bump_candidate
from momentum_scanner.models import SymbolState
from momentum_scanner.orderpad import PadController, PadMode
from momentum_scanner.scanner import ScanHit
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

    blocks: list = field(default_factory=list)
    results: list = field(default_factory=list)
    symbol_texts: list = field(default_factory=list)
    raised: list = field(default_factory=list)
    dirty: int = 0
    message_clears: int = 0

    def mark_dirty(self):
        self.dirty += 1

    def show_block(self, reason):
        self.blocks.append(reason)

    def show_result(self, message):
        self.results.append(message)

    def clear_message(self):
        self.message_clears += 1

    def set_symbol_text(self, text):
        self.symbol_texts.append(text)

    def raise_window(self, focus_entry=False):
        self.raised.append(focus_entry)


class FakeClock:
    """The pad's injectable monotonic clock (see ScannerApp._pad_clock)."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


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
    app._pending_hits = {}
    app._slot_cooldown = {}
    app._dead_hold = {}
    app._ignored_until = {}
    app._excluded_stock_types = set()
    app._logged_no_slot = set()
    app._filter_reasons = {}
    app.pad = FakePad()
    app._pad_ctl = PadController()
    app._pad_clock = FakeClock()
    app._pad_feed = None
    app._pad_risk_usd = app.tunables.risk_usd
    app._pad_order_ids = {}
    app.ib = None  # only the submission-path tests (test_orderpad_submission.py) touch this
    return app


class RecordingIB:
    """Just enough IB surface for feed-ownership tests: records cancels."""

    def __init__(self):
        self.cancelled_mkt = []
        self.cancelled_hist = []
        self.req_mkt = []

    def cancelMktData(self, contract):
        self.cancelled_mkt.append(contract)

    def cancelHistoricalData(self, bars):
        self.cancelled_hist.append(bars)

    def reqMktData(self, contract, snapshot=False):
        self.req_mkt.append(contract)
        return SimpleNamespace(contract=contract)


def fake_open_feed(app, opened=None, reason=None):
    """Replaces the IB-touching _open_feed with one that just marks the
    state as subscribed (or fails with `reason`)."""

    async def _open(state):
        if reason is not None:
            return reason
        state.conid = 4242
        state._ticker = SimpleNamespace(contract=SimpleNamespace(conId=4242))
        if opened is not None:
            opened.append(state.symbol)
        return None

    app._open_feed = _open


def load(app, symbol):
    """Type `symbol` into the pad and let the (faked) subscribe finish."""

    async def _go():
        app._on_pad_symbol(symbol)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_go())


# -- loading ------------------------------------------------------------------


def test_loading_a_live_symbol_borrows_its_feed_and_pins_the_slot():
    app = make_app([make_state("CVDK", last=4.12)])
    app._on_pad_symbol("CVDK")
    assert app._pad_ctl.symbol == "CVDK"
    assert app._pad_ctl.mode(app._pad_clock(), 300) is PadMode.LOADED
    assert app._pad_feed.owned is False
    assert app._pad_feed.state is app.states["CVDK"]  # the SAME state -- no second subscription
    assert app._pinned == {"CVDK"}


def test_loading_normalizes_the_symbol():
    app = make_app([make_state("CVDK")])
    app._on_pad_symbol("  cvdk ")
    assert app._pad_ctl.symbol == "CVDK"


def test_loading_a_second_symbol_releases_the_first_pin():
    app = make_app([make_state("AAA"), make_state("BBB")])
    app._on_pad_symbol("AAA")
    app._on_pad_symbol("BBB")
    assert app._pinned == {"BBB"}


def test_loading_a_held_out_symbol_is_refused():
    app = make_app([make_state("CVDK")])
    app._ignored_until["NOPE"] = NOW + timedelta(hours=1)
    app._excluded_stock_types.add("ETFX")
    app._on_pad_symbol("NOPE")
    app._on_pad_symbol("ETFX")
    assert app._pad_ctl.symbol is None
    assert app._pinned == set()
    assert len(app.pad.blocks) == 2 and all("held out" in b for b in app.pad.blocks)


def test_loading_is_never_armed():
    app = make_app([make_state("CVDK")])
    app._on_pad_symbol("CVDK")
    assert app._pad_ctl.mode(app._pad_clock(), 300) is PadMode.LOADED


def test_tui_load_key_loads_the_cursor_row_and_raises_the_pad():
    app = make_app([make_state("CVDK")])
    app._cursor_symbol = lambda: "CVDK"
    app.action_load_pad()
    assert app._pad_ctl.symbol == "CVDK"
    assert app._pad_ctl.mode(app._pad_clock(), 300) is PadMode.LOADED  # never armed from the TUI
    assert app.pad.raised == [False]


def test_tui_load_key_with_no_row_raises_the_pad_focused_on_the_symbol_box():
    app = make_app([make_state("CVDK")])
    app._cursor_symbol = lambda: None
    app.action_load_pad()
    assert app._pad_ctl.symbol is None
    assert app.pad.raised == [True]


# -- the pad's own feed (outside the pool) -------------------------------------


def test_typed_symbol_not_in_the_pool_gets_its_own_feed_outside_self_states():
    app = make_app([make_state("AAA")])
    app.ib = RecordingIB()
    opened = []
    fake_open_feed(app, opened)
    load(app, "NEWCO")
    assert opened == ["NEWCO"]
    assert app._pad_feed.owned is True
    assert "NEWCO" not in app.states          # no live slot consumed
    assert len(app.states) == 1
    assert app._pinned == set()               # nothing pooled to protect


def test_a_pad_owned_feed_does_not_touch_the_slot_counts():
    app = make_app()
    app._pending_hits = {}
    app.ib = RecordingIB()
    fake_open_feed(app)
    load(app, "NEWCO")
    assert app._waiting_for_slot_count() == 0
    assert app._cooldown_wait_count() == 0
    assert app._held_count() == 0


def test_the_pool_can_be_full_and_the_pad_still_loads():
    """The exemption itself: max_live_symbols worth of pool symbols, and the
    pad's own line still opens without bumping anything."""
    app = make_app([make_state(f"S{i}") for i in range(app_max())])
    app.ib = RecordingIB()
    opened = []
    fake_open_feed(app, opened)
    load(app, "NEWCO")
    assert opened == ["NEWCO"]
    assert len(app.states) == Tunables().max_live_symbols


def app_max():
    return Tunables().max_live_symbols


def test_releasing_an_owned_feed_cancels_it():
    app = make_app()
    app.ib = RecordingIB()
    fake_open_feed(app)
    load(app, "NEWCO")
    app._on_pad_clear()
    assert len(app.ib.cancelled_mkt) == 1
    assert app._pad_feed is None


def test_releasing_a_borrowed_feed_never_cancels_the_scanners_line():
    """ib_async keys Tickers by contract, so a pad-side cancel on a symbol
    the scanner also holds would cancel the scanner's line (see
    ScannerApp._pad_attach)."""
    state = make_state("CVDK")
    state._ticker = SimpleNamespace(contract=SimpleNamespace(conId=1))
    app = make_app([state])
    app.ib = RecordingIB()
    app._on_pad_symbol("CVDK")
    app._on_pad_clear()
    assert app.ib.cancelled_mkt == []
    assert "CVDK" in app.states and state._ticker is not None
    assert app._pinned == set()


def test_changing_symbol_releases_the_previous_owned_feed():
    app = make_app()
    app.ib = RecordingIB()
    fake_open_feed(app)
    load(app, "AAA")
    load(app, "BBB")
    assert len(app.ib.cancelled_mkt) == 1   # AAA's line, and only that
    assert app._pad_feed.symbol == "BBB"


def test_clearing_while_the_feed_is_still_opening_closes_it_on_arrival():
    """Symbol cleared between typing and the contract qualifying: the line
    that then opens belongs to nobody and must be cancelled, not leaked."""
    app = make_app()
    app.ib = RecordingIB()
    fake_open_feed(app)

    async def _go():
        app._on_pad_symbol("SLOW")
        app._on_pad_clear()              # before the open task has run
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_go())
    assert len(app.ib.cancelled_mkt) == 1
    assert app._pad_feed is None


def test_a_feed_that_cannot_be_opened_empties_the_pad_with_the_reason():
    app = make_app()
    app.ib = RecordingIB()
    fake_open_feed(app, reason="IB could not qualify a contract")
    load(app, "BOGUS")
    assert app._pad_ctl.symbol is None
    assert app._pad_feed is None
    assert any("BOGUS" in b and "qualify" in b for b in app.pad.blocks)


def test_ib_101_on_the_pads_own_feed_is_logged_distinctly_and_empties_the_pad(caplog):
    app = make_app()
    app.ib = RecordingIB()
    fake_open_feed(app)
    load(app, "NEWCO")
    contract = SimpleNamespace(conId=4242)
    with caplog.at_level("ERROR", logger="momentum_scanner.app"):
        app._on_ib_feed_error(7, 101, "Max number of tickers has been reached.", contract)
    assert any("IB error 101" in r.getMessage() and "NEWCO" in r.getMessage() for r in caplog.records)
    assert app._pad_ctl.symbol is None
    assert any("101" in b for b in app.pad.blocks)


def test_ib_101_for_some_other_contract_is_not_the_pads_business():
    app = make_app()
    app.ib = RecordingIB()
    fake_open_feed(app)
    load(app, "NEWCO")
    app._on_ib_feed_error(7, 101, "Max number of tickers", SimpleNamespace(conId=9999))
    app._on_ib_feed_error(8, 200, "some other error", SimpleNamespace(conId=4242))
    assert app._pad_ctl.symbol == "NEWCO"


def test_scanner_admitting_the_pads_symbol_adopts_the_feed_instead_of_opening_a_second_line():
    app = make_app()
    app.ib = RecordingIB()
    fake_open_feed(app)
    load(app, "NEWCO")
    pad_state = app._pad_feed.state

    async def _noop(_state):
        return None

    app._load_baseline = app._load_float = app._load_short_interest = _noop
    app.float_map = {}

    async def _go():
        await app._add_symbol(ScanHit(symbol="NEWCO", con_id=0, rank=3, source="SCAN"))
        await asyncio.sleep(0)

    asyncio.run(_go())
    assert app.states["NEWCO"] is pad_state          # the pool took over the pad's state
    assert app.ib.req_mkt == []                       # no second reqMktData
    assert app._pad_feed.owned is False and app._pinned == {"NEWCO"}
    app._on_pad_clear()
    assert app.ib.cancelled_mkt == []                 # and releasing it now leaves the pool's line alone


def test_a_pinned_symbol_forced_out_of_the_pool_moves_to_the_pads_own_feed():
    """Session rollover, reconnect, a non-tradable mark: the pad isn't
    speculative, so it doesn't lose its symbol to pool housekeeping."""
    state = make_state("CVDK")
    state._ticker = SimpleNamespace(contract=SimpleNamespace(conId=1))
    app = make_app([state])
    app.ib = RecordingIB()
    fake_open_feed(app)

    async def _go():
        app._on_pad_symbol("CVDK")
        app._remove_symbol("CVDK")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_go())
    assert "CVDK" not in app.states
    assert app._pad_ctl.symbol == "CVDK"          # still loaded
    assert app._pad_feed.owned is True
    assert app._pinned == set()
    assert len(app.ib.cancelled_mkt) == 1         # the pool's line was cancelled once


# -- the borrowed pin holds against every eviction path ---------------------------


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
    app._on_pad_symbol("CVDK")
    app._evict_unqualified()
    assert "CVDK" in app.states
    assert app._pad_ctl.symbol == "CVDK"


def test_pin_survives_spike_quiet_eviction():
    """The likeliest real case: a symbol pops, you load it, and it goes quiet
    for a minute while you wait for an entry."""
    state = make_state("CVDK")
    state.spike.last_spike_at = NOW - timedelta(seconds=10_000)
    state.spike.last_new_high_at = NOW - timedelta(seconds=10_000)
    app = make_app([state], persistence_streak=999)
    app._on_pad_symbol("CVDK")
    app._evict_unqualified()
    assert "CVDK" in app.states


def test_unpinned_symbol_still_evicts_on_persistence_decay():
    """The pin must not accidentally disable eviction for everything else."""
    app = make_app([make_state("CVDK")], persistence_streak=0)
    app._evict_unqualified()
    assert "CVDK" not in app.states


def test_loading_does_not_inflate_the_waiting_count():
    """A pinned symbol is live, not queued -- it must not show up in any of
    the three slot-status buckets."""
    app = make_app([make_state("CVDK")])
    app._on_pad_symbol("CVDK")
    assert app._waiting_for_slot_count() == 0
    assert app._cooldown_wait_count() == 0
    assert app._held_count() == 0


# -- arming (F2) --------------------------------------------------------------


def test_f2_toggles_loaded_and_armed():
    app = make_app([make_state("CVDK")])
    app._on_pad_symbol("CVDK")
    mode = lambda: app._pad_ctl.mode(app._pad_clock(), app.tunables.order_pad_arm_timeout_sec)
    app._on_pad_toggle_arm()
    assert mode() is PadMode.ARMED
    app._on_pad_toggle_arm()
    assert mode() is PadMode.LOADED


def test_f2_on_an_empty_pad_does_nothing():
    app = make_app([make_state("CVDK")])
    app._on_pad_toggle_arm()
    assert app._pad_ctl.mode(app._pad_clock(), 300) is PadMode.EMPTY


def test_arm_expires_after_the_configured_timeout_without_a_word_on_screen():
    app = make_app([make_state("CVDK")])
    app._on_pad_symbol("CVDK")
    app._on_pad_toggle_arm()
    app.pad.blocks.clear(), app.pad.results.clear()
    app._pad_clock.advance(app.tunables.order_pad_arm_timeout_sec + 1)
    view = app._pad_view()
    assert view.mode is PadMode.LOADED
    assert view.armed_remaining is None
    assert app.pad.blocks == [] and app.pad.results == []   # silent: no warning, no message


def test_arm_timeout_is_a_tunable():
    app = make_app([make_state("CVDK")])
    app.tunables.order_pad_arm_timeout_sec = 30
    app._on_pad_symbol("CVDK")
    app._on_pad_toggle_arm()
    app._pad_clock.advance(31)
    assert app._pad_view().mode is PadMode.LOADED


def test_symbol_change_forces_armed_back_to_loaded():
    app = make_app([make_state("AAA"), make_state("BBB")])
    app._on_pad_symbol("AAA")
    app._on_pad_toggle_arm()
    assert app._pad_view().mode is PadMode.ARMED
    app._on_pad_symbol("BBB")
    assert app._pad_view().mode is PadMode.LOADED
    app._on_pad_symbol("AAA")     # and back again: still not armed
    assert app._pad_view().mode is PadMode.LOADED


def test_reentering_the_loaded_symbol_keeps_the_arm():
    app = make_app([make_state("AAA")])
    app._on_pad_symbol("AAA")
    app._on_pad_toggle_arm()
    app._on_pad_symbol("AAA")
    assert app._pad_view().mode is PadMode.ARMED


def test_escape_clears_everything():
    app = make_app([make_state("CVDK")])
    app._on_pad_symbol("CVDK")
    app._on_pad_toggle_arm()
    app._on_pad_clear()
    view = app._pad_view()
    assert view.mode is PadMode.EMPTY and view.symbol is None and view.sizing is None
    assert app._pinned == set() and app._pad_feed is None
    assert app.pad.symbol_texts[-1] == ""


# -- the live view ----------------------------------------------------------------


def test_view_sizing_follows_the_tick():
    state = make_state("CVDK", last=4.12)
    app = make_app([state])
    app._on_pad_symbol("CVDK")
    first = app._pad_view().sizing
    state.tick.last = 4.20
    second = app._pad_view().sizing
    assert first.price == 4.12 and second.price == 4.20
    assert second.stop_price > first.stop_price


def test_pad_risk_is_local_and_recalculates_live():
    app = make_app([make_state("CVDK", last=4.12)])
    app._on_pad_symbol("CVDK")
    base = app._pad_view().sizing.shares
    app._on_pad_risk(app.tunables.risk_usd * 2)
    assert app._pad_view().sizing.shares > base
    assert app.tunables.risk_usd == Tunables().risk_usd      # the shared tunable is untouched
    assert app._pad_view().risk_usd == Tunables().risk_usd * 2


def test_view_reports_dry_run_from_the_tunable():
    app = make_app([make_state("CVDK")])
    assert app._pad_view().dry_run is False
    app.tunables.order_pad_dry_run = True
    assert app._pad_view().dry_run is True


def test_dry_run_defaults_off():
    assert Tunables().order_pad_dry_run is False


def test_view_notes_a_feed_still_subscribing_and_a_feed_awaiting_its_first_tick():
    app = make_app()
    app.ib = RecordingIB()
    fake_open_feed(app)

    async def _go():
        app._on_pad_symbol("NEWCO")
        note_before = app._pad_view().feed_note
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return note_before, app._pad_view()

    before, after = asyncio.run(_go())
    assert before == "subscribing..."
    assert after.feed_note == "waiting for first tick" and after.sizing is None


def test_a_tick_on_the_pads_symbol_marks_the_window_dirty_and_others_do_not():
    pad_state, other = make_state("CVDK"), make_state("OTHER")
    app = make_app([pad_state, other])
    app._on_pad_symbol("CVDK")
    tick = SimpleNamespace(last=4.5, bid=4.49, ask=4.51, volume=float("nan"))
    before = app.pad.dirty
    app._apply_tick(other, tick)
    assert app.pad.dirty == before
    app._apply_tick(pad_state, tick)
    assert app.pad.dirty == before + 1
    assert pad_state.tick.last == 4.5


# -- firing (F4) --------------------------------------------------------------------


def armed_app(**state_kw):
    app = make_app([make_state("CVDK", last=4.12, **state_kw)])
    app.tunables.order_pad_dry_run = True   # exercise the whole path but the order API
    app._on_pad_symbol("CVDK")
    app._on_pad_toggle_arm()
    return app


def test_fire_dry_runs_and_drops_back_to_loaded():
    app = armed_app()
    app._on_pad_fire()
    assert app.pad.results and app.pad.results[-1].startswith("DRY RUN")
    assert "NO ORDER SENT" in app.pad.results[-1]
    assert not app.pad.blocks
    assert app._pad_view().mode is PadMode.LOADED


def test_a_second_f4_after_a_fire_does_nothing():
    app = armed_app()
    app._on_pad_fire()
    n = len(app.pad.results)
    app._on_pad_fire()
    assert len(app.pad.results) == n and not app.pad.blocks


def test_f4_does_nothing_when_merely_loaded():
    app = make_app([make_state("CVDK")])
    app.tunables.order_pad_dry_run = True
    app._on_pad_symbol("CVDK")
    app._on_pad_fire()
    assert not app.pad.blocks and not app.pad.results


def test_f4_does_nothing_when_empty():
    app = make_app([make_state("CVDK")])
    app._on_pad_fire()
    assert not app.pad.blocks and not app.pad.results


def test_f4_does_nothing_after_the_arm_expires():
    app = armed_app()
    app._pad_clock.advance(app.tunables.order_pad_arm_timeout_sec + 1)
    app._on_pad_fire()
    assert not app.pad.blocks and not app.pad.results


def test_f2_then_f4_fires_again_after_expiry():
    app = armed_app()
    app._pad_clock.advance(app.tunables.order_pad_arm_timeout_sec + 1)
    app._on_pad_fire()
    assert not app.pad.results
    app._on_pad_toggle_arm()
    app._on_pad_fire()
    assert app.pad.results and app.pad.results[-1].startswith("DRY RUN")


def test_fire_is_refused_on_a_stale_quote_and_stays_armed():
    app = armed_app()
    app.states["CVDK"].tick.updated_at = datetime.now(config.TZ) - timedelta(seconds=30)
    app._on_pad_fire()
    assert app.pad.blocks and "stale" in app.pad.blocks[0]
    assert not app.pad.results
    assert app._pad_view().mode is PadMode.ARMED     # a refusal doesn't burn the arm


def test_stale_threshold_is_the_tunable():
    app = armed_app()
    app.states["CVDK"].tick.updated_at = datetime.now(config.TZ) - timedelta(seconds=5)
    app.tunables.order_pad_max_quote_age_sec = 10
    app._on_pad_fire()
    assert not app.pad.blocks and app.pad.results


def test_price_moving_after_arming_does_not_block_the_fire():
    """The regression this whole rework exists for: sit armed while the tape
    moves, fire, and it goes through -- sized from where the price is NOW."""
    app = armed_app()
    armed_sizing = app._pad_view().sizing
    app.states["CVDK"].tick.last = armed_sizing.price + 0.30
    app._on_pad_fire()
    assert not app.pad.blocks
    assert app.pad.results and app.pad.results[-1].startswith("DRY RUN")


def test_fire_sizes_from_the_price_at_the_press_not_the_last_drawn_frame(caplog):
    app = armed_app()
    app._pad_view()                                  # a frame is drawn at 4.12
    app.states["CVDK"].tick.last = 4.40              # then a tick lands before F4
    with caplog.at_level("INFO", logger="momentum_scanner.app"):
        app._on_pad_fire()
    assert any("4.4" in r.getMessage() for r in caplog.records if "DRY RUN" in r.getMessage())


def test_fire_uses_the_pads_own_risk():
    app = armed_app()
    app._on_pad_risk(app.tunables.risk_usd * 3)
    view_shares = app._pad_view().sizing.shares
    app._on_pad_fire()
    assert f"{view_shares}sh" in app.pad.results[-1]


def test_fire_is_refused_for_a_non_tradable_listed_symbol():
    app = armed_app()
    app._ignored_until["CVDK"] = NOW + timedelta(hours=10)
    app._on_pad_fire()
    assert app.pad.blocks and "non-tradable" in app.pad.blocks[0]


def test_fire_is_refused_while_atr_is_warming():
    app = make_app([make_state("CVDK", last=4.12)])
    app.states["CVDK"].atr.true_ranges.clear()
    app.tunables.order_pad_dry_run = True
    app._on_pad_symbol("CVDK")
    app._on_pad_toggle_arm()
    app._on_pad_fire()
    assert app.pad.blocks and "ATR" in app.pad.blocks[0]


def test_fire_is_refused_on_a_feed_that_has_not_ticked_yet():
    state = make_state("CVDK")
    state.tick.last = None
    state.tick.updated_at = None
    app = make_app([state])
    app.tunables.order_pad_dry_run = True
    app._on_pad_symbol("CVDK")
    app._on_pad_toggle_arm()
    app._on_pad_fire()
    assert app.pad.blocks and "no quote" in app.pad.blocks[0]


def test_arm_is_logged_with_the_live_sizing(tmp_path):
    import json
    from momentum_scanner import order_history

    app = armed_app()
    path = order_history.file_for(datetime.now(config.TZ).date())
    events = [json.loads(line) for line in path.read_text().splitlines()]
    arm = next(e for e in events if e["event"] == "ARM")
    assert arm["symbol"] == "CVDK" and arm["dry_run"] is True
    assert arm["sizing"]["shares"] == app._pad_view().sizing.shares


# -- _open_feed / _open_pad_feed against a fake IB -------------------------------


class QualifyingIB(RecordingIB):
    def __init__(self, qualified):
        super().__init__()
        self._qualified = qualified

    async def qualifyContractsAsync(self, contract):
        return self._qualified


def test_open_feed_refuses_an_unknown_symbol_that_qualifies_to_none():
    """Regression, found live 2026-09-18: for a bogus symbol ib_async returns
    [None], not [], and the old `if not qualified` guard let it through to an
    AttributeError inside a fire-and-forget task."""
    app = make_app()
    app.ib = QualifyingIB([None])
    state = SymbolState(symbol="ZZZZQXQ")
    reason = asyncio.run(app._open_feed(state))
    assert reason is not None and "qualify" in reason
    assert app.ib.req_mkt == []          # nothing was subscribed


def test_an_unknown_typed_symbol_empties_the_pad_with_a_reason():
    app = make_app()
    app.ib = QualifyingIB([None])

    async def _go():
        app._on_pad_symbol("ZZZZQXQ")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_go())
    assert app._pad_ctl.symbol is None and app._pad_feed is None
    assert any("ZZZZQXQ" in b and "qualify" in b for b in app.pad.blocks)


def test_an_exception_while_opening_the_pads_feed_is_reported_not_swallowed():
    app = make_app()

    async def _boom(_state):
        raise RuntimeError("socket died")

    app._open_feed = _boom

    async def _go():
        app._on_pad_symbol("NEWCO")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(_go())
    assert app._pad_ctl.symbol is None
    assert any("failed to open" in b for b in app.pad.blocks)
