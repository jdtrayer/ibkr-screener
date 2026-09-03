"""SentimentClassifier's wrapping logic around the FinBERT pipeline --
confidence floor and fallback behavior. Does NOT load the real model (would
pull in a ~440MB download and multi-second load, not appropriate for a fast
local suite -- see memory: testing_approach); _pipeline is set directly to a
fake callable instead, since load() is just "call transformers.pipeline()
and stash the result" with nothing else to test."""
import asyncio

from momentum_scanner import config
from momentum_scanner.sentiment import SentimentClassifier


def make_classifier(fake_pipeline):
    clf = SentimentClassifier()
    clf._pipeline = fake_pipeline
    return clf


def test_not_loaded_returns_neutral():
    clf = SentimentClassifier()  # _pipeline is None -- never loaded
    result = asyncio.run(clf.classify("Apple Rises 5% On Strong Earnings"))
    assert result == "neutral"


def test_high_confidence_label_passes_through():
    clf = make_classifier(lambda headline: [{"label": "positive", "score": 0.91}])
    result = asyncio.run(clf.classify("Apple Rises 5% On Strong Earnings"))
    assert result == "positive"


def test_low_confidence_falls_back_to_neutral():
    # Live-observed case: a generic market-roundup headline misclassified
    # "negative" at only 0.53 confidence -- below the floor, must not paint red.
    assert 0.53 < config.NEWS_SENTIMENT_CONFIDENCE_FLOOR
    clf = make_classifier(lambda headline: [{"label": "negative", "score": 0.53}])
    result = asyncio.run(clf.classify("Stock Market Today: Nasdaq Posts Back-To-Back Gains"))
    assert result == "neutral"


def test_score_exactly_at_floor_is_not_discarded():
    floor = config.NEWS_SENTIMENT_CONFIDENCE_FLOOR
    clf = make_classifier(lambda headline: [{"label": "negative", "score": floor}])
    result = asyncio.run(clf.classify("Some headline"))
    assert result == "negative"


def test_pipeline_exception_falls_back_to_neutral():
    def raises(headline):
        raise RuntimeError("boom")

    clf = make_classifier(raises)
    result = asyncio.run(clf.classify("Some headline"))
    assert result == "neutral"


def test_ready_reflects_pipeline_state():
    assert SentimentClassifier().ready is False
    assert make_classifier(lambda h: []).ready is True
