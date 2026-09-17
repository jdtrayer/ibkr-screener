"""compute_sizing replaced the old fixed-$-position scalp_sizing (spikes.py)
with stop-distance-first sizing: max(ATR, spread floor) drives shares, not
the other way around. Real money math, so these pin the formula down
precisely rather than just smoke-testing it -- see sizing.py's module
docstring for the spec."""
import math

import pytest

from momentum_scanner.models import LiveTick
from momentum_scanner.sizing import compute_sizing, round_to_tick, tick_size
from momentum_scanner.tunables import Tunables


def make_tick(bid, ask):
    return LiveTick(last=(bid + ask) / 2, bid=bid, ask=ask)


def base_tunables(**overrides):
    t = Tunables()
    for k, v in overrides.items():
        setattr(t, k, v)
    return t


# -- spread floor vs ATR (max()) --------------------------------------------

def test_spread_floor_dominates_when_atr_small():
    tick = make_tick(1.00, 1.02)  # spread_abs = 0.02
    t = base_tunables(min_spreads=8, atr_multiplier=1.0, risk_usd=10.0, r_multiple=2.0)
    r = compute_sizing(1.01, tick, atr_value=0.01, tunables=t)  # atr_distance=0.01 < floor=0.16
    assert r.stop_distance == pytest.approx(0.16)
    assert r.stop_price == pytest.approx(1.01 - 0.16)
    assert r.target_price == pytest.approx(1.01 + 0.16 * 2.0)


def test_atr_dominates_when_larger_than_spread_floor():
    tick = make_tick(1.00, 1.02)  # spread_abs = 0.02, floor at min_spreads=8 -> 0.16
    t = base_tunables(min_spreads=8, atr_multiplier=2.0, risk_usd=10.0, r_multiple=2.0)
    r = compute_sizing(1.01, tick, atr_value=0.20, tunables=t)  # atr_distance = 0.40
    assert r.stop_distance == pytest.approx(0.40)


def test_missing_atr_falls_back_to_spread_floor_alone():
    tick = make_tick(1.00, 1.02)
    t = base_tunables(min_spreads=8, risk_usd=10.0)
    r = compute_sizing(1.01, tick, atr_value=None, tunables=t)
    assert r.stop_distance == pytest.approx(0.16)


# -- shares / position -------------------------------------------------------

def test_shares_floored_to_whole_number():
    tick = make_tick(1.00, 1.02)  # spread_abs=0.02, floor(min_spreads=8)=0.16
    t = base_tunables(min_spreads=8, risk_usd=10.0, min_shares=1, max_position_usd=1_000_000)
    r = compute_sizing(1.01, tick, atr_value=None, tunables=t)
    # 10 / 0.16 = 62.5 -> floors to 62
    assert r.shares == 62
    assert r.position_size == pytest.approx(62 * 1.01)


def test_stop_in_spreads_equals_min_spreads_when_floor_binds():
    tick = make_tick(1.00, 1.02)
    t = base_tunables(min_spreads=8, risk_usd=10.0, min_shares=1, max_position_usd=1_000_000)
    r = compute_sizing(1.01, tick, atr_value=None, tunables=t)
    assert r.stop_in_spreads == pytest.approx(8.0)


def test_stop_in_spreads_none_when_spread_unknown():
    tick = LiveTick(last=1.01, bid=None, ask=None)
    t = base_tunables(risk_usd=10.0, min_shares=1, max_position_usd=1_000_000)
    r = compute_sizing(1.01, tick, atr_value=0.05, tunables=t)
    assert r.stop_in_spreads is None


# -- non-tradeable ------------------------------------------------------------

def test_non_tradeable_below_min_shares():
    tick = make_tick(1.00, 1.02)  # floor(8) = 0.16 -> shares = floor(10/0.16) = 62
    t = base_tunables(min_spreads=8, risk_usd=10.0, min_shares=100, max_position_usd=1_000_000)
    r = compute_sizing(1.01, tick, atr_value=None, tunables=t)
    assert r.non_tradeable_reason is not None
    assert "below minimum" in r.non_tradeable_reason


