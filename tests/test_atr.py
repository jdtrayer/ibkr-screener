"""atr.py's bar bookkeeping (seed_atr_state/update_atr_state/atr_value) is
pure logic over plain bar objects -- no IB connection needed. The actual
reqHistoricalData/keepUpToDate subscription plumbing in atr.py/app.py isn't
covered here; it needs a live TWS/Gateway session to validate (see
testing_approach memory)."""
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from momentum_scanner.atr import atr_value, seed_atr_state, update_atr_state
from momentum_scanner.models import AtrState


@dataclass
class FakeBar:
    date: datetime
    high: float
    low: float
    close: float


def bars_at(*ohlc, start=datetime(2026, 9, 15, 9, 30)):
    """ohlc: sequence of (high, low, close), one per minute starting at `start`."""
    return [FakeBar(date=start + timedelta(minutes=i), high=h, low=l, close=c)
            for i, (h, l, c) in enumerate(ohlc)]


def test_atr_value_none_when_no_bars_closed_yet():
    assert atr_value(AtrState()) is None


def test_seed_atr_state_excludes_still_forming_last_bar():
    # 3 bars total, only the first 2 are "closed" (bars[:-1]).
    bars = bars_at((1.10, 1.00, 1.05), (1.12, 1.02, 1.08), (1.20, 1.15, 1.18))
    state = AtrState()
    seed_atr_state(state, bars)
    # Only 2 closed bars folded: TR1 = high-low (no prev close) = 0.10;
    # TR2 = max(1.12-1.02, |1.12-1.05|, |1.02-1.05|) = max(0.10, 0.07, 0.03) = 0.10
    assert atr_value(state) == pytest.approx((0.10 + 0.10) / 2)


def test_seed_atr_state_keeps_only_lookback_plus_one_tail():
    import momentum_scanner.config as config
    n = config.ATR_LOOKBACK_BARS
    # n+3 closed bars plus one forming bar -- only the trailing n+1 closed
    # bars should ever be considered, and true_ranges itself caps at n.
    ohlc = [(1.0 + 0.01 * i, 1.0, 1.0 + 0.005 * i) for i in range(n + 4)]
    bars = bars_at(*ohlc)
    state = AtrState()
    seed_atr_state(state, bars)
    assert len(state.true_ranges) == n


def test_update_atr_state_ignores_forming_bar_updates():
    bars = bars_at((1.10, 1.00, 1.05))
    state = AtrState()
    update_atr_state(state, bars, has_new_bar=False)
    assert atr_value(state) is None


def test_update_atr_state_folds_newly_closed_bar_on_has_new_bar():
    # First event: bar 0 closes, bar 1 starts forming.
    bars = bars_at((1.10, 1.00, 1.05), (1.12, 1.02, 1.08))
    state = AtrState()
    update_atr_state(state, bars, has_new_bar=True)
    assert len(state.true_ranges) == 1
    assert atr_value(state) == pytest.approx(0.10)  # bar 0: high-low, no prior close

    # Second event: bar 1 closes, bar 2 starts forming.
    bars2 = bars_at((1.10, 1.00, 1.05), (1.12, 1.02, 1.08), (1.20, 1.15, 1.18))
    update_atr_state(state, bars2, has_new_bar=True)
    assert len(state.true_ranges) == 2
    # bar 1 TR = max(1.12-1.02, |1.12-1.05|, |1.02-1.05|) = 0.10
    assert atr_value(state) == pytest.approx((0.10 + 0.10) / 2)


def test_update_atr_state_is_idempotent_on_same_bar():
    bars = bars_at((1.10, 1.00, 1.05), (1.12, 1.02, 1.08))
    state = AtrState()
    update_atr_state(state, bars, has_new_bar=True)
    update_atr_state(state, bars, has_new_bar=True)  # duplicate event, same bars
    assert len(state.true_ranges) == 1


def test_seed_then_update_does_not_double_count_seeded_bar():
    bars = bars_at((1.10, 1.00, 1.05), (1.12, 1.02, 1.08))
    state = AtrState()
    seed_atr_state(state, bars)  # folds bar 0 only (bars[:-1])
    assert len(state.true_ranges) == 1

    # Now bar 0 closes for real via the live event stream, bar 1 starts forming --
    # same bars list ib_async would deliver right after the seed snapshot.
    bars_after_close = bars_at((1.10, 1.00, 1.05), (1.12, 1.02, 1.08), (1.20, 1.15, 1.18))
    update_atr_state(state, bars_after_close, has_new_bar=True)
    assert len(state.true_ranges) == 2  # bar 0 (seeded) + bar 1 (new), not bar 0 twice
