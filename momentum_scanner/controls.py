"""
Textual sidebar widget for adjusting the live Tunables (persistence + spike
thresholds) with buttons instead of editing config.py + restarting.

Purely a view over the shared Tunables instance -- it mutates it via
tunables.bump() and re-renders its own value labels, but no business logic
(PersistenceTracker, spikes.py) lives here.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static

from .tunables import TUNABLE_SPECS, Tunables, bump

_PERSISTENCE_ATTRS = {"persistence_required", "persistence_top_n", "persistence_reset_sec"}
_SCALP_ATTRS = {"scalp_target_usd", "scalp_rr_ratio"}


class TunablesPanel(Vertical):
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
            if spec.attr in _PERSISTENCE_ATTRS or spec.attr in _SCALP_ATTRS:
                continue
            yield self._row(spec)
        yield Static("Scalp", classes="section-header")
        for spec in TUNABLE_SPECS:
            if spec.attr not in _SCALP_ATTRS:
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