def test_non_tradeable_over_max_position():
    tick = make_tick(1.00, 1.02)
    t = base_tunables(min_spreads=8, risk_usd=10.0, min_shares=1, max_position_usd=1.0)
    r = compute_sizing(1.01, tick, atr_value=None, tunables=t)
    assert r.non_tradeable_reason is not None
    assert "over max" in r.non_tradeable_reason


def test_tradeable_when_within_both_thresholds():
    tick = make_tick(1.00, 1.02)
    t = base_tunables(min_spreads=8, risk_usd=10.0, min_shares=10, max_position_usd=800.0)
    r = compute_sizing(1.01, tick, atr_value=None, tunables=t)
    assert r.non_tradeable_reason is None


def test_invalid_price_returns_none():
    tick = make_tick(1.00, 1.02)
    assert compute_sizing(0.0, tick, atr_value=None, tunables=Tunables()) is None
    assert compute_sizing(-5.0, tick, atr_value=None, tunables=Tunables()) is None


def test_no_stop_distance_available_is_non_tradeable_not_a_crash():
    tick = LiveTick(last=1.0, bid=None, ask=None)  # no spread, no ATR either
    r = compute_sizing(1.0, tick, atr_value=None, tunables=Tunables())
    assert r is not None
    assert r.shares == 0
    assert r.non_tradeable_reason is not None


# -- commission (IBKR Pro Tiered) --------------------------------------------

def test_commission_hits_per_order_minimum_at_low_share_count():
    # 20 shares @ ~$4: shares*0.0035=$0.07 (under the $0.35 floor), but
    # shares*price*0.01=$0.80 (above it) -- so the $0.35 floor should bind,
    # not the 1%-of-trade cap.
    tick = make_tick(4.00, 4.02)  # spread_abs=0.02
    t = base_tunables(min_spreads=1, risk_usd=0.40, min_shares=1, max_position_usd=1_000_000,
                       pass_through_per_share=0.0)
    r = compute_sizing(4.01, tick, atr_value=None, tunables=t)
    assert r.shares == 20  # floor(0.40 / 0.02) = 20
    assert r.commission_rt == pytest.approx(2 * 0.35)


def test_commission_hits_percent_of_trade_cap_at_low_price():
    # Low price, many shares -> shares*price*0.01 can undercut shares*0.0035.
    tick = make_tick(0.099, 0.101)  # spread_abs ~= 0.002, price ~0.10
    t = base_tunables(min_spreads=1, risk_usd=10.0, min_shares=1, max_position_usd=1_000_000,
                       pass_through_per_share=0.0)
    r = compute_sizing(0.10, tick, atr_value=None, tunables=t)
    # shares*price*0.01 = shares*0.10*0.01 = shares*0.001; shares*0.0035 > shares*0.001 always
    # at this price, so the 1%-of-trade cap should always bind under the per-share rate.
    per_order = r.commission_rt / 2
    assert per_order == pytest.approx(r.shares * 0.10 * 0.01)
    assert per_order < r.shares * 0.0035


def test_pass_through_adds_flat_amount_per_share_per_leg():
    tick = make_tick(4.00, 4.02)
    t_no_pt = base_tunables(min_spreads=1, risk_usd=1.0, min_shares=1, max_position_usd=1_000_000,
                             pass_through_per_share=0.0)
    t_pt = base_tunables(min_spreads=1, risk_usd=1.0, min_shares=1, max_position_usd=1_000_000,
                          pass_through_per_share=0.001)
    r_no_pt = compute_sizing(4.01, tick, atr_value=None, tunables=t_no_pt)
    r_pt = compute_sizing(4.01, tick, atr_value=None, tunables=t_pt)
    assert r_no_pt.shares == r_pt.shares
    assert r_pt.commission_rt - r_no_pt.commission_rt == pytest.approx(2 * r_pt.shares * 0.001)


# -- effective R --------------------------------------------------------------

