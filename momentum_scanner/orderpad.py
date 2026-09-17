"""
Order pad logic: the frozen sizing snapshot taken at arm time, the fire-time
validation gate, and the bracket description that gets submitted (or, while
tunables.order_pad_dry_run is True, merely logged).

Deliberately free of Tk and of IB -- padwindow.py owns the window, app.py
owns the wiring and the eventual order submission, and everything in here is
a pure function of a SymbolState plus a clock, so the whole arm/validate path
is testable without a display or a broker connection (see tests/test_orderpad.py).

Two ideas drive the design:

1. FROZEN NUMBERS. Everything on the pad except the quote age is captured
   once, at arm time, and never recomputed. A pad whose numbers shift while
   you look at it is not glanceable, which is the entire point of it sitting
   over TWS. The cost of freezing is that the snapshot goes stale, which is
   what validate_fire() exists to catch.

2. ONE SOURCE OF TRUTH FOR SIZING. build_snapshot() calls the same
   sizing.compute_sizing() that display.py renders the table's Shares/Target/
   Stop/S-spr/EffR columns from, with the same shared Tunables instance. The
   pad cannot disagree with the row it was armed from, because it isn't doing
   its own arithmetic.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime

from . import config
from .atr import atr_value
from .models import SymbolState
from .sizing import compute_sizing, round_to_tick, tick_size
from .tunables import Tunables


@dataclass(frozen=True)
class ArmedSnapshot:
    """One symbol's sizing, frozen at the instant it was armed.

    Frozen (not just conventionally, but `frozen=True`) because a stale
    snapshot that silently updated itself would defeat the drift check: the
    guard works by comparing the live price against a price that genuinely
    cannot have moved.
    """

    symbol: str
    armed_at: datetime
    price: float
    bid: float | None
    ask: float | None
    spread_abs: float | None
    shares: int
    stop_price: float
    target_price: float
    stop_distance: float
    position_size: float
    stop_in_spreads: float | None
    commission_rt: float
    effective_r: float | None
    nominal_r: float
    stop_trigger_lead_pct: float
    non_tradeable_reason: str | None

    @property
    def risk_usd(self) -> float:
        """Dollars at risk if the stop fills exactly: shares * stop distance.

        Recomputed from the frozen shares/distance rather than read off
        tunables.risk_usd, because shares is floored to a whole number -- the
        actual risk is always a little under the requested risk, and it's the
        actual number that belongs on a pad you're about to fire from.
        """
        return self.shares * self.stop_distance

    @property
    def drift_allowance(self) -> float:
        """Absolute price move tolerated between arming and firing."""
        return self.stop_distance * config.ORDER_PAD_MAX_DRIFT_FRACTION

    @property
    def max_entry_price(self) -> float:
        """Limit cap for the bracket's parent (see bracket_plan). The same
        budget the drift check enforces, applied to the fill itself: refusing
        to fire on a 25%-of-stop drift is pointless if the order that IS sent
        can then slip further than that while crossing the spread."""
        return self.price + self.drift_allowance


def build_snapshot(state: SymbolState, tunables: Tunables, now: datetime) -> ArmedSnapshot | None:
    """Freeze `state`'s current sizing. None if the symbol has no price yet
    (nothing to size from, and nothing worth putting on screen).

    A symbol whose sizing is non-tradeable (too few shares, position over the
    cap) still produces a snapshot rather than None -- arming it is allowed so
    that the pad can display the reason it won't fire. Refusing to arm would
    just leave the pad blank with no explanation, and the pad isn't focused
    until arming succeeds, so there'd be nowhere to show one.
    """
    price = state.tick.last
    if price is None:
        return None
    sizing = compute_sizing(price, state.tick, atr_value(state.atr), tunables)
    if sizing is None:
        return None
    return ArmedSnapshot(
        symbol=state.symbol,
        armed_at=now,
        price=price,
        bid=state.tick.bid,
        ask=state.tick.ask,
        spread_abs=state.tick.spread_abs,
        shares=sizing.shares,
        stop_price=sizing.stop_price,
        target_price=sizing.target_price,
        stop_distance=sizing.stop_distance,
        position_size=sizing.position_size,
        stop_in_spreads=sizing.stop_in_spreads,
        commission_rt=sizing.commission_rt,
        effective_r=sizing.effective_r,
        nominal_r=tunables.r_multiple,
        stop_trigger_lead_pct=tunables.stop_trigger_lead_pct,
        non_tradeable_reason=sizing.non_tradeable_reason,
    )


def snapshot_to_dict(snapshot: ArmedSnapshot) -> dict:
    """Every field on `snapshot` plus its derived properties (risk_usd,
    drift_allowance, max_entry_price), for the order history log (see
    order_history.py). That log exists so another agent can recompute this
    against sizing.compute_sizing() and cross-check it, so nothing here is
    rounded or summarized the way the pad's own display is."""
    return {
        **asdict(snapshot),
        "risk_usd": snapshot.risk_usd,
        "drift_allowance": snapshot.drift_allowance,
        "max_entry_price": snapshot.max_entry_price,
    }


