"""
Pure-logic tests for the order pad: live sizing, the fire-time validation
gate, the Empty/Loaded/Armed state machine, and the bracket. No Tk, no IB --
padwindow.py and the submission path stay on live scratch testing, same split
as the rest of this suite.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from momentum_scanner import config, orderpad
from momentum_scanner.models import SymbolState
from momentum_scanner.orderpad import PadController, PadMode
from momentum_scanner.sizing import round_to_tick
from momentum_scanner.tunables import Tunables

NOW = datetime(2026, 9, 16, 10, 30, tzinfo=config.TZ)
MAX_AGE = 2.0
TIMEOUT = 300.0


def make_state(symbol="TEST", last=10.0, bid=9.98, ask=10.02, atr=0.10, ticked_at=NOW) -> SymbolState:
    state = SymbolState(symbol=symbol)
    state.tick.last = last
    state.tick.bid = bid
    state.tick.ask = ask
    state.tick.updated_at = ticked_at
    # Seed the ATR deque directly rather than through a bar subscription --
    # atr_value() averages true_ranges, so N identical entries average to atr.
    if atr is not None:
        state.atr.true_ranges.extend([atr] * config.ATR_LOOKBACK_BARS)
    return state


@pytest.fixture
def tunables() -> Tunables:
    return Tunables()


def sized(state, tunables, **kw) -> orderpad.PadSizing:
    return orderpad.live_sizing(state, tunables, NOW, **kw)


# -- live_sizing -----------------------------------------------------------


def test_sizing_matches_the_table_sizing(tunables):
    """The pad must never do its own arithmetic: shares and the stop
    distance come straight out of compute_sizing, the same function the
    table renders from."""
    from momentum_scanner.atr import atr_value
    from momentum_scanner.sizing import compute_sizing

    state = make_state()
    sizing = sized(state, tunables)
    table = compute_sizing(state.tick.last, state.tick, atr_value(state.atr), tunables)

    assert sizing.shares == table.shares
    assert sizing.stop_distance == table.stop_distance
    assert sizing.stop_in_spreads == table.stop_in_spreads
    assert sizing.effective_r == table.effective_r
    # Displayed stop/target are the tick-rounded prices that would be sent.
    assert sizing.stop_price == pytest.approx(table.stop_price, abs=0.005)
    assert sizing.target_price == pytest.approx(table.target_price, abs=0.005)


def test_sizing_is_none_without_a_price(tunables):
    state = make_state()
    state.tick.last = None
    assert sized(state, tunables) is None


def test_sizing_follows_the_tick_instead_of_freezing(tunables):
    """The whole point of the rework: nothing is captured. The same state,
    ticked, sizes differently -- and an earlier PadSizing is untouched."""
    state = make_state(last=10.0)
    before = sized(state, tunables)
    state.tick.last = 10.50
    after = sized(state, tunables)
    assert before.price == 10.0 and after.price == 10.50
    assert after.stop_price == pytest.approx(before.stop_price + 0.50)
    assert after.target_price == pytest.approx(before.target_price + 0.50)


def test_shares_recalculate_when_the_stop_distance_changes(tunables):
    state = make_state(atr=0.10)
    narrow = sized(state, tunables)
    state.atr.true_ranges.clear()
    state.atr.true_ranges.extend([1.0] * config.ATR_LOOKBACK_BARS)
    wide = sized(state, tunables)
    assert wide.shares < narrow.shares


def test_risk_override_sizes_from_the_pad_risk_without_touching_tunables(tunables):
    state = make_state()
    base = sized(state, tunables)
    doubled = sized(state, tunables, risk_usd=tunables.risk_usd * 2)
    assert doubled.shares == pytest.approx(base.shares * 2, abs=1)
    assert doubled.risk_requested == tunables.risk_usd * 2
    assert tunables.risk_usd == Tunables().risk_usd  # the shared tunable is not written to


def test_exit_prices_are_tick_rounded_and_ordered(tunables):
    sizing = sized(make_state(last=4.12, bid=4.11, ask=4.13), tunables)
    assert sizing.stop_price < sizing.stop_trigger_price < sizing.price < sizing.target_price
    for price in (sizing.stop_price, sizing.stop_trigger_price, sizing.target_price):
        assert price == round_to_tick(price)


def test_risk_usd_reflects_floored_shares(tunables):
    """Shares floor to a whole number, so actual risk sits under the request."""
    sizing = sized(make_state(), tunables)
    assert sizing.risk_usd == pytest.approx(sizing.shares * sizing.stop_distance)
    assert sizing.risk_usd <= tunables.risk_usd


def test_atr_not_warmed_is_non_tradeable(tunables):
    """compute_sizing treats a missing ATR as 0 and sizes from the spread
    floor alone; the pad must not send an order from that."""
    sizing = sized(make_state(atr=None), tunables)
    assert sizing is not None
    assert "ATR" in sizing.non_tradeable_reason
    assert sizing.atr is None


def test_non_tradeable_symbol_still_produces_a_sizing(tunables):
    """The pad has to be able to show WHY a name won't fire."""
    # A $500 stock blows past max_position_usd at any sane share count.
    state = make_state(last=500.0, bid=499.0, ask=501.0, atr=0.01)
    sizing = sized(state, tunables)
    assert sizing is not None and sizing.non_tradeable_reason


