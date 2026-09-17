"""
Coverage for the real order-submission path added on top of the dry-run-only
phase 1 (see tests/test_orderpad_wiring.py for the arm/fire/pin wiring, and
memory: order_pad_2026_09_16 for the phase history).

ib_async's Order/Trade/Fill are plain dataclasses that don't need a live
connection to construct, so this stays in the same "pure logic, no Tk, no
IB socket" testing lane as the rest of the order-pad suite (see memory:
testing_approach) -- FakeIB stands in for app.ib exactly the way FakePad
stands in for OrderPadWindow, fabricating Trade objects instead of talking
to TWS. The actual placeOrder round-trip still needs a real paper session.
"""
from dataclasses import dataclass, field
from datetime import datetime

from ib_async import LimitOrder, Order, StopLimitOrder, Trade
from ib_async.objects import CommissionReport, Execution, Fill
from ib_async.order import OrderStatus

from momentum_scanner import config

from test_orderpad_wiring import make_app, make_state


@dataclass
class FakeIB:
    """Fabricates Trades the way ib.placeOrder does (auto-assigns orderId,
    wraps the order in a live-updating Trade), without a socket."""

    placed: list = field(default_factory=list)  # [(contract, order), ...] in call order
    _next_id: int = 1000

    def placeOrder(self, contract, order: Order) -> Trade:
        if not order.orderId:
            order.orderId = self._next_id
            self._next_id += 1
        self.placed.append((contract, order))
        return Trade(contract, order, OrderStatus(orderId=order.orderId), [], [])


def fire_live(states=(), port=7497, dry_run=False):
    """An armed, fired app with submission actually enabled -- the shared
    setup for every test below."""
    app = make_app(states)
    app.ib = FakeIB()
    app.tunables.order_pad_dry_run = dry_run
    orig_port = config.IB_PORT
    config.IB_PORT = port
    try:
        app._arm(states[0].symbol)
        app._on_pad_fire()
    finally:
        config.IB_PORT = orig_port
    return app


def add_fill(trade: Trade, shares: float) -> Fill:
    fill = Fill(
        trade.contract,
        Execution(orderId=trade.order.orderId, shares=shares),
        CommissionReport(),
        datetime.now(),
    )
    trade.fills.append(fill)
    return fill


# -- the paper-port guard ----------------------------------------------------


def test_live_port_blocks_submission_even_with_dry_run_off():
    app = fire_live([make_state("CVDK")], port=7496)  # 7496 = TWS live
    assert app.ib.placed == []
    assert any("paper account" in (b or "") for b in app.pad.blocks)


def test_paper_ports_are_allowed():
    for port in sorted(config.ORDER_PAD_PAPER_PORTS):
        state = make_state("CVDK")
        state.conid = 12345
        app = fire_live([state], port=port)
        assert len(app.ib.placed) == 1, f"port {port} should have been allowed"


# -- parent submission --------------------------------------------------------


def test_fire_submits_a_marketable_limit_parent():
    state = make_state("CVDK", last=4.12)
    state.conid = 12345
    app = fire_live([state])

    assert len(app.ib.placed) == 1
    contract, order = app.ib.placed[0]
    assert contract.conId == 12345
    # Regression: conId alone isn't enough for placeOrder -- IB rejected a
    # bare-conId contract live with "Error 321: Missing order exchange."
    assert contract.exchange == "SMART"
    assert contract.currency == "USD"
    assert isinstance(order, LimitOrder)
    assert order.action == "BUY"
    assert order.tif == "DAY"
    assert order.outsideRth is True
    assert order.orderId in app._pad_order_ids
    assert any(r.startswith("SENT") for r in app.pad.results)


def test_fire_blocked_without_a_qualified_contract():
    state = make_state("CVDK", last=4.12)
    state.conid = None
    app = fire_live([state])
    assert app.ib.placed == []
    assert any("qualified contract" in (b or "") for b in app.pad.blocks)


# -- fill-driven protective orders --------------------------------------------


