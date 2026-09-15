"""
Intraday ATR (Average True Range) per live symbol, fed by a keepUpToDate
reqHistoricalData subscription -- one 1-min-bar stream opened at admission
and cancelled at removal (see app.py's _try_admit/_remove_symbol), not a
polling loop: after the initial fetch, IB streams bar updates into the same
BarDataList via its own updateEvent, so steady state makes no further calls
at all. Used by sizing.py's compute_sizing() as the ATR leg of the
stop-distance formula.

Deliberately standalone, same style as spikes.py/trend.py -- app.py owns the
IB subscription lifecycle (opening it, wiring BarDataList.updateEvent to
update_atr_state, cancelling it on eviction) and calls into this module for
the actual bar bookkeeping.
"""
from __future__ import annotations

import asyncio

from ib_async import IB

from . import config
from .models import AtrState

_fetch_semaphore = asyncio.Semaphore(config.ATR_FETCH_CONCURRENCY)
_last_fetch_at: float = 0.0
_fetch_lock = asyncio.Lock()


async def start_atr_subscription(ib: IB, contract):
    """
    Opens the keepUpToDate 1-min-bar subscription for `contract`, throttled
    against IB's historical-data pacing limit the same way rvol.py/news.py
    throttle their own historical fetches -- a burst of symbols admitted
    within a couple seconds of each other would otherwise risk a pacing
    violation on the initial fetch. Once this returns, no further calls are
    made for this symbol: IB streams bar updates into the returned
    BarDataList via its own updateEvent (the caller wires that to
    update_atr_state, and calls seed_atr_state once up front to warm up ATR
    from the closed bars this initial fetch already returned, rather than
    waiting for the first live bar close).

    Cancel via ib.cancelHistoricalData(bars) when the symbol is removed.
    """
    global _last_fetch_at
    async with _fetch_semaphore:
        async with _fetch_lock:
            now = asyncio.get_event_loop().time()
            wait = config.ATR_FETCH_MIN_INTERVAL_SEC - (now - _last_fetch_at)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_fetch_at = asyncio.get_event_loop().time()
        return await ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr="1 D",
            barSizeSetting=config.ATR_BAR_SIZE,
            whatToShow="TRADES",
            useRTH=False,
            formatDate=2,
            keepUpToDate=True,
        )


def _fold_closed_bar(atr: AtrState, bar) -> None:
    """Folds one fully-CLOSED bar's true range into `atr`. Idempotent via
    last_bar_time -- safe to call more than once with the same bar."""
    if atr.last_bar_time is not None and bar.date <= atr.last_bar_time:
        return
    if atr.prev_close is None:
        tr = bar.high - bar.low
    else:
        tr = max(bar.high - bar.low, abs(bar.high - atr.prev_close), abs(bar.low - atr.prev_close))
    atr.true_ranges.append(tr)
    while len(atr.true_ranges) > config.ATR_LOOKBACK_BARS:
        atr.true_ranges.popleft()
    atr.prev_close = bar.close
    atr.last_bar_time = bar.date


def seed_atr_state(atr: AtrState, bars) -> None:
    """
    Folds in the already-CLOSED bars from start_atr_subscription's initial
    fetch (everything but the still-forming last bar) so ATR is available
    immediately on admission instead of waiting for the first live bar close
    (up to ATR_BAR_SIZE minutes later). Only the trailing
    ATR_LOOKBACK_BARS+1 closed bars are needed -- the +1 supplies prev_close
    for the oldest kept bar's own true range.
    """
    closed = list(bars)[:-1]
    for bar in closed[-(config.ATR_LOOKBACK_BARS + 1):]:
        _fold_closed_bar(atr, bar)


def update_atr_state(atr: AtrState, bars, has_new_bar: bool) -> None:
    """
    Feed one BarDataList.updateEvent(bars, hasNewBar) here on every update.
    Only hasNewBar=True carries a newly-CLOSED bar: per ib_async's
    historicalDataUpdate wrapper, a fresh bar is appended to the list
    (hasNewBar=True) the instant a new bar STARTS, meaning the previous last
    bar (now bars[-2]) is final and will never be revised again;
    hasNewBar=False just means the still-forming current bar (bars[-1])
    ticked, not true-range material yet.
    """
    if not has_new_bar or len(bars) < 2:
        return
    _fold_closed_bar(atr, bars[-2])


def atr_value(atr: AtrState) -> float | None:
    """Simple average of the last up-to-ATR_LOOKBACK_BARS true ranges, or
    None if no bar has closed yet (fresh admission, or the first
    ATR_BAR_SIZE-minute bar is still in progress) -- sizing.py treats None
    as 0, falling back to the spread floor alone until this warms up."""
    if not atr.true_ranges:
        return None
    return sum(atr.true_ranges) / len(atr.true_ranges)
