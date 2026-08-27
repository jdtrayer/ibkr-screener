"""Shared data structures passed between the scanner, RVOL, filter, and display layers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .session import Session


@dataclass
class RvolBaseline:
    """Empirical expected-cumulative-volume-by-minute curve for one symbol+session."""

    symbol: str
    session: Session
    built_at: datetime
    # bucket_minutes[i] = minutes-since-session-start at the END of bucket i
    bucket_minutes: list[float]
    # cumulative_volume[i] = average cumulative volume across history through bucket i
    cumulative_volume: list[float]
    sample_days: list[int]  # number of historical days contributing to each bucket

    def expected_cumulative_volume(self, minutes_elapsed: float) -> float | None:
        """Interpolate expected cumulative volume at `minutes_elapsed` into the session."""
        if not self.bucket_minutes:
            return None
        # Find the two bracketing buckets and linearly interpolate.
        prev_m, prev_v = 0.0, 0.0
        for m, v, days in zip(self.bucket_minutes, self.cumulative_volume, self.sample_days):
            if days < 1:
                prev_m, prev_v = m, v
                continue
            if minutes_elapsed <= m:
                if m == prev_m:
                    return v
                frac = (minutes_elapsed - prev_m) / (m - prev_m)
                return prev_v + frac * (v - prev_v)
            prev_m, prev_v = m, v
        return prev_v  # past the end of the curve -- extrapolate flat


@dataclass
class PersistenceState:
    streak: int = 0
    last_seen: datetime | None = None
    first_qualified_at: datetime | None = None
    displayed: bool = False


@dataclass
class HaltState:
    is_halted: bool = False
    last_transition_at: datetime | None = None
    last_resume_at: datetime | None = None

    def recently_resumed(self, within_minutes: float) -> bool:
        if self.is_halted or self.last_resume_at is None:
            return False
        age_min = (datetime.now(self.last_resume_at.tzinfo) - self.last_resume_at).total_seconds() / 60.0
        return age_min <= within_minutes


@dataclass
class LiveTick:
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: float | None = None  # cumulative session volume from IB tick 8
    updated_at: datetime | None = None

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return None

    @property
    def spread_pct(self) -> float | None:
        mid = self.mid
        if mid is None or mid <= 0 or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / mid * 100.0


@dataclass
class SymbolState:
    """Everything the app tracks about one candidate symbol."""

    symbol: str
    conid: int | None = None
    scan_rank: int | None = None
    scan_source: str = ""  # which scanCode surfaced it, e.g. "HOT_BY_VOLUME"

    persistence: PersistenceState = field(default_factory=PersistenceState)
    baseline: RvolBaseline | None = None
    tick: LiveTick = field(default_factory=LiveTick)
    halt: HaltState = field(default_factory=HaltState)

    float_shares: float | None = None
    float_known: bool = False

    live_subscribed: bool = False

    @property
    def rvol(self) -> float | None:
        if self.baseline is None or self.tick.volume is None:
            return None
        from .session import minutes_elapsed

        # Use the session the baseline was built for (authoritative), not a
        # fresh wall-clock lookup -- keeps this correct even for the brief
        # window right at a session boundary before state gets torn down.
        mins = minutes_elapsed(self.baseline.session)
        if mins <= 0:
            return None
        expected = self.baseline.expected_cumulative_volume(mins)
        if not expected or expected <= 0:
            return None
        return self.tick.volume / expected

    @property
    def dollar_volume(self) -> float | None:
        if self.tick.last is None or self.tick.volume is None:
            return None
        return self.tick.last * self.tick.volume
