"""
Country reference: symbol -> issuer country, for a short letter-abbreviation
hint in the Flags column (CN, TW, CA, ...). The motivating case is
foreign-domiciled small caps (Chinese ADRs especially) that momentum traders
want a heads-up on at a glance.

IBKR's ContractDetails has no country field -- issuer country is only
exposed via Reuters Fundamentals, a paid add-on this account doesn't have
(same gap floatref.py hits for float shares, via a different alternative
field). Nasdaq's public screener download endpoint
(api.nasdaq.com/api/screener/stocks?download=true) covers the whole
~7000-symbol listed universe in one unauthenticated call and does include a
country column, so that's the source here.

Not authoritative: spot-checked live 2026-09-13, NIO and SE (both Chinese/
Singapore ADRs) come back "United States" from this endpoint. Treat
abbr_for() as a quick heads-up, not ground truth -- fine for the target use
case (a visual nudge to go check), not for anything that gates a decision on
its own.

One bulk fetch rather than per-symbol (unlike floatref.get_float's Yahoo
lookup) -- the whole table is a couple MB and changes rarely (new IPOs), so
it's cached to disk whole and refreshed whole on COUNTRY_CACHE_MAX_AGE_DAYS
expiry rather than incrementally.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import config

log = logging.getLogger(__name__)

_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# "United States" (and unrecognized/blank countries) intentionally map to no
# abbreviation -- most rows are US, so tagging them too would just be noise;
# the point is a heads-up for foreign-domiciled names. Not strict ISO 3166
# throughout -- UK over GB since that's what a trader recognizes faster.
_ABBR = {
    "China": "CN",
    "Hong Kong": "HK",
    "Taiwan": "TW",
    "United Kingdom": "UK",
    "Canada": "CA",
    "Israel": "IL",
    "Germany": "DE",
    "Japan": "JP",
    "South Korea": "KR",
    "Singapore": "SG",
    "Switzerland": "CH",
    "France": "FR",
    "Netherlands": "NL",
    "Ireland": "IE",
    "Australia": "AU",
    "India": "IN",
    "Brazil": "BR",
    "Mexico": "MX",
    "Bermuda": "BM",
    "Cayman Islands": "KY",
    "British Virgin Islands": "VG",
    "Luxembourg": "LU",
    "Spain": "ES",
    "Italy": "IT",
    "Sweden": "SE",
    "Belgium": "BE",
    "Denmark": "DK",
    "Norway": "NO",
    "Finland": "FI",
    "South Africa": "ZA",
    "New Zealand": "NZ",
    "Argentina": "AR",
    "Chile": "CL",
    "Colombia": "CO",
    "Indonesia": "ID",
    "Philippines": "PH",
    "Vietnam": "VN",
    "Thailand": "TH",
    "Malaysia": "MY",
    "Turkey": "TR",
    "Greece": "GR",
    "Portugal": "PT",
    "Austria": "AT",
    "Poland": "PL",
    "Russia": "RU",
    "Monaco": "MC",
    "Jersey": "JE",
    "Guernsey": "GG",
    "Isle of Man": "IM",
    "Panama": "PA",
    "Puerto Rico": "PR",
    "United Arab Emirates": "AE",
}

_cache: dict[str, str] | None = None  # symbol -> country, lazily loaded
_fetched_at: datetime | None = None
_refreshing = False  # re-entry guard -- refresh_if_stale() gets called on every periodic tick


def _cache_path() -> Path:
    path = Path(config.COUNTRY_CACHE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _fetch_blocking() -> dict[str, str] | None:
    """Runs in a worker thread -- synchronous HTTP call. Returns None on
    failure so the caller can fail open (keep serving the existing cache)."""
    try:
        r = requests.get(
            _SCREENER_URL,
            params={"download": "true"},
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            timeout=20,
        )
        r.raise_for_status()
        rows = r.json()["data"]["rows"]
    except Exception:
        log.exception("Nasdaq screener fetch failed")
        return None
    countries: dict[str, str] = {}
    for row in rows:
        sym = (row.get("symbol") or "").strip().upper()
        country = (row.get("country") or "").strip()
        if sym and country:
            countries[sym] = country
    return countries


def _load_from_disk() -> tuple[dict[str, str], datetime | None]:
    path = _cache_path()
    if not path.exists():
        return {}, None
    try:
        state = json.loads(path.read_text())
        return state.get("countries", {}), datetime.fromisoformat(state["fetched_at"])
    except Exception:
        log.exception("Failed to read country cache %s", path)
        return {}, None


def _save_to_disk(countries: dict[str, str], fetched_at: datetime) -> None:
    try:
        _cache_path().write_text(json.dumps({"fetched_at": fetched_at.isoformat(), "countries": countries}))
    except Exception:
        log.exception("Failed to write country cache %s", _cache_path())


async def refresh_if_stale() -> None:
    """Refetches the whole symbol->country table if the cache is missing or
    older than COUNTRY_CACHE_MAX_AGE_DAYS. Fails open -- a failed fetch keeps
    serving whatever's already cached (even if stale) rather than going
    blank, same fail-open behavior as the scorer's instrument-type check.
    Re-entry guarded (like news.pull_sweep) since this gets called on every
    periodic tick and the staleness check alone doesn't prevent two fetches
    stacking while the first is still in flight."""
    global _cache, _fetched_at, _refreshing
    if _refreshing:
        return
    if _cache is None:
        _cache, _fetched_at = _load_from_disk()
    if _fetched_at is not None:
        age_days = (datetime.now(timezone.utc) - _fetched_at).total_seconds() / 86400.0
        if age_days < config.COUNTRY_CACHE_MAX_AGE_DAYS:
            return
    _refreshing = True
    try:
        fresh = await asyncio.to_thread(_fetch_blocking)
    finally:
        _refreshing = False
    if fresh is None:
        return
    _cache, _fetched_at = fresh, datetime.now(timezone.utc)
    _save_to_disk(_cache, _fetched_at)
    log.info("Country reference refreshed: %d symbols", len(_cache))


def abbr_for(symbol: str) -> str | None:
    """Letter abbreviation for symbol's issuer country (e.g. "CN"), or None
    (unknown, US, or cache not loaded yet -- see module docstring for
    accuracy caveats)."""
    if not _cache:
        return None
    return _ABBR.get(_cache.get(symbol.upper(), ""))
