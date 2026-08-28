"""
Session-aware IBKR scanner subscriptions.

HOT_BY_VOLUME + TOP_PERC_GAIN run concurrently in every open session and are
merged into one candidate set, since a name can be a volume story before
it's a % gain story or vice versa.

Previously PREMARKET/AFTERHOURS used TOP_OPEN_PERC_GAIN on the assumption it
compares last trade to prior close. Live-tested against this account on
2026-08-28: TOP_OPEN_PERC_GAIN's ranking column is "Change Since Open", which
needs the 9:30am regular-session open as a reference -- it returned zero rows
premarket every time, silently, no matter what was actually moving (confirmed
against real gappers AEMD/CELU). TOP_PERC_GAIN and HOT_BY_VOLUME were
confirmed live in the same test to carry real premarket data and ranked
AEMD/CELU near the top, so they're now used across all sessions instead.

Results from all active scanCodes for the session are merged into a single
dict of symbol -> ScanHit, keyed by the BEST (lowest) rank the symbol
achieved across sources. Updates arrive via ib_async's ScanDataList
updateEvent -- this is subscription-push, not polling.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ib_async import IB, ScannerSubscription

from . import config
from .session import Session

log = logging.getLogger(__name__)


@dataclass
class ScanHit:
    symbol: str
    con_id: int
    rank: int
    source: str


def _base_subscription(scan_code: str) -> ScannerSubscription:
    sub = ScannerSubscription()
    sub.numberOfRows = config.SCANNER_NUM_ROWS
    sub.instrument = config.SCANNER_INSTRUMENT
    sub.locationCode = config.SCANNER_LOCATION_CODE
    sub.scanCode = scan_code
    sub.abovePrice = config.PRICE_MIN
    sub.belowPrice = config.PRICE_MAX
    sub.aboveVolume = config.SCANNER_SHARE_VOLUME_ABOVE
    return sub


def profiles_for_session(session: Session) -> list[str]:
    if session in (Session.PREMARKET, Session.REGULAR, Session.AFTERHOURS):
        return ["HOT_BY_VOLUME", "TOP_PERC_GAIN"]
    return []


class ScannerManager:
    """Owns the live ScannerSubscriptions for the current session and merges their output."""

    def __init__(self, ib: IB):
        self.ib = ib
        self._data_lists: dict[str, object] = {}  # scanCode -> ScanDataList
        self._handlers: dict[str, callable] = {}
        self.session: Session | None = None
        self._on_update = None  # set via on_update()

    def on_update(self, callback):
        """callback(dict[str, ScanHit]) is invoked whenever any active scan refreshes."""
        self._on_update = callback

    def start(self, session: Session) -> None:
        self.stop()
        self.session = session
        for scan_code in profiles_for_session(session):
            sub = _base_subscription(scan_code)
            data_list = self.ib.reqScannerSubscription(sub)

            def handler(_data_list=None, _scan_code=scan_code):
                self._emit()

            data_list.updateEvent += handler
            self._data_lists[scan_code] = data_list
            self._handlers[scan_code] = handler
            log.info("Started scanner %s for session %s", scan_code, session.value)

    def stop(self) -> None:
        for scan_code, data_list in self._data_lists.items():
            try:
                data_list.updateEvent -= self._handlers[scan_code]
                self.ib.cancelScannerSubscription(data_list)
            except Exception:
                log.exception("Error cancelling scanner subscription %s", scan_code)
        self._data_lists.clear()
        self._handlers.clear()

    def _emit(self) -> None:
        merged: dict[str, ScanHit] = {}
        for scan_code, data_list in self._data_lists.items():
            for item in data_list:
                contract = item.contractDetails.contract
                symbol = contract.symbol
                rank = item.rank
                existing = merged.get(symbol)
                if existing is None or rank < existing.rank:
                    merged[symbol] = ScanHit(symbol=symbol, con_id=contract.conId, rank=rank, source=scan_code)
        if self._on_update:
            self._on_update(merged)
