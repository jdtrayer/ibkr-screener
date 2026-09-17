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
import json
from dataclasses import dataclass, field
from datetime import datetime

import pytest
from ib_async import LimitOrder, Order, StopLimitOrder, Trade
from ib_async.objects import CommissionReport, Execution, Fill
from ib_async.order import OrderStatus

from momentum_scanner import config, order_history

from test_orderpad_wiring import make_app, make_state


@pytest.fixture(autouse=True)
def isolated_order_history_dir(tmp_path, monkeypatch):
    """Same reasoning as test_orderpad_wiring.py's fixture of the same name
    -- this file fires for real (FakeIB, no dry run) and would otherwise
    write into the real ./logs/orders/ on every test run."""
    monkeypatch.setattr(config, "ORDER_HISTORY_DIR", str(tmp_path / "orders"))


@dataclass
class FakeIB:
    """Fabricates Trades the way ib.placeOrder does (auto-assigns orderId,
    wraps the order in a live-updating Trade), without a socket.

    Reuses the SAME Trade object for a second placeOrder call on an orderId
    it's already seen, exactly like the real ib.placeOrder (a modification
    looks up the existing Trade by (clientId, orderId) and returns it rather
    than building a new one) -- app.py's exit-fill wiring (_on_pad_parent_fill)
    depends on that identity to decide whether to subscribe fillEvent, so a
    fake that handed back a fresh object every time would let a bug there
    pass silently."""

    placed: list = field(default_factory=list)  # [(contract, order), ...] in call order
    _next_id: int = 1000
    _trades: dict = field(default_factory=dict)  # orderId -> Trade

    def placeOrder(self, contract, order: Order) -> Trade:
        if not order.orderId:
            order.orderId = self._next_id
            self._next_id += 1
        self.placed.append((contract, order))
        trade = self._trades.get(order.orderId)
        if trade is not None:
            trade.order = order  # modify in place, same object
            return trade
        trade = Trade(contract, order, OrderStatus(orderId=order.orderId), [], [])
        self._trades[order.orderId] = trade
        return trade


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


