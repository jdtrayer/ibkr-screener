"""
Orchestrator: wires scanner -> persistence -> RVOL baseline -> live ticks ->
filters -> display together and drives the Textual UI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from ib_async import IB, LimitOrder, Stock, StopLimitOrder, Ticker, Trade
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header

from . import atr, config, country, display, floatref, order_history, orderpad, rvol, short_interest, spikes, trend
from .controls import SymbolActionsPanel, TunablesPanel
from .news import NewsTracker
from .padwindow import OrderPadWindow
from .scorer import SnapshotScorer
from .sentiment import SentimentClassifier
from .filters import (
    PersistenceTracker,
    ScorerAdmission,
    bump_candidate,
    bump_reason,
    display_reason,
    is_dead_on_bump,
    slot_warmed_up,
    spike_held,
    update_halt_state,
)
from .models import SymbolState
from .scanner import ScanHit, ScannerManager
from .session import Session, current_session, next_trading_day
from .tunables import Tunables

log = logging.getLogger(__name__)

SESSION_CHECK_EVERY_N_TICKS = int(30 / config.DISPLAY_REFRESH_SEC) or 1
FLOAT_REFRESH_EVERY_N_TICKS = int(60 / config.DISPLAY_REFRESH_SEC) or 1
SORT_REFRESH_EVERY_N_TICKS = int(config.SORT_REFRESH_SEC / config.DISPLAY_REFRESH_SEC) or 1

# IB codes delivered through errorEvent that are notices, not failures -- the
# order itself is fine. 2161: IB's regulatory price cap on a limit order
# (confirmed live 2026-09-18, PAAI target: the limit was capped to the
# reference price band and the order went on to fill normally).
INFORMATIONAL_IB_CODES = frozenset({2161})


@dataclass
class _PadBracket:
    """Tracks one order-pad fire from parent submission through to a fully
    protected fill.

    The stop/target legs are submitted only once the parent reports a fill,
    sized to shares actually held rather than the requested quantity (see
    orderpad.BracketPlan's docstring) -- so stop_trade/target_trade start
    None and get created, then resized in place (same orderId, larger
    totalQuantity) as more of the parent fills.
    """

    plan: orderpad.BracketPlan
    contract: Stock
    # The fire record: the PadSizing computed at F4, frozen for the life of
    # the position -- partial-fill re-anchoring reads its stop_distance /
    # nominal_r / stop_trigger_lead_pct, never live state (see
    # orderpad.recompute_bracket_exit).
    sizing: orderpad.PadSizing
    stop_trade: Trade | None = None
    target_trade: Trade | None = None
    # The ACTUAL stop/target prices last sent to IB -- recomputed from the
    # real fill price on every parent fill (see _on_pad_parent_fill), so
    # these can differ from `plan`'s planned figures whenever the entry
    # slips. None until the first fill creates the legs.
    realized_stop_limit: float | None = None
    realized_stop_trigger: float | None = None
    realized_target_price: float | None = None
    # time.monotonic() of the first stop/target placeOrder -- diagnostics
    # only (see _leg_diagnostics), never read by order logic.
    legs_first_placed_at: float | None = None


@dataclass
class _PadFeed:
    """The market-data feed behind the order pad's loaded symbol.

    owned=True: the pad opened this subscription itself (a private
    SymbolState that is NOT in ScannerApp.states, so it takes no live slot
    and none of the pool's bump/evict/persistence machinery ever sees it),
    and is responsible for cancelling it. owned=False: the symbol was
    already live in the pool, so the pad borrows that state instead of
    opening a second line -- and must never cancel it (see _pad_release_feed
    for why that matters with ib_async).
    """

    symbol: str
    state: SymbolState
    owned: bool


class ScannerApp(App):
    BINDINGS = [
        # In the TUI this key LOADS the row under the cursor into the pad
        # (Loaded, never Armed -- arming is the pad's own F2). See
        # config.ORDER_PAD_TOGGLE_KEY for why it's a function key.
        (config.ORDER_PAD_TOGGLE_KEY, "load_pad", "Load order pad"),
    ]

    CSS = """
    #main-column {
        width: 1fr;
        height: 1fr;
    }
    #scanner-table {
        width: 1fr;
        height: 1fr;
        border: solid $primary;
    }
    #scorer-table {
        width: 1fr;
        height: 14;
        border: solid $primary;
    }
    #news-feed-table {
        width: 1fr;
        height: 14;
        border: solid $primary;
    }
    #side-panel {
        width: auto;
        height: 1fr;
    }
    #tunables-panel {
        height: 1fr;
    }
    #symbol-actions-panel {
        height: auto;
        max-height: 20;
    }
    """

    def __init__(self):
        super().__init__()
        self.ib = IB()
        self.tunables = Tunables()
        self.scanner_mgr = ScannerManager(self.ib)
        self.persistence = PersistenceTracker(self.tunables)
        self.states: dict[str, SymbolState] = {}
        self.float_map: dict[str, float] = {}
        self.session: Session = Session.CLOSED
        self._pending_hits: dict = {}
        self._tick_count = 0
        self._logged_no_slot: set[str] = set()
        self._filter_reasons: dict[str, str | None] = {}
        self._slot_cooldown: dict[str, datetime] = {}  # symbol -> when it was bumped from a slot
        self._row_order: list[str] = []
        # Set by on_data_table_row_selected (click/Enter a row in either the
        # main or scorer table) -- filters the News Feed panel to just this
        # symbol. Selecting the same symbol again clears it. Independent of
        # self.states -- selecting a symbol that's since been evicted still
        # shows its recorded headlines.
        self._selected_symbol: str | None = None
        # Unlike the main table, the scorer table has no separate row_order --
        # self.scorer.ranked() is already stable between sweeps on its own, so
        # this just tracks whether last_sweep_at advanced since the last
        # render, to know when it's safe to reposition scorer table rows
        # (see display.sync_scorer_table's reorder docstring).
        self._last_scorer_sweep_rendered: datetime | None = None
        self.scorer = SnapshotScorer(self.ib)
        self.sentiment = SentimentClassifier()
        self.news = NewsTracker(self.ib, sentiment=self.sentiment)
        self.scorer_admission = ScorerAdmission(self.tunables)
        self._scorer_pending: set[str] = set()  # admit tasks created but not yet landed in self.states
        self._ignored_until: dict[str, datetime] = {}  # symbol -> when its manual non-tradable hold expires
        self._dead_hold: dict[str, datetime] = {}  # symbol -> when its auto dead-hold expires (see filters.is_dead_on_bump)
        # Instrument type doesn't change day to day, so this never expires --
        # unlike every other hold above. Prevents the infinite re-admit/drop
        # loop confirmed live 2026-09-03: an ETF stayed persistence-qualified
        # (IB kept re-surfacing it on scan refreshes) but nothing ever held it
        # out after the stockType check dropped it, so it got re-qualified,
        # re-checked, and re-dropped every ~1-3s indefinitely -- 7 symbols
        # cycling this way simultaneously showed up as "7 waiting for a slot"
        # even though none of them would ever actually get one.
        self._excluded_stock_types: set[str] = set()
        self._reconnecting = False
        # Symbols the pad is BORROWING from the live pool (see _PadFeed): a
        # borrowed slot is exempt from every eviction path (bump, scorer
        # swap, persistence decay, spike-quiet) while the pad has it loaded
        # -- see _pinned_reason. Only ever the pad's one loaded symbol, and
        # only when that symbol was already live; a pad-owned feed is never
        # in the pool and needs no pin. Sized like a set for consistency
        # with the other hold-outs above.
        self._pinned: set[str] = set()
        self.pad: OrderPadWindow | None = None
        self._pad_pump_task: asyncio.Task | None = None
        # Empty / Loaded / Armed and the arm timer; the clock is injectable
        # (monotonic seconds) so tests never sleep.
        self._pad_ctl = orderpad.PadController()
        self._pad_clock = time.monotonic
        self._pad_feed: _PadFeed | None = None
        # The pad's own risk $, editable on the pad and deliberately NOT
        # written back to tunables.risk_usd (which sizes the scanner tables).
        # Seeded from the tunable once, at startup.
        self._pad_risk_usd: float = self.tunables.risk_usd
        # Every orderId belonging to a live pad-submitted bracket (parent,
        # then its stop/target once created) maps back to the same
        # _PadBracket, so fill/cancel/error events on any leg can find their
        # way back to the plan and the pad. Entries outlive the pad's loaded
        # symbol changing -- a fired position stays protected regardless of
        # what's currently on screen.
        self._pad_order_ids: dict[int, _PadBracket] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="main-column"):
                # cursor_foreground_priority="renderable" -- DataTable's default
                # ("css") lets the cursor row's highlight override every cell's own
                # text color, which washed out the RVOL/Target/Stop color coding
                # (green3/red3/etc, see rvol_style and SCALP_TARGET_STYLE/
                # SCALP_STOP_STYLE) on whichever row currently has the cursor.
                # This keeps each cell's own color and lets only the background
                # show the cursor.
                yield DataTable(id="scanner-table", cursor_foreground_priority="renderable")
                yield DataTable(id="scorer-table", cursor_foreground_priority="renderable")
                yield DataTable(id="news-feed-table", cursor_foreground_priority="renderable")
            with Vertical(id="side-panel"):
                yield TunablesPanel(self.tunables, id="tunables-panel")
                yield SymbolActionsPanel(id="symbol-actions-panel")
        yield Footer()

    async def connect(self) -> None:
        await self.ib.connectAsync(config.IB_HOST, config.IB_PORT, clientId=config.IB_CLIENT_ID)
        log.info("Connected to IB at %s:%s (clientId=%s)", config.IB_HOST, config.IB_PORT, config.IB_CLIENT_ID)

    async def on_mount(self) -> None:
        # Fired, not awaited -- model load is ~5.6s warm (minutes on the
        # very first run, downloading weights) and must never delay the
        # first render or any of the setup below. Sentiment simply becomes
        # available a few seconds later once this finishes.
        asyncio.create_task(self.sentiment.load())
        # Fired, not awaited -- same reasoning as sentiment.load() above: the
        # first fetch is a ~2MB HTTP call and country tags simply become
        # available a few seconds later once this finishes.
        asyncio.create_task(country.refresh_if_stale())
        await self.connect()
        self.ib.disconnectedEvent += self._on_disconnected
        self.ib.errorEvent += self._on_ib_order_error
        self.ib.errorEvent += self._on_ib_feed_error
        # Commission arrives on its own event, after the fill it belongs to
        # (see wrapper.commissionReport) -- global on the IB object, like
        # errorEvent, so the handler filters down to pad-tracked orders.
        self.ib.commissionReportEvent += self._on_pad_commission_report
        self.float_map = floatref.load()
        self._load_non_tradable()
        await self.news.load_providers()
        self._reconfigure_for_session(current_session())
        self.scanner_mgr.on_update(self._on_scan_update)

        scanner_table = self.query_one("#scanner-table", DataTable)
        scanner_table.cursor_type = "row"
        scanner_table.zebra_stripes = True
        scanner_table.add_columns(*display.MAIN_TABLE_COLUMNS)

        scorer_table = self.query_one("#scorer-table", DataTable)
        scorer_table.cursor_type = "row"
        scorer_table.zebra_stripes = True
        scorer_table.add_columns(*display.SCORER_TABLE_COLUMNS)

        news_table = self.query_one("#news-feed-table", DataTable)
        # Deliberately not cursor_type="row" -- unlike the scanner/scorer
        # tables, a news row's key isn't a symbol (a symbol can have several
        # headlines), so it must never post RowSelected into
        # on_data_table_row_selected, which assumes row_key.value IS a
        # symbol. Default "cell" cursor still supports arrow-key/PageUp/Down
        # scrolling through the panel.
        news_table.zebra_stripes = True
        news_table.add_columns(*display.NEWS_TABLE_COLUMNS)

        self._start_order_pad()

        self._render(reorder=True)
        self.set_interval(config.DISPLAY_REFRESH_SEC, self._tick)
        self.set_interval(config.SCORE_REFRESH_SEC, self._scorer_tick)
        self.set_interval(config.NEWS_PULL_INTERVAL_SEC, self._news_tick)

    def _on_disconnected(self) -> None:
        log.warning("Lost connection to IB -- will retry every %ds until it's back", config.RECONNECT_RETRY_SEC)
        if not self._reconnecting:
            asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Scanner subscriptions and live market data both die with the socket
        and ib_async does not resubscribe them on its own, so once the
        connection is back this drops all live state and restarts the scanner
        for the current session -- same as a session change, and for the same
        reason: whatever ticked during the outage is gone, so there's nothing
        worth preserving in self.states."""
        self._reconnecting = True
        try:
            while not self.ib.isConnected():
                try:
                    await self.connect()
                except Exception:
                    log.warning("Reconnect attempt failed, retrying in %ds", config.RECONNECT_RETRY_SEC)
                    await asyncio.sleep(config.RECONNECT_RETRY_SEC)
            log.info("Reconnected to IB -- restarting scanner for session %s", self.session.value)
            # The pad's own feed died with the socket too. A BORROWED feed is
            # re-homed by _remove_symbol during the reconfigure below, so only
            # a feed that was already the pad's own needs re-opening here.
            pad_feed_was_own = self._pad_feed is not None and self._pad_feed.owned
            self._reconfigure_for_session(self.session)
            if pad_feed_was_own:
                self._pad_reopen_feed()
            self._render()
        finally:
            self._reconnecting = False

    def _scorer_tick(self) -> None:
        # sweep() is re-entry-guarded internally, so a slow sweep can't stack.
        asyncio.create_task(self._scorer_sweep_and_admit())

    def _news_tick(self) -> None:
        # pull_sweep() is re-entry-guarded internally, so a slow sweep can't stack.
        # Union of the scorer pool and the main table's live/held symbols --
        # so the main table's Flags column (backlog #12 fast-follow) gets
        # news coverage too, not just the scorer table's.
        pool = self.scorer.pool_symbols() | set(self.states)
        asyncio.create_task(self.news.pull_sweep(pool, self._contract_for_news))

    def _contract_for_news(self, symbol: str) -> Stock | None:
        """contract_for callback for news.pull_sweep -- prefers the scorer's
        already-qualified contract (cheap, no re-qualify), falling back to a
        bare Stock built from the main table's stored conid for symbols the
        scorer hasn't (yet, or ever) pooled. Only .conId is used downstream
        (reqHistoricalNewsAsync), so this doesn't need to be qualified."""
        contract = self.scorer.contract_for(symbol)
        if contract is not None:
            return contract
        state = self.states.get(symbol)
        if state is not None and state.conid is not None:
            return Stock(conId=state.conid)
        return None

    async def _scorer_sweep_and_admit(self) -> None:
        await self.scorer.sweep()
        self._scorer_admit()

    def _tick(self) -> None:
        self._tick_count += 1

        if self._tick_count % SESSION_CHECK_EVERY_N_TICKS == 0:
            new_session = current_session()
            if new_session != self.session:
                self._reconfigure_for_session(new_session)

        if self._tick_count % FLOAT_REFRESH_EVERY_N_TICKS == 0:
            self.float_map = floatref.load()
            for s in self.states.values():
                self._apply_float(s)
            # Same cadence as the float reload above -- refresh_if_stale() is
            # a cheap no-op unless COUNTRY_CACHE_MAX_AGE_DAYS has elapsed, and
            # is re-entry guarded so this can't stack fetches.
            asyncio.create_task(country.refresh_if_stale())

        now = datetime.now(config.TZ)
        for s in self.states.values():
            s.record_volume_sample(now)

        self._process_pending_hits()
        self._evict_unqualified()
        self._log_filter_transitions()

        just_resorted = False
        if self._tick_count % SORT_REFRESH_EVERY_N_TICKS == 0 or not self._row_order:
            self._resort()
            just_resorted = True

        self._render(reorder=just_resorted)

    def _resort(self) -> None:
        """Recompute row ORDER by display.priority_key (trend, then active
        spike count, then RVOL as tiebreaker). Called on a slower cadence
        than _render() so rows hold still between resorts -- see
        display.sync_table's row_order docstring."""
        now = datetime.now(config.TZ)
        self._row_order = sorted(
            self.states,
            key=lambda sym: display.priority_key(self.states[sym], self.tunables, now),
            reverse=True,
        )

    def _render(self, reorder: bool = False) -> None:
        news_sentiment = self.news.sentiment_map()
        display.sync_table(
            self.query_one("#scanner-table", DataTable),
            list(self.states.values()),
            self.session,
            self.ib.isConnected(),
            self.tunables,
            self._row_order,
            waiting_count=self._waiting_for_slot_count(),
            cooldown_count=self._cooldown_wait_count(),
            held_count=self._held_count(),
            news_sentiment=news_sentiment,
            reorder=reorder,
        )
        scorer_reorder = self.scorer.last_sweep_at != self._last_scorer_sweep_rendered
        self._last_scorer_sweep_rendered = self.scorer.last_sweep_at
        display.sync_scorer_table(
            self.query_one("#scorer-table", DataTable),
            self.scorer.ranked(), self.scorer.pool_size, self.scorer.last_sweep_at,
            news_sentiment,
            reorder=scorer_reorder,
        )
        display.sync_news_table(
            self.query_one("#news-feed-table", DataTable),
            self.news.feed(limit=config.NEWS_FEED_DISPLAY_ROWS, symbol=self._selected_symbol),
            symbol_filter=self._selected_symbol,
        )
        self.query_one(SymbolActionsPanel).refresh_status(self._ignored_until, datetime.now(config.TZ))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Click or Enter a row in the main or scorer table -- filters the
        News Feed panel to that symbol (selecting the same symbol again
        clears the filter). Both tables post this same message type, so one
        handler on the App covers either (it bubbles up regardless of which
        DataTable posted it)."""
        symbol = event.row_key.value
        self._selected_symbol = None if symbol == self._selected_symbol else symbol
        self._render()

    def _waiting_for_slot_count(self) -> int:
        """Symbols that have cleared persistence and are genuinely blocked by a
        full pool with nothing bump-eligible -- raising max_live_symbols (or a
        weaker occupant showing up) is what unblocks these. Excludes symbols
        sitting out a re-entry cooldown (see _cooldown_wait_count) and symbols
        held out by a dead-hold, the manual non-tradable list, or an excluded
        instrument type (see _held_count) -- none of those is a capacity
        problem, so lumping them in here made an empty pool look full
        (reproduced live twice: 2026-09-03 with dead-hold/non-tradable, and
        again the same day with a permanent ETF exclusion that had no
        hold-out at all -- see _excluded_stock_types). Recomputed fresh each
        render rather than tracked incrementally, so it self-corrects if a
        candidate drops out of the top-N while queued instead of ever
        getting a slot."""
        return sum(
            1 for sym in self._pending_hits
            if sym not in self.states
            and self.persistence.state_for(sym).streak >= self.tunables.persistence_required
            and sym not in self._slot_cooldown
            and sym not in self._dead_hold
            and sym not in self._ignored_until
            and sym not in self._excluded_stock_types
        )

    def _cooldown_wait_count(self) -> int:
        """Symbols that have cleared persistence but are barred from re-taking
        a slot for slot_reentry_cooldown_sec after being bumped -- unlike
        _waiting_for_slot_count, raising max_live_symbols does NOT admit these;
        only waiting out the cooldown does."""
        return sum(
            1 for sym in self._pending_hits
            if sym not in self.states
            and self.persistence.state_for(sym).streak >= self.tunables.persistence_required
            and sym in self._slot_cooldown
        )

    def _held_count(self) -> int:
        """Symbols that have cleared persistence but are held out by a
        dead-hold (filters.is_dead_on_bump), the manual non-tradable list, or
        a permanently excluded instrument type -- like cooldown, unrelated to
        capacity; raising max_live_symbols does NOT admit these."""
        return sum(
            1 for sym in self._pending_hits
            if sym not in self.states
            and self.persistence.state_for(sym).streak >= self.tunables.persistence_required
            and sym not in self._slot_cooldown
            and (sym in self._dead_hold or sym in self._ignored_until or sym in self._excluded_stock_types)
        )

    # -- session lifecycle -------------------------------------------------

    def _reconfigure_for_session(self, session: Session) -> None:
        log.info("Session changed: %s -> %s", self.session.value, session.value)
        self.session = session
        for symbol in list(self.states.keys()):
            self._remove_symbol(symbol)
        self._slot_cooldown.clear()
        self._dead_hold.clear()  # "dead" was judged against the old session's $ floor -- doesn't carry over
        self.persistence = PersistenceTracker(self.tunables)
        self.scanner_mgr.start(session)

    # -- scanner callback (fires on ScanDataList.updateEvent) --------------

    def _on_scan_update(self, hits: dict, pool_symbols: set) -> None:
        self._pending_hits = hits
        # The scorer takes ALL rows of ALL lists as pool membership --
        # including the pool-only discovery lists (scanner.py's
        # pool_only_profiles_for_session) that never feed the persistence
        # gate, which is exactly the filter that buried LABT (rank 41-45)
        # while it was the top afterhours mover.
        self.scorer.update_pool(pool_symbols)
        qualified = self.persistence.update(hits)
        for symbol in qualified:
            self._try_admit(symbol, hits.get(symbol))

    def _process_pending_hits(self) -> None:
        # Re-run in case room freed up since the last scan callback.
        if not self._pending_hits:
            return
        for symbol, hit in self._pending_hits.items():
            if self.persistence.state_for(symbol).streak >= self.tunables.persistence_required:
                self._try_admit(symbol, hit)

    def _try_admit(self, symbol: str, hit) -> None:
        """Single admission path for both the scan callback and the tick-cadence
        retry. When the pool is full, bumps the weakest occupant currently
        failing the $ floor or the spread ceiling (see filters.bump_candidate)
        rather than turning the newcomer away -- purely demand-driven, so a
        squatter is only ever touched when something better needs its slot."""
        if symbol in self.states:
            if hit is not None:
                # A hit from the admission-eligible scans means this symbol
                # genuinely earned its spot -- graduate it out of a reserved
                # scorer slot (if it came in that way) so that slot frees up
                # for the next scorer candidate, without re-subscribing it.
                self.states[symbol].scan_rank = hit.rank
                self.states[symbol].scan_source = hit.source
            self._logged_no_slot.discard(symbol)
            return
        if symbol in self._ignored_until or symbol in self._dead_hold or symbol in self._excluded_stock_types:
            if symbol not in self._logged_no_slot:
                if symbol in self._ignored_until:
                    reason = "non-tradable list"
                elif symbol in self._dead_hold:
                    reason = "dead-hold"
                else:
                    reason = "excluded instrument type"
                log.info(
                    "%s qualified but held out by %s -- NOT capacity-blocked, "
                    "raising max_live_symbols won't admit it",
                    symbol, reason,
                )
                self._logged_no_slot.add(symbol)
            return
        if hit is None:
            return
        if symbol in self._slot_cooldown:
            if symbol not in self._logged_no_slot:
                remaining = self.tunables.slot_reentry_cooldown_sec - (
                    datetime.now(config.TZ) - self._slot_cooldown[symbol]
                ).total_seconds()
                log.info(
                    "%s qualified but held out by re-entry cooldown (%.0fs remaining) -- "
                    "NOT capacity-blocked, raising max_live_symbols won't admit it",
                    symbol, max(remaining, 0.0),
                )
                self._logged_no_slot.add(symbol)
            return
        # scorer_reserved_slots are set aside for _scorer_admit -- this path
        # (the scan-rank persistence gate) never bumps into or out of them.
        non_scorer_states = {s: st for s, st in self.states.items() if st.scan_source != "SCORER"}
        if len(non_scorer_states) >= self.tunables.max_live_symbols - self.tunables.scorer_reserved_slots:
            now = datetime.now(config.TZ)
            bump = bump_candidate(non_scorer_states, self.session, now, pinned=self._pinned)
            if bump is None:
                self._log_no_slot(symbol)
                return
            reason = bump_reason(bump, self.session)
            if is_dead_on_bump(bump, self.session, config.DEAD_DV_FRACTION):
                self._dead_hold[bump.symbol] = now + timedelta(seconds=self.tunables.dead_hold_sec)
                log.info(
                    "Bumped %s (%s, genuinely dead) to admit %s; held out %.0fm instead of the "
                    "normal re-entry cooldown -- re-qualifying via scan rank alone won't readmit it",
                    bump.symbol, reason, symbol, self.tunables.dead_hold_sec / 60,
                )
            else:
                log.info(
                    "Bumped %s (%s) to admit %s; re-entry barred for %.0fs",
                    bump.symbol, reason, symbol, self.tunables.slot_reentry_cooldown_sec,
                )
            self._remove_symbol(bump.symbol)
            self._slot_cooldown[bump.symbol] = now
        asyncio.create_task(self._add_symbol(hit))

    def _scorer_admit(self) -> None:
        """Fills up to tunables.scorer_reserved_slots live slots from the
        Tier-1 scorer's own ranking (scorer.ranked(), which includes the
        pool-only lists) instead of the scan-rank persistence gate -- see
        config.py's SCORER_RESERVED_SLOTS for the motivating case. Pressure-
        only: an existing reserved occupant is bumped only when a confirmed
        new candidate needs its slot and it's the weakest one, never on a
        timer, mirroring bump_candidate's philosophy for the main gate."""
        ranked = self.scorer.ranked()
        non_scorer_live = {s for s, st in self.states.items() if st.scan_source != "SCORER"}
        ready = self.scorer_admission.update(ranked, non_scorer_live)
        if not ready:
            return

        ranked_by_symbol = {r.symbol: r for r in ranked}
        now = datetime.now(config.TZ)
        swapped = False
        for row in ready:
            symbol = row.symbol
            if (
                symbol in self.states
                or symbol in self._slot_cooldown
                or symbol in self._scorer_pending
                or symbol in self._ignored_until
                or symbol in self._dead_hold
            ):
                continue

            scorer_syms = [s for s, st in self.states.items() if st.scan_source == "SCORER"]
            # _scorer_pending covers admits from earlier in THIS loop whose
            # _add_symbol task hasn't run yet -- without it, two candidates
            # both see the same not-yet-taken slot as free in the same pass.
            occupied = len(scorer_syms) + len(self._scorer_pending - set(scorer_syms))
            if occupied < self.tunables.scorer_reserved_slots:
                self._admit_scorer_candidate(row)
                continue

            if swapped:
                continue  # at most one reserved-slot swap per sweep, to limit churn
            bumpable = [
                s for s in scorer_syms
                if s not in self._pinned
                and slot_warmed_up(self.states[s], now) and not spike_held(self.states[s], now)
            ]
            if not bumpable:
                continue
            weakest = min(bumpable, key=lambda s: ranked_by_symbol[s].score if s in ranked_by_symbol else float("-inf"))
            weakest_score = ranked_by_symbol[weakest].score if weakest in ranked_by_symbol else float("-inf")
            if row.score <= weakest_score:
                continue
            log.info(
                "Bumped %s (scorer slot, score %.2f) to admit %s (score %.2f); re-entry barred for %.0fs",
                weakest, weakest_score, symbol, row.score, self.tunables.slot_reentry_cooldown_sec,
            )
            self._remove_symbol(weakest)
            self._slot_cooldown[weakest] = now
            self._admit_scorer_candidate(row)
            swapped = True

    def _admit_scorer_candidate(self, row) -> None:
        log.info(
            "%s qualified via Tier-1 scorer (score=%.2f, move=%.2f%%/min, fast_lane=%s) for a reserved slot",
            row.symbol, row.score, row.move_pct_per_min, row.fast_lane,
        )
        hit = ScanHit(symbol=row.symbol, con_id=0, rank=None, source="SCORER")
        self._scorer_pending.add(row.symbol)
        asyncio.create_task(self._add_symbol_scorer(hit))

    async def _add_symbol_scorer(self, hit) -> None:
        try:
            await self._add_symbol(hit)
        finally:
            self._scorer_pending.discard(hit.symbol)

    def _log_no_slot(self, symbol: str) -> None:
        if symbol in self._logged_no_slot:
            return  # already logged for this symbol; avoid spamming every tick
        pinned_note = ""
        if self._pinned:
            # Without this the message actively misleads: it would claim every
            # occupant clears the $ floor and spread ceiling when one of them
            # may be failing both and simply be un-bumpable because it's armed.
            pinned_note = f", {len(self._pinned)} pinned by the order pad"
        log.info(
            "%s qualified but no live-symbol slot free (%d/%d in use%s, none bump-eligible: "
            "all clear the $ floor and spread ceiling, are warming up, or spiked recently)",
            symbol, len(self.states), self.tunables.max_live_symbols, pinned_note,
        )
        self._logged_no_slot.add(symbol)

    def _log_filter_transitions(self) -> None:
        """Logs, once per state change, why a live-subscribed symbol is or isn't
        clearing display.sync_table's row filter -- otherwise a symbol can spike
        heavily under the hood and stay invisible with no trace in the log."""
        for symbol, state in self.states.items():
            if state.tick.last is None:
                continue  # no live tick yet, nothing meaningful to report
            reason = display_reason(state, self.session)
            if reason == self._filter_reasons.get(symbol):
                continue
            if reason is None:
                rvol_txt = f"{state.rvol:.2f}x" if state.rvol is not None else "unavailable"
                log.info("%s now passing display filters (rvol=%s)", symbol, rvol_txt)
            else:
                log.info("%s hidden from display: %s", symbol, reason)
            self._filter_reasons[symbol] = reason
        for symbol in list(self._filter_reasons):
            if symbol not in self.states:
                del self._filter_reasons[symbol]

    def _evict_unqualified(self) -> None:
        now = datetime.now(config.TZ)
        for sym, evicted_at in list(self._slot_cooldown.items()):
            if (now - evicted_at).total_seconds() >= self.tunables.slot_reentry_cooldown_sec:
                del self._slot_cooldown[sym]
                self._logged_no_slot.discard(sym)  # let a fresh block reason log again
        expired_non_tradable = [sym for sym, until in self._ignored_until.items() if now >= until]
        for sym in expired_non_tradable:
            del self._ignored_until[sym]
            self._logged_no_slot.discard(sym)  # let a fresh block reason log again
            log.info("%s non-tradable hold expired (next trading day reached)", sym)
        if expired_non_tradable:
            self._save_non_tradable()
        for sym, until in list(self._dead_hold.items()):
            if now >= until:
                del self._dead_hold[sym]
                self._logged_no_slot.discard(sym)
                log.info("%s dead-hold expired (%.0fm elapsed)", sym, self.tunables.dead_hold_sec / 60)
        for symbol in list(self.states.keys()):
            state = self.states[symbol]
            # A pinned symbol is exempt from BOTH evictions below, not just
            # from bumping: spike-quiet in particular would fire on exactly
            # the symbol you're most likely to be sitting armed on -- one
            # that popped, got armed, and then went quiet for a minute while
            # you waited for an entry. Losing its subscription mid-arm would
            # leave the pad holding a symbol with no live quote, which
            # validate_fire can only turn into a refusal.
            pin = self._pinned_reason(symbol)
            if pin is not None:
                continue
            # Scorer-admitted symbols (state.scan_source == "SCORER") never
            # entered via the scan-rank persistence gate, so they have no
            # streak to decay -- _scorer_admit's pressure-only swap is their
            # only eviction path (besides spike-quiet below).
            if state.scan_source != "SCORER" and self.persistence.state_for(symbol).streak <= 0:
                log.info(
                    "%s evicted: persistence streak decayed to 0 (out of scanner top-N for "
                    "over %.0fs) -- will lose its RVOL/$Vol history if re-admitted later",
                    symbol, self.tunables.persistence_reset_sec,
                )
                self._remove_symbol(symbol)
                continue
            if spikes.ready_to_evict(state.spike, self.tunables, now):
                log.info(
                    "%s evicted: spike-quiet for %.0fs (no new spike or session high) -- "
                    "will lose its RVOL/$Vol history if re-admitted later",
                    symbol, self.tunables.spike_quiet_sec,
                )
                self._remove_symbol(symbol)

    # -- manual non-tradable list (SymbolActionsPanel) ----------------------

    def on_symbol_actions_panel_action(self, message: SymbolActionsPanel.Action) -> None:
        if message.action == "clear":
            self._unignore_symbol(message.symbol)
        else:
            self._ignore_symbol(message.symbol)

    def _ignore_symbol(self, symbol: str) -> None:
        now = datetime.now(config.TZ)
        next_day = next_trading_day(now.date())
        until = datetime(next_day.year, next_day.month, next_day.day, tzinfo=config.TZ)
        self._ignored_until[symbol] = until
        log.info("%s marked non-tradable, held out of admission until %s (next trading day)", symbol, next_day.isoformat())
        self._save_non_tradable()
        if symbol in self.states:
            self._remove_symbol(symbol)

    def _unignore_symbol(self, symbol: str) -> None:
        if self._ignored_until.pop(symbol, None) is not None:
            log.info("%s removed from non-tradable list", symbol)
            self._save_non_tradable()

    def _load_non_tradable(self) -> None:
        """Restore the manual non-tradable list across a restart. Entries
        whose next-trading-day boundary has already passed while the app was
        down are dropped rather than re-added -- same as a normal expiry."""
        try:
            raw = json.loads(Path(config.NON_TRADABLE_STATE_FILE).read_text())
        except FileNotFoundError:
            return
        except Exception:
            log.exception("Non-tradable list load failed (non-fatal); starting empty")
            return
        now = datetime.now(config.TZ)
        loaded = {}
        for sym, until_str in raw.items():
            until = datetime.fromisoformat(until_str)
            if until > now:
                loaded[sym] = until
        self._ignored_until = loaded
        if self._ignored_until:
            log.info("Restored %d non-tradable symbol(s) from disk: %s", len(loaded), ", ".join(sorted(loaded)))

    def _save_non_tradable(self) -> None:
        try:
            path = Path(config.NON_TRADABLE_STATE_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({sym: until.isoformat() for sym, until in self._ignored_until.items()}))
        except Exception:
            log.exception("Non-tradable list save failed (non-fatal)")

    # -- order pad (load / arm / fire) -------------------------------------

    def _start_order_pad(self) -> None:
        """Bring up the pad window and start pumping it from this app's own
        asyncio loop.

        Non-fatal by design: the scanner is useful without the pad, so a
        machine with no DISPLAY (or no Tk) logs and carries on rather than
        taking the whole TUI down over a secondary window.
        """
        try:
            self.pad = OrderPadWindow(
                on_symbol=self._on_pad_symbol,
                on_toggle_arm=self._on_pad_toggle_arm,
                on_fire=self._on_pad_fire,
                on_clear=self._on_pad_clear,
                on_risk=self._on_pad_risk,
                view_provider=self._pad_view,
                initial_risk_usd=self._pad_risk_usd,
            )
        except Exception:
            log.exception("Order pad window failed to start (non-fatal); scanner continues without it")
            self.pad = None
            return
        self._pad_pump_task = asyncio.create_task(self.pad.pump())
        log.info(
            "Order pad ready -- type a symbol (or %s on a scanner row) to load, %s toggles "
            "armed, %s fires, Esc clears; dry run %s",
            config.ORDER_PAD_TOGGLE_KEY, config.ORDER_PAD_TOGGLE_KEY.upper(),
            config.ORDER_PAD_FIRE_KEY, "ON" if self.tunables.order_pad_dry_run else "OFF",
        )

    def _pad_state(self) -> SymbolState | None:
        return self._pad_feed.state if self._pad_feed is not None else None

    def _pad_shows(self, symbol: str) -> bool:
        """Whether the pad currently has `symbol` loaded -- gates every
        message that reports on a fired bracket (fills, exits, IB errors),
        which outlives whatever the pad has moved on to."""
        return self.pad is not None and self._pad_ctl.symbol == symbol

    def _pad_view(self) -> orderpad.PadView:
        """One frame's worth of pad state, assembled fresh on every redraw:
        sizing is recomputed from the current tick right here (4us -- see
        orderpad.live_sizing), so what is drawn is always what a fire at
        this instant would send. The window calls this at most ~10x/s, on a
        dirty flag set by every tick (see _apply_tick) plus a slower timer
        for the countdown and quote age."""
        now_mono = self._pad_clock()
        timeout = self.tunables.order_pad_arm_timeout_sec
        if self._pad_ctl.expire_if_due(now_mono, timeout):
            # Silent on the pad by design; the log is the only trace.
            log.info("Order pad arm on %s expired -- back to Loaded", self._pad_ctl.symbol)
        mode = self._pad_ctl.mode(now_mono, timeout)
        state = self._pad_state()
        sizing = None
        quote_age = None
        note = None
        if state is not None:
            now = datetime.now(config.TZ)
            sizing = orderpad.live_sizing(state, self.tunables, now, self._pad_risk_usd)
            quote_age = orderpad.quote_age_sec(state, now)
            if getattr(state, "_ticker", None) is None:
                note = "subscribing..."
            elif sizing is None:
                note = "waiting for first tick"
        return orderpad.PadView(
            mode=mode,
            symbol=self._pad_ctl.symbol,
            sizing=sizing,
            quote_age=quote_age,
            armed_remaining=self._pad_ctl.armed_remaining(now_mono, timeout),
            dry_run=self.tunables.order_pad_dry_run,
            risk_usd=self._pad_risk_usd,
            max_quote_age=self.tunables.order_pad_max_quote_age_sec,
            feed_note=note,
        )

    def _cursor_symbol(self) -> str | None:
        """The symbol under the row cursor of whichever of the two symbol
        tables currently has focus, falling back to the main scanner table.
        The news table is excluded on purpose -- its row keys aren't symbols
        (see on_mount's comment on why it has no row cursor)."""
        focused = self.focused
        table = None
        if isinstance(focused, DataTable) and focused.id in ("scanner-table", "scorer-table"):
            table = focused
        else:
            table = self.query_one("#scanner-table", DataTable)
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None  # empty table, or the cursor is on a row that just went away

    def action_load_pad(self) -> None:
        """TUI F2: load the row under the cursor into the pad -- Loaded,
        never Armed; arming is deliberately the pad's own key so nothing
        typed in the terminal can put the pad in a live state."""
        if self.pad is None:
            self.notify("Order pad is not available", severity="warning")
            return
        symbol = self._cursor_symbol()
        if symbol is None:
            # Nothing under the cursor: raise the pad with its symbol box
            # focused so the name can just be typed.
            self.pad.raise_window(focus_entry=True)
            return
        self._on_pad_symbol(symbol)
        self.pad.raise_window()

    # -- pad symbol / feed ownership ----------------------------------------

    def _on_pad_symbol(self, symbol: str) -> None:
        """A symbol was typed into the pad (or sent from a scanner row).
        Always lands in Loaded: a symbol CHANGE drops any arm (see
        PadController.load), and the feed is (re)acquired immediately so
        ATR is already there by the time the name is armed."""
        symbol = symbol.strip().upper()
        if not symbol or self.pad is None:
            return
        if symbol == self._pad_ctl.symbol and self._pad_feed is not None:
            return  # Enter pressed again on the symbol that's already loaded
        if symbol in self._ignored_until or symbol in self._excluded_stock_types:
            self._pad_release_feed()
            self._pad_ctl.clear()
            self.pad.set_symbol_text("")
            self.pad.show_block(f"{symbol} is held out (non-tradable list / excluded type)")
            return
        previous = self._pad_ctl.symbol
        self._pad_release_feed()
        self._pad_ctl.load(symbol)
        self.pad.set_symbol_text(symbol)
        self.pad.clear_message()
        self._pad_attach(symbol)
        log.info(
            "Order pad loaded %s%s (%s feed)", symbol,
            f" (was {previous})" if previous else "",
            "borrowed" if self._pad_feed is not None and not self._pad_feed.owned else "own",
        )
        self.pad.mark_dirty()

    def _pad_attach(self, symbol: str) -> None:
        """Acquire a feed for `symbol`. If the scanner already has it live,
        borrow that state rather than subscribing again: a second
        reqMktData on the same contract opens a second IB line but shares
        ONE ib_async Ticker (Wrapper.startTicker keys on hash(contract)), and
        cancelMktData(contract) then cancels only the latest line -- the
        other can never be cancelled by contract and keeps updating the
        shared Ticker for the rest of the session (reproduced live
        2026-09-18: reqIds 5 and 7 on one Ticker, a second cancel returns
        "No reqId found"). Otherwise open a private feed outside the pool."""
        state = self.states.get(symbol)
        if state is not None:
            self._pad_feed = _PadFeed(symbol, state, owned=False)
            self._pinned.clear()
            self._pinned.add(symbol)
            return
        feed = _PadFeed(symbol, SymbolState(symbol=symbol, scan_source="PAD"), owned=True)
        self._pad_feed = feed
        asyncio.create_task(self._open_pad_feed(feed))

    async def _open_pad_feed(self, feed: _PadFeed) -> None:
        try:
            reason = await self._open_feed(feed.state)
        except Exception:
            # This runs as a fire-and-forget task: an escape here would leave
            # the pad Loaded on a feed that will never deliver, with nothing
            # on screen to say so.
            log.exception("Order pad feed for %s failed unexpectedly", feed.symbol)
            reason = "feed failed to open (see scanner.log)"
        if self._pad_feed is not feed:
            # Cleared or changed while the contract was qualifying: whatever
            # was just opened belongs to nobody now.
            self._close_feed(feed.state)
            return
        if reason is not None:
            self._pad_fail(feed, reason)

    def _pad_fail(self, feed: _PadFeed, reason: str) -> None:
        """The pad's feed could not be established: back to Empty, with the
        reason on screen (the failure itself was already logged)."""
        symbol = feed.symbol
        self._pad_release_feed()
        self._pad_ctl.clear()
        if self.pad is not None:
            self.pad.set_symbol_text("")
            self.pad.show_block(f"{symbol}: {reason}")
            self.pad.mark_dirty()

    def _pad_release_feed(self) -> None:
        """Drop the pad's feed. Cancels only a feed the pad OPENED; a
        borrowed one is merely unpinned and keeps running for the scanner."""
        feed = self._pad_feed
        self._pad_feed = None
        self._pinned.clear()
        if feed is not None and feed.owned:
            self._close_feed(feed.state)

    def _adopt_pad_feed(self, feed: _PadFeed, hit) -> None:
        """The scanner admitted a symbol the pad already has its own feed
        for. Opening a second line would double the subscription and double-
        wire on_tick onto the shared Ticker, so the pool takes over the
        pad's state instead: the pad's feed becomes a borrowed (pinned) one,
        and the pool-only loaders (RVOL baseline, float, short interest),
        which a pad feed never runs, start now."""
        state = feed.state
        state.scan_rank = hit.rank
        state.scan_source = hit.source
        self._apply_float(state)
        self.states[hit.symbol] = state
        feed.owned = False
        self._pinned.clear()
        self._pinned.add(hit.symbol)
        log.info(
            "%s admitted to live tracking (%d/%d slots, scan_rank=%s, source=%s) by adopting the "
            "order pad's existing feed (no second market data line) -- RVOL/$Vol start from zero",
            hit.symbol, len(self.states), self.tunables.max_live_symbols, hit.rank, hit.source,
        )
        asyncio.create_task(self._load_baseline(state))
        asyncio.create_task(self._load_float(state))
        asyncio.create_task(self._load_short_interest(state))

    def _on_ib_feed_error(self, reqId: int, errorCode: int, errorString: str, contract) -> None:
        """IB error 101 (max number of tickers reached) on the PAD's own
        subscription. Logged distinctly from ordinary errors: it means the
        account is out of market data lines, which the pool's admission
        (capped by max_live_symbols, not by IB's line limit) can't see coming
        and which the pad's slot exemption makes possible."""
        if errorCode != 101:
            return
        feed = self._pad_feed
        if (
            feed is None or not feed.owned or contract is None
            or getattr(contract, "conId", None) != feed.state.conid
        ):
            return
        log.error(
            "Order pad %s: IB error 101 (max tickers reached) -- the pad's own market data line "
            "was REFUSED (pool holds %d/%d symbols; the pad's feed is outside the pool): %s",
            feed.symbol, len(self.states), self.tunables.max_live_symbols, errorString,
        )
        self._pad_fail(feed, "IB 101: max market data lines reached")

    def _pad_reopen_feed(self) -> None:
        """After an IB reconnect every market-data subscription is gone,
        including the pad's own. Re-acquire the loaded symbol's feed without
        cancelling the dead one (its reqIds no longer exist)."""
        symbol = self._pad_ctl.symbol
        if symbol is None or self.pad is None:
            return
        self._pad_feed = None
        self._pinned.clear()
        self._pad_attach(symbol)
        log.info("Order pad re-subscribed %s after reconnect", symbol)

    # -- pad keys -------------------------------------------------------------

    def _on_pad_clear(self) -> None:
        """Escape: forget the symbol entirely and release its subscription."""
        symbol = self._pad_ctl.symbol
        self._pad_release_feed()
        self._pad_ctl.clear()
        if self.pad is not None:
            self.pad.set_symbol_text("")
            self.pad.clear_message()
            self.pad.mark_dirty()
        if symbol:
            log.info("Order pad cleared %s -- feed released", symbol)

    def _on_pad_risk(self, risk_usd: float) -> None:
        self._pad_risk_usd = risk_usd
        if self.pad is not None:
            self.pad.mark_dirty()
        log.info("Order pad risk set to $%.2f (pad-local; tunables.risk_usd unchanged)", risk_usd)

    def _on_pad_toggle_arm(self) -> None:
        """F2: a pure Loaded <-> Armed toggle (the timer restarts only via
        F2 twice)."""
        if self.pad is None:
            return
        now_mono = self._pad_clock()
        timeout = self.tunables.order_pad_arm_timeout_sec
        if self._pad_ctl.mode(now_mono, timeout) is orderpad.PadMode.EMPTY:
            return
        mode = self._pad_ctl.toggle_arm(now_mono, timeout)
        symbol = self._pad_ctl.symbol
        self.pad.clear_message()
        self.pad.mark_dirty()
        if mode is not orderpad.PadMode.ARMED:
            log.info("Order pad disarmed %s (F2 toggle)", symbol)
            return
        state = self._pad_state()
        now = datetime.now(config.TZ)
        sizing = orderpad.live_sizing(state, self.tunables, now, self._pad_risk_usd) if state else None
        if sizing is not None:
            log.info(
                "Order pad ARMED %s for %.0fs: %dsh @ %.2f, stop %.2f (trigger %.2f), target %.2f, "
                "risk $%.2f, S/spr %s, effR %s%s",
                symbol, timeout, sizing.shares, sizing.price, sizing.stop_price or 0.0,
                sizing.stop_trigger_price or 0.0, sizing.target_price or 0.0, sizing.risk_usd,
                f"{sizing.stop_in_spreads:.1f}" if sizing.stop_in_spreads is not None else "unknown",
                f"{sizing.effective_r:.2f}" if sizing.effective_r is not None else "unknown",
                " [DRY RUN]" if self.tunables.order_pad_dry_run else "",
            )
        else:
            log.info("Order pad ARMED %s for %.0fs with no price yet", symbol, timeout)
        order_history.log_event(
            now, "ARM",
            symbol=symbol,
            session=self.session.value,
            dry_run=self.tunables.order_pad_dry_run,
            scan_source=state.scan_source if state else None,
            scan_rank=state.scan_rank if state else None,
            conid=state.conid if state else None,
            atr_value=atr.atr_value(state.atr) if state else None,
            pad_risk_usd=self._pad_risk_usd,
            tunables=asdict(self.tunables),
            sizing=orderpad.sizing_to_dict(sizing) if sizing else None,
        )

    def _pinned_reason(self, symbol: str) -> str | None:
        """Why `symbol` is exempt from eviction right now, or None. Every
        eviction path routes its pin check through here so the log says
        'loaded on the order pad' rather than silently skipping a symbol."""
        if symbol in self._pinned:
            return "loaded on the order pad"
        return None

    def _on_pad_fire(self) -> None:
        """F4. Fires immediately -- no confirmation -- but only when Armed;
        Loaded, Empty and an expired arm all do nothing (a line in the log,
        nothing on screen). Sizing is recomputed here from the current tick
        rather than trusting the last drawn frame. A refusal (stale quote,
        non-tradeable, failed sanity bounds) keeps the arm and says why; a
        dispatched order (or dry run) drops back to Loaded so a double press
        can't send a second one."""
        if self.pad is None:
            return
        now_mono = self._pad_clock()
        timeout = self.tunables.order_pad_arm_timeout_sec
        mode = self._pad_ctl.mode(now_mono, timeout)
        symbol = self._pad_ctl.symbol
        if mode is not orderpad.PadMode.ARMED:
            log.info("Order pad F4 ignored: pad is %s (%s)", mode.value, symbol or "no symbol")
            return

        state = self._pad_state()
        now = datetime.now(config.TZ)
        sizing = orderpad.live_sizing(state, self.tunables, now, self._pad_risk_usd) if state else None
        reason = orderpad.validate_fire(
            sizing, state, now, self.tunables.order_pad_max_quote_age_sec,
            non_tradable_listed=symbol in self._ignored_until, symbol=symbol,
        )
        if reason is not None:
            log.info("Order pad REFUSED to fire %s: %s", symbol, reason)
            self.pad.show_block(reason)
            order_history.log_event(
                now, "FIRE_BLOCKED",
                symbol=symbol,
                reason=reason,
                sizing=orderpad.sizing_to_dict(sizing) if sizing else None,
                live_price=state.tick.last if state else None,
                live_bid=state.tick.bid if state else None,
                live_ask=state.tick.ask if state else None,
                quote_age_sec=orderpad.quote_age_sec(state, now),
            )
            return

        plan = orderpad.bracket_plan(sizing)
        if self.tunables.order_pad_dry_run:
            # Everything up to here has run and passed; this is the only
            # thing being skipped. Logging the real BracketPlan (not a
            # paraphrase of it) means what's read back in scanner.log during
            # testing is exactly what the live path will send.
            log.info("Order pad DRY RUN (dry run on) -- would submit: %s", plan.describe())
            self._pad_ctl.consume_arm()
            self.pad.show_result(f"DRY RUN -- NO ORDER SENT: {plan.quantity}sh @ {plan.entry_limit:.2f}")
            order_history.log_event(
                now, "FIRE_DRY_RUN",
                symbol=symbol,
                sizing=orderpad.sizing_to_dict(sizing),
                plan=orderpad.bracket_plan_to_dict(plan),
            )
            return

        if config.IB_PORT not in config.ORDER_PAD_PAPER_PORTS:
            # Independent of the dry-run toggle -- see config.ORDER_PAD_PAPER_PORTS.
            log.error(
                "Order pad fire blocked: IB_PORT %s is not a recognized paper-trading "
                "port %s -- refusing to submit a real order",
                config.IB_PORT, sorted(config.ORDER_PAD_PAPER_PORTS),
            )
            self.pad.show_block("blocked: not on a paper account (see IB_PORT)")
            return

        if state.conid is None:
            log.error("Order pad fire blocked: %s has no qualified contract yet", symbol)
            self.pad.show_block("no qualified contract yet")
            return

        self._pad_ctl.consume_arm()
        self._submit_pad_bracket(plan, sizing, state)

    def _submit_pad_bracket(
        self, plan: orderpad.BracketPlan, sizing: orderpad.PadSizing, state: SymbolState,
    ) -> None:
        """Send the parent (entry) leg standalone -- not IB's own linked
        bracket -- because the stop/target legs are sized from the actual
        fill, not the requested quantity (see orderpad.BracketPlan). They get
        created in _on_pad_parent_fill once the first fill reports in.

        conId alone identifies the security but is NOT enough for
        placeOrder -- unlike _contract_for_news's bare-conId contract (only
        used for reqHistoricalNewsAsync), IB's order validation rejects a
        contract with no exchange ("Error 321: Missing order exchange"),
        confirmed against IB's own contract-by-conId docs, which pair conId
        with an explicit exchange in every example. SMART/USD matches every
        other contract built in this codebase (_add_symbol, rvol.py,
        scorer.py)."""
        contract = Stock(conId=state.conid, exchange="SMART", currency="USD")
        parent = LimitOrder(
            "BUY", plan.quantity, plan.entry_limit,
            tif="DAY", outsideRth=True, orderRef=plan.oca_group,
        )
        trade = self.ib.placeOrder(contract, parent)
        bracket = _PadBracket(plan=plan, contract=contract, sizing=sizing)
        self._pad_order_ids[trade.order.orderId] = bracket
        trade.fillEvent += self._on_pad_parent_fill
        trade.cancelledEvent += self._on_pad_parent_cancelled
        log.info(
            "Order pad SUBMITTED %s (orderId %d): %s",
            plan.symbol, trade.order.orderId, plan.describe(),
        )
        if self.pad:
            self.pad.show_result(f"SENT {plan.quantity}sh @ {plan.entry_limit:.2f}")
        order_history.log_event(
            datetime.now(config.TZ), "SUBMITTED",
            symbol=plan.symbol,
            order_id=trade.order.orderId,
            conid=state.conid,
            sizing=orderpad.sizing_to_dict(sizing),
            plan=orderpad.bracket_plan_to_dict(plan),
        )

    def _on_pad_parent_fill(self, trade: Trade, fill) -> None:
        """A parent fill (partial or full) arrived -- (re)create the stop and
        target legs sized to shares actually held so far, and re-anchored to
        the order's running average fill price (not the price at F4), so
        entry slippage can't silently widen realized risk past risk_usd.
        Reuses the existing stop/target orderIds on a later fill so this is
        a resize (placeOrder with a non-zero orderId modifies in place), not
        a second pair of orders stacking on top of the first."""
        bracket = self._pad_order_ids.get(trade.order.orderId)
        if bracket is None:
            return
        filled = int(trade.filled())
        if filled <= 0:
            return

        # execution.avgPrice, not orderStatus.avgFillPrice: IB delivers it
        # atomically as part of THIS fill's own execDetails report, with no
        # risk of racing a separate orderStatus message that may not have
        # caught up yet. Falls back to this fill's own price if avgPrice
        # isn't populated (seen on some synthetic/first-fill feeds).
        fill_price = fill.execution.avgPrice or fill.execution.price
        exit_prices = orderpad.recompute_bracket_exit(fill_price, bracket.sizing)
        bracket.realized_stop_limit = exit_prices.stop_limit_price
        bracket.realized_stop_trigger = exit_prices.stop_trigger_price
        bracket.realized_target_price = exit_prices.target_price

        # Entry slippage, in dollars -- planned_risk_usd is the frozen,
        # fire-time number (shares * stop_distance); actual_risk_usd is what
        # the JUST-recomputed stop actually locks in for the shares filled
        # so far (filled * distance-to-the-real-stop), so the two land on
        # the same value once the fix is working, with only tick-rounding
        # residue between them -- logged on every PARENT_FILL so the gap (or
        # lack of one) is visible without recomputing it from the fire record.
        fire_price = bracket.sizing.price
        slip = fill_price - fire_price
        planned_risk_usd = bracket.sizing.risk_usd
        actual_risk_usd = filled * (fill_price - exit_prices.stop_limit_price)
        # The parent's entry_limit already caps how far a BUY can slip
        # (marketable limit at fire_price + entry_slippage_allowance), so this
        # should never trip -- if it does, the fill landed worse than the
        # entry_limit should have permitted, and that is worth knowing
        # about immediately, not just in the log.
        slip_exceeds_entry_allowance = abs(slip) > bracket.sizing.entry_slippage_allowance
        if slip_exceeds_entry_allowance:
            log.warning(
                "Order pad %s filled %.4f, slipped %.4f from the price at F4 (%.4f) -- exceeds the "
                "%.4f entry slippage allowance (entry_limit was %.4f); the fill landed worse "
                "than the entry limit should have permitted",
                bracket.plan.symbol, fill_price, slip, fire_price,
                bracket.sizing.entry_slippage_allowance, bracket.plan.entry_limit,
            )

        # STP LMT, not plain STP: confirmed live 2026-09-17 (TURB) that a
        # plain stop order's outsideRth flag is silently ignored on US
        # stocks -- IB's own IB 2109 warning says as much ("ignored based on
        # the order type and destination") -- so the stop never actually
        # triggers outside regular hours. STP LMT IS eligible.
        stop = StopLimitOrder(
            "SELL", filled, exit_prices.stop_limit_price, exit_prices.stop_trigger_price,
            tif="DAY", outsideRth=True,
        )
        target = LimitOrder("SELL", filled, exit_prices.target_price, tif="DAY", outsideRth=True)
        stop.ocaGroup = target.ocaGroup = bracket.plan.oca_group
        stop.ocaType = target.ocaType = 1  # cancel the other leg outright once either fills
        # First creation vs. a later resize: placeOrder with an existing
        # orderId modifies in place and hands back the SAME Trade object
        # (ib_async keys its trade table on (clientId, orderId) once orderId
        # is set -- confirmed against ib_async's own placeOrder/orderKey), so
        # subscribing fillEvent/cancelledEvent only here, on first creation,
        # is both necessary and sufficient -- a resize would otherwise stack
        # a duplicate handler on the same object and double-log the eventual
        # exit fill.
        first_creation = bracket.stop_trade is None
        # Diagnostics for the target-leg 404 investigation (see memory:
        # ib_404_locate_hold_investigation): what the client believed about
        # the position and about the legs' own state right BEFORE this
        # placeOrder, so a 404 can be lined up against "did this modify race
        # an unacknowledged order" / "was the position already booked".
        leg_state_before = self._leg_diagnostics(bracket)
        if bracket.stop_trade is not None:
            stop.orderId = bracket.stop_trade.order.orderId
        if bracket.target_trade is not None:
            target.orderId = bracket.target_trade.order.orderId

        if first_creation:
            bracket.legs_first_placed_at = time.monotonic()
        bracket.stop_trade = self.ib.placeOrder(bracket.contract, stop)
        bracket.target_trade = self.ib.placeOrder(bracket.contract, target)
        self._pad_order_ids[bracket.stop_trade.order.orderId] = bracket
        self._pad_order_ids[bracket.target_trade.order.orderId] = bracket
        if first_creation:
            bracket.stop_trade.fillEvent += self._on_pad_exit_fill
            bracket.stop_trade.cancelledEvent += self._on_pad_exit_cancelled
            bracket.target_trade.fillEvent += self._on_pad_exit_fill
            bracket.target_trade.cancelledEvent += self._on_pad_exit_cancelled

        log.info(
            "Order pad %s filled %d/%d @ avg %.4f -- protective stop %.2f "
            "(trigger %.2f) / target %.2f now cover %d sh",
            bracket.plan.symbol, filled, bracket.plan.quantity, fill_price,
            exit_prices.stop_limit_price, exit_prices.stop_trigger_price,
            exit_prices.target_price, filled,
        )
        if self._pad_shows(bracket.plan.symbol):
            self.pad.show_result(
                f"FILLED {filled}/{bracket.plan.quantity} -- stop {exit_prices.stop_limit_price:.2f} "
                f"(trig {exit_prices.stop_trigger_price:.2f}) / tgt {exit_prices.target_price:.2f}"
            )
        order_history.log_event(
            datetime.now(config.TZ), "PARENT_FILL",
            symbol=bracket.plan.symbol,
            order_id=trade.order.orderId,
            filled=filled,
            planned_quantity=bracket.plan.quantity,
            fill_price=fill.execution.price,
            avg_fill_price=fill_price,
            fill_cum_qty=fill.execution.cumQty,
            commission=fill.commissionReport.commission,
            entry_limit=bracket.plan.entry_limit,
            stop_order_id=bracket.stop_trade.order.orderId,
            target_order_id=bracket.target_trade.order.orderId,
            planned_stop_price=bracket.plan.stop_price,
            planned_stop_trigger_price=bracket.plan.stop_trigger_price,
            planned_target_price=bracket.plan.target_price,
            realized_stop_price=exit_prices.stop_limit_price,
            realized_stop_trigger_price=exit_prices.stop_trigger_price,
            realized_target_price=exit_prices.target_price,
            fire_price=fire_price,
            slip=slip,
            planned_risk_usd=planned_risk_usd,
            actual_risk_usd=actual_risk_usd,
            slip_exceeds_entry_allowance=slip_exceeds_entry_allowance,
            is_modify=not first_creation,
            known_position=self._known_position(bracket.contract.conId),
            **{f"{k}_before": v for k, v in leg_state_before.items()},
        )

    def _known_position(self, conid: int) -> float:
        """Shares of conid the client's own position cache shows right now
        (ib.positions(), fed by IB's position stream) -- what the client
        KNOWS it holds, which can lag the fill it just reported."""
        return sum(p.position for p in self.ib.positions() if p.contract.conId == conid)

    def _leg_diagnostics(self, bracket: _PadBracket) -> dict:
        """Snapshot of both exit legs as the client currently sees them:
        status/whyHeld/quantity, plus ms since the legs were first placed.
        Empty-valued (None) before the legs exist."""
        out: dict = {
            "ms_since_first_placed": None if bracket.legs_first_placed_at is None
            else round((time.monotonic() - bracket.legs_first_placed_at) * 1000),
        }
        for name, trade in (("stop", bracket.stop_trade), ("target", bracket.target_trade)):
            out[f"{name}_status"] = None if trade is None else trade.orderStatus.status
            out[f"{name}_why_held"] = None if trade is None else trade.orderStatus.whyHeld
            out[f"{name}_qty"] = None if trade is None else trade.order.totalQuantity
        return out

    def _on_pad_parent_cancelled(self, trade: Trade) -> None:
        bracket = self._pad_order_ids.get(trade.order.orderId)
        if bracket is None:
            return
        filled = int(trade.filled())
        log.info(
            "Order pad parent order for %s cancelled (%d/%d filled before cancel)",
            bracket.plan.symbol, filled, bracket.plan.quantity,
        )
        if filled == 0:
            del self._pad_order_ids[trade.order.orderId]
            if self._pad_shows(bracket.plan.symbol):
                self.pad.show_result("order cancelled, nothing filled")
        order_history.log_event(
            datetime.now(config.TZ), "PARENT_CANCELLED",
            symbol=bracket.plan.symbol,
            order_id=trade.order.orderId,
            filled=filled,
            planned_quantity=bracket.plan.quantity,
        )

    def _leg_name(self, bracket: _PadBracket, order_id: int) -> str:
        if bracket.stop_trade is not None and order_id == bracket.stop_trade.order.orderId:
            return "STOP"
        return "TARGET"

    def _on_pad_exit_fill(self, trade: Trade, fill) -> None:
        """The stop or target leg filled, partially or fully -- this is how
        a position actually closes. Wired once per bracket, right when the
        legs are first created (see _on_pad_parent_fill), off IB's own
        execDetails stream -- the same mechanism (not market data) that
        already delivers the parent's fills, so it fires whether or not the
        symbol still holds one of the live pool's slots."""
        bracket = self._pad_order_ids.get(trade.order.orderId)
        if bracket is None:
            return
        leg = self._leg_name(bracket, trade.order.orderId)
        filled = int(trade.filled())
        fill_price = fill.execution.price
        log.info(
            "Order pad %s %s leg filled %d/%d @ %.2f",
            bracket.plan.symbol, leg, filled, bracket.plan.quantity, fill_price,
        )

        # Fill-quality measurement for the STOP leg only -- empirical data to
        # tune tunables.stop_trigger_lead_pct against, rather than guessing.
        # slipped_past_limit should be rare-to-never for a genuine LMT fill
        # (a limit order fills at its price or better); it's here as a
        # sanity flag. fill_vs_trigger is the real signal: how far price ran
        # between the trigger waking the order and it actually filling --
        # too tight a lead and this (or the fill itself) gets worse.
        stop_fields = {}
        pad_message = f"{leg} FILLED {filled}sh @ {fill_price:.2f}"
        if leg == "STOP":
            limit_price = bracket.realized_stop_limit
            trigger_price = bracket.realized_stop_trigger
            stop_fields = dict(
                stop_limit_price=limit_price,
                stop_trigger_price=trigger_price,
                slipped_past_limit=limit_price is not None and fill_price < limit_price,
                fill_vs_limit=None if limit_price is None else fill_price - limit_price,
                fill_vs_trigger=None if trigger_price is None else fill_price - trigger_price,
            )
            if limit_price is not None:
                pad_message = f"STOP FILLED {filled}sh @ {fill_price:.2f} (limit {limit_price:.2f})"

        if self._pad_shows(bracket.plan.symbol):
            self.pad.show_result(pad_message)
        order_history.log_event(
            datetime.now(config.TZ), "EXIT_FILL",
            symbol=bracket.plan.symbol,
            leg=leg,
            order_id=trade.order.orderId,
            filled=filled,
            planned_quantity=bracket.plan.quantity,
            fill_price=fill_price,
            fill_avg_price=fill.execution.avgPrice,
            fill_cum_qty=fill.execution.cumQty,
            commission=fill.commissionReport.commission,
            entry_limit=bracket.plan.entry_limit,
            realized_stop_price=bracket.realized_stop_limit,
            realized_target_price=bracket.realized_target_price,
            **stop_fields,
        )

    def _on_pad_exit_cancelled(self, trade: Trade) -> None:
        """The sibling of whichever leg just filled -- IB's OCA group
        (ocaType=1) cancels it automatically. Expected, not an error; logged
        so the order history shows the whole bracket resolving rather than
        one leg silently vanishing."""
        bracket = self._pad_order_ids.get(trade.order.orderId)
        if bracket is None:
            return
        leg = self._leg_name(bracket, trade.order.orderId)
        log.info("Order pad %s %s leg cancelled (OCA -- other leg filled)", bracket.plan.symbol, leg)
        order_history.log_event(
            datetime.now(config.TZ), "EXIT_LEG_CANCELLED",
            symbol=bracket.plan.symbol,
            leg=leg,
            order_id=trade.order.orderId,
        )

    def _on_pad_commission_report(self, trade: Trade, fill, report) -> None:
        """Commission (and, for a closing fill, IB's own realizedPNL) arrives
        on its own event after the fill it belongs to -- see ib_async's
        wrapper.commissionReport, which patches the same Fill object
        fillEvent already emitted. Global on the IB object like errorEvent
        (fires for every fill on the account), so filtered down to
        pad-tracked orders here."""
        bracket = self._pad_order_ids.get(trade.order.orderId)
        if bracket is None:
            return
        order_history.log_event(
            datetime.now(config.TZ), "COMMISSION",
            symbol=bracket.plan.symbol,
            order_id=trade.order.orderId,
            fill_price=fill.execution.price,
            fill_shares=fill.execution.shares,
            commission=report.commission,
            realized_pnl=report.realizedPNL,
        )

    def _on_ib_order_error(self, reqId: int, errorCode: int, errorString: str, contract) -> None:
        """Surface an IB-side reject/warning for a pad-submitted order onto
        the pad itself -- scanner.log alone isn't glanceable over TWS, which
        is the entire reason the pad exists.

        Informational codes (INFORMATIONAL_IB_CODES) are logged at INFO under
        their own IB_INFO history event and kept off the pad, so a routine
        notice neither reads as a failure in scanner.log/order history nor
        overwrites a real result (e.g. FILLED ...) with IB's boilerplate."""
        bracket = self._pad_order_ids.get(reqId)
        if bracket is None:
            return
        if bracket.stop_trade is not None and reqId == bracket.stop_trade.order.orderId:
            leg = "STOP"
        elif bracket.target_trade is not None and reqId == bracket.target_trade.order.orderId:
            leg = "TARGET"
        else:
            leg = "PARENT"
        diagnostics = dict(
            leg=leg,
            known_position=self._known_position(bracket.contract.conId),
            **self._leg_diagnostics(bracket),
        )
        if errorCode in INFORMATIONAL_IB_CODES:
            log.info(
                "Order pad %s order %d notice %d: %s",
                bracket.plan.symbol, reqId, errorCode, errorString,
            )
            order_history.log_event(
                datetime.now(config.TZ), "IB_INFO",
                symbol=bracket.plan.symbol,
                order_id=reqId,
                error_code=errorCode,
                error_string=errorString,
                **diagnostics,
            )
            return
        log.error(
            "Order pad %s order %d error %d: %s",
            bracket.plan.symbol, reqId, errorCode, errorString,
        )
        order_history.log_event(
            datetime.now(config.TZ), "IB_ERROR",
            symbol=bracket.plan.symbol,
            order_id=reqId,
            error_code=errorCode,
            error_string=errorString,
            **diagnostics,
        )
        if self._pad_shows(bracket.plan.symbol):
            self.pad.show_result(f"IB {errorCode}: {errorString}")

    # -- per-symbol lifecycle ----------------------------------------------

    async def _add_symbol(self, hit) -> None:
        if (
            hit.symbol in self.states
            or len(self.states) >= self.tunables.max_live_symbols
            or hit.symbol in self._ignored_until
            or hit.symbol in self._dead_hold
            or hit.symbol in self._excluded_stock_types
        ):
            return
        feed = self._pad_feed
        if feed is not None and feed.owned and feed.symbol == hit.symbol:
            # The pad already holds a live line for this contract -- adopt it
            # rather than opening a second (see _adopt_pad_feed). Only once
            # it is actually open; if the pad's own subscribe is still in
            # flight the scan gate simply retries this symbol later.
            if getattr(feed.state, "_ticker", None) is not None:
                self._adopt_pad_feed(feed, hit)
            return
        state = SymbolState(symbol=hit.symbol, scan_rank=hit.rank, scan_source=hit.source)
        self._apply_float(state)
        self.states[hit.symbol] = state

        if await self._open_feed(state) is not None:
            self._remove_symbol(hit.symbol)
            return
        log.info(
            "%s admitted to live tracking (%d/%d slots, scan_rank=%s, source=%s) -- "
            "RVOL/$Vol start from zero and rebuild from here",
            hit.symbol, len(self.states), self.tunables.max_live_symbols, hit.rank, hit.source,
        )

        asyncio.create_task(self._load_baseline(state))
        asyncio.create_task(self._load_float(state))
        asyncio.create_task(self._load_short_interest(state))

    async def _open_feed(self, state: SymbolState) -> str | None:
        """Qualify the contract and open the symbol's market-data line and
        1-min ATR stream -- the part of admission that is the same whether
        the symbol is taking a live slot (_add_symbol) or is the order pad's
        own feed outside the pool (_open_pad_feed). Returns None on success,
        or a short reason on failure (already logged, nothing left open);
        cleanup of the caller's own bookkeeping stays with the caller."""
        symbol = state.symbol
        contract = Stock(symbol, "SMART", "USD")
        try:
            qualified = await self.ib.qualifyContractsAsync(contract)
        except Exception:
            log.exception("Failed to qualify contract for %s", symbol)
            return "could not qualify contract"
        # [None], not []: for an unknown symbol ib_async logs "Unknown
        # contract" and hands back a list holding None (confirmed live
        # 2026-09-18) -- reachable from the pad, where the symbol is typed.
        if not qualified or qualified[0] is None:
            log.info("%s dropped: IB could not qualify a contract for it", symbol)
            return "IB could not qualify a contract"
        contract = qualified[0]
        state.conid = contract.conId

        try:
            details_list = await self.ib.reqContractDetailsAsync(contract)
            stock_type = details_list[0].stockType if details_list else None
        except Exception:
            log.exception("Failed to fetch contract details for %s (proceeding -- not excluded)", symbol)
            stock_type = None
        if stock_type in config.EXCLUDE_STOCK_TYPES:
            log.info(
                "%s dropped: excluded instrument type (stockType=%s) -- held out permanently, "
                "won't be re-attempted",
                symbol, stock_type,
            )
            self._excluded_stock_types.add(symbol)
            return f"excluded instrument type ({stock_type})"

        # Halted status (tick 49) is pushed automatically by TWS whenever it applies --
        # it cannot be requested via genericTickList (IB rejects the whole reqMktData
        # call with error 321 if you try), so no generic ticks need to be requested here.
        ticker = self.ib.reqMktData(contract, snapshot=False)
        state.live_subscribed = True
        state.subscribed_at = datetime.now(config.TZ)

        def on_tick(t: Ticker, _state=state):
            self._apply_tick(_state, t)

        ticker.updateEvent += on_tick
        state._ticker = ticker  # keep a reference for cleanup

        asyncio.create_task(self._start_atr(state, contract))
        return None

    def _close_feed(self, state: SymbolState) -> None:
        """Cancel whatever _open_feed opened for `state` (market data and the
        ATR bar stream). Safe on a state that never got that far. The one
        place a feed is torn down, for the pool (_remove_symbol) and for a
        pad-owned feed alike."""
        ticker = getattr(state, "_ticker", None)
        if ticker is not None:
            state._ticker = None
            try:
                self.ib.cancelMktData(ticker.contract)
            except Exception:
                log.exception("Error cancelling market data for %s", state.symbol)
        atr_bars = getattr(state, "_atr_bars", None)
        if atr_bars is not None:
            state._atr_bars = None
            try:
                self.ib.cancelHistoricalData(atr_bars)
            except Exception:
                log.exception("Error cancelling ATR subscription for %s", state.symbol)

    def _feed_is_live(self, state: SymbolState) -> bool:
        """Whether `state` is still what its symbol's feed should be feeding:
        the pool's entry for it, or the pad's current feed."""
        return self.states.get(state.symbol) is state or (
            self._pad_feed is not None and self._pad_feed.state is state
        )

    async def _start_atr(self, state: SymbolState, contract) -> None:
        try:
            bars = await atr.start_atr_subscription(self.ib, contract)
        except Exception:
            log.exception("Failed to start ATR bar subscription for %s", state.symbol)
            return
        if not self._feed_is_live(state):
            # Evicted/removed (or the pad moved on) while the throttled fetch
            # was in flight -- this subscription was never registered on
            # state, so the teardown couldn't have cancelled it; do it here
            # instead of leaking it.
            try:
                self.ib.cancelHistoricalData(bars)
            except Exception:
                log.exception("Error cancelling orphaned ATR subscription for %s", state.symbol)
            return
        atr.seed_atr_state(state.atr, bars)
        state._atr_bars = bars  # keep a reference for cleanup, same convention as state._ticker

        def on_bar_update(_bars, has_new_bar, _state=state):
            atr.update_atr_state(_state.atr, _bars, has_new_bar)

        bars.updateEvent += on_bar_update

    async def _load_baseline(self, state: SymbolState) -> None:
        baseline = await rvol.build_baseline(self.ib, state.symbol, self.session)
        if state.symbol not in self.states:
            return
        state.baseline = baseline
        state.baseline_unavailable = baseline is None

    async def _load_float(self, state: SymbolState) -> None:
        if state.float_known:
            return  # float_reference.csv already covered this one -- manual override wins
        shares = await floatref.get_float(state.symbol)
        current = self.states.get(state.symbol)
        if current is not None and not current.float_known:
            current.float_shares = shares
            current.float_known = shares is not None

    async def _load_short_interest(self, state: SymbolState) -> None:
        result = await short_interest.get_short_interest(state.symbol)
        current = self.states.get(state.symbol)
        if current is not None:
            current.short_pct = result["pct_float"] if result else None
            current.short_interest_known = result is not None

    def _remove_symbol(self, symbol: str) -> None:
        state = self.states.pop(symbol, None)
        self._logged_no_slot.discard(symbol)
        self._filter_reasons.pop(symbol, None)
        # Belt and braces for the pin: _evict_unqualified and both bump paths
        # already skip pinned symbols, but removal also happens for reasons a
        # pin has no business overriding (session rollover, reconnect, a
        # manual non-tradable mark, a contract that fails to qualify). This
        # is the single choke point every one of those routes through. The
        # pad isn't speculative -- it was loaded deliberately -- so instead of
        # losing its symbol to pool housekeeping it re-opens it on a feed of
        # its own (below, after the pool's line is cancelled).
        reopen_for_pad = False
        if symbol in self._pinned:
            self._pinned.discard(symbol)
            feed = self._pad_feed
            if feed is not None and not feed.owned and feed.symbol == symbol:
                self._pad_feed = None
                reopen_for_pad = True
        if state is not None:
            self._close_feed(state)
        if reopen_for_pad:
            log.info(
                "Order pad: %s left the live pool while loaded -- re-subscribing on the pad's own feed",
                symbol,
            )
            self._pad_attach(symbol)
            if self.pad is not None:
                self.pad.mark_dirty()

    def _apply_tick(self, state: SymbolState, t: Ticker) -> None:
        # Feed liveness, stamped on every update regardless of which fields
        # moved -- this is what the order pad's staleness guard reads (see
        # orderpad.quote_age_sec). Deliberately not "when the price last
        # CHANGED": a symbol genuinely printing at a steady 4.12 isn't stale,
        # one whose feed has stopped delivering is.
        state.tick.updated_at = datetime.now(config.TZ)
        if t.last is not None and not _isnan(t.last):
            state.tick.last = t.last
            now = datetime.now(config.TZ)
            spikes.update_spike_state(state.spike, t.last, now, self.tunables)
            trend.update_trend_state(state.trend, t.last, now, self.tunables)
        if t.bid is not None and not _isnan(t.bid):
            state.tick.bid = t.bid
        if t.ask is not None and not _isnan(t.ask):
            state.tick.ask = t.ask
        if t.volume is not None and not _isnan(t.volume):
            if state.volume_offset is None:
                state.volume_offset = t.volume  # first reading -- this session's zero point
            state.tick.volume = t.volume

        halted = getattr(t, "halted", None)
        if halted is not None and not _isnan(halted):
            update_halt_state(state.halt, halted)

        # The pad recalculates on every tick of ITS symbol: flag the window,
        # which redraws (recomputing sizing from this fresh tick, see
        # _pad_view) at most ~10x/s. Identity check, not a symbol compare --
        # a pool state and the pad's own state for one symbol can't coexist
        # (see _adopt_pad_feed), but a stale closure from a released feed can.
        if self.pad is not None and self._pad_feed is not None and self._pad_feed.state is state:
            self.pad.mark_dirty()

    def _apply_float(self, state: SymbolState) -> None:
        """Applies the CSV override, which always wins and re-applies live on
        every periodic refresh. If there's no CSV entry, leaves float_known/
        float_shares alone rather than resetting them -- _load_float's
        one-time Yahoo fetch (see _add_symbol) fills them in asynchronously,
        and this must not clobber that result on the next refresh."""
        shares = self.float_map.get(state.symbol.upper())
        if shares is not None:
            state.float_known = True
            state.float_shares = shares
        elif not state.float_known:
            state.float_shares = None

    async def on_unmount(self) -> None:
        self.scanner_mgr.stop()
        if self._pad_pump_task is not None:
            self._pad_pump_task.cancel()
        if self.pad is not None:
            self.pad.close()  # also flushes the window geometry to disk
            # Dropped before the teardown loop below, which routes through
            # _remove_symbol and would otherwise try to render into a window
            # that no longer exists.
            self.pad = None
        self._pad_release_feed()
        for symbol in list(self.states.keys()):
            self._remove_symbol(symbol)
        self.ib.disconnect()


def _isnan(v) -> bool:
    try:
        return math.isnan(v)
    except TypeError:
        return False
