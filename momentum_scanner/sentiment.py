"""
Local headline sentiment classification (backlog #12 fast-follow), via
FinBERT (ProsusAI/finbert on HuggingFace) -- deliberately local rather than
an LLM API, avoiding the API-key/billing dependency that got this deferred
in the first place. FinBERT is encoder-only (classification, not
generative), which is exactly the shape of this task: positive/negative/
neutral in, no summarization needed since a headline is already one line.

Confirmed live on this machine (AMD Ryzen 7 3750H, CPU-only torch):
inference is 50-70ms/headline -- trivial at this workload's volume (a
handful of headlines per pull cycle, every NEWS_PULL_INTERVAL_SEC). Model
load is ~5.6s warm (weights cached under ~/.cache/huggingface after the
one-time ~440MB download) -- loaded once, off the event loop
(asyncio.to_thread), so it never blocks app startup or any tick.
Classification itself also runs via asyncio.to_thread since it's
synchronous CPU-bound work that would otherwise stall the Textual UI/tick
loop for the duration of each call.

Also confirmed live: FinBERT is strong on genuine per-company financial
headlines (correctly classified Tesla/Workhorse/Apple/bankruptcy headlines
at 0.90+ confidence) but weaker on the generic market-wide roundup
headlines this account's Dow Jones wires also carry -- one such headline
("Stock Market Today: Nasdaq Posts Back-To-Back Gains") was misclassified
at only 0.53 confidence, barely above the 3-way random baseline. Rather
than trust every label, low-confidence calls fall back to neutral --
see config.NEWS_SENTIMENT_CONFIDENCE_FLOOR.
"""
from __future__ import annotations

import asyncio
import logging

from . import config

log = logging.getLogger(__name__)


class SentimentClassifier:
    def __init__(self):
        self._pipeline = None
        self._loading = False

    async def load(self) -> None:
        if self._pipeline is not None or self._loading:
            return
        self._loading = True
        try:
            self._pipeline = await asyncio.to_thread(self._load_sync)
            log.info("Sentiment model (%s) loaded", config.NEWS_SENTIMENT_MODEL)
        except Exception:
            log.exception("Failed to load sentiment model -- classification disabled this session")
        finally:
            self._loading = False

    @staticmethod
    def _load_sync():
        from transformers import pipeline  # heavy import -- kept out of module scope
        return pipeline("sentiment-analysis", model=config.NEWS_SENTIMENT_MODEL)

    @property
    def ready(self) -> bool:
        return self._pipeline is not None

    async def classify(self, headline: str) -> str:
        """Returns 'positive' / 'negative' / 'neutral'. 'neutral' also covers
        not-yet-loaded, a low-confidence call, or a classification error, so
        callers never need to distinguish "no opinion" from "actually
        neutral" -- both render as the same plain icon."""
        if self._pipeline is None:
            return "neutral"
        try:
            result = await asyncio.to_thread(self._pipeline, headline)
        except Exception:
            log.exception("Sentiment classification failed for headline: %s", headline)
            return "neutral"
        label, score = result[0]["label"], result[0]["score"]
        if score < config.NEWS_SENTIMENT_CONFIDENCE_FLOOR:
            return "neutral"
        return label