def quote_age_sec(state: SymbolState | None, now: datetime) -> float | None:
    """Seconds since this symbol last ticked. None if it has never ticked.

    Feed liveness, not "seconds since the price last changed" -- a name
    genuinely printing at a steady 4.12 is not stale, a name whose feed has
    stopped delivering is. app.py stamps tick.updated_at on every ticker
    update for exactly this.
    """
    if state is None or state.tick.updated_at is None:
        return None
    return (now - state.tick.updated_at).total_seconds()


def validate_fire(
    snapshot: ArmedSnapshot,
    state: SymbolState | None,
    now: datetime,
    non_tradable_listed: bool = False,
) -> str | None:
    """None if `snapshot` may be fired right now; otherwise a short reason,
    written to be readable at a glance on a 300px-wide pad.

    Order matters. Staleness is checked before drift because drift is measured
    against the last price received: on a dead feed the two prices would agree
    perfectly and the drift check would wave through exactly the case it
    exists to stop.
    """
    if state is None:
        return f"{snapshot.symbol} left the feed pool"

    if non_tradable_listed:
        return f"{snapshot.symbol} marked non-tradable"

    if snapshot.non_tradeable_reason:
        return snapshot.non_tradeable_reason

    age = quote_age_sec(state, now)
    if age is None:
        return "no quote received yet"
    if age > config.ORDER_PAD_MAX_QUOTE_AGE_SEC:
        return f"quote {age:.1f}s stale (max {config.ORDER_PAD_MAX_QUOTE_AGE_SEC:.0f}s)"

    live_price = state.tick.last
    if live_price is None:
        return "no live price"
    drift = abs(live_price - snapshot.price)
    if drift > snapshot.drift_allowance:
        direction = "up" if live_price > snapshot.price else "down"
        return (
            f"drifted {direction} {drift:.2f} from {snapshot.price:.2f} "
            f"(max {snapshot.drift_allowance:.2f}) -- re-arm"
        )

    return None


@dataclass(frozen=True)
class BracketExitPrices:
    """The stop-limit/stop-trigger/target prices actually sent to IB for one
    fill price. See recompute_bracket_exit -- the LIMIT is the real stop,
    the TRIGGER sits above it."""

    stop_limit_price: float
    stop_trigger_price: float
    target_price: float


