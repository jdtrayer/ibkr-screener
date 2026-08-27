"""
Session bucketing against America/New_York wall-clock time.

Uses zoneinfo exclusively -- no manual UTC offset arithmetic -- so DST
transitions (spring forward / fall back) are handled correctly by the
platform tz database rather than by us.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
from enum import Enum

from . import config


class Session(str, Enum):
    PREMARKET = "premarket"
    REGULAR = "regular"
    AFTERHOURS = "afterhours"
    CLOSED = "closed"


@dataclass(frozen=True)
class SessionWindow:
    session: Session
    start: dtime | None   # None for CLOSED
    end: dtime | None


def _t(hm: tuple[int, int]) -> dtime:
    return dtime(hour=hm[0], minute=hm[1])


PREMARKET_START = _t(config.PREMARKET_START)
REGULAR_START = _t(config.REGULAR_START)
REGULAR_END = _t(config.REGULAR_END)
AFTERHOURS_END = _t(config.AFTERHOURS_END)


def now_et() -> datetime:
    """Current wall-clock time localized to America/New_York."""
    return datetime.now(config.TZ)


def current_session(now: datetime | None = None) -> Session:
    """
    Bucket `now` (must be tz-aware; defaults to current NY time) into a
    Session. Weekends are always CLOSED. No holiday calendar is applied --
    on a market holiday this will still report a normal weekday session,
    since IBKR's own historical-data responses are the ultimate source of
    truth for whether trading actually happened.
    """
    now = now or now_et()
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return Session.CLOSED

    t = now.timetz().replace(tzinfo=None)

    if PREMARKET_START <= t < REGULAR_START:
        return Session.PREMARKET
    if REGULAR_START <= t < REGULAR_END:
        return Session.REGULAR
    if REGULAR_END <= t < AFTERHOURS_END:
        return Session.AFTERHOURS
    return Session.CLOSED


def session_window(session: Session, on_date: datetime) -> SessionWindow:
    """The (start, end) wall-clock bounds for `session` on the calendar date of `on_date`."""
    if session is Session.PREMARKET:
        return SessionWindow(session, PREMARKET_START, REGULAR_START)
    if session is Session.REGULAR:
        return SessionWindow(session, REGULAR_START, REGULAR_END)
    if session is Session.AFTERHOURS:
        return SessionWindow(session, REGULAR_END, AFTERHOURS_END)
    return SessionWindow(session, None, None)


def minutes_elapsed(session: Session, now: datetime | None = None) -> float:
    """Minutes since the start of `session`'s window, clamped to [0, window length]."""
    now = now or now_et()
    window = session_window(session, now)
    if window.start is None:
        return 0.0

    start_dt = now.replace(hour=window.start.hour, minute=window.start.minute, second=0, microsecond=0)
    end_dt = now.replace(hour=window.end.hour, minute=window.end.minute, second=0, microsecond=0)
    elapsed = (now - start_dt).total_seconds() / 60.0
    window_len = (end_dt - start_dt).total_seconds() / 60.0
    return max(0.0, min(elapsed, window_len))
