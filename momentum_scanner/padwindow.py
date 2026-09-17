"""
The order pad's actual window: a small always-on-top Tk toplevel that sits
over TWS, showing one armed symbol's frozen sizing snapshot.

Tk rather than Textual because this is a real window-manager window --
always-on-top, ~300x150, remembering its position between sessions -- which
a terminal UI cannot be. It runs in the SAME process and on the SAME asyncio
loop as the scanner (see pump()), so it reads live SymbolStates and calls
sizing.compute_sizing() directly, with no IPC and no second IB connection
burning a duplicate market-data line on the armed symbol.

Purely a view, in the same spirit as controls.py: it renders whatever
snapshot it's handed and posts key presses back through callbacks. All the
arm/validate/pin state lives in app.py, and the snapshot/validation logic in
orderpad.py.

The pad takes keyboard focus when armed, so firing is one keypress without a
focus dance -- see config.ORDER_PAD_ARM_KEY/ORDER_PAD_FIRE_KEY for why both
are function keys.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import tkinter as tk
from pathlib import Path
from typing import Callable

from . import config, display
from .orderpad import ArmedSnapshot

log = logging.getLogger(__name__)

PUMP_INTERVAL_SEC = 0.02      # Tk event pump -- 50Hz, keeps the window responsive
REFRESH_INTERVAL_SEC = 0.25   # how often the quote-age readout is redrawn
GEOMETRY_SAVE_INTERVAL_SEC = 2.0

_BG = "#1e1e1e"
_FG = "#d0d0d0"
_DIM = "#808080"
_ARMED_BG = "#2e7d32"
_BLOCKED_BG = "#b71c1c"
_DISARMED_BG = "#3a3a3a"
_FIRED_BG = "#1565c0"

# Rich style names (display.py's) -> Tk colors, so the pad inherits the
# table's existing S/spr and target/stop color thresholds rather than
# hardcoding a second set that could drift out of step with them. Unknown
# style names fall back to the default foreground.
_STYLE_COLORS = {
    "red3": "#d70000",
    "yellow": "#d7d700",
    "green3": "#00af00",
    "dim": _DIM,
    "": _FG,
}


def _color_for(style: str) -> str:
    return _STYLE_COLORS.get(style, _FG)


class OrderPadWindow:
    """The pad window. Created once at startup and withdrawn/deiconified as
    it's armed and disarmed, rather than built and destroyed per arm -- a
    rebuilt window loses its position and flickers, and arming needs to be
    instant."""

    def __init__(
        self,
        on_fire: Callable[[], None],
        on_disarm: Callable[[], None],
        on_manual_arm: Callable[[str], None],
        quote_age_provider: Callable[[], float | None],
    ):
        self._on_fire = on_fire
        self._on_disarm = on_disarm
        self._on_manual_arm = on_manual_arm
        self._quote_age_provider = quote_age_provider

        self.snapshot: ArmedSnapshot | None = None
        self._block_reason: str | None = None
        self._status_override: str | None = None
        self._fired = False
        self._last_geometry: str | None = None
        self._geometry_saved_at = 0.0
        self._closed = False

        self._root = tk.Tk()
        self._root.title("Order Pad")
        self._root.configure(bg=_BG)
        self._root.attributes("-topmost", True)
        self._root.resizable(True, True)
        self._root.geometry(self._load_geometry())
        # Closing the window hides it rather than destroying it -- the next
        # arm brings it straight back, and a destroyed Tk root would take the
        # pump loop (and with it any chance of re-arming) down with it.
        self._root.protocol("WM_DELETE_WINDOW", self._hide)

        self._build_widgets()
        self._bind_keys()
        self._render()
        self._root.withdraw()  # stays hidden until the first arm

    # -- construction ------------------------------------------------------

    def _build_widgets(self) -> None:
        mono = ("TkFixedFont", 10)
        mono_bold = ("TkFixedFont", 11, "bold")

        self._status = tk.Label(
            self._root, text="", font=mono_bold, bg=_DISARMED_BG, fg=_FG, anchor="w", padx=6
        )
        self._status.pack(fill="x")

        body = tk.Frame(self._root, bg=_BG, padx=6, pady=2)
        body.pack(fill="both", expand=True)

        def line() -> tk.Frame:
            row = tk.Frame(body, bg=_BG)
            row.pack(fill="x")
            return row

        def cell(parent, width, font=mono, color=_FG) -> tk.Label:
            lbl = tk.Label(parent, text="", font=font, bg=_BG, fg=color, anchor="w", width=width)
            lbl.pack(side="left")
            return lbl

        row1 = line()
        self._price = cell(row1, 8, mono_bold)
        self._shares = cell(row1, 9)
        self._risk = cell(row1, 14)

        row2 = line()
        self._stop = cell(row2, 12, color=_color_for(display.SIZING_STOP_STYLE))
        self._target = cell(row2, 12, color=_color_for(display.SIZING_TARGET_STYLE))

        row3 = line()
        self._s_spr = cell(row3, 12)
        self._eff_r = cell(row3, 14)

        self._message = tk.Label(
            self._root, text="", font=mono, bg=_BG, fg=_DIM, anchor="w", padx=6,
            wraplength=290, justify="left",
        )
        self._message.pack(fill="x")

        # Manual fallback for when the row-transfer path isn't usable (e.g.
        # the symbol is live but the cursor is somewhere else). Deliberately
        # last in the tab order and never focused automatically -- it must
        # never swallow a fire press, which is why fire/disarm are bound with
        # bind_all rather than on the root alone.
        self._entry = tk.Entry(
            self._root, font=mono, bg="#2a2a2a", fg=_FG, insertbackground=_FG,
            relief="flat", width=10,
        )
        self._entry.pack(fill="x", padx=6, pady=(0, 4))
        self._entry.bind("<Return>", self._on_entry_submit)

    def _bind_keys(self) -> None:
        # bind_all, not bind: the symbol Entry has its own focus, and a fire
        # press must work regardless of which widget inside the pad holds it.
        self._root.bind_all(f"<{config.ORDER_PAD_FIRE_KEY}>", lambda _e: self._fire())
        self._root.bind_all("<Escape>", lambda _e: self._on_disarm())
        # Same arm key as the scanner TUI (Tk keysyms are uppercase where
        # Textual's are lowercase), so a drifted/blocked snapshot can be
        # refreshed without alt-tabbing back to reacquire the row cursor.
        # Re-arms whatever symbol is currently on screen -- there's no row
        # cursor here, so this is a no-op while fully disarmed.
        self._root.bind_all(f"<{config.ORDER_PAD_ARM_KEY.upper()}>", lambda _e: self._rearm())

    # -- app-facing API ----------------------------------------------------

    def arm(self, snapshot: ArmedSnapshot) -> None:
        """Show `snapshot`, raise the pad and take keyboard focus."""
        self.snapshot = snapshot
        self._block_reason = None
        self._status_override = None
        self._fired = False
        if self._closed:
            return
        try:
            self._entry.delete(0, "end")
            self._root.deiconify()
            self._root.lift()
            self._root.attributes("-topmost", True)
            # focus_force, not focus_set: focus is being taken from another
            # application (TWS), which a cooperative focus request won't do.
            self._root.focus_force()
        except tk.TclError:
            self._closed = True
            return
        self._render()

    def prompt_manual_entry(self) -> None:
        """Raise the pad and focus its manual-entry box, without touching
        whatever is currently armed (or not). For F2 pressed with nothing
        under the row cursor -- typically a symbol the user wants to trade
        that isn't on either table at all, so there's nothing to transfer in
        and typing it is the only way in."""
        if self._closed:
            return
        try:
            self._root.deiconify()
            self._root.lift()
            self._root.attributes("-topmost", True)
            self._root.focus_force()
            self._entry.focus_set()
        except tk.TclError:
            self._closed = True

    def disarm(self, reason: str | None = None) -> None:
        """Clear the armed symbol. `reason` is shown for an involuntary
        disarm (the symbol left the pool, the session rolled over) so the pad
        doesn't just silently go blank."""
        self.snapshot = None
        self._block_reason = None
        self._status_override = reason
        self._fired = False
        self._render()

    def show_block(self, reason: str) -> None:
        """A fire attempt was refused -- stay armed, show why."""
        self._block_reason = reason
        self._render()

    def show_result(self, message: str) -> None:
        """Outcome of a fire that passed validation (or of its dry run).
        Moves the status from ARMED to FIRED -- a fired snapshot stays on
        screen (so the sizing that was sent is still visible) but won't fire
        again; F2 re-arms for another entry."""
        self._block_reason = None
        self._status_override = message
        self._fired = True
        self._render()

    def close(self) -> None:
        self._closed = True
        self._save_geometry(force=True)
        try:
            self._root.destroy()
        except tk.TclError:
            pass

    # -- event loop --------------------------------------------------------

    async def pump(self) -> None:
        """Drive Tk from the scanner's own asyncio loop.

        Single-threaded on purpose: everything the pad touches (self.states,
        the shared Tunables, the IB connection) belongs to this loop, so
        staying on it means no locking and no cross-thread marshalling. The
        cost is that a slow Tk callback stalls the scanner, which is why every
        callback here is trivial and anything real is handed to app.py.
        """
        loop = asyncio.get_running_loop()
        next_refresh = 0.0
        while not self._closed:
            try:
                self._root.update()
            except tk.TclError:
                return  # window destroyed out from under us; nothing left to pump
            now = loop.time()
            if now >= next_refresh:
                next_refresh = now + REFRESH_INTERVAL_SEC
                self._render()
                self._save_geometry()
            await asyncio.sleep(PUMP_INTERVAL_SEC)

    # -- internals ---------------------------------------------------------

    def _fire(self) -> None:
        if self.snapshot is None or self._fired:
            return
        self._on_fire()

    def _rearm(self) -> None:
        if self.snapshot is None:
            return
        self._on_manual_arm(self.snapshot.symbol)

    def _on_entry_submit(self, _event) -> None:
        symbol = self._entry.get().strip().upper()
        if symbol:
            self._on_manual_arm(symbol)

    def _hide(self) -> None:
        self._save_geometry(force=True)
        self._root.withdraw()

    def _render(self) -> None:
        # Every caller is either a Tk callback or app.py reacting to a state
        # change, neither of which should have to know whether the window is
        # still alive -- a closed pad quietly renders nothing.
        if self._closed:
            return
        try:
            self._render_unguarded()
        except tk.TclError:
            self._closed = True

    def _render_unguarded(self) -> None:
        snap = self.snapshot
        if snap is None:
            self._status.configure(text="DISARMED", bg=_DISARMED_BG, fg=_FG)
            for widget in (self._price, self._shares, self._risk, self._stop,
                           self._target, self._s_spr, self._eff_r):
                widget.configure(text="")
            self._message.configure(
                text=self._status_override or f"{config.ORDER_PAD_ARM_KEY} on a row to arm",
                fg=_DIM,
            )
            return

        age = self._quote_age_provider()
        age_txt = f"{age:.1f}s" if age is not None else "--"
        blocked = self._block_reason is not None
        if blocked:
            label, bg = "BLOCKED", _BLOCKED_BG
        elif self._fired:
            label, bg = "FIRED", _FIRED_BG
        else:
            label, bg = "ARMED", _ARMED_BG
        self._status.configure(
            text=f"{label}  {snap.symbol}".ljust(20) + age_txt,
            bg=bg,
            fg="#ffffff",
        )

        self._price.configure(text=f"{snap.price:.2f}")
        self._shares.configure(text=f"{snap.shares}sh")
        self._risk.configure(text=f"risk ${snap.risk_usd:,.0f}")
        self._stop.configure(text=f"stop {snap.stop_price:.2f}")
        self._target.configure(text=f"tgt  {snap.target_price:.2f}")

        if snap.stop_in_spreads is None:
            self._s_spr.configure(text="S/spr  -", fg=_DIM)
        else:
            self._s_spr.configure(
                text=f"S/spr {snap.stop_in_spreads:.1f}",
                fg=_color_for(display.stop_in_spreads_style(snap.stop_in_spreads)),
            )

        if snap.effective_r is None:
            self._eff_r.configure(text=f"{snap.nominal_r:.1f}/-R", fg=_DIM)
        else:
            self._eff_r.configure(
                text=f"{snap.nominal_r:.1f}/{snap.effective_r:.1f}R",
                fg=_DIM if snap.effective_r <= 0 else _FG,
            )

        if blocked:
            self._message.configure(text=self._block_reason, fg="#ff6b6b")
        elif self._status_override:
            self._message.configure(text=self._status_override, fg=_FG)
        else:
            self._message.configure(
                text=f"{config.ORDER_PAD_FIRE_KEY} fire  ·  Esc disarm", fg=_DIM
            )

    # -- geometry persistence ---------------------------------------------

    def _load_geometry(self) -> str:
        try:
            raw = json.loads(Path(config.ORDER_PAD_STATE_FILE).read_text())
        except FileNotFoundError:
            return config.ORDER_PAD_DEFAULT_GEOMETRY
        except Exception:
            log.exception("Order pad geometry load failed (non-fatal); using the default")
            return config.ORDER_PAD_DEFAULT_GEOMETRY
        geometry = raw.get("geometry")
        if not isinstance(geometry, str) or not geometry:
            return config.ORDER_PAD_DEFAULT_GEOMETRY
        # A window saved while withdrawn (never mapped) reports a degenerate
        # 1x1 size -- guard against replaying that back as the real geometry.
        size = geometry.split("+", 1)[0]
        try:
            width, height = (int(part) for part in size.split("x", 1))
        except ValueError:
            return config.ORDER_PAD_DEFAULT_GEOMETRY
        if width < 100 or height < 80:
            return config.ORDER_PAD_DEFAULT_GEOMETRY
        return geometry

    def _save_geometry(self, force: bool = False) -> None:
        """Persist the window's position/size when it changes.

        Written on change rather than only at shutdown, so a crash or a kill
        doesn't cost you the placement you just spent time getting right over
        TWS -- rate-limited so dragging the window isn't a write per frame.
        """
        try:
            # Skip while hidden: before the window is ever mapped (or while
            # withdrawn), Tk hasn't run geometry propagation from the packed
            # widgets and reports a bogus 1x1, which would otherwise get
            # written to disk and replayed as the geometry on the next arm.
            if not self._root.winfo_viewable():
                return
            geometry = self._root.geometry()
        except tk.TclError:
            return
        if geometry == self._last_geometry:
            return
        now = time.monotonic()
        if not force and now - self._geometry_saved_at < GEOMETRY_SAVE_INTERVAL_SEC:
            return
        self._last_geometry = geometry
        self._geometry_saved_at = now
        try:
            path = Path(config.ORDER_PAD_STATE_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"geometry": geometry}))
        except Exception:
            log.exception("Order pad geometry save failed (non-fatal)")
