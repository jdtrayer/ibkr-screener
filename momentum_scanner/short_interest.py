"""
Short interest: symbol -> most recent FINRA settlement short position, %
of float, and days to cover.

IBKR has no path to this either (same Reuters Fundamentals gate
floatref.py/country.py hit), and Nasdaq's own free per-symbol short-interest
endpoint is confirmed live to be Nasdaq-listed only (blank for NYSE names
like GME). Equibles (api.equibles.com) resells the same underlying FINRA
data for both exchanges via a REST API, free tier 100 req/day -- requires
config.EQUIBLES_API_KEY (./secrets.json, gitignored). With no key configured
this fails open (returns None for every symbol) rather than erroring, same
as every other optional data source in this app.

Equibles returns share counts and days-to-cover only, no %-of-float --
computed here against floatref.get_float(), so a result here inherits that
lookup's own gaps/staleness on top of the short-interest data's own.

The underlying FINRA number only updates twice a month (settlement dates
the 15th/end of month, reported ~2 weeks later) regardless of source --
cached per symbol for SHORT_INTEREST_CACHE_MAX_AGE_DAYS, set to roughly that
cadence rather than pretending this can be fresher than the data allows.
Negative results (symbol has no coverage) are cached too, same reasoning as
floatref.get_float, so an uncovered symbol isn't re-queried every admission.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from . import config, floatref

log = logging.getLogger(__name__)

_URL = "https://api.equibles.com/v1/stocks/{symbol}/short-interest"

_cache: dict[str, dict] | None = None  # symbol -> {"result": dict|None, "fetched_at": iso str}
_cache_lock = asyncio.Lock()
_fetch_semaphore = asyncio.Semaphore(3)  # a handful of concurrent lookups is plenty


def _cache_path() -> Path:
    path = Path(config.SHORT_INTEREST_CACHE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_cache_from_disk() -> dict[str, dict]:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        log.exception("Failed to read short interest cache %s", path)
        return {}


def _save_cache_to_disk(cache: dict[str, dict]) -> None:
    try:
        _cache_path().write_text(json.dumps(cache))
    except Exception:
        log.exception("Failed to write short interest cache %s", _cache_path())


def _fetch_blocking(symbol: str) -> dict | None:
    """Runs in a worker thread -- synchronous HTTP call. Returns None on a
    request failure OR when Equibles simply has no data for this symbol
    (too new, delisted, no coverage) -- both cached the same as a real
    "nothing here" result."""
    try:
        r = requests.get(
            _URL.format(symbol=symbol),
            headers={"Authorization": f"Bearer {config.EQUIBLES_API_KEY}"},
            params={"limit": 1},
            timeout=10,
        )
        r.raise_for_status()
        rows = r.json().get("data") or []
    except Exception:
        log.exception("Equibles short-interest fetch failed for %s", symbol)
        return None
    if not rows:
        return None
    latest = rows[0]
    return {
        "settlement_date": latest["settlementDate"],
        "shares": latest["shortPosition"],
        "days_to_cover": latest.get("daysToCover"),
    }


async def _with_pct_float(result: dict, symbol: str) -> dict:
    out = dict(result)
    settlement = date.fromisoformat(result["settlement_date"])
    out["age_days"] = (date.today() - settlement).days
    float_shares = await floatref.get_float(symbol)
    out["pct_float"] = (result["shares"] / float_shares * 100) if float_shares else None
    return out


async def get_short_interest(symbol: str) -> dict | None:
    """{"shares", "settlement_date", "days_to_cover", "age_days", "pct_float"}
    for symbol's most recent FINRA settlement, or None (no API key
    configured, Equibles has no coverage for it, or the fetch failed).
    pct_float is None if floatref.get_float() has no float data either."""
    global _cache
    if not config.EQUIBLES_API_KEY:
        return None
    symbol = symbol.upper()

    async with _cache_lock:
        if _cache is None:
            _cache = _load_cache_from_disk()
        entry = _cache.get(symbol)
        if entry is not None:
            fetched_at = datetime.fromisoformat(entry["fetched_at"])
            age_days = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 86400.0
            if age_days < config.SHORT_INTEREST_CACHE_MAX_AGE_DAYS:
                result = entry["result"]
                return await _with_pct_float(result, symbol) if result else None

    async with _fetch_semaphore:
        result = await asyncio.to_thread(_fetch_blocking, symbol)

    async with _cache_lock:
        _cache[symbol] = {"result": result, "fetched_at": datetime.now(timezone.utc).isoformat()}
        _save_cache_to_disk(_cache)

    return await _with_pct_float(result, symbol) if result else None
