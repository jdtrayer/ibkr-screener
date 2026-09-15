"""sync_news_table: the news panel's DataTable sync. Covers the title/
caption text (symbol_filter is purely a label -- the caller (app.py) does
the actual filtering via NewsTracker.feed(symbol=...)) and the no-op-when-
nothing-changed behavior that keeps a mid-scroll read from getting yanked
back to the top on every tick (see the docstring on sync_news_table)."""
import asyncio
from datetime import datetime, timezone

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable

from momentum_scanner import display


class _HarnessApp(App):
    def compose(self) -> ComposeResult:
        yield DataTable(id="news-feed-table")

    def on_mount(self) -> None:
        table = self.query_one("#news-feed-table", DataTable)
        table.add_columns(*display.NEWS_TABLE_COLUMNS)


def _sync(app, feed, symbol_filter=None):
    table = app.query_one("#news-feed-table", DataTable)
    display.sync_news_table(table, feed, symbol_filter=symbol_filter)
    return table


def test_no_filter_shows_default_title():
    async def body():
        app = _HarnessApp()
        async with app.run_test():
            table = _sync(app, [], symbol_filter=None)
            assert table.border_title == "News Feed"
            assert table.border_subtitle == "No headlines yet today"

    asyncio.run(body())


def test_symbol_filter_shows_symbol_in_title_and_empty_caption():
    async def body():
        app = _HarnessApp()
        async with app.run_test():
            table = _sync(app, [], symbol_filter="AAPL")
            assert table.border_title == "News Feed — AAPL (select row again to clear)"
            assert table.border_subtitle == "No headlines yet today for AAPL"

    asyncio.run(body())


def test_symbol_filter_with_rows_has_no_caption():
    async def body():
        app = _HarnessApp()
        async with app.run_test():
            feed = [(datetime.now(timezone.utc), "AAPL", "Some headline", "positive")]
            table = _sync(app, feed, symbol_filter="AAPL")
            assert table.border_title == "News Feed — AAPL (select row again to clear)"
            assert table.border_subtitle is None
            assert table.row_count == 1
            assert table.get_cell(list(table.rows)[0], "sym").plain == "AAPL"

    asyncio.run(body())


def test_sentiment_column_shows_icon_for_each_headline():
    async def body():
        app = _HarnessApp()
        async with app.run_test():
            now = datetime.now(timezone.utc)
            feed = [
                (now, "AAPL", "Good news", "positive"),
                (now, "TSLA", "Bad news", "negative"),
            ]
            table = _sync(app, feed)
            row_keys = list(table.rows)
            assert table.get_cell(row_keys[0], "sentiment").plain == "📈"
            assert table.get_cell(row_keys[1], "sentiment").plain == "📉"

    asyncio.run(body())


def test_unchanged_feed_does_not_rebuild_rows():
    """Same feed passed twice -- row keys are identical, so this must be a
    no-op rather than a clear+rebuild (which would reset the DataTable's own
    scroll position on every tick even when nothing actually changed)."""
    async def body():
        app = _HarnessApp()
        async with app.run_test():
            now = datetime.now(timezone.utc)
            feed = [(now, "AAPL", "Some headline", "neutral")]
            table = _sync(app, feed)
            first_key = list(table.rows)[0]

            _sync(app, feed)
            assert list(table.rows)[0] == first_key
            assert table.row_count == 1

    asyncio.run(body())


def test_new_headline_prepended_is_added():
    async def body():
        app = _HarnessApp()
        async with app.run_test():
            now = datetime.now(timezone.utc)
            older = (now, "AAPL", "Older headline", "neutral")
            table = _sync(app, [older])
            assert table.row_count == 1

            newer = (now.replace(microsecond=0), "AAPL", "Newer headline", "positive")
            _sync(app, [newer, older])
            assert table.row_count == 2

    asyncio.run(body())
