"""
Single source of truth for every tunable threshold in the scanner.

Nothing outside this file should hardcode a number that a user might
reasonably want to tune. If you're about to write a magic number in
scanner.py / rvol.py / filters.py, it probably belongs here instead.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# IB connection
# --------------------------------------------------------------------------
IB_HOST = "127.0.0.1"
IB_PORT = 7497          # 7497 = TWS paper, 7496 = TWS live, 4002 = Gateway paper, 4001 = Gateway live
IB_CLIENT_ID = 17

# --------------------------------------------------------------------------
# Session detection (America/New_York, DST-safe via zoneinfo)
# --------------------------------------------------------------------------
TZ = ZoneInfo("America/New_York")

PREMARKET_START = (4, 0)     # 04:00
REGULAR_START = (9, 30)      # 09:30
REGULAR_END = (16, 0)        # 16:00
AFTERHOURS_END = (20, 0)     # 20:00

# --------------------------------------------------------------------------
# Universe filters (base filters applied to every scan)
# --------------------------------------------------------------------------
PRICE_MIN = 1.0
PRICE_MAX = 15.0

# True $ floor, enforced as a POST-filter against live price*volume, since
# IBKR's ScannerSubscription has no native USD-volume filter tag.
MIN_DOLLAR_VOLUME = 5_000_000

# Coarse scanner-SIDE pre-filter (shares, not dollars) to cut noise before
# we ever pull live data. Keep this well below what you'd expect a real
# candidate to clear -- it exists to reduce scanner payload size, not to
# enforce the real $ floor.
SCANNER_SHARE_VOLUME_ABOVE = 300_000

# Which US listing venues the scanner should draw from.
SCANNER_LOCATION_CODE = "STK.US.MAJOR"
SCANNER_INSTRUMENT = "STK"

# How many rows to ask the scanner for / consider from each profile.
SCANNER_NUM_ROWS = 50

# --------------------------------------------------------------------------
# RVOL (relative volume-per-minute) -- the primary signal
# --------------------------------------------------------------------------
RVOL_LOOKBACK_DAYS = 20
RVOL_BAR_SIZE = "5 mins"

# Baselines are cached to disk (one file per symbol per session-type) and
# rebuilt at most once per this many hours, to respect IB historical-data
# pacing limits and avoid re-fetching on every restart.
RVOL_CACHE_DIR = "./cache/rvol_baselines"
RVOL_CACHE_MAX_AGE_HOURS = 20

# Max concurrent reqHistoricalData calls in flight while building baselines.
HISTORICAL_FETCH_CONCURRENCY = 5
# Minimum spacing between historical data requests (seconds), extra safety
# margin against "Historical Market Data Service error - pacing violation".
HISTORICAL_FETCH_MIN_INTERVAL_SEC = 1.5

# A reqHistoricalData call can time out or come back empty under IB's own
# pacing/load (no exception raised -- just an empty result), so each
# baseline fetch gets a few attempts with a pause between them before we
# give up and leave that symbol without an RVOL baseline this cycle.
RVOL_FETCH_MAX_ATTEMPTS = 3
RVOL_FETCH_RETRY_DELAY_SEC = 5.0

# Minimum number of historical days that must have data in a given 5-min
# bucket before we trust the baseline for that bucket. Early in a session,
# thinly-traded buckets can otherwise produce a near-zero baseline and an
# absurd, meaningless RVOL spike.
RVOL_MIN_SAMPLE_DAYS = 5

# Row must clear this RVOL to be shown at all (post persistence/filters).
RVOL_DISPLAY_FLOOR = 1.5

# (min_rvol_inclusive, rich style) -- evaluated highest-first.
RVOL_TIERS: list[tuple[float, str]] = [
    (10.0, "bold white on red3"),
    (5.0, "bold black on chartreuse3"),
    (3.0, "bold green3"),
    (2.0, "green"),
    (0.0, "white"),
]

# --------------------------------------------------------------------------
# Persistence filter (suppress single-tick flashes / reprint artifacts)
# --------------------------------------------------------------------------
PERSISTENCE_TOP_N = 15           # a symbol must rank in the top N of a scan refresh to count
PERSISTENCE_REQUIRED = 3         # consecutive qualifying refreshes needed before display
PERSISTENCE_STREAK_RESET_SEC = 45  # if a symbol misses the top-N for longer than this, its streak resets to 0

# --------------------------------------------------------------------------
# Spike detection -- seed values for the runtime-mutable Tunables object
# (momentum_scanner/tunables.py). Nothing reads these directly at runtime;
# they only set the initial Tunables state.
# --------------------------------------------------------------------------
SPIKE_THRESHOLD_PCT = 3.0     # price move within the detection window that counts as a spike
SPIKE_WINDOW_SEC = 20.0       # detection window; also used as the spike-refire cooldown
SPIKE_LOOKBACK_SEC = 600.0    # trailing window for the SPIKE×N event count
SPIKE_QUIET_SEC = 300.0       # no new spike + no new session-high for this long -> clear + evict

# --------------------------------------------------------------------------
# Scalp sizing -- seed values for the runtime-mutable Tunables object. A
# rough, at-a-glance "does this deserve a closer look" heuristic, not a
# risk-managed trade plan. Worked forward purely from the current price, with
# no dependency on recent price history/technical levels: buy this many
# dollars of shares at the current price, solve for the target price that
# nets SCALP_TARGET_USD profit using that position, then set the stop at
# SCALP_RR_RATIO reward:risk from there. See spikes.scalp_sizing().
# --------------------------------------------------------------------------
SCALP_POSITION_USD = 300.0
SCALP_TARGET_USD = 20.0
SCALP_RR_RATIO = 2.0

# --------------------------------------------------------------------------
# Spread filter
# --------------------------------------------------------------------------
MAX_SPREAD_PCT = 1.5      # % of mid price
SPREAD_HARD_REJECT = False  # False = flag with a warning style, True = drop from display entirely

# --------------------------------------------------------------------------
# Float reference (local file you maintain -- IBKR has no float filter)
# --------------------------------------------------------------------------
FLOAT_REFERENCE_FILE = "./float_reference.csv"   # columns: symbol,float_shares
FLOAT_CEILING_SHARES = 20_000_000
FLOAT_HARD_REJECT = False  # False = flag oversized/unknown float, True = drop

# --------------------------------------------------------------------------
# Halt detection
# --------------------------------------------------------------------------
# Tick type 49 = "Halted": 0 not halted, 1 general halt, 2 volatility halt.
# TWS pushes this automatically on a subscribed symbol when it applies -- it is NOT
# requestable via reqMktData's genericTickList (doing so gets the whole request
# rejected with error 321), so there is no config knob for it beyond this comment.
HALT_RESUME_RECENT_MIN = 15  # flag as "recently resumed" for this many minutes after resume

# --------------------------------------------------------------------------
# Live market-data budget
# --------------------------------------------------------------------------
# Cap on symbols with an active streaming reqMktData subscription at once.
# Keep comfortably under your TWS/Gateway market-data-line limit (default
# tier is commonly ~100 lines; other open windows consume lines too).
MAX_LIVE_SYMBOLS = 25

# --------------------------------------------------------------------------
# Refresh cadence
# --------------------------------------------------------------------------
DISPLAY_REFRESH_SEC = 2.0   # rich.Live redraw cadence
TOP_DISPLAY_ROWS = 20       # rows rendered in the table

# Row ORDER is re-sorted by RVOL at this cadence instead of every redraw, so
# rows hold still while their cell values (price, RVOL, flags) keep updating
# live in place -- avoids rows jumping around every 2s on minor RVOL noise.
SORT_REFRESH_SEC = 8.0
