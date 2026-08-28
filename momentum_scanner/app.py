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
from textual.containers import Horizontal
from textual.widgets import Footer, Header, Static

from . import config, display, floatref, rvol, spikes
from .controls import TunablesPanel
from .filters import (
    PersistenceTracker,
    bump_candidate,
    display_reason,
    dv_evict_reason,
    update_dv_floor_timer,
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
    #scanner-table {
        width: 1fr;
        border: solid $primary;
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
        self._dv_cooldown: dict[str, datetime] = {}  # symbol -> when it was DV-evicted/bumped
        self._row_order: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield Static(id="scanner-table")
            yield TunablesPanel(self.tunables, id="tunables-panel")
        yield Footer()

    async def connect(self) -> None:
        await self.ib.connectAsync(config.IB_HOST, config.IB_PORT, clientId=config.IB_CLIENT_ID)
        log.info("Connected to IB at %s:%s (clientId=%s)", config.IB_HOST, config.IB_PORT, config.IB_CLIENT_ID)

    async def on_mount(self) -> None:
        await self.connect()
        self.float_map = floatref.load()
        self._reconfigure_for_session(current_session())
        self.scanner_mgr.on_update(self._on_scan_update)
        self._render()
        self.set_interval(config.DISPLAY_REFRESH_SEC, self._tick)

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
        )
        self.query_one("#scanner-table", Static).update(table)

    def _waiting_for_slot_count(self) -> int:
        """Symbols that have cleared persistence but hold no live-symbol slot
        right now -- pool full with nothing bump-eligible, or sitting out a
        DV-eviction re-entry cooldown. Recomputed fresh each render rather
        than tracked incrementally, so it self-corrects if a candidate drops
        out of the top-N while queued instead of ever getting a slot."""
        return sum(
            1 for sym in self._pending_hits
            if sym not in self.states
            and self.persistence.state_for(sym).streak >= self.tunables.persistence_required
        )

    # -- session lifecycle -------------------------------------------------

    def _reconfigure_for_session(self, session: Session) -> None:
        log.info("Session changed: %s -> %s", self.session.value, session.value)
        self.session = session
        for symbol in list(self.states.keys()):
            self._remove_symbol(symbol)
        self._dv_cooldown.clear()
        self.persistence = PersistenceTracker(self.tunables)
        self.scanner_mgr.start(session)

    # -- scanner callback (fires on ScanDataList.updateEvent) --------------

    def _on_scan_update(self, hits: dict) -> None:
        self._pending_hits = hits
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
        retry. When the pool is full, bumps the weakest dollar-volume squatter
        (see filters.bump_candidate) rather than turning the newcomer away."""
        if symbol in self.states:
            if hit is not None:
                self.states[symbol].scan_rank = hit.rank
            self._logged_no_slot.discard(symbol)
            return
        if hit is None or symbol in self._dv_cooldown:
            return
        if len(self.states) >= config.MAX_LIVE_SYMBOLS:
            now = datetime.now(config.TZ)
            bump = bump_candidate(self.states, self.tunables, now)
            if bump is None:
                self._log_no_slot(symbol)
                return
            dv = bump.dollar_volume
            dv_txt = f"{dv:,.0f}" if dv is not None else "unknown"
            log.info(
                "Bumped %s (dollar volume %s below floor %s) to admit %s; re-entry barred for %.0fs",
                bump.symbol, dv_txt, f"{config.MIN_DOLLAR_VOLUME:,}", symbol,
                self.tunables.dv_reentry_cooldown_sec,
            )
            self._remove_symbol(bump.symbol)
            self._dv_cooldown[bump.symbol] = now
        asyncio.create_task(self._add_symbol(hit))

    def _log_no_slot(self, symbol: str) -> None:
        if symbol in self._logged_no_slot:
            return  # already logged for this symbol; avoid spamming every tick
        log.info(
            "%s qualified but no live-symbol slot free (%d/%d in use, none bump-eligible: "
            "all clear the $ floor, are warming up, or spiked recently)",
            symbol, len(self.states), config.MAX_LIVE_SYMBOLS,
        )
        self._logged_no_slot.add(symbol)

    def _log_filter_transitions(self) -> None:
        """Logs, once per state change, why a live-subscribed symbol is or isn't
        clearing display.render's row filter -- otherwise a symbol can spike
        heavily under the hood and stay invisible with no trace in the log."""
        for symbol, state in self.states.items():
            if state.tick.last is None:
                continue  # no live tick yet, nothing meaningful to report
            reason = display_reason(state)
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
        for sym, evicted_at in list(self._dv_cooldown.items()):
            if (now - evicted_at).total_seconds() >= self.tunables.dv_reentry_cooldown_sec:
                del self._dv_cooldown[sym]
        for symbol in list(self.states.keys()):
            state = self.states[symbol]
            if self.persistence.state_for(symbol).streak <= 0:
                self._remove_symbol(symbol)
                continue
            if spikes.ready_to_evict(state.spike, self.tunables, now):
                self._remove_symbol(symbol)
                continue
            update_dv_floor_timer(state, now)
            reason = dv_evict_reason(state, self.tunables, now)
            if reason is not None:
                log.info(
                    "Evicted %s: %s; re-entry barred for %.0fs",
                    symbol, reason, self.tunables.dv_reentry_cooldown_sec,
                )
                self._remove_symbol(symbol)
                self._dv_cooldown[symbol] = now

    # -- per-symbol lifecycle ----------------------------------------------

    async def _add_symbol(self, hit) -> None:
        if hit.symbol in self.states or len(self.states) >= config.MAX_LIVE_SYMBOLS:
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

        def on_tick(t: Ticker, _state=state):
            self._apply_tick(_state, t)

        ticker.updateEvent += on_tick
        state._ticker = ticker  # keep a reference for cleanup

        asyncio.create_task(self._load_baseline(state))

    async def _load_baseline(self, state: SymbolState) -> None:
        baseline = await rvol.build_baseline(self.ib, state.symbol, self.session)
        if state.symbol in self.states:
            state.baseline = baseline

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
            state.tick.volume = t.volume

        halted = getattr(t, "halted", None)
        if halted is not None and not _isnan(halted):
            update_halt_state(state.halt, halted)

    def _apply_float(self, state: SymbolState) -> None:
        shares = self.float_map.get(state.symbol.upper())
        state.float_known = shares is not None
        state.float_shares = shares

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
