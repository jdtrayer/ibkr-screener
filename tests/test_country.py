"""country.abbr_for()'s three-way result: a real abbreviation, None (only
for confirmed "United States"), or "??" for everything else we can't put a
short code on (cache not loaded, symbol outside the Nasdaq universe, or a
known country missing from _ABBR) -- see country.py's module docstring for
why blank must never be the fallback for "we don't know"."""
from momentum_scanner import country


def teardown_function():
    country._cache = None  # don't leak state into other test modules


def test_known_foreign_country_returns_abbreviation():
    country._cache = {"NIO": "China"}
    assert country.abbr_for("nio") == "CN"  # case-insensitive lookup


def test_united_states_returns_none():
    country._cache = {"AAPL": "United States"}
    assert country.abbr_for("AAPL") is None


def test_symbol_missing_from_cache_returns_unknown_marker():
    country._cache = {"AAPL": "United States"}
    assert country.abbr_for("ZZZZ") == "??"


def test_country_not_in_abbr_table_returns_unknown_marker():
    country._cache = {"WEIRD": "Antarctica"}
    assert country.abbr_for("WEIRD") == "??"


def test_cache_not_loaded_returns_unknown_marker():
    country._cache = None
    assert country.abbr_for("AAPL") == "??"


def test_cache_loaded_but_empty_returns_unknown_marker():
    country._cache = {}
    assert country.abbr_for("AAPL") == "??"
