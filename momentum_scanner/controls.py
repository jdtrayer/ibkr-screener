"""
Textual sidebar widget for adjusting the live Tunables (persistence + spike
thresholds) with buttons instead of editing config.py + restarting.

Purely a view over the shared Tunables instance -- it mutates it via
tunables.bump() and re-renders its own value labels, but no business logic
(PersistenceTracker, spikes.py) lives here.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Input, Static

from .tunables import TUNABLE_SPECS, Tunables, bump

_PERSISTENCE_ATTRS = {"persistence_required", "persistence_top_n", "persistence_reset_sec"}
_SCALP_ATTRS = {"scalp_position_usd", "scalp_target_usd", "scalp_rr_ratio"}
_SLOT_ATTRS = {"slot_reentry_cooldown_sec", "max_live_symbols"}


class TunablesPanel(VerticalScroll):
    DEFAULT_CSS = """
    TunablesPanel {
        width: 32;
        border: solid $primary;
        padding: 0 1;
    }
    TunablesPanel .section-header {
        text-style: bold;
        margin-top: 1;
    }
    TunablesPanel Horizontal {
        height: 3;
        align: left middle;
    }
    TunablesPanel .tunable-label {
        width: 1fr;
    }
    TunablesPanel .tunable-value {
        width: 6;
        text-align: right;
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
        value = getattr(self.tunables, spec.attr)
        return Horizontal(
            Static(spec.label, classes="tunable-label"),
            Button("-", id=f"dec-{spec.attr}"),
            Static(spec.fmt(value), id=f"val-{spec.attr}", classes="tunable-value"),
            Button("+", id=f"inc-{spec.attr}"),
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
    Sidebar widget for manually holding a symbol out of live tracking --
    distinct from every automatic filter/eviction path in filters.py. Purely
    a view: it reads the typed symbol and posts an Action message, app.py
    owns the actual ignore-set/timer state and calls refresh_status() back.
    """

    DEFAULT_CSS = """
    SymbolActionsPanel {
        width: 32;
        border: solid $primary;
        padding: 0 1;
    }
    SymbolActionsPanel .section-header {
        text-style: bold;
        margin-top: 1;
    }
    SymbolActionsPanel Horizontal {
        height: 3;
        align: left middle;
    }
    SymbolActionsPanel #ignore-status {
        color: $text-muted;
    }
    """

    class Action(Message):
        def __init__(self, symbol: str, action: str) -> None:
            self.symbol = symbol
            self.action = action  # "short" | "daily" | "clear"
            super().__init__()

    def compose(self) -> ComposeResult:
        yield Static("Ignore symbol", classes="section-header")
        yield Input(placeholder="SYMBOL", id="ignore-symbol-input")
        yield Horizontal(
            Button("5m", id="ignore-short"),
            Button("Today", id="ignore-daily"),
            Button("Clear", id="ignore-clear"),
        )
        yield Static("", id="ignore-status")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        field = self.query_one("#ignore-symbol-input", Input)
        symbol = field.value.strip().upper()
        if not symbol:
            return
        action = {"ignore-short": "short", "ignore-daily": "daily", "ignore-clear": "clear"}.get(
            event.button.id or ""
        )
        if action is None:
            return
        self.post_message(self.Action(symbol, action))
        field.value = ""

    def refresh_status(self, ignored_today: set[str], ignored_short: set[str]) -> None:
        bits = []
        if ignored_today:
            bits.append("Today: " + ", ".join(sorted(ignored_today)))
        if ignored_short:
            bits.append("5m: " + ", ".join(sorted(ignored_short)))
        self.query_one("#ignore-status", Static).update("\n".join(bits))
