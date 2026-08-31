"""
Tier-1 snapshot scorer: our own ranking over the scan lists' candidates.

The scan lists (scanner.py) are treated as membership only -- every symbol on
any list joins the pool, and IB's rank is never used for scoring (it proved
misleading: HOT_BY_VOLUME ranks whole-day cumulative volume, burying real
afterhours movers; HIGH_OPEN_GAP freezes at the 9:30 open). Instead, the pool
is batch-snapshotted (`reqTickersAsync`, ~1.4s per 20 symbols measured live)
every SCORE_REFRESH_SEC, and signals are computed from OUR readings:

- move%/min: price change between sweeps -- true short-window velocity
- $/min: volume delta between sweeps x price -- short-window dollar flow,
  immune to the whole-day-cumulative contamination in IB's volume scans
- gap% vs prior close (context only; includes earlier sessions' move)
- spread% from the snapshot quote

See config.py's scorer section for the score formula, weights, and worked
numeric examples. This slice is OBSERVATION ONLY: ranked() feeds a second
display table so the ranking can be watched side by side against the current
persistence-gated pipeline before it is ever allowed to drive admission.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from ib_async import IB, Stock

from . import config

log = logging.getLogger(__name__)


def _clean(v) -> float | None:
    """IB pads unavailable fields with None or NaN; normalize both to None."""
    if v is None:
        return None
    try:
        if math.isnan(v):
            return None
    except TypeError:
        return None
    return float(v)


def compute_score(
    move_pct_per_min: float,
    dollar_per_min: float,
    gap_pct: float | None,
    spread_pct: float | None,
) -> float:
    """Pure scoring function -- see config.py for weights and worked examples.

    gap/spread may be unknown early in a symbol's life; unknown gap earns no
    bonus and unknown spread costs no penalty rather than blocking a score.
    """
    score = config.SCORE_W_MOVE * move_pct_per_min
    score += config.SCORE_W_DOLLAR * math.log10(
        max(dollar_per_min, 1.0) / config.SCORE_DOLLAR_REF_PER_MIN
    )
    if gap_pct is not None:
        score += config.SCORE_W_GAP * min(max(gap_pct, 0.0) / config.SCORE_GAP_CAP_PCT, 1.0)
    if spread_pct is not None:
        score -= config.SCORE_W_SPREAD * max(spread_pct - config.SCORE_SPREAD_FREE_PCT, 0.0)
    return score


@dataclass
class Reading:
    ts: float      # epoch seconds
    last: float
    volume: float  # IB cumulative day volume at this instant


@dataclass
class ScoreRow:
    symbol: str
    score: float
    move_pct_per_min: float
    dollar_per_min: float
    gap_pct: float | None
    spread_pct: float | None
    fast_lane: bool  # move%/min over SCORE_FAST_MOVE_PCT_PER_MIN right now


class SnapshotScorer:
    """Owns the candidate pool, sweep loop state, and per-symbol reading history."""

    def __init__(self, ib: IB):
        self.ib = ib
        self._pool: dict[str, float] = {}          # symbol -> last seen on any scan list (epoch)
        self._contracts: dict[str, Stock] = {}     # qualified-contract cache
        self._unqualifiable: set[str] = set()      # IB couldn't qualify; don't retry this run
        self._history: dict[str, deque[Reading]] = {}
        self._close: dict[str, float] = {}         # prior close per symbol
        self._spread: dict[str, float] = {}        # latest snapshot spread% per symbol
        self._sweeping = False
        self.last_sweep_at: datetime | None = None
        self._load_state()

    # -- pool membership ----------------------------------------------------

    def update_pool(self, symbols) -> None:
        """Feed the merged scan hits (ALL rows -- membership, not top-N)."""
        now = time.time()
        for sym in symbols:
            self._pool[sym] = now
        expired = [s for s, seen in self._pool.items() if now - seen > config.SCORER_POOL_TTL_SEC]
        for sym in expired:
            del self._pool[sym]
            self._history.pop(sym, None)
        if len(self._pool) > config.SCORER_POOL_MAX:
            keep = sorted(self._pool, key=self._pool.get, reverse=True)[: config.SCORER_POOL_MAX]
            dropped = set(self._pool) - set(keep)
            self._pool = {s: self._pool[s] for s in keep}
            for sym in dropped:
                self._history.pop(sym, None)

    @property
    def pool_size(self) -> int:
        return len(self._pool)

    # -- sweep --------------------------------------------------------------

    async def sweep(self) -> None:
        """One batch-snapshot pass over the pool. Re-entry-guarded so a slow
        sweep can't stack behind the interval timer."""
        if self._sweeping or not self.ib.isConnected():
            return
        self._sweeping = True
        try:
            await self._sweep_inner()
        except Exception:
            log.exception("Scorer sweep failed")
        finally:
            self._sweeping = False

    async def _sweep_inner(self) -> None:
        symbols = [s for s in self._pool if s not in self._unqualifiable]
        if not symbols:
            return

        new = [s for s in symbols if s not in self._contracts]
        if new:
            candidates = [Stock(s, "SMART", "USD") for s in new]
            try:
                qualified = await self.ib.qualifyContractsAsync(*candidates)
            except Exception:
                log.exception("Scorer contract qualification failed for %d symbols", len(new))
                qualified = []
            got = {c.symbol: c for c in qualified if c.conId}
            self._contracts.update(got)
            for sym in new:
                if sym not in got:
                    self._unqualifiable.add(sym)
                    log.info("Scorer: %s unqualifiable, excluded from sweeps this run", sym)

        contracts = [self._contracts[s] for s in symbols if s in self._contracts]
        now = time.time()
        filled = 0
        for i in range(0, len(contracts), config.SCORER_SNAPSHOT_CHUNK):
            chunk = contracts[i : i + config.SCORER_SNAPSHOT_CHUNK]
            try:
                tickers = await asyncio.wait_for(self.ib.reqTickersAsync(*chunk), timeout=25)
            except Exception:
                log.warning("Scorer snapshot chunk of %d timed out/failed; skipping", len(chunk))
                continue
            for t in tickers:
                filled += self._record(t.contract.symbol, t, now)

        self._prune_history(now)
        self.last_sweep_at = datetime.now(config.TZ)
        log.info("Scorer sweep: %d/%d snapshots recorded (pool %d)", filled, len(contracts), len(self._pool))
        self._save_state()

    def _record(self, symbol: str, ticker, ts: float) -> bool:
        last, volume = _clean(ticker.last), _clean(ticker.volume)
        if last is None or last <= 0 or volume is None:
            return False  # no usable print in this snapshot; keep prior history
        self._history.setdefault(symbol, deque()).append(Reading(ts=ts, last=last, volume=volume))
        close = _clean(ticker.close)
        if close is not None and close > 0:
            self._close[symbol] = close
        bid, ask = _clean(ticker.bid), _clean(ticker.ask)
        if bid is not None and ask is not None and bid > 0 and ask > bid >= 0:
            mid = (bid + ask) / 2
            self._spread[symbol] = (ask - bid) / mid * 100.0
        return True

    def _prune_history(self, now: float) -> None:
        for readings in self._history.values():
            while readings and now - readings[0].ts > config.SCORER_HISTORY_KEEP_SEC:
                readings.popleft()

    # -- scoring ------------------------------------------------------------

    def _score_symbol(self, symbol: str) -> ScoreRow | None:
        readings = self._history.get(symbol)
        if not readings or len(readings) < 2:
            return None
        oldest, newest = readings[0], readings[-1]
        span = newest.ts - oldest.ts
        if span < config.SCORER_MIN_SPAN_SEC or oldest.last <= 0:
            return None
        mins = span / 60.0
        move_pct_per_min = (newest.last - oldest.last) / oldest.last * 100.0 / mins
        # Volume delta clamped at 0: IB's day-cumulative tick should never
        # decrease intraday, but a stale/corrected snapshot shouldn't produce
        # a negative flow reading.
        dollar_per_min = max(newest.volume - oldest.volume, 0.0) * newest.last / mins

        close = self._close.get(symbol)
        gap_pct = (newest.last - close) / close * 100.0 if close else None
        spread_pct = self._spread.get(symbol)

        return ScoreRow(
            symbol=symbol,
            score=compute_score(move_pct_per_min, dollar_per_min, gap_pct, spread_pct),
            move_pct_per_min=move_pct_per_min,
            dollar_per_min=dollar_per_min,
            gap_pct=gap_pct,
            spread_pct=spread_pct,
            fast_lane=move_pct_per_min >= config.SCORE_FAST_MOVE_PCT_PER_MIN,
        )

    def ranked(self) -> list[ScoreRow]:
        rows = [r for s in self._pool if (r := self._score_symbol(s)) is not None]
        rows.sort(key=lambda r: r.score, reverse=True)
        return rows

    # -- state persistence (restart resilience, same trading day only) ------

    def _today(self) -> str:
        return datetime.now(config.TZ).date().isoformat()

    def _save_state(self) -> None:
        try:
            state = {
                "date": self._today(),
                "close": self._close,
                "history": {
                    sym: [[r.ts, r.last, r.volume] for r in readings]
                    for sym, readings in self._history.items()
                },
            }
            with open(config.SCORER_STATE_FILE, "w") as fh:
                json.dump(state, fh)
        except Exception:
            log.exception("Scorer state save failed (non-fatal)")

    def _load_state(self) -> None:
        try:
            with open(config.SCORER_STATE_FILE) as fh:
                state = json.load(fh)
        except FileNotFoundError:
            return
        except Exception:
            log.exception("Scorer state load failed (non-fatal); starting cold")
            return
        if state.get("date") != self._today():
            return  # readings are junk across days (volume tick resets overnight)
        self._close = {s: float(v) for s, v in state.get("close", {}).items()}
        for sym, rows in state.get("history", {}).items():
            self._history[sym] = deque(Reading(ts=r[0], last=r[1], volume=r[2]) for r in rows)
        if self._history:
            log.info("Scorer resumed same-day history for %d symbols", len(self._history))
