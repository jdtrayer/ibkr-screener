"""
Terminal UI rendering. RVOL is the primary sort key and color driver.

The main table (sync_table) is a textual.widgets.DataTable, synced in place
rather than rebuilt -- see its docstring for why. The scorer/news-feed
tables are still plain rich.Table objects pushed into a Static widget
(render_scorer/render_news_feed) -- out of scope for the DataTable move,
see the datatable-rewrite branch's starting point.
"""
from __future__ import annotations

from datetime import datetime

from rich.table import Table
from rich.text import Text
from textual.widgets import DataTable

from . import config, country, spikes
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


def _mmss(seconds: float) -> str:
    s = max(int(seconds), 0)
    return f"{s // 60}:{s % 60:02d}"


def _fmt_halt_flag(halt, now: datetime) -> str:
    """
    HALTED flag with a countdown for volatility (LULD) halts, which normally
    run 5 minutes and commonly extend to ~10 (config.HALT_EXPECTED_DURATIONS_MIN):
    shows elapsed plus estimated time left against the first tier the halt
    hasn't outlived. General halts (kind 1) have no standard clock, and a halt
    that outlives every tier is anyone's guess -- both show elapsed only.
    Estimates are prefixed ~ because reopen times vary and a symbol subscribed
    mid-halt starts its clock late.
    """
    started = halt.last_transition_at
    if started is None:
        return "[bold white on red3]HALTED[/]"
    elapsed = (now - started).total_seconds()
    if halt.kind == 2:
        for tier_min in config.HALT_EXPECTED_DURATIONS_MIN:
            tier = tier_min * 60
            if elapsed < tier:
                return f"[bold white on red3]HALTED {_mmss(elapsed)} ~{_mmss(tier - elapsed)} left[/]"
    return f"[bold white on red3]HALTED {_mmss(elapsed)}[/]"


SCALP_TARGET_STYLE = "green3"  # muted, not pure green -- readable on a dark background
SCALP_STOP_STYLE = "red3"      # muted, not pure red -- readable on a dark background


def _fmt_scalp_shares(sizing: tuple[int, float, float] | None) -> Text:
    # No color cue: shares is deterministic from price alone
    # (scalp_position_usd / price), so it no longer reflects the move's
    # quality -- a share-count-based style would just encode price, not
    # signal anything about the setup. See spikes.scalp_sizing docstring.
    if sizing is None:
        return Text("-", style="dim")
    shares, _target, _stop = sizing
    return Text(_fmt_shares(shares))


def _fmt_scalp_target(sizing: tuple[int, float, float] | None) -> Text:
    if sizing is None:
        return Text("-", style="dim")
    _shares, target, _stop = sizing
    return Text(f"{target:.2f}", style=SCALP_TARGET_STYLE)


def _fmt_scalp_stop(sizing: tuple[int, float, float] | None) -> Text:
    if sizing is None:
        return Text("-", style="dim")
    _shares, _target, stop = sizing
    return Text(f"{stop:.2f}", style=SCALP_STOP_STYLE)


NEWS_SENTIMENT_ICONS = {"positive": "📈", "negative": "📉", "neutral": "📰"}


MAIN_TABLE_COLUMNS = [
    ("Sym", "sym"),
    ("Flags", "flags"),
    ("Price", "price"),
    ("RVOL", "rvol"),
    ("$Vol", "dvol"),
    ("Spread%", "spread"),
    ("Float", "float"),
    ("Short%", "short"),
    ("Shares", "shares"),
    ("Target", "target"),
    ("Stop", "stop"),
]
"""(label, key) pairs for the main table's DataTable -- app.py's on_mount adds
these once via add_columns(); sync_table's update_cell calls address cells by
the same keys, so this is the single source of truth for both."""