def add_fill(trade: Trade, shares: float, price: float = 4.12) -> Fill:
    """price defaults to 4.12 -- the `last` every FakeIB test in this file
    arms at, so a fill "matches the arm" (no slippage) unless a test passes
    a different price on purpose (see the entry-slippage tests below).
    avgPrice mirrors price: _on_pad_parent_fill anchors the stop/target
    recompute to execution.avgPrice (falling back to execution.price), not
    orderStatus.avgFillPrice -- see its comment for why."""
    fill = Fill(
        trade.contract,
        Execution(orderId=trade.order.orderId, shares=shares, price=price, avgPrice=price),
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
    bracket = app._pad_order_ids[parent.orderId]
    assert bracket.stop_trade is None

    plan = bracket.plan
    add_fill(parent_trade, shares=20)  # less than plan.quantity, price matches the arm
    app._on_pad_parent_fill(parent_trade, parent_trade.fills[-1])

    assert len(app.ib.placed) == 3  # parent + stop + target
    _stop_contract, stop = app.ib.placed[1]
    _tgt_contract, target = app.ib.placed[2]
    assert isinstance(stop, StopLimitOrder) and stop.action == "SELL" and stop.totalQuantity == 20
    assert isinstance(target, LimitOrder) and target.action == "SELL" and target.totalQuantity == 20
    # STP LMT, not plain STP -- a plain STP's outsideRth is silently ignored
    # on US stocks (confirmed live 2026-09-17: TURB's stop never triggered
    # falling through it during extended hours). lmtPrice is the REAL stop
    # (fill_price - stop_distance); auxPrice (the trigger) sits above it --
    # fixed 2026-09-17, previously inverted (see config.STOP_TRIGGER_LEAD_PCT).
    assert stop.lmtPrice == pytest.approx(plan.stop_price)
    assert stop.auxPrice == pytest.approx(plan.stop_trigger_price)
    assert stop.auxPrice > stop.lmtPrice
    assert target.lmtPrice == pytest.approx(plan.target_price)
    assert stop.ocaGroup == target.ocaGroup == plan.oca_group
    assert stop.ocaType == target.ocaType == 1
    assert any("FILLED 20/" in (r or "") for r in app.pad.results)
    assert bracket.realized_stop_limit == pytest.approx(plan.stop_price)
    assert bracket.realized_stop_trigger == pytest.approx(plan.stop_trigger_price)


def test_entry_slippage_does_not_widen_realized_risk():
    """Regression for the exact bug reported live 2026-09-17 (order 25002:
    armed 2.26, filled 2.28, planned risk $9.92, realized $11.16) -- the
    stop must re-anchor to the actual fill, not the stale armed price."""
    state = make_state("CVDK", last=2.26)
    state.conid = 12345
    app = fire_live([state])
    _contract, parent = app.ib.placed[0]
    parent_trade = Trade(_contract, parent, OrderStatus(orderId=parent.orderId), [], [])
    bracket = app._pad_order_ids[parent.orderId]
    snapshot = bracket.snapshot

    add_fill(parent_trade, shares=snapshot.shares, price=2.28)  # filled 2c above the arm
    app._on_pad_parent_fill(parent_trade, parent_trade.fills[-1])

    _stop_contract, stop = app.ib.placed[1]
    realized_risk = snapshot.shares * (2.28 - stop.lmtPrice)
    assert realized_risk == pytest.approx(snapshot.risk_usd, abs=0.05)
    # Sanity: the OLD (broken) construction would have kept the stop at the
    # armed-price-based level regardless of the fill, realizing MORE risk.
    assert stop.lmtPrice > bracket.plan.stop_price


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


# -- exit fills (the stop or target leg itself) -------------------------------


def _fill_parent(app, parent_trade, shares):
    add_fill(parent_trade, shares=shares)
    app._on_pad_parent_fill(parent_trade, parent_trade.fills[-1])


def _read_order_history():
    path = order_history.file_for(datetime.now(config.TZ).date())
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_target_fill_is_reported_and_the_stop_cancel_is_logged_not_erased():
    state = make_state("CVDK", last=4.12)
    state.conid = 12345
    app = fire_live([state])
    _contract, parent = app.ib.placed[0]
    parent_trade = Trade(_contract, parent, OrderStatus(orderId=parent.orderId), [], [])
    _fill_parent(app, parent_trade, 31)
    bracket = app._pad_order_ids[parent.orderId]
    target_trade = bracket.target_trade
    stop_trade = bracket.stop_trade

    fill = add_fill(target_trade, shares=31)
    target_trade.fillEvent.emit(target_trade, fill)

    assert any("TARGET FILLED" in (r or "") for r in app.pad.results)

    stop_trade.cancelledEvent.emit(stop_trade)  # IB's OCA auto-cancel of the loser
    # Cancelling the loser must not clobber the winner's result message --
    # regression for a dispatcher that didn't tell stop from target apart.
    assert app.pad.results[-1].startswith("TARGET FILLED")


def test_stop_fill_is_identified_as_the_stop_leg():
    state = make_state("CVDK", last=4.12)
    state.conid = 12345
    app = fire_live([state])
    _contract, parent = app.ib.placed[0]
    parent_trade = Trade(_contract, parent, OrderStatus(orderId=parent.orderId), [], [])
    _fill_parent(app, parent_trade, 31)
    bracket = app._pad_order_ids[parent.orderId]

    fill = add_fill(bracket.stop_trade, shares=31)
    bracket.stop_trade.fillEvent.emit(bracket.stop_trade, fill)

    assert any("STOP FILLED" in (r or "") for r in app.pad.results)


def test_stop_fill_logs_fill_quality_against_limit_and_trigger():
    """User's empirical-tuning ask: enough on every STOP EXIT_FILL to build
    a fill rate for stop-limits and tune stop_trigger_lead_pct from real
    data rather than guessing."""
    state = make_state("CVDK", last=4.12)
    state.conid = 12345
    app = fire_live([state])
    _contract, parent = app.ib.placed[0]
    parent_trade = Trade(_contract, parent, OrderStatus(orderId=parent.orderId), [], [])
    _fill_parent(app, parent_trade, 31)
    bracket = app._pad_order_ids[parent.orderId]
    limit_price = bracket.realized_stop_limit
    trigger_price = bracket.realized_stop_trigger

    fill_price = limit_price - 0.02  # filled worse than the limit
    fill = add_fill(bracket.stop_trade, shares=31, price=fill_price)
    bracket.stop_trade.fillEvent.emit(bracket.stop_trade, fill)

    exit_fill = next(r for r in _read_order_history() if r["event"] == "EXIT_FILL")
    assert exit_fill["leg"] == "STOP"
    assert exit_fill["stop_limit_price"] == pytest.approx(limit_price)
    assert exit_fill["stop_trigger_price"] == pytest.approx(trigger_price)
    assert exit_fill["slipped_past_limit"] is True
    assert exit_fill["fill_vs_limit"] == pytest.approx(-0.02)
    assert exit_fill["fill_vs_trigger"] == pytest.approx(fill_price - trigger_price)


def test_target_fill_does_not_log_stop_only_fill_quality_fields():
    state = make_state("CVDK", last=4.12)
    state.conid = 12345
    app = fire_live([state])
    _contract, parent = app.ib.placed[0]
    parent_trade = Trade(_contract, parent, OrderStatus(orderId=parent.orderId), [], [])
    _fill_parent(app, parent_trade, 31)
    bracket = app._pad_order_ids[parent.orderId]

    fill = add_fill(bracket.target_trade, shares=31, price=bracket.realized_target_price)
    bracket.target_trade.fillEvent.emit(bracket.target_trade, fill)

    exit_fill = next(r for r in _read_order_history() if r["event"] == "EXIT_FILL")
    assert exit_fill["leg"] == "TARGET"
    assert "slipped_past_limit" not in exit_fill
    assert "fill_vs_limit" not in exit_fill


def test_exit_fill_subscription_is_not_duplicated_across_a_resize():
    """A second (resizing) parent fill must not stack a second fillEvent
    handler on the same stop/target Trade -- see FakeIB's docstring for why
    it reuses the same object on a modify, which is what would expose this."""
    state = make_state("CVDK", last=4.12)
    state.conid = 12345
    app = fire_live([state])
    _contract, parent = app.ib.placed[0]
    parent_trade = Trade(_contract, parent, OrderStatus(orderId=parent.orderId), [], [])
    _fill_parent(app, parent_trade, 20)
    _fill_parent(app, parent_trade, 15)  # resize, same stop/target orderIds
    bracket = app._pad_order_ids[parent.orderId]

    fill = add_fill(bracket.target_trade, shares=35)
    bracket.target_trade.fillEvent.emit(bracket.target_trade, fill)

    hits = [r for r in app.pad.results if r.startswith("TARGET FILLED")]
    assert len(hits) == 1, f"handler fired {len(hits)} times, expected exactly 1: {app.pad.results}"


def test_commission_report_for_a_tracked_order_is_not_dropped():
    """Just needs to not raise and to be filtered by order_id -- the actual
    write goes to order_history, exercised at the file level in
    test_order_history.py, not asserted on here."""
    state = make_state("CVDK", last=4.12)
    state.conid = 12345
    app = fire_live([state])
    order_id = next(iter(app._pad_order_ids))
    contract, order = app.ib.placed[0]
    trade = Trade(contract, order, OrderStatus(orderId=order_id), [], [])
    fill = add_fill(trade, shares=31)

    app._on_pad_commission_report(trade, fill, CommissionReport(commission=0.35, realizedPNL=12.5))
    app._on_pad_commission_report(  # untracked order -- must be silently ignored
        Trade(contract, Order(orderId=999999), OrderStatus(orderId=999999), [], []),
        fill, CommissionReport(commission=1.0),
    )