def test_effective_r_matches_manual_formula():
    tick = make_tick(1.00, 1.02)  # spread_abs=0.02
    t = base_tunables(min_spreads=8, risk_usd=10.0, r_multiple=2.0, min_shares=1,
                       max_position_usd=1_000_000, pass_through_per_share=0.0002)
    r = compute_sizing(1.01, tick, atr_value=None, tunables=t)
    stop_distance = 0.16
    shares = r.shares
    target_distance = stop_distance * 2.0
    spread_cost_rt = 0.02 * shares
    expected = (
        (target_distance * shares - spread_cost_rt - r.commission_rt)
        / (stop_distance * shares + spread_cost_rt + r.commission_rt)
    )
    assert r.effective_r == pytest.approx(expected)


def test_effective_r_none_when_non_tradeable_at_zero_shares():
    tick = LiveTick(last=1.0, bid=None, ask=None)
    r = compute_sizing(1.0, tick, atr_value=None, tunables=Tunables())
    assert r.effective_r is None


# -- tick_size / round_to_tick -------------------------------------------------
# Reg NMS Rule 612: $0.0001 below $1.00, $0.01 at or above. Real money is
# rounded to these, so the order pad's stop/trigger construction (see
# orderpad.recompute_bracket_exit) depends on getting the boundary right.


def test_tick_size_is_a_penny_at_and_above_a_dollar():
    assert tick_size(1.00) == 0.01
    assert tick_size(4.12) == 0.01
    assert tick_size(500.0) == 0.01


def test_tick_size_is_a_hundredth_of_a_cent_below_a_dollar():
    assert tick_size(0.9999) == 0.0001
    assert tick_size(0.85) == 0.0001


def test_round_to_tick_rounds_penny_stocks_to_two_decimals():
    assert round_to_tick(3.8049) == pytest.approx(3.80)
    assert round_to_tick(3.8051) == pytest.approx(3.81)


def test_round_to_tick_rounds_sub_dollar_names_to_four_decimals():
    assert round_to_tick(0.85006) == pytest.approx(0.8501)
    assert round_to_tick(0.85002) == pytest.approx(0.8500)


def test_round_to_tick_uses_the_reference_tier_not_the_values_own_magnitude():
    """A stop DISTANCE like 0.106 is smaller than $1, but the price it's
    being subtracted from (2.04) is not -- rounding by the distance's own
    magnitude would wrongly apply the sub-dollar $0.0001 tick."""
    assert round_to_tick(0.106, reference=2.04) == pytest.approx(0.11)
    assert round_to_tick(0.106) == pytest.approx(0.106)  # no reference: rounds by its own (wrong) tier


# -- stop_distance is tick-rounded BEFORE shares are sized from it ----------
# Order of operations matters: sizing shares from the raw distance, then
# rounding the resulting stop price independently, lets the two disagree.
# Confirmed live 2026-09-17, order 31872: 94sh * 0.106 = $9.96 planned, but
# the stop price rounds to imply a 0.11 distance, realizing $10.34.


def test_stop_distance_is_tick_rounded_before_sizing_shares():
    tick = make_tick(2.035, 2.045)  # spread_abs = 0.01
    t = base_tunables(min_spreads=8, atr_multiplier=1.0, risk_usd=10.0, r_multiple=2.0)
    r = compute_sizing(2.04, tick, atr_value=0.106, tunables=t)  # atr_distance 0.106 > spread_floor 0.08

    assert r.stop_distance == pytest.approx(0.11)
    assert r.shares == 90  # floor(10.0 / 0.11), not floor(10.0 / 0.106) == 94
    assert r.stop_price == pytest.approx(1.93)

    # The point of the fix: shares and the stop price now derive from the
    # SAME rounded distance, so planned risk (shares * stop_distance) equals
    # what stopping out at stop_price actually realizes.
    realized_risk = r.shares * (2.04 - r.stop_price)
    planned_risk = r.shares * r.stop_distance
    assert realized_risk == pytest.approx(planned_risk)

    # Regression check: the OLD order of operations -- shares sized from the
    # RAW 0.106 distance, the resulting price rounded independently after --
    # would have sized 94sh against a price that actually implies 0.11, a
    # ~$0.38 planned-vs-realized gap that this fix eliminates.
    old_shares = math.floor(t.risk_usd / 0.106)
    assert old_shares == 94
    old_realized_risk = old_shares * 0.11
    old_planned_risk = old_shares * 0.106
    assert old_realized_risk - old_planned_risk == pytest.approx(0.376, abs=0.01)
