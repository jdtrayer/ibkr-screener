"""
Pure-logic tests for the order pad: snapshot freezing and the fire-time
validation gate. No Tk, no IB -- padwindow.py and the submission path stay on
live scratch testing, same split as the rest of this suite.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from momentum_scanner import config, orderpad
from momentum_scanner.models import SymbolState
from momentum_scanner.tunables import Tunables

NOW = datetime(2026, 9, 16, 10, 30, tzinfo=config.TZ)


def make_state(symbol="TEST", last=10.0, bid=9.98, ask=10.02, atr=0.10, ticked_at=NOW) -> SymbolState:
    state = SymbolState(symbol=symbol)
    state.tick.last = last
    state.tick.bid = bid
    state.tick.ask = ask
    state.tick.updated_at = ticked_at
    # Seed the ATR deque directly rather than through a bar subscription --
    # atr_value() averages true_ranges, so N identical entries average to atr.
    state.atr.true_ranges.extend([atr] * config.ATR_LOOKBACK_BARS)
    return state


@pytest.fixture
def tunables() -> Tunables:
    return Tunables()


# -- build_snapshot ---------------------------------------------------------


def test_snapshot_matches_the_table_sizing(tunables):
    """The pad must never disagree with the row it was armed from."""
    from momentum_scanner.atr import atr_value
    from momentum_scanner.sizing import compute_sizing

    state = make_state()
    snapshot = orderpad.build_snapshot(state, tunables, NOW)
    sizing = compute_sizing(state.tick.last, state.tick, atr_value(state.atr), tunables)

    assert snapshot.shares == sizing.shares
    assert snapshot.stop_price == sizing.stop_price
    assert snapshot.target_price == sizing.target_price
    assert snapshot.stop_in_spreads == sizing.stop_in_spreads
    assert snapshot.effective_r == sizing.effective_r


def test_snapshot_is_none_without_a_price(tunables):
    state = make_state()
    state.tick.last = None
    assert orderpad.build_snapshot(state, tunables, NOW) is None


def test_snapshot_survives_the_symbol_moving(tunables):
    """Frozen means frozen: mutating the state afterwards must not change it."""
    state = make_state(last=10.0)
    snapshot = orderpad.build_snapshot(state, tunables, NOW)
    state.tick.last = 12.0
    assert snapshot.price == 10.0


def test_risk_usd_reflects_floored_shares(tunables):
    """Shares floor to a whole number, so actual risk sits under the request."""
    snapshot = orderpad.build_snapshot(make_state(), tunables, NOW)
    assert snapshot.risk_usd == pytest.approx(snapshot.shares * snapshot.stop_distance)
    assert snapshot.risk_usd <= tunables.risk_usd


def test_non_tradeable_symbol_still_arms(tunables):
    """Arming has to succeed so the pad has somewhere to show the reason."""
    # A $500 stock blows past max_position_usd at any sane share count.
    state = make_state(last=500.0, bid=499.0, ask=501.0, atr=0.01)
    snapshot = orderpad.build_snapshot(state, tunables, NOW)
    assert snapshot is not None
    assert snapshot.non_tradeable_reason is not None


# -- validate_fire ----------------------------------------------------------


def test_fires_when_nothing_has_changed(tunables):
    state = make_state()
    snapshot = orderpad.build_snapshot(state, tunables, NOW)
    assert orderpad.validate_fire(snapshot, state, NOW) is None


def test_refuses_when_the_symbol_left_the_pool(tunables):
    snapshot = orderpad.build_snapshot(make_state(), tunables, NOW)
    reason = orderpad.validate_fire(snapshot, None, NOW)
    assert "left the feed pool" in reason


def test_refuses_a_non_tradable_listed_symbol(tunables):
    state = make_state()
    snapshot = orderpad.build_snapshot(state, tunables, NOW)
    reason = orderpad.validate_fire(snapshot, state, NOW, non_tradable_listed=True)
    assert "non-tradable" in reason


def test_refuses_a_stale_quote(tunables):
    state = make_state()
    snapshot = orderpad.build_snapshot(state, tunables, NOW)
    later = NOW + timedelta(seconds=config.ORDER_PAD_MAX_QUOTE_AGE_SEC + 1)
    reason = orderpad.validate_fire(snapshot, state, later)
    assert "stale" in reason


def test_staleness_is_checked_before_drift(tunables):
    """A dead feed makes the two prices agree perfectly, so drift alone would
    wave through exactly the case the guard exists to catch."""
    state = make_state()
    snapshot = orderpad.build_snapshot(state, tunables, NOW)
    # Price hasn't moved (no drift) but nothing has ticked in a minute.
    reason = orderpad.validate_fire(snapshot, state, NOW + timedelta(seconds=60))
    assert "stale" in reason


def test_refuses_upward_drift_past_the_threshold(tunables):
    state = make_state()
    snapshot = orderpad.build_snapshot(state, tunables, NOW)
    state.tick.last = snapshot.price + snapshot.drift_allowance * 1.5
    state.tick.updated_at = NOW
    reason = orderpad.validate_fire(snapshot, state, NOW)
    assert "drifted up" in reason and "re-arm" in reason


def test_refuses_downward_drift_past_the_threshold(tunables):
    state = make_state()
    snapshot = orderpad.build_snapshot(state, tunables, NOW)
    state.tick.last = snapshot.price - snapshot.drift_allowance * 1.5
    state.tick.updated_at = NOW
    reason = orderpad.validate_fire(snapshot, state, NOW)
    assert "drifted down" in reason


def test_allows_drift_inside_the_threshold(tunables):
    state = make_state()
    snapshot = orderpad.build_snapshot(state, tunables, NOW)
    state.tick.last = snapshot.price + snapshot.drift_allowance * 0.9
    state.tick.updated_at = NOW
    assert orderpad.validate_fire(snapshot, state, NOW) is None


def test_refuses_a_non_tradeable_snapshot(tunables):
    """Shares below minimum / position over cap, evaluated at arm time."""
    state = make_state(last=500.0, bid=499.0, ask=501.0, atr=0.01)
    snapshot = orderpad.build_snapshot(state, tunables, NOW)
    reason = orderpad.validate_fire(snapshot, state, NOW)
    assert reason == snapshot.non_tradeable_reason


def test_drift_allowance_scales_with_stop_distance(tunables):
    """A volatile wide-stop name gets proportionally more room than a tight
    one -- the point of expressing the threshold against stop distance."""
    tight = orderpad.build_snapshot(make_state(atr=0.05), tunables, NOW)
    wide = orderpad.build_snapshot(make_state(atr=0.50), tunables, NOW)
    assert wide.drift_allowance > tight.drift_allowance
    assert wide.drift_allowance == pytest.approx(
        wide.stop_distance * config.ORDER_PAD_MAX_DRIFT_FRACTION
    )


# -- bracket_plan -----------------------------------------------------------


def test_entry_limit_caps_at_the_drift_budget(tunables):
    """The fill can't slip past the same budget the drift check enforces."""
    snapshot = orderpad.build_snapshot(make_state(), tunables, NOW)
    plan = orderpad.bracket_plan(snapshot)
    assert plan.entry_limit == pytest.approx(
        round(snapshot.price + snapshot.drift_allowance, 2)
    )
    assert plan.entry_limit > snapshot.price  # marketable: crosses the spread


def test_plan_carries_the_armed_stop_and_target(tunables):
    snapshot = orderpad.build_snapshot(make_state(), tunables, NOW)
    plan = orderpad.bracket_plan(snapshot)
    assert plan.stop_price == pytest.approx(round(snapshot.stop_price, 2))
    assert plan.target_price == pytest.approx(round(snapshot.target_price, 2))
    assert plan.quantity == snapshot.shares


def test_oca_group_is_unique_per_arm(tunables):
    a = orderpad.build_snapshot(make_state(symbol="AAA"), tunables, NOW)
    b = orderpad.build_snapshot(make_state(symbol="BBB"), tunables, NOW)
    assert orderpad.bracket_plan(a).oca_group != orderpad.bracket_plan(b).oca_group
