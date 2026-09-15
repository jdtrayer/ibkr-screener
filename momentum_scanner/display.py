"""
Terminal UI rendering. Row order is priority_key (trend, then active spike
count, then RVOL as tiebreaker); RVOL remains the color driver via
rvol_style.

The main table (sync_table), scorer table (sync_scorer_table), and news
panel (sync_news_table) are all textual.widgets.DataTable, synced in place
rather than rebuilt -- see sync_table's docstring for why.
"""
from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.widgets import DataTable

from . import config, country, spikes, trend
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


def _fmt_spike(spike_n: int) -> Text:
    if spike_n > 0:
        return Text.from_markup(f"[bold black on orange3]SPIKE×{spike_n}[/]")
    return Text("-", style="dim")


def _fmt_trend(direction: str | None) -> Text:
    # Purely descriptive context alongside SPIKE×N -- not a gate on it (see
    # trend.py's docstring: a fast 20s pop and a falling multi-minute trend
    # aren't mutually exclusive, and a real reversal looks identical to a
    # dead-cat bounce at the moment it starts, so suppressing SPIKE×N on
    # trend direction would hide genuine reversals along with the noise).
    if direction == "up":
        return Text("↑", style="green3")
    if direction == "down":
        return Text("↓", style="red3")
    if direction == "flat":
        return Text("→", style="dim")
    return Text("-", style="dim")


_TREND_PRIORITY = {"up": 2, "flat": 1, None: 1, "down": 0}


def priority_key(s: SymbolState, tunables: Tunables, now: datetime) -> tuple[int, int, float]:
    """
    Row priority for the main table's sort order: trend direction first (an
    up or sideways symbol with active spikes is more interesting to look at
    than a spike inside a downtrend), then active spike count, then RVOL as
    the final tiebreaker -- RVOL is already a floor to hold a live slot at
    all (see filters.py), so once a symbol is admitted it differentiates
    rows least. Unknown trend (not enough history yet, e.g. a newcomer)
    ranks with flat/sideways rather than being punished or rewarded for
    missing data. Purely a display-order choice -- doesn't gate or suppress
    SPIKE×N itself, same reasoning as trend.py's docstring.
    """
    trend_rank = _TREND_PRIORITY[trend.trend_direction(s.trend, tunables)]
    spike_n = spikes.active_spike_count(s.spike, tunables, now)
    rvol = s.rvol if s.rvol is not None else -1.0
    return (trend_rank, spike_n, rvol)


NEWS_SENTIMENT_ICONS = {"positive": "📈", "negative": "📉", "neutral": "📰"}

# A distinct hue from every other color in these tables (green=bullish/target,
# red=stop/halted, yellow=warning, orange=spike) -- cyan reads as "heads-up
# info", matching country.abbr_for's own docstring ("a heads-up, not ground
# truth"), and stays legible against both zebra-striped rows and the cursor
# row's blue highlight background (spot-checked via a screenshot harness --
# candidates in the blue family, e.g. dodger_blue2/steel_blue1, washed out
# against that same blue when a row is under the cursor).
COUNTRY_STYLE = "cyan"


