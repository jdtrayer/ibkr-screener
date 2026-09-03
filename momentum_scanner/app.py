"""
Orchestrator: wires scanner -> persistence -> RVOL baseline -> live ticks ->
filters -> display together and drives the Textual UI.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

from ib_async import IB, Stock, Ticker
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Static

from . import config, display, floatref, rvol, spikes
from .controls import SymbolActionsPanel, TunablesPanel
from .news import NewsTracker
from .scorer import SnapshotScorer
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
    #symbol-actions-panel {
        height: 14;
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
        self.news = NewsTracker(self.ib)
        self.scorer_admission = ScorerAdmission(self.tunables)
        self._scorer_pending: set[str] = set()  # admit tasks created but not yet landed in self.states
        self._ignored_until: dict[str, datetime] = {}  # symbol -> when its manual non-tradable hold expires
        self._dead_hold: dict[str, datetime] = {}  # symbol -> when its auto dead-hold expires (see filters.is_dead_on_bump)
        self._reconnecting = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with VerticalScroll(id="scanner-table-scroll"):
                yield Static(id="scanner-table")
                yield Static(id="scorer-table")
            with Vertical(id="side-panel"):
                yield TunablesPanel(self.tunables, id="tunables-panel")
                yield SymbolActionsPanel(id="symbol-actions-panel")
        yield Footer()

    async def connect(self) -> None:
        await self.ib.connectAsync(config.IB_HOST, config.IB_PORT, clientId=config.IB_CLIENT_ID)
        log.info("Connected to IB at %s:%s (clientId=%s)", config.IB_HOST, config.IB_PORT, config.IB_CLIENT_ID)

    async def on_mount(self) -> None:
        await self.connect()
        self.ib.disconnectedEvent += self._on_disconnected
        self.float_map = floatref.load()
        self._load_non_tradable()
        await self.news.load_providers()
        self._reconfigure_for_session(current_session())
        self.scanner_mgr.on_update(self._on_scan_update)
        self._render()
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
        asyncio.create_task(self.news.pull_sweep(self.scorer.pool_symbols(), self.scorer.contract_for))

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
            held_count=self._held_count(),
        )
        self.query_one("#scanner-table", Static).update(table)
        self.query_one("#scorer-table", Static).update(
            display.render_scorer(
                self.scorer.ranked(), self.scorer.pool_size, self.scorer.last_sweep_at,
                self.news.symbols_with_news(),
            )
        )
        self.query_one(SymbolActionsPanel).refresh_status(self._ignored_until, datetime.now(config.TZ))

    def _waiting_for_slot_count(self) -> int:
        """Symbols that have cleared persistence and are genuinely blocked by a
        full pool with nothing bump-eligible -- raising max_live_symbols (or a
        weaker occupant showing up) is what unblocks these. Excludes symbols
        sitting out a re-entry cooldown (see _cooldown_wait_count) and symbols
        held out by a dead-hold or the manual non-tradable list (see
        _held_count) -- neither of those is a capacity problem, so lumping
        them in here made an empty pool look full (reproduced live 2026-09-03:
        22/35 slots used, 10 "waiting" -- all 10 were actually dead-held or
        non-tradable, not capacity-blocked). Recomputed fresh each render
        rather than tracked incrementally, so it self-corrects if a candidate
        drops out of the top-N while queued instead of ever getting a slot."""
        return sum(
            1 for sym in self._pending_hits
            if sym not in self.states
            and self.persistence.state_for(sym).streak >= self.tunables.persistence_required
            and sym not in self._slot_cooldown
            and sym not in self._dead_hold
            and sym not in self._ignored_until
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
        dead-hold (filters.is_dead_on_bump) or the manual non-tradable list --
        like cooldown, unrelated to capacity; raising max_live_symbols does
        NOT admit these."""
        return sum(
            1 for sym in self._pending_hits
            if sym not in self.states
            and self.persistence.state_for(sym).streak >= self.tunables.persistence_required
            and sym not in self._slot_cooldown
            and (sym in self._dead_hold or sym in self._ignored_until)
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
        if symbol in self._ignored_until or symbol in self._dead_hold:
            if symbol not in self._logged_no_slot:
                reason = "non-tradable list" if symbol in self._ignored_until else "dead-hold"
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
            bump = bump_candidate(non_scorer_states, self.session, now)
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
                if slot_warmed_up(self.states[s], now) and not spike_held(self.states[s], now)
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

    # -- per-symbol lifecycle ----------------------------------------------

    async def _add_symbol(self, hit) -> None:
        if (
            hit.symbol in self.states
            or len(self.states) >= self.tunables.max_live_symbols
            or hit.symbol in self._ignored_until
            or hit.symbol in self._dead_hold
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
            log.info("%s dropped: excluded instrument type (stockType=%s)", hit.symbol, stock_type)
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
