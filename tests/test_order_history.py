"""order_history.py: pure file I/O (day-bucketing + JSON serialization), no
IB, no Tk -- see memory: testing_approach. app.py's actual call sites (what
gets logged, and when) are covered indirectly by test_orderpad_wiring.py and
test_orderpad_submission.py, both of which isolate config.ORDER_HISTORY_DIR
to a tmp dir so they don't pollute the real ./logs/orders/."""
import json
from datetime import date, datetime

import pytest

from momentum_scanner import config, order_history


@pytest.fixture(autouse=True)
def isolated_order_history_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ORDER_HISTORY_DIR", str(tmp_path / "orders"))


def _read_lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_file_for_buckets_by_calendar_date():
    path = order_history.file_for(date(2026, 9, 17))
    assert path.name == "order_history_2026-09-17.jsonl"


def test_log_event_appends_one_json_line_per_call():
    now = datetime(2026, 9, 17, 10, 30, tzinfo=config.TZ)
    order_history.log_event(now, "ARM", symbol="CVDK", shares=31)
    order_history.log_event(now, "FIRE_DRY_RUN", symbol="CVDK", shares=31)

    records = _read_lines(order_history.file_for(now.date()))
    assert len(records) == 2
    assert records[0]["event"] == "ARM"
    assert records[0]["symbol"] == "CVDK"
    assert records[0]["shares"] == 31
    assert records[1]["event"] == "FIRE_DRY_RUN"


def test_log_event_separates_by_trading_day():
    day1 = datetime(2026, 9, 17, 15, 0, tzinfo=config.TZ)
    day2 = datetime(2026, 9, 18, 9, 0, tzinfo=config.TZ)
    order_history.log_event(day1, "ARM", symbol="AAA")
    order_history.log_event(day2, "ARM", symbol="BBB")

    assert len(_read_lines(order_history.file_for(day1.date()))) == 1
    assert len(_read_lines(order_history.file_for(day2.date()))) == 1


def test_log_event_serializes_nested_datetimes():
    now = datetime(2026, 9, 17, 10, 30, tzinfo=config.TZ)
    order_history.log_event(now, "ARM", symbol="CVDK", snapshot={"armed_at": now})

    record = _read_lines(order_history.file_for(now.date()))[0]
    assert record["snapshot"]["armed_at"] == now.isoformat()


def test_log_event_is_non_fatal_on_a_bad_field():
    now = datetime(2026, 9, 17, 10, 30, tzinfo=config.TZ)
    order_history.log_event(now, "ARM", symbol="CVDK", bad=object())  # doesn't raise

    assert not order_history.file_for(now.date()).exists()
