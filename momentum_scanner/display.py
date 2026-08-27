"""Rich terminal table rendering. RVOL is the primary sort key and color driver."""
from __future__ import annotations

from rich.table import Table
from rich.text import Text

from . import config
from .filters import check_dollar_volume, check_float, check_spread
from .models import SymbolState
from .session import Session


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


def render(states: list[SymbolState], session: Session, connected: bool) -> Table:
    title = f"IBKR Momentum Scanner — session: {session.value.upper()}"
    if not connected:
        title += "  [bold red](DISCONNECTED)[/]"

    table = Table(title=title, expand=True)
    table.add_column("Sym", style="bold")
    table.add_column("Src", justify="left")
    table.add_column("Price", justify="right")
    table.add_column("RVOL", justify="right")
    table.add_column("$Vol", justify="right")
    table.add_column("Spread%", justify="right")
    table.add_column("Float", justify="right")
    table.add_column("Flags", justify="left")

    ranked = sorted(
        states,
        key=lambda s: (s.rvol if s.rvol is not None else -1),
        reverse=True,
    )
    def _passes(s: SymbolState) -> bool:
        if s.rvol is None or s.rvol < config.RVOL_DISPLAY_FLOOR:
            return False
        if not check_dollar_volume(s):  # base $ volume floor -- always a hard gate
            return False
        spread_ok, _ = check_spread(s)
        if not spread_ok:  # only False when SPREAD_HARD_REJECT is on and over threshold
            return False
        if not check_float(s):  # only False when FLOAT_HARD_REJECT is on and over ceiling
            return False
        return True

    ranked = [s for s in ranked if _passes(s)]
    ranked = ranked[: config.TOP_DISPLAY_ROWS]

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
        if s.halt.is_halted:
            flags.append("[bold white on red3]HALTED[/]")
        elif s.halt.recently_resumed(config.HALT_RESUME_RECENT_MIN):
            flags.append("[bold black on yellow]RESUMED[/]")
        if spread_pct is not None and spread_pct > config.MAX_SPREAD_PCT:
            flags.append("[yellow]WIDE[/]")
        if s.float_known and (s.float_shares or 0) > config.FLOAT_CEILING_SHARES:
            flags.append("[yellow]FLOAT[/]")
        flags_txt = Text.from_markup(" ".join(flags)) if flags else Text("")

        table.add_row(
            Text(s.symbol, style=style),
            Text(s.scan_source, style="dim"),
            Text(price_txt, style=style),
            Text(rvol_txt, style=style),
            Text(_fmt_money(s.dollar_volume), style=style),
            Text(spread_txt, style=spread_style),
            Text(float_txt, style=float_style),
            flags_txt,
        )

    if not ranked:
        table.caption = "No symbols have cleared persistence + RVOL floor yet…"

    return table
