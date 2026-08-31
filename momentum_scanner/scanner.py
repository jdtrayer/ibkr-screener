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
    """Scan codes whose hits are ADMISSION-eligible (feed the persistence gate)."""
    if session in (Session.PREMARKET, Session.REGULAR, Session.AFTERHOURS):
        return ["HOT_BY_VOLUME", "TOP_PERC_GAIN"]
    return []


def pool_only_profiles_for_session(session: Session) -> list[str]:
    """
    Extra scan codes whose rows feed ONLY the snapshot scorer's candidate pool
    (scorer.py), never the persistence/admission gate. Deliberate split, for
    two reasons:

    - Observation integrity: while the Tier-1 scorer is being evaluated, the
      live pipeline's behavior must stay unchanged so the two rankings can be
      compared side by side against the same day.
    - HIGH_STVOLUME_5MIN ranks by ABSOLUTE 5-minute volume, so ordinary
      megacaps inside the price band (Ford and TAL made its top 5 in live
      testing 2026-08-31) would qualify for live slots through the top-N
      persistence gate. The scorer's velocity-vs-dollars math handles them
      correctly; the persistence gate would not.

    TOP_AFTER_HOURS_PERC_GAIN ranks by change since the 4pm close -- confirmed
    live 2026-08-31 to surface real afterhours movers (LABT #1, WETO #3) that
    the whole-day-volume-contaminated HOT_BY_VOLUME buried at rank 41-45.
    HIGH_OPEN_GAP is the premarket analog but is deliberately NOT here yet:
    its ranking freezes at the 9:30 open, and whether it tracks the live gap
    during premarket is unverified until a 4:00-9:25am run of
    premarket_scan_check.py says so.
    """
    if session == Session.AFTERHOURS:
        return ["HIGH_STVOLUME_5MIN", "TOP_AFTER_HOURS_PERC_GAIN"]
    if session in (Session.PREMARKET, Session.REGULAR):
        return ["HIGH_STVOLUME_5MIN"]
    return []


class ScannerManager:
    """Owns the live ScannerSubscriptions for the current session and merges their output."""

    def __init__(self, ib: IB):
        self.ib = ib
        self._data_lists: dict[str, object] = {}  # scanCode -> ScanDataList
        self._handlers: dict[str, callable] = {}
        self._pool_only: set[str] = set()  # scanCodes excluded from the admission merge
        self.session: Session | None = None
        self._on_update = None  # set via on_update()

    def on_update(self, callback):
        """callback(hits: dict[str, ScanHit], pool_symbols: set[str]) is invoked
        whenever any active scan refreshes. `hits` covers admission-eligible
        lists only; `pool_symbols` is every symbol on ANY list (hits included),
        for the snapshot scorer's membership."""
        self._on_update = callback

    def start(self, session: Session) -> None:
        self.stop()
        self.session = session
        admission = profiles_for_session(session)
        pool_only = pool_only_profiles_for_session(session)
        self._pool_only = set(pool_only)
        for scan_code in admission + pool_only:
            sub = _base_subscription(scan_code)
            data_list = self.ib.reqScannerSubscription(sub)

            def handler(_data_list=None, _scan_code=scan_code):
                self._emit()

            data_list.updateEvent += handler
            self._data_lists[scan_code] = data_list
            self._handlers[scan_code] = handler
            log.info(
                "Started scanner %s for session %s%s",
                scan_code, session.value,
                " (scorer pool only)" if scan_code in self._pool_only else "",
            )

    def stop(self) -> None:
        for scan_code, data_list in self._data_lists.items():
            try:
                data_list.updateEvent -= self._handlers[scan_code]
                self.ib.cancelScannerSubscription(data_list)
            except Exception:
                log.exception("Error cancelling scanner subscription %s", scan_code)
        self._data_lists.clear()
        self._handlers.clear()
        self._pool_only.clear()

    def _emit(self) -> None:
        merged: dict[str, ScanHit] = {}
        pool_symbols: set[str] = set()
        for scan_code, data_list in self._data_lists.items():
            for item in data_list:
                contract = item.contractDetails.contract
                symbol = contract.symbol
                pool_symbols.add(symbol)
                if scan_code in self._pool_only:
                    continue  # scorer-pool membership only; never feeds the persistence gate
                rank = item.rank
                existing = merged.get(symbol)
                if existing is None or rank < existing.rank:
                    merged[symbol] = ScanHit(symbol=symbol, con_id=contract.conId, rank=rank, source=scan_code)
        if self._on_update:
            self._on_update(merged, pool_symbols)
