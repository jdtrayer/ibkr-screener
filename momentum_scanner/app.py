"""
Orchestrator: wires scanner -> persistence -> RVOL baseline -> live ticks ->
filters -> display together and drives the Textual UI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
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
    snapshot: orderpad.ArmedSnapshot
    stop_trade: Trade | None = None
    target_trade: Trade | None = None
    # The ACTUAL stop/target prices last sent to IB -- recomputed from the
    # real fill price on every parent fill (see _on_pad_parent_fill), so
    # these can differ from `plan`'s planned figures whenever the entry
    # slips. None until the first fill creates the legs.
    realized_stop_limit: float | None = None
    realized_stop_trigger: float | None = None
    realized_target_price: float | None = None


class ScannerApp(App):
    BINDINGS = [
        # Arm is pressed here, in the TUI, because that's where the row
        # cursor already is; fire is pressed in the pad, which takes focus on
        # arm. See config.ORDER_PAD_ARM_KEY for why it's a function key.
        (config.ORDER_PAD_ARM_KEY, "arm_pad", "Arm order pad"),
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
        # The order pad's armed symbol. A live slot it holds is exempt from
        # every eviction path (bump, scorer swap, persistence decay, spike-
        # quiet) for as long as it's armed -- see _pinned_reason. Sized like a
        # set for consistency with the other hold-outs above, though only one
        # symbol is ever armed at a time.
        self._pinned: set[str] = set()
        # Symbols currently being subscribed on demand for a manually-typed
        # pad arm (see _on_pad_manual_arm) -- guards against a second F2/Enter
        # while the qualify/subscribe pipeline for the first one is in flight.
        self._manual_pending: set[str] = set()
        self.pad: OrderPadWindow | None = None
        self._pad_pump_task: asyncio.Task | None = None
        # Every orderId belonging to a live pad-submitted bracket (parent,
        # then its stop/target once created) maps back to the same
        # _PadBracket, so fill/cancel/error events on any leg can find their
        # way back to the plan and the pad. Entries outlive the pad being
        # disarmed/re-armed -- a fired position stays protected regardless
        # of what's currently on screen.
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
            self._reconfigure_for_session(self.session)
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
            # leave the pad holding a snapshot with no live quote to validate
            # against, which validate_fire can only turn into a refusal.
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

    # -- order pad (arm / fire) --------------------------------------------

    def _start_order_pad(self) -> None:
        """Bring up the always-on-top pad window and start pumping it from
        this app's own asyncio loop.

        Non-fatal by design: the scanner is useful without the pad, so a
        machine with no DISPLAY (or no Tk) logs and carries on rather than
        taking the whole TUI down over a secondary window.
        """
        try:
            self.pad = OrderPadWindow(
                on_fire=self._on_pad_fire,
                on_disarm=self._on_pad_disarm,
                on_manual_arm=self._on_pad_manual_arm,
                quote_age_provider=self._pad_quote_age,
            )
        except Exception:
            log.exception("Order pad window failed to start (non-fatal); scanner continues without it")
            self.pad = None
            return
        self._pad_pump_task = asyncio.create_task(self.pad.pump())
        log.info(
            "Order pad ready -- %s arms the selected row, %s fires, dry run %s",
            config.ORDER_PAD_ARM_KEY, config.ORDER_PAD_FIRE_KEY,
            "ON" if self.tunables.order_pad_dry_run else "OFF",
        )

    def _pad_quote_age(self) -> float | None:
        """Quote age for the armed symbol. The one number on the pad that is
        deliberately NOT frozen -- everything else is a snapshot, but a stale
        feed is precisely what the frozen numbers can't tell you about."""
        snapshot = self.pad.snapshot if self.pad else None
        if snapshot is None:
            return None
        return orderpad.quote_age_sec(self.states.get(snapshot.symbol), datetime.now(config.TZ))

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

    def action_arm_pad(self) -> None:
        symbol = self._cursor_symbol()
        if symbol is None:
            # Nothing under the cursor -- most likely a symbol the user wants
            # to trade isn't on either table at all. Raise the pad (if it
            # isn't already up) so they can type it into the manual-entry box
            # rather than just telling them F2 didn't find anything.
            if self.pad is not None:
                self.pad.prompt_manual_entry()
            else:
                self.notify("Order pad is not available", severity="warning")
            return
        self._arm(symbol)

    def _on_pad_manual_arm(self, symbol: str) -> None:
        """Typed-symbol path. A symbol already live arms immediately; one
        that isn't gets admitted on demand -- the same qualify/subscribe
        pipeline a scan hit goes through -- so a name outside the scan/scorer
        universe can still be sized and fired from here."""
        if symbol in self.states:
            self._arm(symbol)
            return
        if symbol in self._ignored_until or symbol in self._dead_hold or symbol in self._excluded_stock_types:
            if self.pad:
                self.pad.disarm(f"{symbol} is held out (non-tradable / dead-hold / excluded type)")
            return
        if symbol in self._manual_pending:
            return  # already being subscribed from an earlier Enter/F2
        asyncio.create_task(self._admit_manual(symbol))

    async def _admit_manual(self, symbol: str) -> None:
        """On-demand admission for a symbol typed into the pad that isn't in
        the live pool. Bumps a slot exactly the way a qualifying scan hit
        would (same bump_candidate call, same pressure-only rule: only a
        weak occupant gets bumped, never a healthy one just to make room) --
        a manual request is demand too, but it doesn't get to skip the gate
        that everything else goes through."""
        non_scorer_states = {s: st for s, st in self.states.items() if st.scan_source != "SCORER"}
        if len(non_scorer_states) >= self.tunables.max_live_symbols - self.tunables.scorer_reserved_slots:
            now = datetime.now(config.TZ)
            bump = bump_candidate(non_scorer_states, self.session, now, pinned=self._pinned)
            if bump is None:
                if self.pad:
                    self.pad.disarm(f"{symbol}: pool full, nothing bump-eligible")
                return
            log.info(
                "Bumped %s (%s) to admit %s (order pad, manual entry); re-entry barred for %.0fs",
                bump.symbol, bump_reason(bump, self.session), symbol, self.tunables.slot_reentry_cooldown_sec,
            )
            self._remove_symbol(bump.symbol)
            self._slot_cooldown[bump.symbol] = now

        self._manual_pending.add(symbol)
        if self.pad:
            self.pad.disarm(f"subscribing to {symbol}...")
        try:
            await self._add_symbol(ScanHit(symbol=symbol, con_id=0, rank=None, source="PAD"))
        finally:
            self._manual_pending.discard(symbol)

        if symbol not in self.states:
            if self.pad:
                self.pad.disarm(f"{symbol}: could not subscribe (bad symbol or excluded type)")
            return

        # Give the fresh subscription a few seconds to deliver a first tick
        # before falling back to _arm's own "has no price yet" message --
        # ATR/tick data land asynchronously right after _add_symbol returns.
        for _ in range(20):
            state = self.states.get(symbol)
            if state is None:
                break
            if orderpad.build_snapshot(state, self.tunables, datetime.now(config.TZ)) is not None:
                break
            await asyncio.sleep(0.5)
        self._arm(symbol)

    def _arm(self, symbol: str) -> None:
        if self.pad is None:
            return
        state = self.states.get(symbol)
        if state is None:
            self.pad.disarm(f"{symbol} is not in the live pool")
            return
        snapshot = orderpad.build_snapshot(state, self.tunables, datetime.now(config.TZ))
        if snapshot is None:
            self.pad.disarm(f"{symbol} has no price yet")
            return
        self._unpin()
        self._pinned.add(symbol)
        self.pad.arm(snapshot)
        log.info(
            "Order pad armed %s: %dsh @ %.2f, stop %.2f, target %.2f, risk $%.2f, "
            "S/spr %s, effR %s -- slot pinned",
            symbol, snapshot.shares, snapshot.price, snapshot.stop_price,
            snapshot.target_price, snapshot.risk_usd,
            f"{snapshot.stop_in_spreads:.1f}" if snapshot.stop_in_spreads is not None else "unknown",
            f"{snapshot.effective_r:.2f}" if snapshot.effective_r is not None else "unknown",
        )
        order_history.log_event(
            snapshot.armed_at, "ARM",
            symbol=symbol,
            session=self.session.value,
            dry_run=self.tunables.order_pad_dry_run,
            scan_source=state.scan_source,
            scan_rank=state.scan_rank,
            conid=state.conid,
            atr_value=atr.atr_value(state.atr),
            tunables=asdict(self.tunables),
            snapshot=orderpad.snapshot_to_dict(snapshot),
        )

    def _on_pad_disarm(self) -> None:
        if self.pad is None:
            return
        symbol = self.pad.snapshot.symbol if self.pad.snapshot else None
        self._unpin()
        self.pad.disarm()
        if symbol:
            log.info("Order pad disarmed %s -- slot unpinned", symbol)

    def _unpin(self) -> None:
        self._pinned.clear()

    def _pinned_reason(self, symbol: str) -> str | None:
        """Why `symbol` is exempt from eviction right now, or None. Every
        eviction path routes its pin check through here so the log says
        'pinned by the order pad' rather than silently skipping a symbol."""
        if symbol in self._pinned:
            return "armed on the order pad"
        return None

    def _on_pad_fire(self) -> None:
        if self.pad is None or self.pad.snapshot is None:
            return
        snapshot = self.pad.snapshot
        state = self.states.get(snapshot.symbol)
        now = datetime.now(config.TZ)
        reason = orderpad.validate_fire(
            snapshot, state, now,
            non_tradable_listed=snapshot.symbol in self._ignored_until,
        )
        if reason is not None:
            log.info("Order pad REFUSED to fire %s: %s", snapshot.symbol, reason)
            self.pad.show_block(reason)
            order_history.log_event(
                now, "FIRE_BLOCKED",
                symbol=snapshot.symbol,
                reason=reason,
                snapshot=orderpad.snapshot_to_dict(snapshot),
                live_price=state.tick.last if state else None,
                live_bid=state.tick.bid if state else None,
                live_ask=state.tick.ask if state else None,
                quote_age_sec=orderpad.quote_age_sec(state, now),
            )
            return

        plan = orderpad.bracket_plan(snapshot)
        if self.tunables.order_pad_dry_run:
            # The full arm -> validate path has run and passed; this is the
            # only thing being skipped. Logging the real BracketPlan (not a
            # paraphrase of it) means what's read back in scanner.log during
            # testing is exactly what the live path will send.
            log.info("Order pad DRY RUN (dry run on) -- would submit: %s", plan.describe())
            self.pad.show_result(f"DRY RUN: {plan.quantity}sh @ {plan.entry_limit:.2f}")
            order_history.log_event(
                now, "FIRE_DRY_RUN",
                symbol=snapshot.symbol,
                snapshot=orderpad.snapshot_to_dict(snapshot),
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
            log.error("Order pad fire blocked: %s has no qualified contract yet", snapshot.symbol)
            self.pad.show_block("no qualified contract yet")
            return

        self._submit_pad_bracket(plan, snapshot, state)

    def _submit_pad_bracket(
        self, plan: orderpad.BracketPlan, snapshot: orderpad.ArmedSnapshot, state: SymbolState,
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
        bracket = _PadBracket(plan=plan, contract=contract, snapshot=snapshot)
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
            snapshot=orderpad.snapshot_to_dict(snapshot),
            plan=orderpad.bracket_plan_to_dict(plan),
        )

    def _on_pad_parent_fill(self, trade: Trade, fill) -> None:
        """A parent fill (partial or full) arrived -- (re)create the stop and
        target legs sized to shares actually held so far, and re-anchored to
        the order's running average fill price (not the armed price), so
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
        exit_prices = orderpad.recompute_bracket_exit(fill_price, bracket.snapshot)
        bracket.realized_stop_limit = exit_prices.stop_limit_price
        bracket.realized_stop_trigger = exit_prices.stop_trigger_price
        bracket.realized_target_price = exit_prices.target_price

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
        if bracket.stop_trade is not None:
            stop.orderId = bracket.stop_trade.order.orderId
        if bracket.target_trade is not None:
            target.orderId = bracket.target_trade.order.orderId

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
        if self.pad and self.pad.snapshot and self.pad.snapshot.symbol == bracket.plan.symbol:
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
        )

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
            if self.pad and self.pad.snapshot and self.pad.snapshot.symbol == bracket.plan.symbol:
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

        if self.pad and self.pad.snapshot and self.pad.snapshot.symbol == bracket.plan.symbol:
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
        is the entire reason the pad exists."""
        bracket = self._pad_order_ids.get(reqId)
        if bracket is None:
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
        )
        if self.pad and self.pad.snapshot and self.pad.snapshot.symbol == bracket.plan.symbol:
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
        state = SymbolState(symbol=hit.symbol, scan_rank=hit.rank, scan_source=hit.source)
        self._apply_float(state)
        self.states[hit.symbol] = state

        contract = Stock(hit.symbol, "SMART", "USD")
        try:
            qualified = await self.ib.qualifyContractsAsync(contract)
        except Exception:
            log.exception("Failed to qualify contract for %s", hit.symbol)
            self._remove_symbol(hit.symbol)
            return
        if not qualified:
            log.info("%s dropped: IB could not qualify a contract for it", hit.symbol)
            self._remove_symbol(hit.symbol)
            return
        contract = qualified[0]
        state.conid = contract.conId

        try:
            details_list = await self.ib.reqContractDetailsAsync(contract)
            stock_type = details_list[0].stockType if details_list else None
        except Exception:
            log.exception("Failed to fetch contract details for %s (proceeding -- not excluded)", hit.symbol)
            stock_type = None
        if stock_type in config.EXCLUDE_STOCK_TYPES:
            log.info(
                "%s dropped: excluded instrument type (stockType=%s) -- held out permanently, "
                "won't be re-attempted",
                hit.symbol, stock_type,
            )
            self._excluded_stock_types.add(hit.symbol)
            self._remove_symbol(hit.symbol)
            return

        # Halted status (tick 49) is pushed automatically by TWS whenever it applies --
        # it cannot be requested via genericTickList (IB rejects the whole reqMktData
        # call with error 321 if you try), so no generic ticks need to be requested here.
        ticker = self.ib.reqMktData(contract, snapshot=False)
        state.live_subscribed = True
        state.subscribed_at = datetime.now(config.TZ)
        log.info(
            "%s admitted to live tracking (%d/%d slots, scan_rank=%s, source=%s) -- "
            "RVOL/$Vol start from zero and rebuild from here",
            hit.symbol, len(self.states), self.tunables.max_live_symbols, hit.rank, hit.source,
        )

        def on_tick(t: Ticker, _state=state):
            self._apply_tick(_state, t)

        ticker.updateEvent += on_tick
        state._ticker = ticker  # keep a reference for cleanup

        asyncio.create_task(self._load_baseline(state))
        asyncio.create_task(self._load_float(state))
        asyncio.create_task(self._load_short_interest(state))
        asyncio.create_task(self._start_atr(state, contract))

    async def _start_atr(self, state: SymbolState, contract) -> None:
        try:
            bars = await atr.start_atr_subscription(self.ib, contract)
        except Exception:
            log.exception("Failed to start ATR bar subscription for %s", state.symbol)
            return
        current = self.states.get(state.symbol)
        if current is not state:
            # Evicted/removed while the throttled fetch was in flight -- this
            # subscription was never registered on state, so _remove_symbol
            # couldn't have cancelled it; do it here instead of leaking it.
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
        # pin has no business overriding (session rollover, a manual
        # non-tradable mark, a contract that fails to qualify). Disarming here
        # -- the single choke point every one of those routes through --
        # guarantees the pad can never be left armed on a symbol whose live
        # subscription has just been cancelled.
        if symbol in self._pinned:
            self._unpin()
            if self.pad is not None:
                self.pad.disarm(f"{symbol} left the live pool -- disarmed")
            log.info("Order pad disarmed: %s was removed from live tracking while armed", symbol)
        if state is None:
            return
        ticker = getattr(state, "_ticker", None)
        if ticker is not None:
            try:
                self.ib.cancelMktData(ticker.contract)
            except Exception:
                log.exception("Error cancelling market data for %s", symbol)
        atr_bars = getattr(state, "_atr_bars", None)
        if atr_bars is not None:
            try:
                self.ib.cancelHistoricalData(atr_bars)
            except Exception:
                log.exception("Error cancelling ATR subscription for %s", symbol)

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
            # _remove_symbol and would otherwise try to render a disarm into
            # a window that no longer exists.
            self.pad = None
        self._unpin()
        for symbol in list(self.states.keys()):
            self._remove_symbol(symbol)
        self.ib.disconnect()


def _isnan(v) -> bool:
    try:
        return math.isnan(v)
    except TypeError:
        return False