def test_no_stop_distance_leaves_the_exit_prices_none(tunables):
    state = make_state(atr=None, bid=None, ask=None)
    sizing = sized(state, tunables)
    assert sizing.stop_price is None and sizing.target_price is None
    assert sizing.stop_trigger_price is None


# -- validate_fire ----------------------------------------------------------


def validate(sizing, state, now=NOW, **kw):
    return orderpad.validate_fire(sizing, state, now, MAX_AGE, **kw)


def test_fires_when_everything_is_good(tunables):
    state = make_state()
    assert validate(sized(state, tunables), state) is None


def test_price_moving_is_not_a_reason_to_refuse(tunables):
    """The drift guard is gone: with live sizing there is no earlier price to
    have drifted from. Whatever the price is now, it is what is sized."""
    state = make_state(last=10.0)
    state.tick.last = 10.40
    assert validate(sized(state, tunables), state) is None


def test_refuses_when_there_is_no_feed(tunables):
    assert "no live feed" in validate(None, None, symbol="AAA")


def test_refuses_a_non_tradable_listed_symbol(tunables):
    state = make_state()
    assert "non-tradable" in validate(sized(state, tunables), state, non_tradable_listed=True)


def test_refuses_a_stale_quote(tunables):
    state = make_state()
    later = NOW + timedelta(seconds=MAX_AGE + 0.5)
    assert "stale" in validate(sized(state, tunables), state, now=later)


def test_a_quote_inside_the_threshold_is_fine(tunables):
    state = make_state()
    just_inside = NOW + timedelta(seconds=MAX_AGE - 0.1)
    assert validate(sized(state, tunables), state, now=just_inside) is None


def test_staleness_threshold_is_the_one_passed_in(tunables):
    state = make_state()
    at_5s = NOW + timedelta(seconds=5)
    assert validate(sized(state, tunables), state, now=at_5s) is not None
    assert orderpad.validate_fire(sized(state, tunables), state, at_5s, 10.0) is None


def test_refuses_before_the_first_quote(tunables):
    state = make_state(ticked_at=None)
    assert "no quote" in validate(sized(state, tunables), state)


def test_refuses_with_no_price(tunables):
    state = make_state()
    assert "no live price" in validate(None, state)


def test_refuses_a_non_tradeable_sizing(tunables):
    state = make_state(last=500.0, bid=499.0, ask=501.0, atr=0.01)
    sizing = sized(state, tunables)
    assert validate(sizing, state) == sizing.non_tradeable_reason


def test_refuses_while_atr_is_warming(tunables):
    state = make_state(atr=None)
    assert "ATR" in validate(sized(state, tunables), state)


