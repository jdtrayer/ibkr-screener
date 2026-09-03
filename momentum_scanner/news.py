"""
News/catalyst detection (backlog item #12) -- "why is this thing moving?"

Pull-only: reqHistoricalNewsAsync per symbol, on its own periodic sweep,
fully decoupled from the tick/spike pipeline (spike detection reads
Ticker.updateEvent directly in app.py's _apply_tick; nothing here touches
that). A genericTick 292 push path (ib.tickNewsEvent) was tried and
rejected -- confirmed live that on this account it delivers a broad-market
news firehose to every active tick-news subscription rather than
symbol-scoped headlines (a Workhorse Group headline arrived on an
AAPL/TSLA/NVDA-only subscription set), and ib_async's NewsTick carries no
symbol/conId field to attribute it with anyway. reqHistoricalNewsAsync,
which takes a conId directly, is the only mechanism that's actually
symbol-correct on this account's entitlements.

pull_sweep() targets pool symbols that don't have news yet -- once a
symbol gets a headline, it's not pulled again today. Throttled the same
way rvol.py throttles reqHistoricalDataAsync, since this is the same
historical-data API family and subject to the same IB pacing concerns.

Headline text is deduped -- confirmed live that the same story fires once
per matching regional wire (e.g. one Barron's story returned near-identical
entries across DJ-RTA/RTE/RTG/RTPRO), IB does not dedupe this for you.

Scoped to the current trading day only: reset_if_new_day() clears all state
on a calendar-date rollover (America/New_York), not on session transitions
(premarket -> regular -> afterhours) within the same day -- same distinction
already applied to the non-tradable list's expiry semantics. The day
boundary is enforced client-side (against HistoricalNews.time, confirmed
live to be naive-but-UTC) rather than trusted to reqHistoricalNewsAsync's
startDateTime parameter -- confirmed live that IB does not reliably honor
that bound server-side (a start bound of today's midnight still returned
headlines from two days prior).

Each newly-recorded headline is optionally classified (see sentiment.py's
SentimentClassifier, injected rather than imported here so this module
stays free of the heavy torch/transformers import) -- positive/negative/
neutral, stored per symbol from the FIRST successfully-recorded headline in
a given pull (IB returns headlines newest-first, confirmed live, so this is
the most recent one), not overwritten by older headlines recorded in the
same batch.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone

from ib_async import IB

from . import config

log = logging.getLogger(__name__)


class NewsTracker:
    def __init__(self, ib: IB, sentiment=None):
        self.ib = ib
        self._sentiment_clf = sentiment  # SentimentClassifier | None -- optional so tests don't need one
        self._provider_codes: str = ""
        self._headlines: dict[str, list[str]] = {}   # symbol -> deduped headline texts, today only
        self._seen: dict[str, set[str]] = {}          # symbol -> normalized headline texts, for O(1) dedup
        self._sentiment: dict[str, str] = {}           # symbol -> "positive"/"negative"/"neutral", from its most recent headline
        self._date: date = datetime.now(config.TZ).date()
        self._pulling = False
        self._fetch_semaphore = asyncio.Semaphore(config.NEWS_FETCH_CONCURRENCY)
        self._fetch_lock = asyncio.Lock()
        self._last_fetch_at = 0.0

    # -- setup -----------------------------------------------------------------

    async def load_providers(self) -> None:
        try:
            providers = await self.ib.reqNewsProvidersAsync()
        except Exception:
            log.exception("Failed to load news providers -- news detection disabled this session")
            return
        self._provider_codes = "+".join(p.code for p in providers)
        log.info("News providers entitled: %s", self._provider_codes or "(none)")

    # -- read access -------------------------------------------------------------

    def has_news(self, symbol: str) -> bool:
        return bool(self._headlines.get(symbol))

    def symbols_with_news(self) -> set[str]:
        return set(self._headlines)

    def sentiment_map(self) -> dict[str, str]:
        """symbol -> 'positive'/'negative'/'neutral' for every symbol with
        news today -- what display.render_scorer's Flags column reads."""
        return {sym: self.sentiment(sym) for sym in self._headlines}

    def headlines(self, symbol: str) -> list[str]:
        return list(self._headlines.get(symbol, ()))

    def sentiment(self, symbol: str) -> str:
        """'positive' / 'negative' / 'neutral' -- 'neutral' also covers no
        news at all, so callers should gate on has_news() first if they need
        to distinguish "no news" from "neutral news"."""
        return self._sentiment.get(symbol, "neutral")

    # -- record (shared by pull results) ------------------------------------

    def record(self, symbol: str, headline: str) -> bool:
        """Store a headline for symbol if not already seen today. Returns True
        if it was newly recorded (False if it was a dedup no-op)."""
        norm = " ".join(headline.split()).casefold()
        if not norm:
            return False
        seen = self._seen.setdefault(symbol, set())
        if norm in seen:
            return False
        seen.add(norm)
        self._headlines.setdefault(symbol, []).append(headline)
        log.info("%s news: %s", symbol, headline)
        return True

    # -- day scope -------------------------------------------------------------

    def reset_if_new_day(self) -> None:
        today = datetime.now(config.TZ).date()
        if today != self._date:
            log.info("News state cleared for new trading day (%d symbols had news)", len(self._headlines))
            self._headlines.clear()
            self._seen.clear()
            self._sentiment.clear()
            self._date = today

    # -- pull sweep ---------------------------------------------------------

    async def pull_sweep(self, pool_symbols: set[str], contract_for) -> None:
        """One pass over pool_symbols with no news yet. contract_for(symbol) ->
        Stock | None supplies an already-qualified contract (e.g. from the
        scorer's cache); a symbol with no qualified contract yet is skipped
        this cycle rather than re-qualifying here -- the scorer sweeps on a
        faster cadence (SCORE_REFRESH_SEC < NEWS_PULL_INTERVAL_SEC) so it will
        almost always have qualified the symbol first."""
        if self._pulling or not self.ib.isConnected() or not self._provider_codes:
            return
        self._pulling = True
        try:
            await self._pull_inner(pool_symbols, contract_for)
        except Exception:
            log.exception("News pull sweep failed")
        finally:
            self._pulling = False

    async def _pull_inner(self, pool_symbols: set[str], contract_for) -> None:
        self.reset_if_new_day()
        pending = [s for s in pool_symbols if not self.has_news(s)]
        if not pending:
            return

        start = datetime.combine(self._date, datetime.min.time(), tzinfo=config.TZ)
        end = datetime.now(config.TZ)
        # HistoricalNews.time comes back naive-but-UTC (confirmed live by
        # cross-checking against a tickNews epoch timestamp for the same
        # article) -- and confirmed live that reqHistoricalNewsAsync's
        # startDateTime is NOT reliably honored server-side (a start bound of
        # today's midnight still returned headlines from two days prior), so
        # the day-scope has to be enforced client-side against this bound
        # rather than trusted to the query itself.
        start_utc_naive = start.astimezone(timezone.utc).replace(tzinfo=None)
        found = 0
        for symbol in pending:
            contract = contract_for(symbol)
            if contract is None:
                continue
            try:
                items = await self._throttled_fetch(contract.conId, start, end)
            except Exception:
                log.exception("Historical news fetch failed for %s", symbol)
                continue
            symbol_classified = False
            for item in items or ():
                if item.time < start_utc_naive:
                    continue
                if self.record(symbol, item.headline):
                    found += 1
                    if self._sentiment_clf is not None and not symbol_classified:
                        # Headlines come back newest-first (confirmed live), so the
                        # first one recorded here is the most recent -- that's the
                        # sentiment shown, not overwritten by older headlines in
                        # this same batch.
                        self._sentiment[symbol] = await self._sentiment_clf.classify(item.headline)
                        symbol_classified = True
        if found:
            log.info("News pull sweep: %d new headline(s) across %d pending symbol(s)", found, len(pending))

    async def _throttled_fetch(self, con_id: int, start: datetime, end: datetime):
        async with self._fetch_semaphore:
            async with self._fetch_lock:
                now = asyncio.get_event_loop().time()
                wait = config.NEWS_FETCH_MIN_INTERVAL_SEC - (now - self._last_fetch_at)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_fetch_at = asyncio.get_event_loop().time()
            return await self.ib.reqHistoricalNewsAsync(
                con_id, self._provider_codes, start, end, config.NEWS_HEADLINES_PER_PULL
            )