def _row_cells(s: SymbolState, tunables: Tunables, news_sentiment: dict[str, str], now: datetime) -> list[Text]:
    """One SymbolState -> the cell values for MAIN_TABLE_COLUMNS, in order."""
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

    if s.short_interest_known and s.short_pct is not None:
        short_txt = f"{s.short_pct:.1f}"
        short_style = ""
    else:
        short_txt = "?"
        short_style = "dim"

    flags = []
    spike_n = spikes.active_spike_count(s.spike, tunables, now)
    if spike_n > 0:
        flags.append(f"[bold black on orange3]SPIKE×{spike_n}[/]")
    if s.halt.is_halted:
        flags.append(_fmt_halt_flag(s.halt, now))
    elif s.halt.recently_resumed(config.HALT_RESUME_RECENT_MIN):
        flags.append("[bold black on yellow]RESUMED[/]")
    if spread_pct is not None and spread_pct > config.MAX_SPREAD_PCT:
        flags.append("[yellow]WIDE[/]")
    if s.float_known and (s.float_shares or 0) > config.FLOAT_CEILING_SHARES:
        flags.append("[yellow]FLOAT[/]")
    if s.symbol in news_sentiment:
        flags.append(NEWS_SENTIMENT_ICONS[news_sentiment[s.symbol]])
    country_abbr = country.abbr_for(s.symbol)
    if country_abbr:
        flags.append(country_abbr)
    flags_txt = Text.from_markup(" ".join(flags)) if flags else Text("")

    sizing = spikes.scalp_sizing(s.tick.last, tunables) if s.tick.last is not None else None

    return [
        Text(s.symbol, style=style),
        flags_txt,
        Text(price_txt, style=style),
        Text(rvol_txt, style=style),
        Text(_fmt_money(s.dollar_volume), style=style),
        Text(spread_txt, style=spread_style),
        Text(float_txt, style=float_style),
        Text(short_txt, style=short_style),
        _fmt_scalp_shares(sizing),
        _fmt_scalp_target(sizing),
        _fmt_scalp_stop(sizing),
    ]


def sync_table(
    datatable: DataTable,
    states: list[SymbolState],
    session: Session,
    connected: bool,
    tunables: Tunables,
    row_order: list[str] | None = None,
    *,
    waiting_count: int = 0,
    cooldown_count: int = 0,
    held_count: int = 0,
    news_sentiment: dict[str, str] | None = None,
    reorder: bool = False,
) -> None:
    """
    Syncs `datatable` (columns already added via MAIN_TABLE_COLUMNS -- see
    app.py's on_mount) to current state, in place. Replaces the old
    Rich-Table `render()`, which returned a brand new Table every call for a
    Static widget to display -- that widget has no concept of a cursor,
    hover, or selection, so a full rebuild every call cost nothing extra.
    DataTable does have all of that, and rebuilding it from scratch every
    call would reset it on every redraw, defeating the entire point of
    moving off Static+rich.Table. So instead: existing rows' cell values are
    updated via update_cell, newly-admitted symbols are added, and
    symbols that dropped out of view are removed -- all without touching
    rows that are staying right where they are.

    `reorder=True` additionally repositions every row to match the freshly
    computed rank order (a full clear+rebuild, which -- like a fresh
    Table used to implicitly -- resets cursor/scroll/hover). Pass it only on
    app.py's slower resort cadence; this is the same "rows hold still
    between resorts" contract `row_order` documented before, just now also
    covering cursor/scroll/hover state, not only visual row order.

    `row_order` (symbols, best-first) fixes row order when `reorder=True`;
    cell values always reflect live state regardless. Pass None to fall back
    to a fresh live-RVOL sort (e.g. for tests).

    `waiting_count` is the number of symbols that have cleared persistence but
    are blocked by a genuinely full pool -- raising max_live_symbols admits
    these. `cooldown_count` is symbols sitting out a re-entry cooldown after
    being bumped, and `held_count` is symbols held out by a dead-hold, the
    manual non-tradable list, or an excluded instrument type -- none of these
    is a capacity problem, raising max_live_symbols does NOT admit them.
    Kept as separate numbers so the border subtitle doesn't conflate a
    capacity problem with a timer/hold -- see app.py's
    _waiting_for_slot_count / _cooldown_wait_count / _held_count.

    `news_sentiment` (symbol -> "positive"/"negative"/"neutral", see
    news.NewsTracker.sentiment_map) drives the Flags column's news icon,
    same as render_scorer's -- see NEWS_SENTIMENT_ICONS.

    The Flags column also appends a letter country abbreviation (CN, TW, ...)
    from country.abbr_for() when the symbol's issuer country is known and
    non-US -- see country.py's module docstring for the data source and its
    accuracy caveats (it's a heads-up, not ground truth).

    Short% is s.short_pct (see short_interest.py) -- % of float sold short
    as of the most recent FINRA settlement date, which only updates twice a
    month regardless of when this renders; "?" means no API key configured,
    no coverage for the symbol, or not fetched yet.
    """
    news_sentiment = news_sentiment or {}

    def _passes(s: SymbolState) -> bool:
        return display_reason(s, session) is None

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

    if reorder:
        datatable.clear()
        for s in ranked:
            datatable.add_row(*_row_cells(s, tunables, news_sentiment, now), key=s.symbol)
    else:
        existing = {row_key.value for row_key in datatable.rows}
        wanted = {s.symbol for s in ranked}
        for stale_symbol in existing - wanted:
            datatable.remove_row(stale_symbol)
        for s in ranked:
            cells = _row_cells(s, tunables, news_sentiment, now)
            if s.symbol in existing:
                for (_, col_key), value in zip(MAIN_TABLE_COLUMNS, cells):
                    datatable.update_cell(s.symbol, col_key, value)
            else:
                datatable.add_row(*cells, key=s.symbol)

    title = f"IBKR Momentum Scanner — session: {session.value.upper()}"
    title += "  [bold green](CONNECTED)[/]" if connected else "  [bold red](DISCONNECTED)[/]"
    datatable.border_title = title

    status_bits = [f"Live slots: {len(states)}/{tunables.max_live_symbols}"]
    if waiting_count:
        status_bits.append(f"{waiting_count} waiting for a slot")
    if cooldown_count:
        status_bits.append(f"{cooldown_count} in re-entry cooldown")
    if held_count:
        status_bits.append(f"{held_count} held (dead/non-tradable/excluded)")
    if not ranked:
        status_bits.insert(0, "No symbols have cleared persistence + RVOL floor yet…")
    datatable.border_subtitle = "   ".join(status_bits)