def test_staleness_is_checked_before_the_sizing(tunables):
    """A frozen feed makes everything computed from it look fine, so the
    quote's age has to be judged first."""
    state = make_state(atr=None)  # would also fail on ATR
    later = NOW + timedelta(seconds=60)
    assert "stale" in validate(sized(state, tunables), state, now=later)


@pytest.mark.parametrize(
    "override,fragment",
    [
        ({"shares": 0}, "zero shares"),
        ({"stop_price": None}, "no stop/target"),
        ({"target_price": None}, "no stop/target"),
        ({"stop_trigger_price": None}, "no stop/target"),
        ({"stop_price": 10.0}, "not below entry"),
        ({"stop_price": 10.5}, "not below entry"),
        ({"target_price": 10.0}, "not above entry"),
        ({"target_price": 9.0}, "not above entry"),
        ({"stop_trigger_price": 10.0}, "trigger"),
    ],
)
def test_sanity_bounds_refuse_nonsense_numbers(tunables, override, fragment):
    """The last line of defence: even a sizing that claims to be tradeable is
    refused if the numbers don't make sense as a long bracket."""
    state = make_state(last=10.0)
    sizing = replace(sized(state, tunables), **override)
    assert sizing.non_tradeable_reason is None  # so it is the sanity check that fires
    reason = validate(sizing, state)
    assert reason is not None and fragment in reason


# -- bracket_plan -----------------------------------------------------------


def test_entry_limit_caps_at_the_slippage_allowance(tunables):
    """The entry order can't fill further above the price at F4 than the
    allowance -- an order price, not a guard."""
    sizing = sized(make_state(), tunables)
    plan = orderpad.bracket_plan(sizing)
    assert plan.entry_limit == pytest.approx(
        round_to_tick(sizing.price + sizing.stop_distance * config.ORDER_PAD_ENTRY_SLIPPAGE_FRACTION)
    )
    assert plan.entry_limit >= sizing.price


def test_slippage_allowance_scales_with_stop_distance(tunables):
    """A volatile wide-stop name gets proportionally more room than a tight one."""
    tight = sized(make_state(atr=0.05), tunables)
    wide = sized(make_state(atr=0.50), tunables)
    assert wide.entry_slippage_allowance > tight.entry_slippage_allowance


def test_plan_is_exactly_what_the_pad_displayed(tunables):
    """Display == send: the plan carries the sizing's own tick-rounded prices
    and the exit-price formula at the sizing's price gives the same again."""
    sizing = sized(make_state(), tunables)
    plan = orderpad.bracket_plan(sizing)
    preview = orderpad.recompute_bracket_exit(sizing.price, sizing)
    assert plan.stop_price == sizing.stop_price == preview.stop_limit_price
    assert plan.stop_trigger_price == sizing.stop_trigger_price == preview.stop_trigger_price
    assert plan.target_price == sizing.target_price == preview.target_price
    assert plan.quantity == sizing.shares


def test_oca_group_is_unique_per_fire(tunables):
    a = orderpad.live_sizing(make_state(symbol="AAA"), tunables, NOW)
    b = orderpad.live_sizing(make_state(symbol="BBB"), tunables, NOW)
    assert orderpad.bracket_plan(a).oca_group != orderpad.bracket_plan(b).oca_group


def test_oca_group_is_unique_for_two_fires_on_one_symbol_within_a_second(tunables):
    """F4 drops back to Loaded, so F2 + F4 again inside one second is
    possible -- two brackets must not share an OCA group or they cancel each
    other's legs."""
    state = make_state(symbol="AAA")
    first = orderpad.live_sizing(state, tunables, NOW)
    second = orderpad.live_sizing(state, tunables, NOW + timedelta(milliseconds=300))
    assert orderpad.bracket_plan(first).oca_group != orderpad.bracket_plan(second).oca_group


