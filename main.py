#!/usr/bin/env python3
"""
Standalone real-time momentum scanner against IBKR TWS/Gateway.

Run with TWS or IB Gateway open and the API enabled (Configure > API >
Settings > Enable ActiveX and Socket Clients), pointed at the host/port/
clientId in momentum_scanner/config.py (default 127.0.0.1:7497, paper TWS).
"""
from __future__ import annotations

import logging

from momentum_scanner.app import ScannerApp

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    filename="scanner.log",
)


def main() -> None:
    app = ScannerApp()
    app.run()


if __name__ == "__main__":
    main()
