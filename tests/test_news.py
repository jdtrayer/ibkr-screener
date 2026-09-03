"""NewsTracker (backlog #12): dedup, day-scoping, and the client-side
day-boundary filter that pull_sweep depends on -- reqHistoricalNewsAsync's
startDateTime is not reliably honored server-side (confirmed live), so this
filter is the only thing actually enforcing "current trading day only"."""
import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pytest

from momentum_scanner.news import NewsTracker


class FakeIB:
    def isConnected(self):
        return True


@pytest.fixture
def tracker():
    return NewsTracker(FakeIB())


def test_dedup_collapses_whitespace_and_case(tracker):
    assert tracker.record("AAPL", "Apple Rises 5% On Strong Earnings") is True
    assert tracker.record("AAPL", "  apple  rises 5% on strong earnings ") is False
    assert tracker.headlines("AAPL") == ["Apple Rises 5% On Strong Earnings"]


def test_distinct_headlines_both_stored(tracker):
    tracker.record("AAPL", "Apple Rises 5% On Strong Earnings")
    tracker.record("AAPL", "Apple Announces New Product")
    assert len(tracker.headlines("AAPL")) == 2


def test_symbols_with_news_reflects_state(tracker):
    tracker.record("AAPL", "Headline")
    assert tracker.symbols_with_news() == {"AAPL"}


def test_reset_if_new_day_clears_state(tracker):
    tracker.record("AAPL", "Headline")
    tracker._date = date.today() - timedelta(days=1)
    tracker.reset_if_new_day()
    assert tracker.has_news("AAPL") is False
    assert tracker.symbols_with_news() == set()


def test_reset_if_new_day_is_noop_within_same_day(tracker):
    tracker.record("TSLA", "Headline")
    tracker.reset_if_new_day()
    assert tracker.has_news("TSLA") is True


# -- pull_sweep's client-side day-scope filter ------------------------------
# HistoricalNews.time comes back naive-but-UTC (confirmed live by cross-
# checking against a tickNews epoch timestamp for the same article), and
# reqHistoricalNewsAsync's startDateTime is NOT reliably honored server-side
# (confirmed live: a start bound of today's midnight still returned
# headlines from two days prior) -- so this filter is load-bearing.

@dataclass
class FakeItem:
    time: datetime  # naive-UTC, matching real HistoricalNews.time
    headline: str


@dataclass
class FakeContract:
    conId: int


class FakeIBWithNews:
    def __init__(self, items):
        self._items = items

    def isConnected(self):
        return True

    async def reqHistoricalNewsAsync(self, con_id, codes, start, end, total_results):
        return self._items


def test_pull_sweep_drops_stale_headlines_keeps_fresh():
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    old_item = FakeItem(time=now_utc_naive - timedelta(days=2), headline="Old stale headline")
    new_item = FakeItem(time=now_utc_naive - timedelta(minutes=5), headline="Fresh headline today")

    tracker = NewsTracker(FakeIBWithNews([old_item, new_item]))
    tracker._provider_codes = "FAKE"  # bypass the "no providers loaded" early-return

    asyncio.run(tracker.pull_sweep({"XYZ"}, lambda symbol: FakeContract(conId=1)))

    headlines = tracker.headlines("XYZ")
    assert headlines == ["Fresh headline today"]


def test_pull_sweep_skips_symbols_that_already_have_news():
    tracker = NewsTracker(FakeIBWithNews([]))
    tracker._provider_codes = "FAKE"
    tracker.record("XYZ", "Already known headline")

    calls = []

    def contract_for(symbol):
        calls.append(symbol)
        return FakeContract(conId=1)

    asyncio.run(tracker.pull_sweep({"XYZ"}, contract_for))
    assert calls == []  # XYZ already has news -- must not be re-fetched


def test_pull_sweep_skips_symbols_with_no_qualified_contract():
    tracker = NewsTracker(FakeIBWithNews([FakeItem(time=datetime.now(timezone.utc).replace(tzinfo=None), headline="H")]))
    tracker._provider_codes = "FAKE"

    asyncio.run(tracker.pull_sweep({"UNQUALIFIED"}, lambda symbol: None))
    assert tracker.has_news("UNQUALIFIED") is False