# -- recompute_bracket_exit --------------------------------------------------
# The 2026-09-17 fix: the stop LIMIT is the real, risk-math stop
# (fill_price - stop_distance); the TRIGGER sits above it so the order wakes
# early. Previously inverted -- the computed stop was used as the trigger
# with the limit a flat $0.02 below it, which realized worse-than-risk_usd
# losses on every stopped trade (see config.STOP_TRIGGER_LEAD_PCT).


def test_stop_limit_is_the_real_stop_and_trigger_sits_above_it(tunables):
    sizing = sized(make_state(last=4.12), tunables)
    exit_prices = orderpad.recompute_bracket_exit(4.12, sizing)
    assert exit_prices.stop_limit_price == pytest.approx(4.12 - sizing.stop_distance, abs=0.01)
    assert exit_prices.stop_trigger_price > exit_prices.stop_limit_price
    expected_lead = sizing.stop_distance * sizing.stop_trigger_lead_pct
    assert exit_prices.stop_trigger_price == pytest.approx(
        exit_prices.stop_limit_price + expected_lead, abs=0.01
    )


def test_recompute_uses_the_fill_price_not_the_fire_price(tunables):
    """Entry slippage must not silently widen realized risk: the stop has to
    re-anchor to the real fill, not the price at F4."""
    sizing = sized(make_state(last=2.26), tunables)
    at_fire = orderpad.recompute_bracket_exit(2.26, sizing)
    filled_high = orderpad.recompute_bracket_exit(2.28, sizing)  # filled 2c above
    assert filled_high.stop_limit_price > at_fire.stop_limit_price
    # The realized distance from the ACTUAL fill to the new stop must still
    # equal stop_distance -- risk_usd protected regardless of where the
    # entry filled.
    assert (2.28 - filled_high.stop_limit_price) == pytest.approx(sizing.stop_distance, abs=0.01)


def test_fire_record_is_immune_to_later_atr_changes(tunables):
    """Why the fire record is frozen: a partial-fill re-anchor after the
    position is open must use the stop distance that was fired with, not a
    fresher ATR (which would move the stop under the position)."""
    state = make_state(last=4.12, atr=0.10)
    record = sized(state, tunables)
    state.atr.true_ranges.clear()
    state.atr.true_ranges.extend([0.50] * config.ATR_LOOKBACK_BARS)
    exits = orderpad.recompute_bracket_exit(4.12, record)
    assert (4.12 - exits.stop_limit_price) == pytest.approx(record.stop_distance, abs=0.01)


def test_recompute_ticks_sub_dollar_names_finer_than_a_penny(tunables):
    state = make_state(last=0.85, bid=0.8495, ask=0.8505, atr=0.0537)
    sizing = sized(state, tunables)
    exit_prices = orderpad.recompute_bracket_exit(0.85, sizing)
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
    sizing = sized(make_state(last=4.12), tunables)
    exit_prices = orderpad.recompute_bracket_exit(4.12, sizing)
    assert exit_prices.stop_trigger_price > exit_prices.stop_limit_price
    from momentum_scanner.sizing import tick_size
    assert exit_prices.stop_trigger_price == pytest.approx(
        exit_prices.stop_limit_price + tick_size(exit_prices.stop_limit_price)
    )


def test_recompute_target_uses_fill_price_and_frozen_r_multiple(tunables):
    sizing = sized(make_state(last=4.12), tunables)
    exit_prices = orderpad.recompute_bracket_exit(4.14, sizing)
    assert exit_prices.target_price == pytest.approx(
        4.14 + sizing.stop_distance * sizing.nominal_r, abs=0.01
    )


# -- PadController: Empty / Loaded / Armed ----------------------------------


def test_starts_empty():
    ctl = PadController()
    assert ctl.mode(0, TIMEOUT) is PadMode.EMPTY
    assert ctl.symbol is None


def test_loading_a_symbol_is_loaded_not_armed():
    ctl = PadController()
    assert ctl.load("AAA") is True
    assert ctl.mode(0, TIMEOUT) is PadMode.LOADED


