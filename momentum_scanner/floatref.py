"""
Local float reference. IBKR has no float filter, so this is a CSV you
maintain yourself: symbol,float_shares

Reloaded from disk each time `load()` is called (cheap, small file) so
edits during a live session are picked up without a restart -- the app
calls this once per scan cycle.
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

from . import config

log = logging.getLogger(__name__)


def load() -> dict[str, float]:
    path = Path(config.FLOAT_REFERENCE_FILE)
    if not path.exists():
        return {}
    out: dict[str, float] = {}
    try:
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                sym = (row.get("symbol") or "").strip().upper()
                raw = (row.get("float_shares") or "").strip()
                if not sym or not raw:
                    continue
                try:
                    out[sym] = float(raw)
                except ValueError:
                    log.warning("Bad float_shares value for %s: %r", sym, raw)
    except Exception:
        log.exception("Failed to read float reference file %s", path)
    return out