def test_partial_fill_creates_protective_orders_sized_to_the_fill_not_the_plan():
    state = make_state("CVDK", last=4.12)
    state.conid = 12345
    app = fire_live([state])
    _contract, parent = app.ib.placed[0]
    parent_trade = Trade(_contract, parent, OrderStatus(orderId=parent.orderId), [], [])
    assert app._pad_order_ids[parent.orderId].stop_trade is None

    plan = app._pad_order_ids[parent.orderId].plan
    add_fill(parent_trade, shares=20)  # less than plan.quantity
    app._on_pad_parent_fill(parent_trade, parent_trade.fills[-1])

    assert len(app.ib.placed) == 3  # parent + stop + target
    _stop_contract, stop = app.ib.placed[1]
    _tgt_contract, target = app.ib.placed[2]
    assert isinstance(stop, StopLimitOrder) and stop.action == "SELL" and stop.totalQuantity == 20
    assert isinstance(target, LimitOrder) and target.action == "SELL" and target.totalQuantity == 20
    # STP LMT, not plain STP -- a plain STP's outsideRth is silently ignored
    # on US stocks (confirmed live 2026-09-17: TURB's stop never triggered
    # falling through it during extended hours). auxPrice is still the
    # trigger; lmtPrice sits ORDER_PAD_STOP_SLIPPAGE below it.
    assert stop.auxPrice == plan.stop_price
    assert stop.lmtPrice == round(plan.stop_price - config.ORDER_PAD_STOP_SLIPPAGE, 2)
    assert target.lmtPrice == plan.target_price
    assert stop.ocaGroup == target.ocaGroup == plan.oca_group
    assert stop.ocaType == target.ocaType == 1
    assert any("FILLED 20/" in (r or "") for r in app.pad.results)


def test_second_fill_resizes_the_same_protective_orders_instead_of_stacking():
    state = make_state("CVDK", last=4.12)
    state.conid = 12345
    app = fire_live([state])
    _contract, parent = app.ib.placed[0]
    parent_trade = Trade(_contract, parent, OrderStatus(orderId=parent.orderId), [], [])

    add_fill(parent_trade, shares=20)
    app._on_pad_parent_fill(parent_trade, parent_trade.fills[-1])
    first_stop_id = app.ib.placed[1][1].orderId
    first_target_id = app.ib.placed[2][1].orderId

    add_fill(parent_trade, shares=15)  # cumulative 35
    app._on_pad_parent_fill(parent_trade, parent_trade.fills[-1])

    assert len(app.ib.placed) == 5  # parent + (stop, target) x2 -- modifies, not new pairs
    _stop_contract, stop2 = app.ib.placed[3]
    _tgt_contract, target2 = app.ib.placed[4]
    assert stop2.orderId == first_stop_id
    assert target2.orderId == first_target_id
    assert stop2.totalQuantity == 35
    assert target2.totalQuantity == 35


# -- cancellation and IB-side errors ------------------------------------------


def test_parent_cancelled_with_nothing_filled_clears_tracking():
    state = make_state("CVDK", last=4.12)
    state.conid = 12345
    app = fire_live([state])
    _contract, parent = app.ib.placed[0]
    parent_trade = Trade(_contract, parent, OrderStatus(orderId=parent.orderId), [], [])

    app._on_pad_parent_cancelled(parent_trade)

    assert parent.orderId not in app._pad_order_ids
    assert any("cancelled" in (r or "") for r in app.pad.results)


def test_ib_error_on_a_tracked_order_surfaces_on_the_pad():
    state = make_state("CVDK", last=4.12)
    state.conid = 12345
    app = fire_live([state])
    order_id = next(iter(app._pad_order_ids))

    app._on_ib_order_error(order_id, 201, "Order rejected - reason", None)

    assert any("201" in (r or "") for r in app.pad.results)


def test_ib_error_on_an_untracked_order_is_ignored():
    state = make_state("CVDK", last=4.12)
    state.conid = 12345
    app = fire_live([state])
    before = list(app.pad.results)

    app._on_ib_order_error(999999, 201, "unrelated", None)

    assert app.pad.results == before