def test_f2_is_a_pure_toggle():
    ctl = PadController()
    ctl.load("AAA")
    assert ctl.toggle_arm(0, TIMEOUT) is PadMode.ARMED
    assert ctl.toggle_arm(1, TIMEOUT) is PadMode.LOADED
    assert ctl.toggle_arm(2, TIMEOUT) is PadMode.ARMED


def test_f2_on_an_empty_pad_does_nothing():
    ctl = PadController()
    assert ctl.toggle_arm(0, TIMEOUT) is PadMode.EMPTY
    assert ctl.mode(0, TIMEOUT) is PadMode.EMPTY


def test_arm_expires_silently_back_to_loaded():
    ctl = PadController()
    ctl.load("AAA")
    ctl.toggle_arm(100, TIMEOUT)
    assert ctl.mode(100 + TIMEOUT - 0.1, TIMEOUT) is PadMode.ARMED
    assert ctl.mode(100 + TIMEOUT, TIMEOUT) is PadMode.LOADED
    assert ctl.symbol == "AAA"  # only the arm lapsed


def test_after_expiry_f2_arms_again():
    ctl = PadController()
    ctl.load("AAA")
    ctl.toggle_arm(0, TIMEOUT)
    assert ctl.toggle_arm(TIMEOUT + 10, TIMEOUT) is PadMode.ARMED


def test_f2_twice_restarts_the_timer():
    ctl = PadController()
    ctl.load("AAA")
    ctl.toggle_arm(0, TIMEOUT)
    ctl.toggle_arm(200, TIMEOUT)   # disarm
    ctl.toggle_arm(210, TIMEOUT)   # arm again: timer restarts here
    assert ctl.mode(210 + TIMEOUT - 1, TIMEOUT) is PadMode.ARMED
    assert ctl.armed_remaining(210 + 100, TIMEOUT) == pytest.approx(TIMEOUT - 100)


def test_symbol_change_always_forces_armed_back_to_loaded():
    ctl = PadController()
    ctl.load("AAA")
    ctl.toggle_arm(0, TIMEOUT)
    assert ctl.load("BBB") is True
    assert ctl.symbol == "BBB"
    assert ctl.mode(1, TIMEOUT) is PadMode.LOADED


def test_reloading_the_same_symbol_keeps_the_arm():
    ctl = PadController()
    ctl.load("AAA")
    ctl.toggle_arm(0, TIMEOUT)
    assert ctl.load("AAA") is False
    assert ctl.mode(1, TIMEOUT) is PadMode.ARMED


def test_clear_forgets_the_symbol_and_the_arm():
    ctl = PadController()
    ctl.load("AAA")
    ctl.toggle_arm(0, TIMEOUT)
    ctl.clear()
    assert ctl.mode(1, TIMEOUT) is PadMode.EMPTY
    ctl.load("AAA")
    assert ctl.mode(2, TIMEOUT) is PadMode.LOADED  # the old arm did not survive


def test_consume_arm_drops_to_loaded_so_a_second_press_cannot_fire():
    ctl = PadController()
    ctl.load("AAA")
    ctl.toggle_arm(0, TIMEOUT)
    ctl.consume_arm()
    assert ctl.mode(1, TIMEOUT) is PadMode.LOADED
    assert ctl.symbol == "AAA"


def test_expire_if_due_reports_once():
    ctl = PadController()
    ctl.load("AAA")
    ctl.toggle_arm(0, TIMEOUT)
    assert ctl.expire_if_due(TIMEOUT - 1, TIMEOUT) is False
    assert ctl.expire_if_due(TIMEOUT + 1, TIMEOUT) is True
    assert ctl.expire_if_due(TIMEOUT + 2, TIMEOUT) is False


def test_armed_remaining_counts_down_and_is_none_when_not_armed():
    ctl = PadController()
    assert ctl.armed_remaining(0, TIMEOUT) is None
    ctl.load("AAA")
    assert ctl.armed_remaining(0, TIMEOUT) is None
    ctl.toggle_arm(10, TIMEOUT)
    assert ctl.armed_remaining(70, TIMEOUT) == pytest.approx(TIMEOUT - 60)
