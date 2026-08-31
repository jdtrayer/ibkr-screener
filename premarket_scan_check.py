"""
One-shot premarket probe: does HIGH_OPEN_GAP actually populate before 9:30,
and what do its rows track (live premarket gap vs something stale/frozen)?

Run any time between 4:00am and ~9:25am ET with TWS/Gateway up:

    python3 premarket_scan_check.py

Takes ~15s. Prints results and appends them to premarket_scan_check.log so a
later Claude session can read the evidence without having been running live.
Context: TOP_OPEN_PERC_GAIN silently returned zero rows premarket (found
2026-08-28); HIGH_OPEN_GAP was proved (2026-08-31, afterhours) to rank by
open-vs-prior-close frozen at 9:30 -- the open question is whether premarket
it ranks by a LIVE gap (current price / projected open vs prior close), which
would make it a good premarket discovery source, or is dead/stale too.
"""
import asyncio
import math
from datetime import datetime

from ib_async import IB, ScannerSubscription

from momentum_scanner import config

LOG_FILE = "premarket_scan_check.log"
# HIGH_OPEN_GAP is the scan under test; the app's current two are controls
# (both confirmed live-working premarket on 2026-08-28).
SCAN_CODES = ["HIGH_OPEN_GAP", "TOP_PERC_GAIN", "HOT_BY_VOLUME"]


def _f(v):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else v


def _sub(scan_code: str) -> ScannerSubscription:
    sub = ScannerSubscription()
    sub.numberOfRows = 20
    sub.instrument = config.SCANNER_INSTRUMENT
    sub.locationCode = config.SCANNER_LOCATION_CODE
    sub.scanCode = scan_code
    sub.abovePrice = config.PRICE_MIN
    sub.belowPrice = config.PRICE_MAX
    sub.aboveVolume = config.SCANNER_SHARE_VOLUME_ABOVE
    return sub


async def main() -> None:
    lines = [f"=== premarket_scan_check @ {datetime.now(config.TZ):%Y-%m-%d %H:%M:%S %Z} ==="]

    ib = IB()
    # clientId 89: distinct from the app (17) so both can run at once.
    await ib.connectAsync(config.IB_HOST, config.IB_PORT, clientId=89)

    results = {}
    for code in SCAN_CODES:
        try:
            data = await ib.reqScannerDataAsync(_sub(code))
            results[code] = [item.contractDetails.contract for item in data]
            lines.append(f"{code}: {len(data)} rows")
        except Exception as exc:
            results[code] = []
            lines.append(f"{code}: ERROR {exc}")

    # Cross-reference HIGH_OPEN_GAP's rows against snapshot prices so the log
    # shows what the ranking tracks premarket: if rank order matches the LIVE
    # last-vs-prior-close gap, it's a live gap scan and safe to adopt.
    gap_rows = results["HIGH_OPEN_GAP"][:15]
    if gap_rows:
        tickers = await asyncio.wait_for(ib.reqTickersAsync(*gap_rows), timeout=30)
        lines.append(f"{'rank':>4} {'sym':6} {'prior_cls':>9} {'open':>7} {'last':>7} {'live_gap':>8}")
        for i, t in enumerate(tickers):
            last, close, op = _f(t.last), _f(t.close), _f(t.open)
            gap = f"{(last - close) / close * 100:+.1f}%" if last and close else "?"
            lines.append(f"{i + 1:>4} {t.contract.symbol:6} {close!s:>9} {op!s:>7} {last!s:>7} {gap:>8}")
    else:
        lines.append("HIGH_OPEN_GAP returned no rows premarket -- same failure mode as "
                     "TOP_OPEN_PERC_GAIN; do NOT adopt it for premarket discovery.")

    ib.disconnect()

    report = "\n".join(lines) + "\n\n"
    print(report)
    with open(LOG_FILE, "a") as fh:
        fh.write(report)
    print(f"(appended to {LOG_FILE})")


if __name__ == "__main__":
    asyncio.run(main())
