"""
Orchestrator: wires scanner -> persistence -> RVOL baseline -> live ticks ->
filters -> display together and drives the Textual UI.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime

from ib_async import IB, Stock, Ticker
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Static

from . import config, display, floatref, rvol, spikes
from .controls import TunablesPanel
from .scorer import SnapshotScorer
from .filters import (
    PersistenceTracker,
    bump_candidate,
    bump_reason,
    display_reason,
    update_halt_state,
)
from .models import SymbolState
from .scanner import ScannerManager
from .session import Session, current_session
from .tunables import Tunables

log = logging.getLogger(__name__)

SESSION_CHECK_EVERY_N_TICKS = int(30 / config.DISPLAY_REFRESH_SEC) or 1
FLOAT_REFRESH_EVERY_N_TICKS = int(60 / config.DISPLAY_REFRESH_SEC) or 1
SORT_REFRESH_EVERY_N_TICKS = int(config.SORT_REFRESH_SEC / config.DISPLAY_REFRESH_SEC) or 1


class ScannerApp(App):
    CSS = """
    #scanner-table-scroll {
        width: 1fr;
        border: solid $primary;
    }
    #side-panel {
        width: auto;
        height: 1fr;
    }
    #tunables-panel {
        height: 1fr;
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
        self.scorer = SnapshotScorer(self.ib)
        self._reconnecting = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with VerticalScroll(id="scanner-table-scroll"):
                yield Static(id="scanner-table")
                yield Static(id="scorer-table")
            with Vertical(id="side-panel"):
                yield TunablesPanel(self.tunables, id="tunables-panel")
        yield Footer()

    async def connect(self) -> None:
        await self.ib.connectAsync(config.IB_HOST, config.IB_PORT, clientId=config.IB_CLIENT_ID)
        log.info("Connected to IB at %s:%s (clientId=%s)", config.IB_HOST, config.IB_PORT, config.IB_CLIENT_ID)

    async def on_mount(self) -> None:
        await self.connect()
        self.ib.disconnectedEvent += self._on_disconnected
        self.float_map = floatref.load()
        self._reconfigure_for_session(current_session())
        self.scanner_mgr.on_update(self._on_scan_update)
        self._render()
        self.set_interval(config.DISPLAY_REFRESH_SEC, self._tick)
        self.set_interval(config.SCORE_REFRESH_SEC, self._scorer_tick)

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
        asyncio.create_task(self.scorer.sweep())

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

        self._process_pending_hits()
        self._evict_unqualified()
        self._log_filter_transitions()

        if self._tick_count % SORT_REFRESH_EVERY_N_TICKS == 0 or not self._row_order:
            self._resort()

        self._render()

    def _resort(self) -> None:
        """Recompute row ORDER by live RVOL. Called on a slower cadence than
        _render() so rows hold still between resorts -- see display.render's
        row_order docstring."""
        self._row_order = sorted(
            self.states,
            key=lambda sym: (self.states[sym].rvol if self.states[sym].rvol is not None else -1),
            reverse=True,
        )

    def _render(self) -> None:
        table = display.render(
            list(self.states.values()),
            self.session,
            self.ib.isConnected(),
            self.tunables,
            self._row_order,
            waiting_count=self._waiting_for_slot_count(),
            cooldown_count=self._cooldown_wait_count(),
        )
        self.query_one("#scanner-table", Static).update(table)
        self.query_one("#scorer-table", Static).update(
            display.render_scorer(self.scorer.ranked(), self.scorer.pool_size, self.scorer.last_sweep_at)
        )

    def _waiting_for_slot_count(self) -> int:
        """Symbols that have cleared persistence and are genuinely blocked by a
        full pool with nothing bump-eligible -- raising max_live_symbols (or a
        weaker occupant showing up) is what unblocks these. Excludes symbols
        sitting out a re-entry cooldown; see _cooldown_wait_count for those.
        Recomputed fresh each render rather than tracked incrementally, so it
        self-corrects if a candidate drops out of the top-N while queued
        instead of ever getting a slot."""
        return sum(
            1 for sym in self._pending_hits
            if sym not in self.states
            and self.persistence.state_for(sym).streak >= self.tunables.persistence_required
            and sym not in self._slot_cooldown
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

    # -- session lifecycle -------------------------------------------------

    def _reconfigure_for_session(self, session: Session) -> None:
        log.info("Session changed: %s -> %s", self.session.value, session.value)
        self.session = session
        for symbol in list(self.states.keys()):
            self._remove_symbol(symbol)
        self._slot_cooldown.clear()
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
                self.states[symbol].scan_rank = hit.rank
            self._logged_no_slot.discard(symbol)
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
        if len(self.states) >= self.tunables.max_live_symbols:
            now = datetime.now(config.TZ)
            bump = bump_candidate(self.states, self.session, now)
            if bump is None:
                self._log_no_slot(symbol)
                return
            log.info(
                "Bumped %s (%s) to admit %s; re-entry barred for %.0fs",
                bump.symbol, bump_reason(bump, self.session), symbol,
                self.tunables.slot_reentry_cooldown_sec,
            )
            self._remove_symbol(bump.symbol)
            self._slot_cooldown[bump.symbol] = now
        asyncio.create_task(self._add_symbol(hit))

    def _log_no_slot(self, symbol: str) -> None:
        if symbol in self._logged_no_slot:
            return  # already logged for this symbol; avoid spamming every tick
        log.info(
            "%s qualified but no live-symbol slot free (%d/%d in use, none bump-eligible: "
            "all clear the $ floor and spread ceiling, are warming up, or spiked recently)",
            symbol, len(self.states), self.tunables.max_live_symbols,
        )
        self._logged_no_slot.add(symbol)

    def _log_filter_transitions(self) -> None:
        """Logs, once per state change, why a live-subscribed symbol is or isn't
        clearing display.render's row filter -- otherwise a symbol can spike
        heavily under the hood and stay invisible with no trace in the log."""
        for symbol, state in self.states.items():
            if state.tick.last is None:
                continue  # no live tick yet, nothing meaningful to report
            reason = display_reason(state, self.session)
            if reason == self._filter_reasons.get(symbol):
                continue
            if reason is None:
                log.info("%s now passing display filters (rvol=%.2fx)", symbol, state.rvol)
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
        for symbol in list(self.states.keys()):
            state = self.states[symbol]
            if self.persistence.state_for(symbol).streak <= 0:
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

    # -- per-symbol lifecycle ----------------------------------------------

    async def _add_symbol(self, hit) -> None:
        if hit.symbol in self.states or len(self.states) >= self.tunables.max_live_symbols:
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

    async def _load_baseline(self, state: SymbolState) -> None:
        baseline = await rvol.build_baseline(self.ib, state.symbol, self.session)
        if state.symbol in self.states:
            state.baseline = baseline

    async def _load_float(self, state: SymbolState) -> None:
        if state.float_known:
            return  # float_reference.csv already covered this one -- manual override wins
        shares = await floatref.get_float(state.symbol)
        current = self.states.get(state.symbol)
        if current is not None and not current.float_known:
            current.float_shares = shares
            current.float_known = shares is not None

    def _remove_symbol(self, symbol: str) -> None:
        state = self.states.pop(symbol, None)
        self._logged_no_slot.discard(symbol)
        self._filter_reasons.pop(symbol, None)
        if state is None:
            return
        ticker = getattr(state, "_ticker", None)
        if ticker is not None:
            try:
                self.ib.cancelMktData(ticker.contract)
            except Exception:
                log.exception("Error cancelling market data for %s", symbol)

    def _apply_tick(self, state: SymbolState, t: Ticker) -> None:
        if t.last is not None and not _isnan(t.last):
            state.tick.last = t.last
            spikes.update_spike_state(state.spike, t.last, datetime.now(config.TZ), self.tunables)
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
        for symbol in list(self.states.keys()):
            self._remove_symbol(symbol)
        self.ib.disconnect()


def _isnan(v) -> bool:
    try:
        return math.isnan(v)
    except TypeError:
        return False
