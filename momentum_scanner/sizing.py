"""
Position sizing: derives stop distance from the symbol's own volatility and
spread FIRST, then sizes the position from that -- the reverse of the old
scalp_sizing() (formerly in spikes.py, now removed), which fixed the
position size ($300) and let stop distance fall out as whatever produced the
target risk amount. That produced dangerously tight stops on wide-spread/
volatile names: a 2-cent-spread stock could get a stop under a single spread
wide, getting taken out by bid-ask bounce alone.

    atr_distance   = ATR (atr.py, ATR_LOOKBACK_BARS 1-min bars) * atr_multiplier
    spread_floor   = (ask - bid) * min_spreads
    stop_distance  = max(atr_distance, spread_floor)

    shares         = floor(risk_usd / stop_distance)
    position_size  = shares * price                       (output, not an input)
    stop_price     = price - stop_distance
    target_price   = price + stop_distance * r_multiple

Round-trip commission (IBKR Pro TIERED pricing, see config.COMMISSION_*'s
docstring -- wrong under Fixed or any other tier) and the round-trip spread
cost are both folded into effective_r alongside the nominal r_multiple, so
the target's real edge after real trading costs is visible per-symbol
rather than assuming the nominal R multiple is what actually gets realized.

Deliberately standalone, same style as spikes.py/trend.py -- display.py
calls compute_sizing() per row; nothing here touches IB or Textual.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import config
from .models import LiveTick
from .tunables import Tunables


@dataclass
class SizingResult:
    shares: int
    stop_price: float
    target_price: float
    stop_distance: float
    position_size: float
    stop_in_spreads: float | None
    commission_rt: float
    effective_r: float | None
    non_tradeable_reason: str | None


def _commission_per_order(shares: int, price: float, pass_through_per_share: float) -> float:
    """
    IBKR Pro TIERED commission for one order leg (US stocks): a per-share
    rate with a per-order minimum, capped at a percentage of trade value,
    plus a configurable flat pass-through estimate standing in for
    exchange/regulatory fees (not modeled individually).
    """
    base = max(shares * config.COMMISSION_PER_SHARE, config.COMMISSION_MIN_PER_ORDER)
    base = min(base, shares * price * config.COMMISSION_MAX_PCT_OF_TRADE)
    return base + shares * pass_through_per_share


def compute_sizing(price: float, tick: LiveTick, atr_value: float | None, tunables: Tunables) -> SizingResult | None:
    """
    Returns None only if price is invalid (<=0). Every other failure mode --
    no usable stop distance, shares below the minimum, position over the cap
    -- is expressed as a SizingResult with non_tradeable_reason set rather
    than None, so a symbol with thin data still gets a real row (see
    display.py's non-tradeable cell rendering) instead of disappearing.
    """
    if price <= 0:
        return None

    spread_abs = tick.spread_abs
    atr_distance = (atr_value or 0.0) * tunables.atr_multiplier
    spread_floor = (spread_abs or 0.0) * tunables.min_spreads
    stop_distance = max(atr_distance, spread_floor)

    if stop_distance <= 0:
        return SizingResult(
            shares=0, stop_price=price, target_price=price, stop_distance=0.0,
            position_size=0.0, stop_in_spreads=None, commission_rt=0.0, effective_r=None,
            non_tradeable_reason="no stop distance available (spread and ATR both unknown)",
        )

    shares = math.floor(tunables.risk_usd / stop_distance)
    stop_price = price - stop_distance
    target_price = price + stop_distance * tunables.r_multiple
    position_size = shares * price
    stop_in_spreads = stop_distance / spread_abs if spread_abs else None

    if shares > 0:
        commission_rt = 2 * _commission_per_order(shares, price, tunables.pass_through_per_share)
        spread_cost_rt = (spread_abs or 0.0) * shares
        target_distance = stop_distance * tunables.r_multiple
        denom = stop_distance * shares + spread_cost_rt + commission_rt
        effective_r = (
            (target_distance * shares - spread_cost_rt - commission_rt) / denom if denom > 0 else None
        )
    else:
        commission_rt = 0.0
        effective_r = None

    reason = None
    if shares < tunables.min_shares:
        reason = f"size {shares}sh below minimum {tunables.min_shares}sh"
    elif position_size > tunables.max_position_usd:
        reason = f"position ${position_size:,.0f} over max ${tunables.max_position_usd:,.0f}"

    return SizingResult(
        shares=shares, stop_price=stop_price, target_price=target_price,
        stop_distance=stop_distance, position_size=position_size,
        stop_in_spreads=stop_in_spreads, commission_rt=commission_rt,
        effective_r=effective_r, non_tradeable_reason=reason,
    )
