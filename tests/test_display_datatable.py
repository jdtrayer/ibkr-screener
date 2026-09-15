"""sync_table's whole reason for existing (over the old render()->Static.update
approach) is that it can update a live DataTable without disturbing cursor/
selection -- that's the specific behavior the datatable-rewrite branch exists
to deliver, so it's the thing most worth locking in here rather than trusting
a screenshot check alone.

No pytest-asyncio in this repo (see requirements-dev.txt) -- same
asyncio.run()-inside-a-sync-test pattern test_news.py/test_scorer.py already
use, not native `async def test_...`."""
import asyncio

import pytest
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import DataTable

from momentum_scanner import display
from momentum_scanner.models import SymbolState
from momentum_scanner.scorer import ScoreRow
from momentum_scanner.session import Session
from momentum_scanner.tunables import Tunables


@pytest.fixture(autouse=True)
def bypass_admission_filter(monkeypatch):
    """sync_table gates rows through filters.display_reason (persistence/$vol/
    spread/float floors) -- none of that is under test here, only the
    DataTable sync mechanics, so admit everything."""
    monkeypatch.setattr(display, "display_reason", lambda state, session: None)


def make_state(symbol, price):
    state = SymbolState(symbol=symbol)
    state.tick.last = price
    return state


def make_score_row(symbol, score):
    return ScoreRow(
        symbol=symbol, score=score, move_pct_per_min=1.0, dollar_per_min=1000.0,
        gap_pct=None, spread_pct=None, fast_lane=False,
    )


class _HarnessApp(App):
    def compose(self) -> ComposeResult:
        yield DataTable(id="scanner-table")
        yield DataTable(id="scorer-table")

    def on_mount(self) -> None:
        scanner = self.query_one("#scanner-table", DataTable)
        scanner.cursor_type = "row"
        scanner.add_columns(*display.MAIN_TABLE_COLUMNS)

        scorer = self.query_one("#scorer-table", DataTable)
        scorer.cursor_type = "row"
        scorer.add_columns(*display.SCORER_TABLE_COLUMNS)


def _sync(app, states, *, row_order=None, reorder=False):
    table = app.query_one("#scanner-table", DataTable)
    display.sync_table(
        table, states, Session.REGULAR, True, Tunables(), row_order,
        reorder=reorder,
    )
    return table


def _sync_scorer(app, rows, *, reorder=False):
    table = app.query_one("#scorer-table", DataTable)
    display.sync_scorer_table(table, rows, pool_size=len(rows), last_sweep_at=None, reorder=reorder)
    return table


def test_reorder_populates_rows_in_row_order():
    async def body():
        app = _HarnessApp()
        async with app.run_test():
            states = [make_state("AAA", 1.0), make_state("BBB", 2.0), make_state("CCC", 3.0)]
            table = _sync(app, states, row_order=["CCC", "AAA", "BBB"], reorder=True)
            assert table.row_count == 3
            assert [row_key.value for row_key in table.rows] == ["CCC", "AAA", "BBB"]
            assert table.get_cell("AAA", "price").plain == "1.00"

    asyncio.run(body())


def test_cell_update_does_not_reset_cursor_position():
    async def body():
        app = _HarnessApp()
        async with app.run_test():
            states = [make_state("AAA", 1.0), make_state("BBB", 2.0), make_state("CCC", 3.0)]
            table = _sync(app, states, row_order=["AAA", "BBB", "CCC"], reorder=True)

            table.move_cursor(row=1)  # cursor parked on BBB
            assert table.cursor_coordinate == Coordinate(1, 0)

            # A fast-tick update with new prices but no reorder -- cursor must stay put.
            updated = [make_state("AAA", 1.5), make_state("BBB", 2.5), make_state("CCC", 3.5)]
            _sync(app, updated, row_order=["AAA", "BBB", "CCC"], reorder=False)

            assert table.cursor_coordinate == Coordinate(1, 0)
            assert table.get_cell("BBB", "price").plain == "2.50"
            assert table.row_count == 3

    asyncio.run(body())


