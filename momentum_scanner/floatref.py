"""
Float reference: a manual CSV you maintain yourself, plus an auto-fetched
fallback from Yahoo Finance for anything not in it.

load() is the CSV-only loader (unchanged) -- reloaded from disk each scan
cycle so live edits are picked up without a restart. An entry there always
wins over the auto-fetched value.

get_float() is the fallback: IBKR has no float data on this account (Error
10358 -- no Reuters Fundamentals subscription, confirmed against both
reqFundamentalData and generic tick 258), so this hits Yahoo Finance's
quoteSummary endpoint directly. Deliberately not the yfinance library, which
pulls in ~160MB of pandas/numpy/curl_cffi to expose this one field.

Yahoo's endpoint requires a session cookie + crumb token (its anti-bot
layer) obtained via a small warm-up request; both are cached at module level
and only re-fetched if a request comes back 401 (stale crumb). All of this
is synchronous (requests), so the actual network call runs in a worker
thread via asyncio.to_thread to avoid blocking the Textual event loop.
Results (including "not found") are cached to disk for
FLOAT_CACHE_MAX_AGE_DAYS, one shared file since it's a single scalar per
symbol rather than a curve (contrast rvol.py's per-symbol baseline files).
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import config

log = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_QUOTE_SUMMARY_URL = "https://query1.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
_COOKIE_WARMUP_URL = "https://fc.yahoo.com"

_session: requests.Session | None = None
_crumb: str | None = None

_cache: dict[str, dict] | None = None  # lazily loaded; symbol -> {"float_shares": float|None, "fetched_at": iso str}
_cache_lock = asyncio.Lock()
_fetch_semaphore = asyncio.Semaphore(3)  # a handful of concurrent lookups is plenty; no need to hammer Yahoo


def load() -> dict[str, float]:
    path = Path(config.FLOAT_REFERENCE_FILE)
    if not path.exists():
        return {}
    out: dict[str, float] = {}
    try:
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sym = (row.get("symbol") or "").strip().upper()
                raw = (row.get("float_shares") or "").strip()
                if not sym or not raw:
                    continue
                try:
                    out[sym] = float(raw)
                except ValueError:
                    log.warning("Bad float_shares value for %s: %r", sym, raw)
    except Exception:
        log.exception("Failed to read float reference file %s", path)
    return out


def _get_crumb(session: requests.Session) -> str:
    global _crumb
    if _crumb is None:
        session.get(_COOKIE_WARMUP_URL, timeout=8)  # populates the session's cookie jar
        r = session.get(_CRUMB_URL, timeout=8)
        r.raise_for_status()
        _crumb = r.text.strip()
    return _crumb


def _query_float(symbol: str) -> float | None:
    session = _session
    crumb = _get_crumb(session)
    r = session.get(
        _QUOTE_SUMMARY_URL.format(symbol=symbol),
        params={"modules": "defaultKeyStatistics", "crumb": crumb},
        timeout=8,
    )
    if r.status_code == 401:
        raise PermissionError("stale crumb")
    r.raise_for_status()
    result = r.json().get("quoteSummary", {}).get("result")
    if not result:
        return None
    raw = result[0].get("defaultKeyStatistics", {}).get("floatShares", {}).get("raw")
    return float(raw) if raw is not None else None


def _fetch_float_blocking(symbol: str) -> float | None:
    """Runs in a worker thread -- these are synchronous HTTP calls."""
    global _session, _crumb
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": _USER_AGENT})
    try:
        return _query_float(symbol)
    except PermissionError:
        # Crumb/cookie went stale -- refresh once and retry.
        _session, _crumb = requests.Session(), None
        _session.headers.update({"User-Agent": _USER_AGENT})
        try:
            return _query_float(symbol)
        except Exception:
            log.exception("Yahoo float lookup failed for %s after crumb refresh", symbol)
            return None
    except Exception:
        log.exception("Yahoo float lookup failed for %s", symbol)
        return None


def _cache_path() -> Path:
    path = Path(config.FLOAT_CACHE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_cache_from_disk() -> dict[str, dict]:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        log.exception("Failed to read float cache %s", path)
        return {}


def _save_cache_to_disk(cache: dict[str, dict]) -> None:
    try:
        _cache_path().write_text(json.dumps(cache))
    except Exception:
        log.exception("Failed to write float cache %s", _cache_path())


async def get_float(symbol: str) -> float | None:
    """
    Float shares for `symbol` from cache if fresh, else fetched from Yahoo
    and cached. Returns None if Yahoo has no float data for it -- that
    result is cached too (same TTL), so a symbol without float data isn't
    re-queried every time it re-qualifies.
    """
    global _cache
    symbol = symbol.upper()

    async with _cache_lock:
        if _cache is None:
            _cache = _load_cache_from_disk()
        entry = _cache.get(symbol)
        if entry is not None:
            fetched_at = datetime.fromisoformat(entry["fetched_at"])
            age_days = (datetime.now(timezone.utc) - fetched_at).total_seconds() / 86400.0
            if age_days < config.FLOAT_CACHE_MAX_AGE_DAYS:
                return entry["float_shares"]

    async with _fetch_semaphore:
        shares = await asyncio.to_thread(_fetch_float_blocking, symbol)

    async with _cache_lock:
        _cache[symbol] = {"float_shares": shares, "fetched_at": datetime.now(timezone.utc).isoformat()}
        _save_cache_to_disk(_cache)
    return shares