def _score_style(score: float) -> str:
    if score >= 3.0:
        return "bold green3"
    if score >= 1.0:
        return "green"
    if score < 0.0:
        return "dim"
    return "white"


def render_scorer(
    rows, pool_size: int, last_sweep_at: datetime | None, news_sentiment: dict[str, str] | None = None
) -> Table:
    """
    The Tier-1 snapshot scorer's observation table (see scorer.py). Rendered
    below the main table for side-by-side comparison -- this ranking is OURS
    (computed from snapshot sweeps), deliberately independent of both IB's
    scan ranks and the persistence gate, and does not drive admission yet.

    `news_sentiment` (symbol -> "positive"/"negative"/"neutral", see
    news.NewsTracker.sentiment_map) drives the Flags column's news icon --
    📈/📉/📰 respectively. A symbol absent from the dict has no news at all
    and gets no icon, distinct from a "neutral" classification which still
    shows 📰.
    """
    news_sentiment = news_sentiment or {}
    swept = f"swept {last_sweep_at:%H:%M:%S}" if last_sweep_at else "no sweep yet"
    table = Table(
        title=f"Scorer (observation) — pool {pool_size} — {swept}",
        expand=True,
        show_lines=True,
    )
    table.add_column("Sym", style="bold")
    table.add_column("Flags", justify="left")
    table.add_column("Score", justify="right")
    table.add_column("Move/min", justify="right")
    table.add_column("$/min", justify="right")
    table.add_column("Gap%", justify="right")
    table.add_column("Spread%", justify="right")

    for r in rows[: config.SCORER_TOP_DISPLAY]:
        style = _score_style(r.score)
        # Same slot as the main table's Flags -- see backlog_2026_09_02 item #2.
        flags = []
        if r.fast_lane:
            flags.append("⚡")
        if r.symbol in news_sentiment:
            flags.append(NEWS_SENTIMENT_ICONS[news_sentiment[r.symbol]])
        country_abbr = country.abbr_for(r.symbol)
        if country_abbr:
            flags.append(country_abbr)
        flags_txt = Text(" ".join(flags))

        table.add_row(
            Text(r.symbol, style=style),
            flags_txt,
            Text(f"{r.score:+.2f}", style=style),
            Text(f"{r.move_pct_per_min:+.2f}%"),
            Text(_fmt_money(r.dollar_per_min)),
            Text(f"{r.gap_pct:+.1f}%" if r.gap_pct is not None else "-"),
            Text(f"{r.spread_pct:.2f}" if r.spread_pct is not None else "-"),
        )
    if not rows:
        table.caption = "Waiting for two sweeps per symbol…"
        table.caption_justify = "right"
    return table


def render_news_feed(feed: list[tuple[datetime, str, str]]) -> Table:
    """
    Every recorded headline today (see news.NewsTracker.feed), newest first
    -- one row per headline, not one per symbol, so a symbol with several
    stories shows all of them. Rendered in its own scrollable panel at the
    bottom of the main column, independent of the scanner/scorer tables
    above it. No sentiment icon here: sentiment is tracked per-symbol from
    only its most recent headline (see NEWS_SENTIMENT_ICONS), so attaching
    it to every row of that symbol's history would misattribute it to
    older headlines.
    """
    table = Table(title="News Feed", expand=True, show_header=True, show_lines=False)
    table.add_column("Time", style="dim", width=5, no_wrap=True)
    table.add_column("Sym", style="bold", width=6, no_wrap=True)
    table.add_column("Headline", ratio=1)

    for when, symbol, headline in feed:
        table.add_row(Text(f"{when.astimezone(config.TZ):%H:%M}"), Text(symbol), Text(headline))
    if not feed:
        table.caption = "No headlines yet today"
        table.caption_justify = "right"
    return table