def recompute_bracket_exit(fill_price: float, snapshot: ArmedSnapshot) -> BracketExitPrices:
    """The stop/target children, anchored to `fill_price` rather than the
    (possibly slipped) armed price -- same reasoning the children are
    already sized from the parent's actual fill quantity rather than the
    requested one (see BracketPlan). Called again on every parent fill
    event, including a resize, using the order's running average fill
    price, so the exit keeps tracking the true cost basis through partial
    fills at different prices.

    stop_limit_price is the real stop: fill_price - stop_distance. This is
    what all risk math (including sizing.compute_sizing's shares) is
    computed from -- nothing here references the trigger.

    stop_trigger_price sits stop_trigger_lead_pct (frozen into the snapshot
    at arm time) of stop_distance ABOVE the limit, so the stop order wakes
    and is already working in the market before price actually reaches the
    intended stop, rather than being submitted only once price is already
    there. Fixed 2026-09-17: the previous construction used the computed
    stop as the TRIGGER and set the limit a flat $0.02 below it -- backwards,
    and flat rather than proportional to the name's own volatility -- which
    meant every stop fill realized worse than risk_usd priced in (see
    config.STOP_TRIGGER_LEAD_PCT's docstring for the live evidence).

    Both are rounded to the applicable tick (sizing.round_to_tick); if that
    rounding collapses the trigger onto the same tick as the limit, the
    trigger is widened by one tick so the two can never compare equal (IB
    requires the trigger to sit outside the limit for a SELL STP LMT).
    """
    stop_distance = snapshot.stop_distance
    stop_limit_price = round_to_tick(fill_price - stop_distance)
    trigger_lead = stop_distance * snapshot.stop_trigger_lead_pct
    stop_trigger_price = round_to_tick(stop_limit_price + trigger_lead)
    if stop_trigger_price <= stop_limit_price:
        stop_trigger_price = round_to_tick(stop_limit_price + tick_size(stop_limit_price))
    target_price = round_to_tick(fill_price + stop_distance * snapshot.nominal_r)
    return BracketExitPrices(
        stop_limit_price=stop_limit_price,
        stop_trigger_price=stop_trigger_price,
        target_price=target_price,
    )


@dataclass(frozen=True)
class BracketPlan:
    """The PLANNED bracket for one armed snapshot -- what would be sent to
    IB if the parent filled at exactly the armed price, with no slippage.

    Exists so the dry run (tunables.order_pad_dry_run True) logs something
    concrete rather than nothing: what you read in scanner.log during
    testing is what would go out if the fill matches the arm. A real fire's
    ACTUAL stop/target are recomputed from the real fill price -- see
    app.py's _on_pad_parent_fill / orderpad.recompute_bracket_exit -- and
    can differ from these planned figures whenever the entry slips.

    The children are quantity-less on purpose. Their size is not knowable
    here -- it has to come from the parent's reported fill quantity, because
    a partial fill sized from the INTENDED quantity leaves stop/target orders
    larger than the position, which IBKR then treats as an unprotected short
    sale. See app.py's _on_pad_parent_fill, which creates them sized to
    shares actually held once the parent starts filling.
    """

    symbol: str
    quantity: int
    entry_limit: float
    stop_price: float
    stop_trigger_price: float
    target_price: float
    oca_group: str

    def describe(self) -> str:
        return (
            f"BUY {self.quantity} {self.symbol} LMT {self.entry_limit:.2f} "
            f"(marketable, capped at armed price + drift allowance); children "
            f"OCA={self.oca_group}, re-anchored to the parent's actual fill price: "
            f"SELL STP LMT {self.stop_price:.2f} (trigger {self.stop_trigger_price:.2f}) / "
            f"SELL LMT {self.target_price:.2f} -- planned, assuming the fill matches the arm"
        )


def bracket_plan_to_dict(plan: BracketPlan) -> dict:
    """`plan`'s fields plus its human-readable describe() text, for the
    order history log."""
    return {**asdict(plan), "describe": plan.describe()}


def bracket_plan(snapshot: ArmedSnapshot) -> BracketPlan:
    """The bracket for `snapshot`: a marketable-limit parent plus two
    OCA-grouped children.

    The parent is a limit rather than a market order so the fill can't slip
    past the drift budget that was just enforced -- priced at
    max_entry_price, which is above the ask in the normal case (so it crosses
    and fills like a market order) but refuses to chase a sudden run-up into
    a position whose displayed R no longer holds.

    stop_price/stop_trigger_price/target_price are the PLANNED figures --
    recompute_bracket_exit(snapshot.price, snapshot), i.e. exactly what the
    real fill-time recompute would produce if the fill lands right on the
    armed price. Same formula both places, so there's a single source of
    truth for the stop/trigger relationship rather than two that can drift
    apart.
    """
    exit_preview = recompute_bracket_exit(snapshot.price, snapshot)
    return BracketPlan(
        symbol=snapshot.symbol,
        quantity=snapshot.shares,
        entry_limit=round_to_tick(snapshot.max_entry_price),
        stop_price=exit_preview.stop_limit_price,
        stop_trigger_price=exit_preview.stop_trigger_price,
        target_price=exit_preview.target_price,
        oca_group=f"pad-{snapshot.symbol}-{int(snapshot.armed_at.timestamp())}",
    )
