"""
Textual sidebar widget for adjusting the live Tunables (persistence + spike
thresholds) with buttons instead of editing config.py + restarting.

Purely a view over the shared Tunables instance -- it mutates it via
tunables.bump() and re-renders its own value labels, but no business logic
(PersistenceTracker, spikes.py) lives here.
"""
from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Button, DataTable, Input, Static

from .tunables import TUNABLE_SPECS, Tunables, bump

_PERSISTENCE_ATTRS = {"persistence_required", "persistence_top_n", "persistence_reset_sec"}
_SCALP_ATTRS = {"scalp_position_usd", "scalp_target_usd", "scalp_rr_ratio"}
_SLOT_ATTRS = {
    "slot_reentry_cooldown_sec", "max_live_symbols",
    "scorer_reserved_slots", "scorer_admit_min_score", "dead_hold_sec",
}


class TunablesPanel(VerticalScroll):
    DEFAULT_CSS = """
    TunablesPanel {
        width: 36;
        border: solid $primary;
        padding: 0 1;
    }
    TunablesPanel .section-header {
        text-style: bold;
        margin-top: 1;
    }
    TunablesPanel Horizontal {
        height: 1;
        margin-bottom: 1;
        align: left middle;
    }
    TunablesPanel .tunable-label {
        width: 1fr;
        padding-right: 1;
    }
    TunablesPanel .tunable-value {
        width: 8;
        text-align: right;
        padding: 0 1;
    }
    TunablesPanel Button {
        min-width: 3;
        width: 3;
    }
    """

    def __init__(self, tunables: Tunables, **kwargs):
        super().__init__(**kwargs)
        self.tunables = tunables

    def compose(self) -> ComposeResult:
        yield Static("Persistence", classes="section-header")
        for spec in TUNABLE_SPECS:
            if spec.attr not in _PERSISTENCE_ATTRS:
                continue
            yield self._row(spec)
        yield Static("Spike", classes="section-header")
        for spec in TUNABLE_SPECS:
            if spec.attr in _PERSISTENCE_ATTRS or spec.attr in _SCALP_ATTRS or spec.attr in _SLOT_ATTRS:
                continue
            yield self._row(spec)
        yield Static("Scalp", classes="section-header")
        for spec in TUNABLE_SPECS:
            if spec.attr not in _SCALP_ATTRS:
                continue
            yield self._row(spec)
        yield Static("Slots", classes="section-header")
        for spec in TUNABLE_SPECS:
            if spec.attr not in _SLOT_ATTRS:
                continue
            yield self._row(spec)

    def _row(self, spec) -> Horizontal:
        # compact=True -- same built-in borderless/single-line Button style
        # used for SymbolActionsPanel's Add/Clear, replacing the old
        # hand-rolled `border: none !important; height: 2` override with the
        # mechanism Textual already provides for this.
        value = getattr(self.tunables, spec.attr)
        return Horizontal(
            Static(spec.label, classes="tunable-label"),
            Button("-", id=f"dec-{spec.attr}", compact=True),
            Static(spec.fmt(value), id=f"val-{spec.attr}", classes="tunable-value"),
            Button("+", id=f"inc-{spec.attr}", compact=True),
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if "-" not in button_id:
            return
        direction_key, attr = button_id.split("-", 1)
        direction = -1 if direction_key == "dec" else 1
        spec = next((s for s in TUNABLE_SPECS if s.attr == attr), None)
        if spec is None:
            return
        bump(self.tunables, spec, direction)
        self.query_one(f"#val-{attr}", Static).update(spec.fmt(getattr(self.tunables, attr)))


class SymbolActionsPanel(VerticalScroll):
    """
    Sidebar widget for manually marking a symbol non-tradable (a broker-side
    restriction with no IBKR-queryable signal -- see config.py's
    NON_TRADABLE_STATE_FILE). Purely a view: it reads the typed symbol and
    posts an Action message, app.py owns the actual ignore-set/timer state
    (persisted to disk, expiring at the next trading day) and calls
    refresh_status() back.
    """

    DEFAULT_CSS = """
    SymbolActionsPanel {
        width: 36;
        border: solid $primary;
        padding: 0 1;
    }
    SymbolActionsPanel .section-header {
        text-style: bold;
        margin-top: 1;
    }
    SymbolActionsPanel Horizontal {
        height: 1;
        margin-top: 1;
        align: left middle;
    }
    SymbolActionsPanel Input {
        margin-top: 1;
    }
    SymbolActionsPanel #ignore-status-table {
        margin-top: 1;
        height: 6;
    }
    """

    class Action(Message):
        def __init__(self, symbol: str, action: str) -> None:
            self.symbol = symbol
            self.action = action  # "add" | "clear"
            super().__init__()

    def compose(self) -> ComposeResult:
        yield Static("Non-tradable (til next day)", classes="section-header")
        # compact=True (built-in Textual style, same idea as TunablesPanel's
        # borderless +/- buttons) drops the border and shrinks these from the
        # default height-3 boxes down to a single line each.
        yield Input(placeholder="SYMBOL", id="ignore-symbol-input", compact=True)
        yield Horizontal(
            Button("Add", id="ignore-add", compact=True),
            Button("Clear", id="ignore-clear", compact=True),
        )
        # cursor_type="none" -- this is a small read-only status list, not
        # something to navigate/select; show_cursor=False drops the focus
        # outline that "none" alone still leaves on the first cell.
        yield DataTable(id="ignore-status-table", cursor_type="none", show_cursor=False)

    def on_mount(self) -> None:
        self.query_one("#ignore-status-table", DataTable).add_columns(("Sym", "sym"), ("Time left", "time_left"))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit("add")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        action = {"ignore-add": "add", "ignore-clear": "clear"}.get(event.button.id or "")
        if action is not None:
            self._submit(action)

    def _submit(self, action: str) -> None:
        field = self.query_one("#ignore-symbol-input", Input)
        symbol = field.value.strip().upper()
        if not symbol:
            return
        self.post_message(self.Action(symbol, action))
        field.value = ""

    def refresh_status(self, ignored_until: dict[str, datetime], now: datetime) -> None:
        # In-place sync (add/remove rows, update the Time left cell) rather
        # than a full clear+rebuild every call -- #ignore-status-table now
        # has a fixed height (see DEFAULT_CSS) and scrolls internally once
        # the list outgrows it, so a held-out symbol scrolled into view
        # must not get yanked back to the top every render tick just
        # because the countdown text changed, same reasoning as
        # sync_news_table.
        table = self.query_one("#ignore-status-table", DataTable)
        wanted = sorted(ignored_until)
        existing = {row_key.value for row_key in table.rows}
        for stale in existing - set(wanted):
            table.remove_row(stale)
        for sym in wanted:
            remaining = Text(self._fmt_remaining(ignored_until[sym] - now), style="dim")
            if sym in existing:
                table.update_cell(sym, "time_left", remaining)
            else:
                table.add_row(Text(sym, style="bold"), remaining, key=sym)

    @staticmethod
    def _fmt_remaining(delta) -> str:
        hours = max(delta.total_seconds(), 0) / 3600
        if hours >= 24:
            return f"{int(hours // 24)}d {hours % 24:.0f}h"
        return f"{hours:.1f}h"

