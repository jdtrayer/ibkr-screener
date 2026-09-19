"""
The order pad's actual window: a small Tk window that sits over TWS, showing
one loaded symbol's live sizing and a state bar.

Tk rather than Textual because this is a real window-manager window --
positioned over TWS, remembering its position between sessions -- which a
terminal UI cannot be. It runs in the SAME process and on the SAME asyncio
loop as the scanner (see pump()), so it reads live SymbolStates and calls
sizing.compute_sizing() directly, with no IPC and no second IB connection.

Purely a view, in the same spirit as controls.py: each frame it asks app.py
for a PadView (orderpad.py) and draws it, and it posts key presses and edits
back through callbacks. It holds no trading state of its own beyond the
message row and whether the text boxes parse.

It is deliberately NOT always-on-top: if the pad is visible and focused its
keys work, and if it isn't they don't, with no ambiguity about which window
a keypress is going to. (F2 toggles Loaded/Armed, F4 fires, Esc clears --
see config.ORDER_PAD_TOGGLE_KEY/ORDER_PAD_FIRE_KEY for why they are function
keys.)

Two reading distances, because the pad is used two ways: the state bar is
big, high-contrast and colour-coded so it reads peripherally while your eyes
are on time and sales in TWS, and the numbers below it are large enough to
sanity-check deliberately, once, at arm time. Shares and the entry limit sit
in fixed grid cells (top row) so they can be made editable later without
moving anything.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import tkinter as tk
from pathlib import Path
from typing import Callable

from . import config, display
from .orderpad import PadMode, PadView

log = logging.getLogger(__name__)

PUMP_INTERVAL_SEC = 0.02        # Tk event pump -- 50Hz, keeps the window responsive
DRAW_MIN_INTERVAL_SEC = 0.1     # tick-driven redraws are coalesced to at most 10Hz
REFRESH_INTERVAL_SEC = 0.25     # redraw floor: keeps the countdown / quote age moving on a quiet feed
POSITION_SAVE_INTERVAL_SEC = 2.0

_BG = "#1e1e1e"
_FG = "#d0d0d0"
_DIM = "#808080"
_INPUT_BG = "#2a2a2a"
_INPUT_BAD_BG = "#7f1d1d"
_INPUT_PENDING_BG = "#7a5a00"

# State bar colours: (background, foreground). Live Armed is the ONLY red on
# the pad, and dry run lives in a colour family (amber) that is never red, so
# a simulated fire can't be mistaken for a live one from across the desk.
_BAR_NEUTRAL = ("#3a3a3a", "#d0d0d0")
_BAR_ARMED_LIVE = ("#d50000", "#ffffff")
_BAR_DRY_IDLE = ("#6d4c00", "#ffe082")
_BAR_DRY_ARMED = ("#ffb300", "#000000")

_WARN = "#ffb74d"
_BAD = "#ff6b6b"

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

_POSITION_RE = re.compile(r"([+-]-?\d+[+-]-?\d+)$")


def _color_for(style: str) -> str:
    return _STYLE_COLORS.get(style, _FG)


def _px(value: float | None) -> str:
    """A price at the precision it would be sent at: four decimals under
    $1 (the sub-dollar tick), two above."""
    if value is None:
        return "--"
    return f"{value:.4f}" if value < 1.0 else f"{value:.2f}"


def _clock(seconds: float) -> str:
    seconds = max(int(seconds + 0.999), 0)  # count down by whole seconds, never show 0:00 early
    return f"{seconds // 60}:{seconds % 60:02d}"


def bar_colors(mode: PadMode, dry_run: bool) -> tuple[str, str]:
    """(background, foreground) of the state bar. Pure so the colour rules
    -- the part that has to be right -- are testable without a window."""
    if dry_run:
        return _BAR_DRY_ARMED if mode is PadMode.ARMED else _BAR_DRY_IDLE
    return _BAR_ARMED_LIVE if mode is PadMode.ARMED else _BAR_NEUTRAL


def bar_texts(view: PadView) -> tuple[str, str, str]:
    """(state text, timer text, hint text) for the state bar. The hint row
    carries the DRY RUN banner in every state when dry run is on."""
    if view.mode is PadMode.EMPTY:
        state = "NO SYMBOL"
    elif view.mode is PadMode.LOADED:
        state = f"{view.symbol}  LOADED"
    else:
        state = f"{view.symbol}  ARMED"
    timer = _clock(view.armed_remaining) if view.armed_remaining is not None else ""
    if view.dry_run:
        hint = "DRY RUN -- NO ORDER WILL BE SENT"
    elif view.mode is PadMode.EMPTY:
        hint = "type a symbol below, Enter"
    elif view.mode is PadMode.LOADED:
        hint = f"{config.ORDER_PAD_TOGGLE_KEY.upper()} arms  ·  Esc clears"
    else:
        hint = f"{config.ORDER_PAD_FIRE_KEY} SENDS A LIVE ORDER"
    return state, timer, hint


class OrderPadWindow:
    """The pad window. Created once at startup and shown from the start --
    typing a symbol into it is the primary way in, so there is nothing to
    'arm it into' first. Closing it hides it (the next load from the
    scanner brings it back) rather than destroying it, because a destroyed Tk
    root would take the pump loop down with it."""

    def __init__(
        self,
        on_symbol: Callable[[str], None],
        on_toggle_arm: Callable[[], None],
        on_fire: Callable[[], None],
        on_clear: Callable[[], None],
        on_risk: Callable[[float], None],
        view_provider: Callable[[], PadView],
        initial_risk_usd: float,
    ):
        self._on_symbol = on_symbol
        self._on_toggle_arm = on_toggle_arm
        self._on_fire = on_fire
        self._on_clear = on_clear
        self._on_risk = on_risk
        self._view_provider = view_provider

        self._message = ""
        self._message_is_block = False
        self._dirty = True
        self._risk_valid = True
        self._applied_risk = initial_risk_usd
        self._last_position: str | None = None
        self._position_saved_at = 0.0
        self._closed = False

        self._root = tk.Tk()
        self._root.title("Order Pad")
        self._root.configure(bg=_BG)
        # No -topmost, on purpose (see module docstring): focus is the only
        # thing that decides whether the pad's keys work.
        self._root.resizable(False, False)
        self._root.geometry(self._load_position())
        # Closing the window hides it rather than destroying it -- see the
        # class docstring.
        self._root.protocol("WM_DELETE_WINDOW", self._hide)

        self._build_widgets(initial_risk_usd)
        self._bind_keys()
        self._render()

    # -- construction ------------------------------------------------------

    def _build_widgets(self, initial_risk_usd: float) -> None:
        mono = ("TkFixedFont", 11)
        mono_bold = ("TkFixedFont", 12, "bold")
        big = ("TkFixedFont", 20, "bold")
        value_font = ("TkFixedFont", 18, "bold")
        caption_font = ("TkFixedFont", 8)

        # -- state bar: big and high-contrast, for the peripheral glance.
        self._bar = tk.Frame(self._root, bg=_BAR_NEUTRAL[0])
        self._bar.pack(fill="x")
        self._bar_top = tk.Frame(self._bar, bg=_BAR_NEUTRAL[0])
        self._bar_top.pack(fill="x", padx=8, pady=(6, 0))
        top = self._bar_top
        self._bar_state = tk.Label(top, text="", font=big, anchor="w", bg=_BAR_NEUTRAL[0], fg=_BAR_NEUTRAL[1])
        self._bar_state.pack(side="left")
        self._bar_timer = tk.Label(top, text="", font=big, anchor="e", bg=_BAR_NEUTRAL[0], fg=_BAR_NEUTRAL[1])
        self._bar_timer.pack(side="right")
        self._bar_hint = tk.Label(
            self._bar, text="", font=("TkFixedFont", 10, "bold"), anchor="w",
            bg=_BAR_NEUTRAL[0], fg=_BAR_NEUTRAL[1], padx=8,
        )
        self._bar_hint.pack(fill="x", pady=(0, 6))

        # -- inputs: symbol and the pad's own risk $. F2/F4/Esc are bound
        # with bind_all, so they work whichever of these holds focus.
        inputs = tk.Frame(self._root, bg=_BG, padx=8, pady=6)
        inputs.pack(fill="x")
        tk.Label(inputs, text="Symbol", font=mono, bg=_BG, fg=_DIM).pack(side="left")
        self._symbol_var = tk.StringVar()
        self._symbol_var.trace_add("write", self._on_symbol_text_changed)
        self._symbol_entry = tk.Entry(
            inputs, textvariable=self._symbol_var, font=mono_bold, width=8,
            bg=_INPUT_BG, fg=_FG, insertbackground=_FG, relief="flat",
        )
        self._symbol_entry.pack(side="left", padx=(4, 14))
        self._symbol_entry.bind("<Return>", self._on_symbol_submit)
        tk.Label(inputs, text="Risk $", font=mono, bg=_BG, fg=_DIM).pack(side="left")
        self._risk_entry = tk.Entry(
            inputs, font=mono_bold, width=6,
            bg=_INPUT_BG, fg=_FG, insertbackground=_FG, relief="flat",
        )
        self._risk_entry.insert(0, f"{initial_risk_usd:g}")
        self._risk_entry.pack(side="left", padx=(4, 0))
        self._risk_entry.bind("<KeyRelease>", self._on_risk_edited)

        # -- the numbers: a fixed 2-column grid. Shares and the entry limit
        # are the top row so they can become editable in place later.
        grid = tk.Frame(self._root, bg=_BG, padx=8)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1, uniform="col")
        grid.columnconfigure(1, weight=1, uniform="col")

        self._field_colors: dict[tk.Label, str] = {}

        def field(row: int, col: int, caption: str, color: str = _FG) -> tk.Label:
            cell = tk.Frame(grid, bg=_BG)
            cell.grid(row=row, column=col, sticky="w", pady=(0, 2))
            tk.Label(cell, text=caption, font=caption_font, bg=_BG, fg=_DIM, anchor="w").pack(anchor="w")
            value = tk.Label(cell, text="--", font=value_font, bg=_BG, fg=color, anchor="w")
            value.pack(anchor="w")
            self._field_colors[value] = color
            return value

        self._f_shares = field(0, 0, "SHARES")
        self._f_limit = field(0, 1, "ENTRY LIMIT")
        self._f_stop = field(1, 0, "STOP", _color_for(display.SIZING_STOP_STYLE))
        self._f_trigger = field(1, 1, "STOP TRIGGER", _color_for(display.SIZING_STOP_STYLE))
        self._f_target = field(2, 0, "TARGET", _color_for(display.SIZING_TARGET_STYLE))
        self._f_risk = field(2, 1, "RISK $")
        self._f_last = field(3, 0, "LAST")
        self._f_age = field(3, 1, "QUOTE AGE")

        self._quality = tk.Label(
            self._root, text="", font=("TkFixedFont", 10), bg=_BG, fg=_DIM, anchor="w", padx=8,
        )
        self._quality.pack(fill="x")

        self._message_label = tk.Label(
            self._root, text="", font=mono, bg=_BG, fg=_DIM, anchor="w", padx=8, pady=6,
            wraplength=340, justify="left", height=2,
        )
        self._message_label.pack(fill="x")

    def _bind_keys(self) -> None:
        # bind_all, not bind: the entries have their own focus, and a key
        # press must work regardless of which widget inside the pad holds it.
        self._root.bind_all(f"<{config.ORDER_PAD_TOGGLE_KEY.upper()}>", lambda _e: self._on_toggle_arm())
        self._root.bind_all(f"<{config.ORDER_PAD_FIRE_KEY}>", lambda _e: self._fire())
        self._root.bind_all("<Escape>", lambda _e: self._clear())

    # -- app-facing API ----------------------------------------------------

    def mark_dirty(self) -> None:
        """Something the pad shows has changed (a tick, a state change).
        Redraw at the next allowed slot -- coalesced, never per tick."""
        self._dirty = True

    def show_block(self, reason: str) -> None:
        """A fire attempt (or a load) was refused -- shown in the message
        row, in red, until something else replaces it."""
        self._message = reason
        self._message_is_block = True
        self._dirty = True

    def show_result(self, message: str) -> None:
        """Outcome of a fire, or a later report on the position it opened."""
        self._message = message
        self._message_is_block = False
        self._dirty = True

    def clear_message(self) -> None:
        self._message = ""
        self._message_is_block = False
        self._dirty = True

    def set_symbol_text(self, text: str) -> None:
        """Put `text` in the symbol box (the app loaded / cleared a symbol
        from somewhere other than typing)."""
        if self._closed:
            return
        try:
            self._symbol_var.set(text)
        except tk.TclError:
            self._closed = True

    def raise_window(self, focus_entry: bool = False) -> None:
        """Bring the pad forward and give it keyboard focus, once, on an
        explicit request (a row was sent to it from the scanner). Nothing
        else steals focus: an ordinary load or tick never calls this."""
        if self._closed:
            return
        try:
            self._root.deiconify()
            self._root.lift()
            # focus_force, not focus_set: focus is being taken from another
            # application, which a cooperative focus request won't do.
            self._root.focus_force()
            if focus_entry:
                self._symbol_entry.focus_set()
                self._symbol_entry.selection_range(0, "end")
        except tk.TclError:
            self._closed = True

    def close(self) -> None:
        self._closed = True
        self._save_position(force=True)
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
        cost is that a slow Tk callback stalls the scanner, which is why
        every callback here is trivial, anything real is handed to app.py,
        and tick-driven redraws are coalesced to DRAW_MIN_INTERVAL_SEC.
        """
        loop = asyncio.get_running_loop()
        next_draw = 0.0
        next_refresh = 0.0
        while not self._closed:
            try:
                self._root.update()
            except tk.TclError:
                return  # window destroyed out from under us; nothing left to pump
            now = loop.time()
            if (self._dirty and now >= next_draw) or now >= next_refresh:
                self._dirty = False
                next_draw = now + DRAW_MIN_INTERVAL_SEC
                next_refresh = now + REFRESH_INTERVAL_SEC
                self._render()
                self._save_position()
            await asyncio.sleep(PUMP_INTERVAL_SEC)

    # -- key / edit handlers -------------------------------------------------

    def _typed_symbol(self) -> str:
        return self._symbol_var.get().strip().upper()

    def _fire(self) -> None:
        """F4. Refuses (visibly) when what's in the boxes is not what's
        loaded: the pad is meant to show exactly what would be sent, and a
        symbol box reading NVDA over a loaded AAPL, or a risk box that
        doesn't parse, breaks that."""
        if not self._risk_valid:
            self.show_block("risk $ is not a valid amount -- fix it or press Esc in the box")
            return
        typed = self._typed_symbol()
        # Asked for fresh, not read off the last drawn frame: a press
        # straight after Enter must compare against what is loaded NOW.
        loaded = self._view_provider().symbol
        if typed and typed != (loaded or ""):
            self.show_block(f"symbol box says {typed} but {loaded or 'nothing'} is loaded -- Enter to load")
            return
        self._on_fire()

    def _clear(self) -> None:
        self._on_clear()
        # Ready to type the next name straight away.
        try:
            self._symbol_entry.focus_set()
        except tk.TclError:
            pass

    def _on_symbol_text_changed(self, *_args) -> None:
        # Upper-case as typed. Guarded, since set() re-fires this trace.
        current = self._symbol_var.get()
        upper = current.upper()
        if current != upper:
            self._symbol_var.set(upper)
            return
        self._dirty = True  # the pending-symbol highlight follows the text

    def _on_symbol_submit(self, _event) -> None:
        symbol = self._typed_symbol()
        if symbol:
            self._on_symbol(symbol)
            self._symbol_entry.selection_range(0, "end")

    def _on_risk_edited(self, _event) -> None:
        """Live: every keystroke that leaves a valid amount is applied and
        recalculates the pad immediately. An invalid one (empty, junk, zero,
        over the ceiling) turns the box red and leaves the last valid value
        in force -- and blocks F4 until fixed (see _fire)."""
        text = self._risk_entry.get().strip().lstrip("$")
        try:
            value = float(text)
        except ValueError:
            value = None
        if value is None or not (0 < value <= config.ORDER_PAD_MAX_RISK_USD):
            self._risk_valid = False
            self._risk_entry.configure(bg=_INPUT_BAD_BG)
            return
        self._risk_valid = True
        self._risk_entry.configure(bg=_INPUT_BG)
        if value != self._applied_risk:
            self._applied_risk = value
            self._on_risk(value)

    def _hide(self) -> None:
        self._save_position(force=True)
        self._root.withdraw()

    # -- drawing -------------------------------------------------------------

    def _render(self) -> None:
        # Every caller is either a Tk callback or the pump, neither of which
        # should have to know whether the window is still alive -- a closed
        # pad quietly renders nothing.
        if self._closed:
            return
        try:
            self._render_unguarded(self._view_provider())
        except tk.TclError:
            self._closed = True

    def _render_unguarded(self, view: PadView) -> None:
        bg, fg = bar_colors(view.mode, view.dry_run)
        state_text, timer_text, hint_text = bar_texts(view)
        for widget in (self._bar, self._bar_top, self._bar_state, self._bar_timer, self._bar_hint):
            widget.configure(bg=bg)
        for widget in (self._bar_state, self._bar_timer, self._bar_hint):
            widget.configure(fg=fg)
        self._bar_state.configure(text=state_text)
        self._bar_timer.configure(text=timer_text)
        self._bar_hint.configure(text=hint_text)

        # The symbol box turns amber while it holds something other than
        # the loaded symbol (typed but Enter not yet pressed).
        typed = self._typed_symbol()
        pending = bool(typed) and typed != (view.symbol or "")
        self._symbol_entry.configure(bg=_INPUT_PENDING_BG if pending else _INPUT_BG)

        sizing = view.sizing
        if sizing is None:
            for widget in (self._f_shares, self._f_limit, self._f_stop, self._f_trigger,
                           self._f_target, self._f_risk, self._f_last, self._f_age):
                widget.configure(text="--")
            self._quality.configure(text="")
        else:
            self._f_shares.configure(text=str(sizing.shares))
            self._f_limit.configure(text=_px(sizing.entry_limit))
            self._f_stop.configure(text=_px(sizing.stop_price))
            self._f_trigger.configure(text=_px(sizing.stop_trigger_price))
            self._f_target.configure(text=_px(sizing.target_price))
            self._f_risk.configure(text=f"${sizing.risk_usd:,.2f}")
            self._f_last.configure(text=_px(sizing.price))
            # A sizing F4 would refuse is shown, so the reason makes sense,
            # but greyed out: those are not numbers that would be sent.
            tradeable = sizing.non_tradeable_reason is None
            for widget, color in self._field_colors.items():
                if widget is not self._f_age:
                    widget.configure(fg=color if tradeable else _DIM)
            if sizing.stop_in_spreads is None:
                spr = "S/spr -"
            else:
                spr = f"S/spr {sizing.stop_in_spreads:.1f}"
            if sizing.effective_r is None:
                eff = f"{sizing.nominal_r:.1f}/-R"
            else:
                eff = f"{sizing.nominal_r:.1f}/{sizing.effective_r:.1f}R"
            self._quality.configure(text=f"{spr}   {eff}")
        if view.quote_age is None:
            self._f_age.configure(text="--", fg=_FG)
        else:
            stale = view.quote_age > view.max_quote_age
            self._f_age.configure(text=f"{view.quote_age:.1f}s", fg=_BAD if stale else _FG)

        if self._message:
            self._message_label.configure(text=self._message, fg=_BAD if self._message_is_block else _FG)
        elif sizing is not None and sizing.non_tradeable_reason:
            self._message_label.configure(text=sizing.non_tradeable_reason, fg=_WARN)
        elif view.feed_note:
            self._message_label.configure(text=view.feed_note, fg=_DIM)
        else:
            self._message_label.configure(text="", fg=_DIM)

    # -- position persistence ---------------------------------------------

    def _load_position(self) -> str:
        """Saved window position as a Tk geometry string ('+X+Y'). Position
        only -- the window sizes itself to its content, so an old saved
        size (from the previous, smaller pad) can never clip the layout."""
        try:
            raw = json.loads(Path(config.ORDER_PAD_STATE_FILE).read_text())
        except FileNotFoundError:
            return config.ORDER_PAD_DEFAULT_POSITION
        except Exception:
            log.exception("Order pad position load failed (non-fatal); using the default")
            return config.ORDER_PAD_DEFAULT_POSITION
        geometry = raw.get("geometry")
        if not isinstance(geometry, str):
            return config.ORDER_PAD_DEFAULT_POSITION
        match = _POSITION_RE.search(geometry)
        return match.group(1) if match else config.ORDER_PAD_DEFAULT_POSITION

    def _save_position(self, force: bool = False) -> None:
        """Persist the window's position when it changes.

        Written on change rather than only at shutdown, so a crash or a kill
        doesn't cost you the placement you just spent time getting right over
        TWS -- rate-limited so dragging the window isn't a write per frame.
        """
        try:
            # Skip while hidden: a window that was never mapped reports a
            # bogus geometry that would otherwise be written to disk and
            # replayed on the next start.
            if not self._root.winfo_viewable():
                return
            match = _POSITION_RE.search(self._root.geometry())
        except tk.TclError:
            return
        if match is None:
            return
        position = match.group(1)
        if position == self._last_position:
            return
        now = time.monotonic()
        if not force and now - self._position_saved_at < POSITION_SAVE_INTERVAL_SEC:
            return
        self._last_position = position
        self._position_saved_at = now
        try:
            path = Path(config.ORDER_PAD_STATE_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"geometry": position}))
        except Exception:
            log.exception("Order pad position save failed (non-fatal)")
