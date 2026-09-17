"""
Per-trading-day order history: one JSON-lines file per calendar date
(America/New_York), one line per order-pad lifecycle event -- arm, a
refused/blocked fire, a dry run, a real submission, a fill, a cancel, an IB
error.

Deliberately separate from scanner.log: that file interleaves every scan
admission/eviction/spike line from the whole session, which makes it useless
for handing to another agent whose only job is checking whether the sizing/
pricing math behaved as intended. This file carries nothing but order-pad
pricing detail, one trading day per file, so a single day's activity can be
reviewed (or fed to that agent) on its own rather than grepped out of
everything else. One file per day rather than one running log, same
motivation as scorer_history.json's daily reset (see config.SCORER_STATE_FILE).

Pure I/O, no IB, no Tk -- app.py is the only caller, at each of the points
orderpad.py's ArmedSnapshot/BracketPlan and app.py's own IB event handlers
already produce something worth recording.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger(__name__)


def file_for(day: date) -> Path:
    return Path(config.ORDER_HISTORY_DIR) / f"order_history_{day.isoformat()}.jsonl"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


def log_event(now: datetime, event: str, **fields: Any) -> None:
    """Append one JSON record to `now`'s (ET calendar date) trading-day
    file. `now` is used for both the bucketing and the record's own
    timestamp -- callers pass whatever clock read they already have rather
    than this taking a second, possibly-inconsistent one of its own.

    Non-fatal by design, same convention as every other on-disk write in
    this codebase (see e.g. padwindow.py's _save_geometry) -- a full disk or
    an unserializable field must never take a live fire down.
    """
    record = {"ts": now.isoformat(), "event": event, **fields}
    try:
        line = json.dumps(record, default=_json_default)
    except Exception:
        log.exception("Order history event not JSON-serializable (non-fatal, dropped): %s", event)
        return
    path = file_for(now.date())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(line)
            f.write("\n")
    except Exception:
        log.exception("Order history log write failed (non-fatal)")
