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
from .filters import PersistenceTracker, update_halt_state
from .models import SymbolState
from .scanner import ScannerManager
from .session import Session, current_session
from .tunables import Tunables

log = logging.getLogger(__name__)

SESSION_CHECK_EVERY_N_TICKS = int(30 / config.DISPLAY_REFRESH_SEC) or 1
FLOAT_REFRESH_EVERY_N_TICKS = int(60 / config.DISPLAY_REFRESH_SEC) or 1


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
        self._render()

    def _render(self) -> None:
        table = display.render(list(self.states.values()), self.session, self.ib.isConnected(), self.tunables)
        self.query_one("#scanner-table", Static).update(table)

    # -- session lifecycle -------------------------------------------------

    def _reconfigure_for_session(self, session: Session) -> None:
        log.info("Session changed: %s -> %s", self.session.value, session.value)
        self.session = session
        for symbol in list(self.states.keys()):
            self._remove_symbol(symbol)
        self.persistence = PersistenceTracker(self.tunables)
        self.scanner_mgr.start(session)

    # -- scanner callback (fires on ScanDataList.updateEvent) --------------

    def _on_scan_update(self, hits: dict) -> None:
        self._pending_hits = hits
        qualified = self.persistence.update(hits)
        for symbol in qualified:
            if symbol in self.states:
                self.states[symbol].scan_rank = hits[symbol].rank if symbol in hits else self.states[symbol].scan_rank
                continue
            if len(self.states) >= config.MAX_LIVE_SYMBOLS:
                continue  # no room; will retry next cycle after eviction
            hit = hits.get(symbol)
            if hit is None:
                continue
            asyncio.create_task(self._add_symbol(hit))

    def _process_pending_hits(self) -> None:
        # Re-run in case room freed up since the last scan callback.
        if not self._pending_hits:
            return
        qualified = {
            sym for sym in self._pending_hits
            if self.persistence.state_for(sym).streak >= self.tunables.persistence_required
        }
        for symbol in qualified:
            if symbol in self.states or len(self.states) >= config.MAX_LIVE_SYMBOLS:
                continue
            hit = self._pending_hits.get(symbol)
            if hit is not None:
                asyncio.create_task(self._add_symbol(hit))

    def _evict_unqualified(self) -> None:
        now = datetime.now(config.TZ)
        for symbol in list(self.states.keys()):
            state = self.states[symbol]
            if self.persistence.state_for(symbol).streak <= 0:
                self._remove_symbol(symbol)
                continue
            if spikes.ready_to_evict(state.spike, self.tunables, now):
                self._remove_symbol(symbol)

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
