"""
Builds the empirical time-of-day RVOL baseline for a symbol+session.

Why intraday bars and not just daily volume: a 20-day *daily* volume total
tells you nothing about how volume is distributed across the session --
volume is heavily front-loaded near the open (and, for premarket/afterhours,
concentrated right at the session boundary). Dividing a daily total evenly
by elapsed minutes produces a baseline that's wildly too low early in the
session and wildly too high late, which is exactly the kind of naive RVOL
that generates false spikes. Instead we pull 20 days of 5-min bars (with
extended hours included) and build a real average cumulative-volume-by-
minute-of-session curve, per symbol, per session type.

Baselines are cached to disk (one JSON file per symbol+session) and are
considered fresh for RVOL_CACHE_MAX_AGE_HOURS, so a normal restart during
the trading day does not re-trigger a historical data pull.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from ib_async import IB, Stock

from . import config
from .models import RvolBaseline
from .session import Session, session_window

log = logging.getLogger(__name__)

_fetch_semaphore = asyncio.Semaphore(config.HISTORICAL_FETCH_CONCURRENCY)
_last_fetch_at: float = 0.0
_fetch_lock = asyncio.Lock()


def _cache_path(symbol: str, session: Session) -> Path:
    d = Path(config.RVOL_CACHE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{symbol}_{session.value}.json"


def _load_cached(symbol: str, session: Session) -> RvolBaseline | None:
    path = _cache_path(symbol, session)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text())
        built_at = datetime.fromisoformat(raw["built_at"])
        age_hours = (datetime.now(built_at.tzinfo) - built_at).total_seconds() / 3600.0
        if age_hours > config.RVOL_CACHE_MAX_AGE_HOURS:
            return None
        return RvolBaseline(
            symbol=symbol,
            session=session,
            built_at=built_at,
            bucket_minutes=raw["bucket_minutes"],
            cumulative_volume=raw["cumulative_volume"],
            sample_days=raw["sample_days"],
        )
    except Exception:
        log.exception("Failed to load RVOL cache for %s/%s", symbol, session.value)
        return None


def _save_cache(baseline: RvolBaseline) -> None:
    path = _cache_path(baseline.symbol, baseline.session)
    path.write_text(json.dumps({
        "built_at": baseline.built_at.isoformat(),
        "bucket_minutes": baseline.bucket_minutes,
        "cumulative_volume": baseline.cumulative_volume,
        "sample_days": baseline.sample_days,
    }))


async def _throttled_fetch(ib: IB, contract, **kwargs):
    """Serializes + rate-limits reqHistoricalDataAsync calls across all symbols."""
    global _last_fetch_at
    async with _fetch_semaphore:
        async with _fetch_lock:
            now = asyncio.get_event_loop().time()
            wait = config.HISTORICAL_FETCH_MIN_INTERVAL_SEC - (now - _last_fetch_at)
            if wait > 0:
                await asyncio.sleep(wait)
            _last_fetch_at = asyncio.get_event_loop().time()
        return await ib.reqHistoricalDataAsync(contract, **kwargs)


async def build_baseline(ib: IB, symbol: str, session: Session, use_cache: bool = True) -> RvolBaseline | None:
    """Fetch (or load cached) empirical time-of-day volume baseline for symbol+session."""
    if use_cache:
        cached = _load_cached(symbol, session)
        if cached is not None:
            return cached

    window = session_window(session, datetime.now(config.TZ))
    if window.start is None:
        return None

    contract = Stock(symbol, "SMART", "USD")
    bars = None
    for attempt in range(1, config.RVOL_FETCH_MAX_ATTEMPTS + 1):
        try:
            bars = await _throttled_fetch(
                ib,
                contract,
                endDateTime="",
                durationStr=f"{config.RVOL_LOOKBACK_DAYS} D",
                barSizeSetting=config.RVOL_BAR_SIZE,
                whatToShow="TRADES",
                useRTH=False,
                formatDate=2,  # UTC epoch-based datetime objects
            )
        except Exception:
            log.exception("Historical data request failed for %s (attempt %d/%d)",
                           symbol, attempt, config.RVOL_FETCH_MAX_ATTEMPTS)
            bars = None

        if bars:
            break
        # reqHistoricalDataAsync doesn't raise on timeout -- it logs a warning and
        # returns an empty BarDataList, so "no bars" needs its own retry path too.
        if attempt < config.RVOL_FETCH_MAX_ATTEMPTS:
            log.warning("No historical bars for %s (attempt %d/%d), retrying in %.1fs",
                        symbol, attempt, config.RVOL_FETCH_MAX_ATTEMPTS, config.RVOL_FETCH_RETRY_DELAY_SEC)
            await asyncio.sleep(config.RVOL_FETCH_RETRY_DELAY_SEC)

    if not bars:
        log.warning("Giving up on RVOL baseline for %s after %d attempts",
                     symbol, config.RVOL_FETCH_MAX_ATTEMPTS)
        return None

    window_start = window.start
    window_end = window.end
    bar_minutes = _bar_size_to_minutes(config.RVOL_BAR_SIZE)
    n_buckets = max(1, int(round(((window_end.hour * 60 + window_end.minute) -
                                   (window_start.hour * 60 + window_start.minute)) / bar_minutes)))

    bucket_sum = [0.0] * n_buckets
    bucket_days: dict[int, set] = {i: set() for i in range(n_buckets)}

    for bar in bars:
        ts = bar.date
        if ts.tzinfo is None:
            continue
        local = ts.astimezone(config.TZ)
        t = local.time()
        if not (window_start <= t < window_end):
            continue
        offset_min = (local.hour * 60 + local.minute) - (window_start.hour * 60 + window_start.minute)
        idx = int(offset_min // bar_minutes)
        if idx < 0 or idx >= n_buckets:
            continue
        bucket_sum[idx] += float(bar.volume)
        bucket_days[idx].add(local.date())

    sample_days = [len(bucket_days[i]) for i in range(n_buckets)]
    bucket_avg = [
        (bucket_sum[i] / sample_days[i]) if sample_days[i] >= config.RVOL_MIN_SAMPLE_DAYS else 0.0
        for i in range(n_buckets)
    ]

    bucket_minutes = [(i + 1) * bar_minutes for i in range(n_buckets)]
    cumulative = []
    running = 0.0
    for v in bucket_avg:
        running += v
        cumulative.append(running)

    baseline = RvolBaseline(
        symbol=symbol,
        session=session,
        built_at=datetime.now(config.TZ),
        bucket_minutes=bucket_minutes,
        cumulative_volume=cumulative,
        sample_days=sample_days,
    )
    _save_cache(baseline)
    log.info("Built RVOL baseline for %s/%s from %d bars (attempt %d/%d)",
              symbol, session.value, len(bars), attempt, config.RVOL_FETCH_MAX_ATTEMPTS)
    return baseline


def _bar_size_to_minutes(bar_size: str) -> float:
    value, unit = bar_size.split(" ", 1)
    value = float(value)
    unit = unit.lower()
    if "sec" in unit:
        return value / 60.0
    if "min" in unit:
        return value
    if "hour" in unit:
        return value * 60.0
    raise ValueError(f"Unsupported bar size for RVOL baseline: {bar_size!r}")
