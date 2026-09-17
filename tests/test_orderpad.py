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
from momentum_scanner.sizing import round_to_tick
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
        round_to_tick(snapshot.price + snapshot.drift_allowance)
    )
    assert plan.entry_limit > snapshot.price  # marketable: crosses the spread


def test_plan_stop_and_target_are_the_no_slippage_preview(tunables):
    """plan.stop_price/target_price are what recompute_bracket_exit would
    produce if the fill lands exactly on the armed price -- planned, not
    what's actually sent to IB (that's recomputed from the real fill, see
    app.py's _on_pad_parent_fill)."""
    snapshot = orderpad.build_snapshot(make_state(), tunables, NOW)
    plan = orderpad.bracket_plan(snapshot)
    preview = orderpad.recompute_bracket_exit(snapshot.price, snapshot)
    assert plan.stop_price == preview.stop_limit_price
    assert plan.stop_trigger_price == preview.stop_trigger_price
    assert plan.target_price == preview.target_price
    assert plan.quantity == snapshot.shares


def test_oca_group_is_unique_per_arm(tunables):
    a = orderpad.build_snapshot(make_state(symbol="AAA"), tunables, NOW)
    b = orderpad.build_snapshot(make_state(symbol="BBB"), tunables, NOW)
    assert orderpad.bracket_plan(a).oca_group != orderpad.bracket_plan(b).oca_group


# -- recompute_bracket_exit --------------------------------------------------
# The 2026-09-17 fix: the stop LIMIT is the real, risk-math stop
# (fill_price - stop_distance); the TRIGGER sits above it so the order wakes
# early. Previously inverted -- the computed stop was used as the trigger
# with the limit a flat $0.02 below it, which realized worse-than-risk_usd
# losses on every stopped trade (see config.STOP_TRIGGER_LEAD_PCT).


def test_stop_limit_is_the_real_stop_and_trigger_sits_above_it(tunables):
    snapshot = orderpad.build_snapshot(make_state(last=4.12), tunables, NOW)
    exit_prices = orderpad.recompute_bracket_exit(4.12, snapshot)
    assert exit_prices.stop_limit_price == pytest.approx(4.12 - snapshot.stop_distance, abs=0.01)
    assert exit_prices.stop_trigger_price > exit_prices.stop_limit_price
    expected_lead = snapshot.stop_distance * snapshot.stop_trigger_lead_pct
    assert exit_prices.stop_trigger_price == pytest.approx(
        exit_prices.stop_limit_price + expected_lead, abs=0.01
    )


def test_recompute_uses_the_fill_price_not_the_armed_price(tunables):
    """Entry slippage must not silently widen realized risk: the stop has to
    re-anchor to the real fill, not the (possibly stale) armed price."""
    snapshot = orderpad.build_snapshot(make_state(last=2.26), tunables, NOW)
    armed = orderpad.recompute_bracket_exit(2.26, snapshot)
    filled_high = orderpad.recompute_bracket_exit(2.28, snapshot)  # filled 2c above armed
    assert filled_high.stop_limit_price > armed.stop_limit_price
    # The realized distance from the ACTUAL fill to the new stop must still
    # equal stop_distance -- risk_usd protected regardless of where the
    # entry filled.
    assert (2.28 - filled_high.stop_limit_price) == pytest.approx(snapshot.stop_distance, abs=0.01)


def test_recompute_ticks_sub_dollar_names_finer_than_a_penny(tunables):
    state = make_state(last=0.85, bid=0.8495, ask=0.8505, atr=0.0537)
    snapshot = orderpad.build_snapshot(state, tunables, NOW)
    exit_prices = orderpad.recompute_bracket_exit(0.85, snapshot)
    # 0.7963 is not a multiple of $0.01 -- proves the $0.0001 tick was
    # actually applied, not just the general 2-decimal rounding (which would
    # have given 0.80 instead).
    assert exit_prices.stop_limit_price == pytest.approx(0.7963)
    assert round(exit_prices.stop_limit_price, 2) != exit_prices.stop_limit_price


def test_recompute_widens_a_trigger_that_collapses_onto_the_limit(tunables):
    """A near-zero lead can round the trigger onto the exact same tick as
    the limit -- IB requires the trigger to sit outside the limit for a SELL
    STP LMT, so this must widen by one tick rather than submit two equal
    prices."""
    tunables.stop_trigger_lead_pct = 0.0001  # rounds away to nothing
    snapshot = orderpad.build_snapshot(make_state(last=4.12), tunables, NOW)
    exit_prices = orderpad.recompute_bracket_exit(4.12, snapshot)
    assert exit_prices.stop_trigger_price > exit_prices.stop_limit_price
    from momentum_scanner.sizing import tick_size
    assert exit_prices.stop_trigger_price == pytest.approx(
        exit_prices.stop_limit_price + tick_size(exit_prices.stop_limit_price)
    )


def test_recompute_target_uses_fill_price_and_frozen_r_multiple(tunables):
    snapshot = orderpad.build_snapshot(make_state(last=4.12), tunables, NOW)
    exit_prices = orderpad.recompute_bracket_exit(4.14, snapshot)
    assert exit_prices.target_price == pytest.approx(
        4.14 + snapshot.stop_distance * snapshot.nominal_r, abs=0.01
    )