def test_reorder_resets_cursor_same_as_old_full_rebuild_did():
    async def body():
        app = _HarnessApp()
        async with app.run_test():
            states = [make_state("AAA", 1.0), make_state("BBB", 2.0)]
            table = _sync(app, states, row_order=["AAA", "BBB"], reorder=True)
            table.move_cursor(row=1)
            assert table.cursor_coordinate == Coordinate(1, 0)

            _sync(app, states, row_order=["BBB", "AAA"], reorder=True)
            assert table.cursor_coordinate == Coordinate(0, 0)
            assert [row_key.value for row_key in table.rows] == ["BBB", "AAA"]

    asyncio.run(body())


def test_dropped_symbol_is_removed_without_disturbing_others():
    async def body():
        app = _HarnessApp()
        async with app.run_test():
            states = [make_state("AAA", 1.0), make_state("BBB", 2.0), make_state("CCC", 3.0)]
            table = _sync(app, states, row_order=["AAA", "BBB", "CCC"], reorder=True)
            table.move_cursor(row=2)  # cursor on CCC

            remaining = [make_state("AAA", 1.0), make_state("CCC", 3.0)]  # BBB evicted
            _sync(app, remaining, row_order=["AAA", "BBB", "CCC"], reorder=False)

            assert table.row_count == 2
            assert {row_key.value for row_key in table.rows} == {"AAA", "CCC"}

    asyncio.run(body())


def test_new_symbol_between_resorts_is_appended_not_reordered():
    async def body():
        app = _HarnessApp()
        async with app.run_test():
            states = [make_state("AAA", 1.0), make_state("BBB", 2.0)]
            table = _sync(app, states, row_order=["AAA", "BBB"], reorder=True)
            table.move_cursor(row=1)

            with_newcomer = [make_state("AAA", 1.0), make_state("BBB", 2.0), make_state("ZZZ", 9.0)]
            _sync(app, with_newcomer, row_order=["AAA", "BBB"], reorder=False)

            assert table.cursor_coordinate == Coordinate(1, 0)  # unchanged -- BBB never moved
            assert [row_key.value for row_key in table.rows] == ["AAA", "BBB", "ZZZ"]

    asyncio.run(body())


# -- scorer table (sync_scorer_table) -----------------------------------------
# Same update-in-place / reorder-only-on-a-real-sweep contract as the main
# table above, just driven off last_sweep_at advancing (app.py's
# _last_scorer_sweep_rendered) instead of a resort tick -- see
# sync_scorer_table's docstring.

def test_scorer_reorder_populates_rows():
    async def body():
        app = _HarnessApp()
        async with app.run_test():
            rows = [make_score_row("AAA", 1.0), make_score_row("BBB", 3.0)]
            table = _sync_scorer(app, rows, reorder=True)
            assert table.row_count == 2
            assert [row_key.value for row_key in table.rows] == ["AAA", "BBB"]
            assert table.get_cell("BBB", "score").plain == "+3.00"

    asyncio.run(body())


def test_scorer_cell_update_does_not_reset_cursor_position():
    async def body():
        app = _HarnessApp()
        async with app.run_test():
            rows = [make_score_row("AAA", 1.0), make_score_row("BBB", 2.0), make_score_row("CCC", 3.0)]
            table = _sync_scorer(app, rows, reorder=True)

            table.move_cursor(row=1)  # cursor on BBB
            assert table.cursor_coordinate == Coordinate(1, 0)

            updated = [make_score_row("AAA", 1.5), make_score_row("BBB", 2.5), make_score_row("CCC", 3.5)]
            _sync_scorer(app, updated, reorder=False)

            assert table.cursor_coordinate == Coordinate(1, 0)
            assert table.get_cell("BBB", "score").plain == "+2.50"

    asyncio.run(body())


def test_scorer_reorder_resets_cursor():
    async def body():
        app = _HarnessApp()
        async with app.run_test():
            rows = [make_score_row("AAA", 1.0), make_score_row("BBB", 2.0)]
            table = _sync_scorer(app, rows, reorder=True)
            table.move_cursor(row=1)
            assert table.cursor_coordinate == Coordinate(1, 0)

            reordered = [make_score_row("BBB", 5.0), make_score_row("AAA", 1.0)]
            _sync_scorer(app, reordered, reorder=True)
            assert table.cursor_coordinate == Coordinate(0, 0)
            assert [row_key.value for row_key in table.rows] == ["BBB", "AAA"]

    asyncio.run(body())
