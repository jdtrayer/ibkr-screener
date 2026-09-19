"""
Order pad logic: live sizing, the fire-time validation gate, the
Empty/Loaded/Armed state machine, and the bracket description that gets
submitted (or, while tunables.order_pad_dry_run is True, merely logged).

Deliberately free of Tk and of IB -- padwindow.py owns the window, app.py
owns the wiring and the order submission, and everything in here is a pure
function of a SymbolState plus a clock, so the whole load/arm/validate path
is testable without a display or a broker connection (see tests/test_orderpad.py).

Three ideas drive the design:

1. LIVE NUMBERS. There is no armed price to drift from. While a symbol is
   loaded the pad recomputes shares/stop/trigger/target/risk from the
   current tick (live_sizing), and F4 recomputes once more at the instant of
   the press -- so what's on screen is what would be sent, give or take one
   tick. The guard against firing on bad data is not a comparison with an
   earlier price; it is validate_fire's checks on the CURRENT data (quote
   freshness, tradeable sizing, sanity bounds).

2. THE FIRE RECORD IS FROZEN. A PadSizing is immutable, and the one taken at
   F4 is kept on the bracket for the life of the position: the stop/target
   are re-anchored on every partial fill from its stop_distance, and if that
   read live, an ATR change between fills would move the stop under an open
   position (see recompute_bracket_exit).

3. ONE SOURCE OF TRUTH FOR SIZING. live_sizing() calls the same
   sizing.compute_sizing() that display.py renders the table's Shares/Target/
   Stop columns from, so the pad and the table use the same arithmetic. The
   one deliberate difference is the pad's own risk $ (editable on the pad,
   not written back to the shared Tunables).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum

from . import config
from .atr import atr_value
from .models import SymbolState
from .sizing import compute_sizing, round_to_tick, tick_size
from .tunables import Tunables


@dataclass(frozen=True)
class BracketExitPrices:
    """The stop-limit/stop-trigger/target prices for one entry price. See
    recompute_bracket_exit -- the LIMIT is the real stop, the TRIGGER sits
    above it."""

    stop_limit_price: float
    stop_trigger_price: float
    target_price: float


def _exit_prices(
    entry_price: float, stop_distance: float, trigger_lead_pct: float, r_multiple: float
) -> BracketExitPrices:
    """The one formula for the stop/trigger/target relationship, used both
    for the live display (entry_price = last) and for the real fill-time
    re-anchor (entry_price = fill price), so the two can't drift apart.

    stop_limit_price is the real stop: entry - stop_distance. This is what
    all risk math (including sizing.compute_sizing's shares) is computed
    from -- nothing here references the trigger.

    stop_trigger_price sits trigger_lead_pct of stop_distance ABOVE the
    limit, so the stop order wakes and is already working in the market
    before price actually reaches the intended stop, rather than being
    submitted only once price is already there. (Fixed 2026-09-17: the
    previous construction used the computed stop as the TRIGGER and set the
    limit a flat $0.02 below it -- backwards, and flat rather than
    proportional -- which made every stop fill realize worse than risk_usd
    priced in; see config.STOP_TRIGGER_LEAD_PCT.)

    Both are rounded to the applicable tick (sizing.round_to_tick); if that
    rounding collapses the trigger onto the same tick as the limit, the
    trigger is widened by one tick so the two can never compare equal (IB
    requires the trigger to sit outside the limit for a SELL STP LMT).
    """
    stop_limit_price = round_to_tick(entry_price - stop_distance)
    stop_trigger_price = round_to_tick(stop_limit_price + stop_distance * trigger_lead_pct)
    if stop_trigger_price <= stop_limit_price:
        stop_trigger_price = round_to_tick(stop_limit_price + tick_size(stop_limit_price))
    target_price = round_to_tick(entry_price + stop_distance * r_multiple)
    return BracketExitPrices(
        stop_limit_price=stop_limit_price,
        stop_trigger_price=stop_trigger_price,
        target_price=target_price,
    )


@dataclass(frozen=True)
class PadSizing:
    """One symbol's sizing computed from one tick. Frozen: each recompute
    produces a new instance, and the one taken at F4 is the fire record kept
    on the bracket (see the module docstring, idea 2).

    stop_price / stop_trigger_price / target_price are the tick-rounded
    prices that would actually be sent, and are None only when no stop
    distance could be derived (non_tradeable_reason says why) -- validate_fire
    refuses on a None rather than sending one.
    """

    symbol: str
    computed_at: datetime
    price: float
    bid: float | None
    ask: float | None
    spread_abs: float | None
    shares: int
    stop_price: float | None
    stop_trigger_price: float | None
    target_price: float | None
    stop_distance: float
    position_size: float
    stop_in_spreads: float | None
    commission_rt: float
    effective_r: float | None
    nominal_r: float
    stop_trigger_lead_pct: float
    risk_requested: float
    atr: float | None
    non_tradeable_reason: str | None

    @property
    def risk_usd(self) -> float:
        """Dollars at risk if the stop fills exactly: shares * stop distance.

        Recomputed from shares/distance rather than read off risk_requested,
        because shares is floored to a whole number -- the actual risk is
        always a little under the requested risk, and it's the actual number
        that belongs on a pad you're about to fire from.
        """
        return self.shares * self.stop_distance

    @property
    def entry_slippage_allowance(self) -> float:
        """How far above the price at F4 the entry order is allowed to fill.
        Not a guard on firing -- an order price: the parent is a limit
        rather than a market order so a fast run-up between the tick and the
        fill can't drag the entry past the risk the pad displayed."""
        return self.stop_distance * config.ORDER_PAD_ENTRY_SLIPPAGE_FRACTION

    @property
    def max_entry_price(self) -> float:
        return self.price + self.entry_slippage_allowance

    @property
    def entry_limit(self) -> float:
        return round_to_tick(self.max_entry_price)


def live_sizing(
    state: SymbolState, tunables: Tunables, now: datetime, risk_usd: float | None = None
) -> PadSizing | None:
    """`state`'s sizing from its current tick. None if the symbol has no
    price yet (nothing to size from, and nothing worth putting on screen).

    `risk_usd` overrides tunables.risk_usd for this call -- the pad's own
    editable risk, which deliberately does not write back to the shared
    Tunables (the scanner tables keep sizing from theirs).

    A symbol whose sizing is non-tradeable (ATR not warmed, too few shares,
    position over the cap) still produces a PadSizing rather than None, so
    the pad can display the reason it won't fire instead of going blank.

    ATR-not-warmed is a pad-side rule, not part of compute_sizing: sizing.py
    treats a missing ATR as 0 and falls back to the spread floor alone, which
    is a fine thing to show in a table row but not something to send an order
    from -- a spread-only stop can be a fraction of the name's real range.
    """
    price = state.tick.last
    if price is None:
        return None
    effective = tunables if risk_usd is None else replace(tunables, risk_usd=risk_usd)
    atr = atr_value(state.atr)
    result = compute_sizing(price, state.tick, atr, effective)
    if result is None:
        return None

    reason = result.non_tradeable_reason
    if atr is None:
        reason = "ATR warming (no closed 1-min bar yet)"

    if result.stop_distance > 0:
        exits = _exit_prices(
            price, result.stop_distance, tunables.stop_trigger_lead_pct, tunables.r_multiple
        )
        stop_price, trigger_price, target_price = (
            exits.stop_limit_price, exits.stop_trigger_price, exits.target_price
        )
    else:
        stop_price = trigger_price = target_price = None

    return PadSizing(
        symbol=state.symbol,
        computed_at=now,
        price=price,
        bid=state.tick.bid,
        ask=state.tick.ask,
        spread_abs=state.tick.spread_abs,
        shares=result.shares,
        stop_price=stop_price,
        stop_trigger_price=trigger_price,
        target_price=target_price,
        stop_distance=result.stop_distance,
        position_size=result.position_size,
        stop_in_spreads=result.stop_in_spreads,
        commission_rt=result.commission_rt,
        effective_r=result.effective_r,
        nominal_r=tunables.r_multiple,
        stop_trigger_lead_pct=tunables.stop_trigger_lead_pct,
        risk_requested=effective.risk_usd,
        atr=atr,
        non_tradeable_reason=reason,
    )


def sizing_to_dict(sizing: PadSizing) -> dict:
    """Every field on `sizing` plus its derived properties, for the order
    history log (see order_history.py). That log exists so another agent can
    recompute this against sizing.compute_sizing() and cross-check it, so
    nothing here is rounded or summarized the way the pad's own display is."""
    return {
        **asdict(sizing),
        "risk_usd": sizing.risk_usd,
        "entry_slippage_allowance": sizing.entry_slippage_allowance,
        "max_entry_price": sizing.max_entry_price,
        "entry_limit": sizing.entry_limit,
    }


def quote_age_sec(state: SymbolState | None, now: datetime) -> float | None:
    """Seconds since this symbol last ticked. None if it has never ticked.

    Feed liveness, not "seconds since the price last changed" -- a name
    genuinely printing at a steady 4.12 is not stale, a name whose feed has
    stopped delivering is. app.py stamps tick.updated_at on every ticker
    update for exactly this. (It is per ticker UPDATE, not per trade: a quiet
    small cap outside regular hours can legitimately go longer than a tight
    threshold between updates, and will be refused as stale.)
    """
    if state is None or state.tick.updated_at is None:
        return None
    return (now - state.tick.updated_at).total_seconds()


def sanity_problem(sizing: PadSizing) -> str | None:
    """The last line of defence before an order is built: the numbers
    themselves have to make sense as a long bracket. None if they do."""
    if (
        sizing.stop_price is None
        or sizing.stop_trigger_price is None
        or sizing.target_price is None
    ):
        return "no stop/target available"
    if sizing.shares <= 0:
        return "zero shares"
    if sizing.stop_price >= sizing.price:
        return f"stop {sizing.stop_price:.4f} not below entry {sizing.price:.4f}"
    if sizing.target_price <= sizing.price:
        return f"target {sizing.target_price:.4f} not above entry {sizing.price:.4f}"
    # A SELL stop-limit whose trigger is at or above the market triggers the
    # moment it is placed -- an instant sale of the position just bought.
    if sizing.stop_trigger_price >= sizing.price:
        return f"stop trigger {sizing.stop_trigger_price:.4f} not below entry {sizing.price:.4f}"
    return None


def validate_fire(
    sizing: PadSizing | None,
    state: SymbolState | None,
    now: datetime,
    max_quote_age_sec: float,
    non_tradable_listed: bool = False,
    symbol: str = "",
) -> str | None:
    """None if the pad may fire right now; otherwise a short reason, written
    to be readable at a glance on a small pad.

    `sizing` must be the fresh F4-time computation, not the last displayed
    one. Staleness is checked before the sizing-derived checks because a
    frozen feed makes everything computed from it look perfectly fine.
    """
    if state is None:
        return f"{symbol or 'symbol'} has no live feed"

    if non_tradable_listed:
        return f"{state.symbol} marked non-tradable"

    age = quote_age_sec(state, now)
    if age is None:
        return "no quote received yet"
    if age > max_quote_age_sec:
        return f"quote {age:.1f}s stale (max {max_quote_age_sec:g}s)"

    if sizing is None:
        return "no live price"
    if sizing.non_tradeable_reason:
        return sizing.non_tradeable_reason
    return sanity_problem(sizing)


# -- the Empty / Loaded / Armed state machine ---------------------------------


class PadMode(Enum):
    EMPTY = "empty"      # no symbol loaded
    LOADED = "loaded"    # symbol loaded, feed live, sizing recalculating; F4 does nothing
    ARMED = "armed"      # Loaded, plus F4 is live


class PadController:
    """Which symbol is loaded and whether F4 is live. Pure: the clock is
    passed in (monotonic seconds), never read, so the arm timer is testable
    without sleeping.

    Arm state never carries across a symbol change: load() of a different
    symbol always lands in Loaded. Expiry is silent by design -- an armed
    pad that runs out its timer simply reads as Loaded again, with no
    warning beforehand (a countdown alarm creates urgency to enter, which
    is the opposite of what this tool is for).
    """

    def __init__(self) -> None:
        self.symbol: str | None = None
        self._armed_at: float | None = None

    def load(self, symbol: str) -> bool:
        """Load `symbol`. True if that changed the loaded symbol (in which
        case any arm has been dropped); loading the symbol that's already
        loaded is a no-op and leaves the arm alone."""
        if symbol == self.symbol:
            return False
        self.symbol = symbol
        self._armed_at = None
        return True

    def clear(self) -> None:
        self.symbol = None
        self._armed_at = None

    def mode(self, now: float, timeout_sec: float) -> PadMode:
        if self.symbol is None:
            return PadMode.EMPTY
        if self._armed_at is not None and now - self._armed_at < timeout_sec:
            return PadMode.ARMED
        return PadMode.LOADED

    def toggle_arm(self, now: float, timeout_sec: float) -> PadMode:
        """F2. A pure toggle: Loaded -> Armed (timer starts now), Armed ->
        Loaded, Empty stays Empty. Resetting the timer is F2 twice."""
        mode = self.mode(now, timeout_sec)
        if mode is PadMode.EMPTY:
            return mode
        self._armed_at = None if mode is PadMode.ARMED else now
        return self.mode(now, timeout_sec)

    def consume_arm(self) -> None:
        """A fire went out: drop back to Loaded, so a second F4 (key repeat,
        a double press) can't send a second order without a fresh F2."""
        self._armed_at = None

    def armed_remaining(self, now: float, timeout_sec: float) -> float | None:
        if self.mode(now, timeout_sec) is not PadMode.ARMED:
            return None
        return max(timeout_sec - (now - self._armed_at), 0.0)

    def expire_if_due(self, now: float, timeout_sec: float) -> bool:
        """Clears an arm whose timer has run out. True exactly once, when it
        does, so the caller can log the (silent, on screen) expiry."""
        if self._armed_at is not None and self.mode(now, timeout_sec) is not PadMode.ARMED:
            self._armed_at = None
            return True
        return False


@dataclass(frozen=True)
class PadView:
    """Everything the window needs to draw one frame, assembled by app.py.
    The window holds no trading state of its own -- it renders this and
    posts key presses back."""

    mode: PadMode
    symbol: str | None
    sizing: PadSizing | None
    quote_age: float | None
    armed_remaining: float | None
    dry_run: bool
    risk_usd: float
    max_quote_age: float
    feed_note: str | None = None   # e.g. "subscribing...", "waiting for first tick"


# -- the bracket ---------------------------------------------------------------


def recompute_bracket_exit(fill_price: float, sizing: PadSizing) -> BracketExitPrices:
    """The stop/target children, anchored to `fill_price` rather than the
    (possibly slipped) price at F4 -- same reasoning the children are
    already sized from the parent's actual fill quantity rather than the
    requested one (see BracketPlan). Called again on every parent fill
    event, including a resize, using the order's running average fill
    price, so the exit keeps tracking the true cost basis through partial
    fills at different prices.

    Reads stop_distance / stop_trigger_lead_pct / nominal_r from the FIRE
    RECORD (`sizing`, frozen at F4), never from live state -- see the
    module docstring, idea 2. The formula itself is _exit_prices.
    """
    return _exit_prices(
        fill_price, sizing.stop_distance, sizing.stop_trigger_lead_pct, sizing.nominal_r
    )


@dataclass(frozen=True)
class BracketPlan:
    """The PLANNED bracket for one fire -- what would be sent to IB if the
    parent filled at exactly the price at F4, with no slippage.

    Exists so the dry run (tunables.order_pad_dry_run True) logs something
    concrete rather than nothing: what you read in scanner.log during
    testing is what would go out if the fill matches. A real fire's ACTUAL
    stop/target are recomputed from the real fill price -- see app.py's
    _on_pad_parent_fill / recompute_bracket_exit -- and can differ from
    these planned figures whenever the entry slips.

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
            f"(marketable, capped at the price at F4 + entry slippage allowance); children "
            f"OCA={self.oca_group}, re-anchored to the parent's actual fill price: "
            f"SELL STP LMT {self.stop_price:.2f} (trigger {self.stop_trigger_price:.2f}) / "
            f"SELL LMT {self.target_price:.2f} -- planned, assuming the fill matches"
        )


def bracket_plan_to_dict(plan: BracketPlan) -> dict:
    """`plan`'s fields plus its human-readable describe() text, for the
    order history log."""
    return {**asdict(plan), "describe": plan.describe()}


def bracket_plan(sizing: PadSizing) -> BracketPlan:
    """The bracket for `sizing`: a marketable-limit parent plus two
    OCA-grouped children. Caller must have passed validate_fire (which
    includes sanity_problem, so the None-able prices are all present).

    The parent is a limit rather than a market order so the fill can't slip
    past the entry_slippage_allowance -- priced at max_entry_price, which is
    normally above the ask (so it crosses and fills like a market order) but
    refuses to chase a sudden run-up into a position whose displayed R no
    longer holds.

    stop/trigger/target are the same tick-rounded figures the pad displayed
    (live_sizing computes them through the same _exit_prices formula the
    real fill-time re-anchor uses), i.e. exactly what a fill right at the
    price on F4 would produce.
    """
    assert sizing.stop_price is not None
    assert sizing.stop_trigger_price is not None
    assert sizing.target_price is not None
    return BracketPlan(
        symbol=sizing.symbol,
        quantity=sizing.shares,
        entry_limit=sizing.entry_limit,
        stop_price=sizing.stop_price,
        stop_trigger_price=sizing.stop_trigger_price,
        target_price=sizing.target_price,
        # Millisecond stamp, not seconds: with F4 dropping back to Loaded a
        # second fire on the same symbol inside one second is possible, and
        # two brackets sharing an OCA group would cancel each other's legs.
        oca_group=f"pad-{sizing.symbol}-{int(sizing.computed_at.timestamp() * 1000)}",
    )
