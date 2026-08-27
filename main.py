#!/usr/bin/env python3
"""
Standalone real-time momentum scanner against IBKR TWS/Gateway.

Run with TWS or IB Gateway open and the API enabled (Configure > API >
Settings > Enable ActiveX and Socket Clients), pointed at the host/port/
clientId in momentum_scanner/config.py (default 127.0.0.1:7497, paper TWS).
"""
from __future__ import annotations

import asyncio
import logging
import sys

from momentum_scanner.app import ScannerApp

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    filename="scanner.log",
)


async def main() -> None:
    app = ScannerApp()
    try:
        await app.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await app.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
