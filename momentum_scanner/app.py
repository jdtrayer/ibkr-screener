"""
Orchestrator: wires scanner -> persistence -> RVOL baseline -> live ticks ->
filters -> display together and drives the rich.Live render loop.
"""
from __future__ import annotations

import asyncio
import logging
import math

from ib_async import IB, Stock, Ticker
from rich.live import Live

from . import config, display, floatref, rvol
from .filters import PersistenceTracker, update_halt_state
from .models import SymbolState
from .scanner import ScannerManager
from .session import Session, current_session

log = logging.getLogger(__name__)

SESSION_CHECK_EVERY_N_TICKS = int(30 / config.DISPLAY_REFRESH_SEC) or 1
FLOAT_REFRESH_EVERY_N_TICKS = int(60 / config.DISPLAY_REFRESH_SEC) or 1


class ScannerApp:
    def __init__(self):
        self.ib = IB()
        self.scanner_mgr = ScannerManager(self.ib)
        self.persistence = PersistenceTracker()
        self.states: dict[str, SymbolState] = {}
        self.float_map: dict[str, float] = {}
        self.session: Session = Session.CLOSED
        self._pending_hits: dict = {}

    async def connect(self) -> None:
        await self.ib.connectAsync(config.IB_HOST, config.IB_PORT, clientId=config.IB_CLIENT_ID)
        log.info("Connected to IB at %s:%s (clientId=%s)", config.IB_HOST, config.IB_PORT, config.IB_CLIENT_ID)

    async def run(self) -> None:
        await self.connect()
        self.float_map = floatref.load()
        self._reconfigure_for_session(current_session())
        self.scanner_mgr.on_update(self._on_scan_update)

        tick = 0
        with Live(display.render([], self.session, self.ib.isConnected()), refresh_per_second=4, screen=False) as live:
            while True:
                await asyncio.sleep(config.DISPLAY_REFRESH_SEC)
                tick += 1

                if tick % SESSION_CHECK_EVERY_N_TICKS == 0:
                    new_session = current_session()
                    if new_session != self.session:
                        self._reconfigure_for_session(new_session)

                if tick % FLOAT_REFRESH_EVERY_N_TICKS == 0:
                    self.float_map = floatref.load()
                    for s in self.states.values():
                        self._apply_float(s)

                self._process_pending_hits()
                self._evict_unqualified()

                live.update(display.render(list(self.states.values()), self.session, self.ib.isConnected()))

    # -- session lifecycle -------------------------------------------------

    def _reconfigure_for_session(self, session: Session) -> None:
        log.info("Session changed: %s -> %s", self.session.value, session.value)
        self.session = session
        for symbol in list(self.states.keys()):
            self._remove_symbol(symbol)
        self.persistence = PersistenceTracker()
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
            if self.persistence.state_for(sym).streak >= config.PERSISTENCE_REQUIRED
        }
        for symbol in qualified:
            if symbol in self.states or len(self.states) >= config.MAX_LIVE_SYMBOLS:
                continue
            hit = self._pending_hits.get(symbol)
            if hit is not None:
                asyncio.create_task(self._add_symbol(hit))

    def _evict_unqualified(self) -> None:
        for symbol in list(self.states.keys()):
            if self.persistence.state_for(symbol).streak <= 0:
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

        ticker = self.ib.reqMktData(contract, genericTickList=config.HALTED_GENERIC_TICK, snapshot=False)
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

    async def disconnect(self) -> None:
        self.scanner_mgr.stop()
        for symbol in list(self.states.keys()):
            self._remove_symbol(symbol)
        self.ib.disconnect()


def _isnan(v) -> bool:
    try:
        return math.isnan(v)
    except TypeError:
        return False
