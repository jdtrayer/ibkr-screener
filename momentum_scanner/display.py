"""Rich terminal table rendering. RVOL is the primary sort key and color driver."""
from __future__ import annotations

from datetime import datetime

from rich.table import Table
from rich.text import Text

from . import config, spikes
from .filters import check_spread, display_reason
from .models import SymbolState
from .session import Session
from .tunables import Tunables


def rvol_style(rvol: float | None) -> str:
    if rvol is None:
        return "dim white"
    for threshold, style in config.RVOL_TIERS:
        if rvol >= threshold:
            return style
    return "white"


def _fmt_money(v: float | None) -> str:
    if v is None:
        return "-"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"


def _fmt_shares(v: float | None) -> str:
    if v is None:
        return "-"
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.0f}K"
    return f"{v:.0f}"


SCALP_TARGET_STYLE = "green3"  # muted, not pure green -- readable on a dark background
SCALP_STOP_STYLE = "red3"      # muted, not pure red -- readable on a dark background


def _fmt_scalp(sizing: tuple[int, float, float] | None) -> Text:
    # Shares gets no color cue: it's deterministic from price alone
    # (scalp_position_usd / price), so it no longer reflects the move's
    # quality -- a share-count-based style would just encode price, not
    # signal anything about the setup. See spikes.scalp_sizing docstring.
    # Target/stop ARE colored, purely so the two numbers are easy to tell
    # apart at a glance -- not a quality signal like the RVOL tiers are.
    if sizing is None:
        return Text("-", style="dim")
    shares, target, stop = sizing
    # Postfixed units to match the rest of the table's convention (3.0x, 6.4M, 10.0M).
    text = Text(f"{_fmt_shares(shares)}sh ")
    text.append(f"{target:.2f}tgt", style=SCALP_TARGET_STYLE)
    text.append(" ")
    text.append(f"{stop:.2f}stp", style=SCALP_STOP_STYLE)
    return text


def render(
    states: list[SymbolState],
    session: Session,
    connected: bool,
    tunables: Tunables,
    row_order: list[str] | None = None,
    waiting_count: int = 0,
) -> Table:
    """
    `row_order` (symbols, best-first) fixes the row ORDER; cell values still
    reflect live state regardless. Pass None to fall back to a fresh live-RVOL
    sort every call (e.g. for tests). The caller (app.py) is expected to
    refresh row_order on a slower cadence than this is called, so rows hold
    still between resorts instead of jumping around on every redraw.

    `waiting_count` is the number of symbols that have cleared persistence
    but currently hold no live-symbol slot (pool full, or sitting out a
    DV-eviction re-entry cooldown) -- see app.py's _waiting_for_slot_count.
    """
    title = f"IBKR Momentum Scanner — session: {session.value.upper()}"
    if not connected:
        title += "  [bold red](DISCONNECTED)[/]"

    table = Table(title=title, expand=True, show_lines=True)
    table.add_column("Sym", style="bold")
    table.add_column("Flags", justify="left")
    table.add_column("Price", justify="right")
    table.add_column("RVOL", justify="right")
    table.add_column("$Vol", justify="right")
    table.add_column("Spread%", justify="right")
    table.add_column("Float", justify="right")
    table.add_column("Scalp", justify="left")
    table.add_column("Src", justify="left")

    def _passes(s: SymbolState) -> bool:
        return display_reason(s) is None

    def _rvol_key(s: SymbolState) -> float:
        return s.rvol if s.rvol is not None else -1

    passing_by_symbol = {s.symbol: s for s in states if _passes(s)}
    if row_order:
        ordered = [passing_by_symbol.pop(sym) for sym in row_order if sym in passing_by_symbol]
    else:
        ordered = []
    # Anything not covered by row_order yet (newly qualified since the last
    # resort) is appended by live RVOL so it's visible immediately rather than
    # waiting for the next periodic resort.
    newcomers = sorted(passing_by_symbol.values(), key=_rvol_key, reverse=True)
    ranked = (ordered + newcomers)[: config.TOP_DISPLAY_ROWS]

    now = datetime.now(config.TZ)
    for s in ranked:
        style = rvol_style(s.rvol)
        rvol_txt = f"{s.rvol:.1f}x" if s.rvol is not None else "-"
        price_txt = f"{s.tick.last:.2f}" if s.tick.last is not None else "-"

        _, spread_pct = check_spread(s)
        spread_txt = f"{spread_pct:.2f}" if spread_pct is not None else "-"
        spread_style = "yellow" if (spread_pct is not None and spread_pct > config.MAX_SPREAD_PCT) else ""

        if not s.float_known:
            float_txt = "?"
            float_style = "dim"
        else:
            float_txt = _fmt_shares(s.float_shares)
            float_style = "yellow" if (s.float_shares or 0) > config.FLOAT_CEILING_SHARES else ""

        flags = []
        spike_n = spikes.active_spike_count(s.spike, tunables, now)
        if spike_n > 0:
            flags.append(f"[bold black on orange3]SPIKE×{spike_n}[/]")
        if s.halt.is_halted:
            flags.append("[bold white on red3]HALTED[/]")
        elif s.halt.recently_resumed(config.HALT_RESUME_RECENT_MIN):
            flags.append("[bold black on yellow]RESUMED[/]")
        if spread_pct is not None and spread_pct > config.MAX_SPREAD_PCT:
            flags.append("[yellow]WIDE[/]")
        if s.float_known and (s.float_shares or 0) > config.FLOAT_CEILING_SHARES:
            flags.append("[yellow]FLOAT[/]")
        flags_txt = Text.from_markup(" ".join(flags)) if flags else Text("")

        scalp_txt = _fmt_scalp(
            spikes.scalp_sizing(s.tick.last, tunables) if s.tick.last is not None else None
        )

        table.add_row(
            Text(s.symbol, style=style),
            flags_txt,
            Text(price_txt, style=style),
            Text(rvol_txt, style=style),
            Text(_fmt_money(s.dollar_volume), style=style),
            Text(spread_txt, style=spread_style),
            Text(float_txt, style=float_style),
            scalp_txt,
            Text(s.scan_source, style="dim"),
        )

    status_bits = [f"Live slots: {len(states)}/{config.MAX_LIVE_SYMBOLS}"]
    if waiting_count:
        status_bits.append(f"{waiting_count} waiting for a slot")
    if not ranked:
        status_bits.insert(0, "No symbols have cleared persistence + RVOL floor yet…")
    table.caption = "   ".join(status_bits)
    table.caption_justify = "right"

    return table