MAIN_TABLE_COLUMNS = [
    ("Sym", "sym"),
    ("Country", "country"),
    ("Price", "price"),
    ("Trend", "trend"),
    ("Spike", "spike"),
    ("RVOL", "rvol"),
    ("$Vol", "dvol"),
    ("Spread%", "spread"),
    ("Float", "float"),
    ("Short%", "short"),
    ("Shares", "shares"),
    ("Target", "target"),
    ("Stop", "stop"),
    ("Flags", "flags"),
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

    spike_n = spikes.active_spike_count(s.spike, tunables, now)

    flags = []
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
    flags_txt = Text.from_markup(" ".join(flags)) if flags else Text("")

    country_abbr = country.abbr_for(s.symbol)
    country_txt = Text(country_abbr, style=COUNTRY_STYLE) if country_abbr else Text("")

    sizing = spikes.scalp_sizing(s.tick.last, tunables) if s.tick.last is not None else None
    trend_txt = _fmt_trend(trend.trend_direction(s.trend, tunables))
    spike_txt = _fmt_spike(spike_n)

    return [
        Text(s.symbol, style=style),
        country_txt,
        Text(price_txt, style=style),
        trend_txt,
        spike_txt,
        Text(rvol_txt, style=style),
        Text(_fmt_money(s.dollar_volume), style=style),
        Text(spread_txt, style=spread_style),
        Text(float_txt, style=float_style),
        Text(short_txt, style=short_style),
        _fmt_scalp_shares(sizing),
        _fmt_scalp_target(sizing),
        _fmt_scalp_stop(sizing),
        flags_txt,
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
    to a fresh live sort by priority_key (e.g. for tests).

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
    same as sync_scorer_table's -- see NEWS_SENTIMENT_ICONS.

    The Country column shows a letter abbreviation (CN, TW, ...) from
    country.abbr_for() when the symbol's issuer country is known and
    non-US, "??" when it's some other country we don't have a code for or
    the country data isn't available at all, and blank only when the
    country is confirmed US -- see country.py's module docstring for the
    data source and its accuracy caveats (it's a heads-up, not ground
    truth).

    Short% is s.short_pct (see short_interest.py) -- % of float sold short
    as of the most recent FINRA settlement date, which only updates twice a
    month regardless of when this renders; "?" means no API key configured,
    no coverage for the symbol, or not fetched yet.
    """
    news_sentiment = news_sentiment or {}
    now = datetime.now(config.TZ)

    def _passes(s: SymbolState) -> bool:
        return display_reason(s, session) is None

    passing_by_symbol = {s.symbol: s for s in states if _passes(s)}
    if row_order:
        ordered = [passing_by_symbol.pop(sym) for sym in row_order if sym in passing_by_symbol]
    else:
        ordered = []
    # Anything not covered by row_order yet (newly qualified since the last
    # resort) is appended by priority_key so it's visible immediately rather
    # than waiting for the next periodic resort.
    newcomers = sorted(passing_by_symbol.values(), key=lambda s: priority_key(s, tunables, now), reverse=True)
    ranked = (ordered + newcomers)[: config.TOP_DISPLAY_ROWS]

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


SCORER_TABLE_COLUMNS = [
    ("Sym", "sym"),
    ("Country", "country"),
    ("Flags", "flags"),
    ("Score", "score"),
    ("Move/min", "move"),
    ("$/min", "dmin"),
    ("Gap%", "gap"),
    ("Spread%", "spread"),
]
"""(label, key) pairs for the scorer table's DataTable -- same role as
MAIN_TABLE_COLUMNS."""


def _scorer_row_cells(r, news_sentiment: dict[str, str]) -> list[Text]:
    """One ScoreRow -> the cell values for SCORER_TABLE_COLUMNS, in order."""
    style = _score_style(r.score)
    # Same slot as the main table's Flags -- see backlog_2026_09_02 item #2.
    flags = []
    if r.fast_lane:
        flags.append("⚡")
    if r.symbol in news_sentiment:
        flags.append(NEWS_SENTIMENT_ICONS[news_sentiment[r.symbol]])
    flags_txt = Text(" ".join(flags))

    country_abbr = country.abbr_for(r.symbol)
    country_txt = Text(country_abbr, style=COUNTRY_STYLE) if country_abbr else Text("")

    return [
        Text(r.symbol, style=style),
        country_txt,
        flags_txt,
        Text(f"{r.score:+.2f}", style=style),
        Text(f"{r.move_pct_per_min:+.2f}%"),
        Text(_fmt_money(r.dollar_per_min)),
        Text(f"{r.gap_pct:+.1f}%" if r.gap_pct is not None else "-"),
        Text(f"{r.spread_pct:.2f}" if r.spread_pct is not None else "-"),
    ]


def sync_scorer_table(
    datatable: DataTable,
    rows: list,
    pool_size: int,
    last_sweep_at: datetime | None,
    news_sentiment: dict[str, str] | None = None,
    *,
    reorder: bool = False,
) -> None:
    """
    The Tier-1 snapshot scorer's observation table (see scorer.py), synced
    into `datatable` the same way sync_table syncs the main table -- see its
    docstring for the update-in-place / reorder-only-on-resort reasoning.
    Rendered below the main table for side-by-side comparison -- this
    ranking is OURS (computed from snapshot sweeps), deliberately
    independent of both IB's scan ranks and the persistence gate, and does
    not drive admission yet.

    `rows` (ScoreRow, see scorer.py) is expected pre-ranked by the caller
    (self.scorer.ranked()) -- unlike the main table there's no separate
    row_order here, since the scorer's own ranked() list is already stable
    between sweeps on its own. `reorder=True` should be passed only when
    `last_sweep_at` has actually advanced since the last call (app.py tracks
    this), same "don't reposition rows outside of a real resort" contract.

    `news_sentiment` (symbol -> "positive"/"negative"/"neutral", see
    news.NewsTracker.sentiment_map) drives the Flags column's news icon --
    📈/📉/📰 respectively. A symbol absent from the dict has no news at all
    and gets no icon, distinct from a "neutral" classification which still
    shows 📰.
    """
    news_sentiment = news_sentiment or {}
    ranked = rows[: config.SCORER_TOP_DISPLAY]

    if reorder:
        datatable.clear()
        for r in ranked:
            datatable.add_row(*_scorer_row_cells(r, news_sentiment), key=r.symbol)
    else:
        existing = {row_key.value for row_key in datatable.rows}
        wanted = {r.symbol for r in ranked}
        for stale_symbol in existing - wanted:
            datatable.remove_row(stale_symbol)
        for r in ranked:
            cells = _scorer_row_cells(r, news_sentiment)
            if r.symbol in existing:
                for (_, col_key), value in zip(SCORER_TABLE_COLUMNS, cells):
                    datatable.update_cell(r.symbol, col_key, value)
            else:
                datatable.add_row(*cells, key=r.symbol)

    swept = f"swept {last_sweep_at:%H:%M:%S}" if last_sweep_at else "no sweep yet"
    datatable.border_title = f"Scorer (observation) — pool {pool_size} — {swept}"
    datatable.border_subtitle = "Waiting for two sweeps per symbol…" if not ranked else None


NEWS_TABLE_COLUMNS = [
    ("Time", "time"),
    ("Sym", "sym"),
    ("Sentiment", "sentiment"),
    ("Headline", "headline"),
]
"""(label, key) pairs for the news panel's DataTable -- same role as
MAIN_TABLE_COLUMNS."""


def _news_row_key(when: datetime, symbol: str, headline: str) -> str:
    # Headlines are immutable once recorded (see news.NewsTracker.record),
    # so a row's key never needs to survive a text change -- it only needs
    # to be stable for the same headline across calls (to detect "nothing
    # new" in sync_news_table) and unique among rows shown at once (a
    # symbol can have several headlines, so symbol alone won't do).
    return f"{when.timestamp()}|{symbol}|{hash(headline)}"


def _news_row_cells(when: datetime, symbol: str, headline: str, sentiment: str) -> list[Text]:
    return [
        Text(f"{when.astimezone(config.TZ):%H:%M}", style="dim"),
        Text(symbol, style="bold"),
        Text(NEWS_SENTIMENT_ICONS[sentiment]),
        Text(headline),
    ]


def sync_news_table(
    datatable: DataTable,
    feed: list[tuple[datetime, str, str, str]],
    symbol_filter: str | None = None,
) -> None:
    """
    Syncs `datatable` (columns already added via NEWS_TABLE_COLUMNS -- see
    app.py's on_mount) to `feed` -- news.NewsTracker.feed(...), newest first,
    already capped and optionally pre-filtered to one symbol by the caller.

    Unlike sync_table/sync_scorer_table, there's no per-row *cell* update
    here: a recorded headline's text and sentiment never change (see
    news.NewsTracker.record), so the only thing that ever changes between
    calls is which rows are visible at all (a new headline prepended, or an
    old one aging out past the display cap). This is detected by comparing
    `feed`'s row keys against the table's current rows -- when they match,
    this is a deliberate no-op rather than a clear+rebuild, since this
    renders on every tick regardless of whether news actually changed and
    clearing a DataTable resets its scroll position same as sync_table's
    reorder does; skipping the rebuild is what lets a mid-scroll read
    survive ticks with nothing new.

    `symbol_filter`, if given, is purely a label -- the caller (app.py) is
    expected to have already passed a pre-filtered `feed` (via
    NewsTracker.feed(symbol=...)); this only changes the title/empty-state
    text so it's clear a filter is active, and how it got cleared (select
    the same row again in the main/scorer table -- see app.py's
    on_data_table_row_selected).

    The Sentiment column shows NEWS_SENTIMENT_ICONS for that specific
    headline's own classification (feed()'s 4th tuple element) -- not
    necessarily the symbol's current sentiment_map() value, since an older
    headline for the same symbol can have been classified differently.
    """
    wanted_keys = [_news_row_key(when, symbol, headline) for when, symbol, headline, _sentiment in feed]
    existing_keys = [row_key.value for row_key in datatable.rows]
    if wanted_keys != existing_keys:
        datatable.clear()
        for (when, symbol, headline, sentiment), key in zip(feed, wanted_keys):
            datatable.add_row(*_news_row_cells(when, symbol, headline, sentiment), key=key)

    title = f"News Feed — {symbol_filter} (select row again to clear)" if symbol_filter else "News Feed"
    datatable.border_title = title
    if not feed:
        datatable.border_subtitle = (
            f"No headlines yet today for {symbol_filter}" if symbol_filter else "No headlines yet today"
        )
    else:
        datatable.border_subtitle = None
