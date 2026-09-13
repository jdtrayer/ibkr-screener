"""render_news_feed's symbol_filter label -- the caller (app.py) does the
actual filtering via NewsTracker.feed(symbol=...); this only covers the
title/caption text that tells the user a filter is active."""
from datetime import datetime, timezone

from momentum_scanner.display import render_news_feed


def test_no_filter_shows_default_title():
    table = render_news_feed([], symbol_filter=None)
    assert table.title == "News Feed"
    assert table.caption == "No headlines yet today"


def test_symbol_filter_shows_symbol_in_title_and_empty_caption():
    table = render_news_feed([], symbol_filter="AAPL")
    assert table.title == "News Feed — AAPL (select row again to clear)"
    assert table.caption == "No headlines yet today for AAPL"


def test_symbol_filter_with_rows_has_no_caption():
    feed = [(datetime.now(timezone.utc), "AAPL", "Some headline")]
    table = render_news_feed(feed, symbol_filter="AAPL")
    assert table.title == "News Feed — AAPL (select row again to clear)"
    assert table.caption is None
