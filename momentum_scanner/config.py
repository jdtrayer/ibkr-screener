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

# How long to wait between reconnect attempts after TWS/Gateway drops the
# socket (e.g. a TWS restart) -- ib_async does not auto-reconnect on its own.
RECONNECT_RETRY_SEC = 5

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
# IBKR's ScannerSubscription has no native USD-volume filter tag. Applies to
# the REGULAR session; see MIN_DOLLAR_VOLUME_EXTENDED_HOURS for premarket/
# afterhours. price*volume here uses SymbolState.dollar_volume, which is
# scoped to volume traded SINCE THE CURRENT SESSION STARTED (see
# SymbolState.session_volume) -- not IBKR's raw whole-trading-day volume
# tick, which never resets at session boundaries and would silently let a
# stock coast on hours-old regular-session volume through the afterhours
# floor (or premarket volume through the regular-session floor right at the
# open), long after it's actually gone quiet.
MIN_DOLLAR_VOLUME = 5_000_000

# Same idea, but for premarket/afterhours specifically. MIN_DOLLAR_VOLUME
# was only ever a realistic bar because it was silently being checked
# against whole-day volume, most of which comes from the regular session --
# applying it to session-scoped extended-hours volume alone would filter
# out nearly everything, since extended-hours liquidity is thin by nature,
# not a sign of a bad candidate. This is a genuinely lower floor: enough to
# rule out a single stray print (e.g. 3,500 shares at ~$6 is ~$21K), not
# "regular-session liquid." Starting point -- validate against a real
# premarket/afterhours session and adjust.
MIN_DOLLAR_VOLUME_EXTENDED_HOURS = 500_000

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
# Live-slot occupancy -- letting the capped live-symbol pool (MAX_LIVE_SYMBOLS)
# get bumped clear of "squatters" (below MIN_DOLLAR_VOLUME or over
# MAX_SPREAD_PCT) that would be hidden from display anyway. Deliberately
# demand-driven, no idle timer: a squatter keeps its slot indefinitely as
# long as nothing better is waiting -- it's only bumped the instant a newly
# qualified symbol needs the room and this is the weakest occupant. Also
# deliberately NOT keyed off RVOL, which is unreliable in the first minutes
# of a session (near-zero baselines produce meaningless multiples).
# SLOT_REENTRY_COOLDOWN_SEC seeds the runtime-mutable Tunables.
# --------------------------------------------------------------------------
SLOT_REENTRY_COOLDOWN_SEC = 300.0  # a bumped symbol can't re-take a slot for this long
SLOT_BUMP_WARMUP_SEC = 60.0        # no bump judgment for this long after subscribing (data may lag)
SLOT_BUMP_SPIKE_HOLD_SEC = 120.0   # a spike within this window exempts a symbol from being bumped

# --------------------------------------------------------------------------
# Tier-1 snapshot scorer (observation-only slice of the two-tier redesign).
# Candidates from the merged scan lists (ALL rows, not just the persistence
# top-N) are batch-snapshotted every SCORE_REFRESH_SEC and ranked by OUR OWN
# computed signals -- IB's scan rank is membership only, never scoring. This
# slice renders a second table for side-by-side comparison against the
# current pipeline; it does NOT drive admission yet.
#
# score = SCORE_W_MOVE   * move%/min                      (velocity between sweeps)
#       + SCORE_W_DOLLAR * log10($/min / SCORE_DOLLAR_REF) (per decade of real flow)
#       + SCORE_W_GAP    * min(gap% / SCORE_GAP_CAP, 1)    (saturating context)
#       - SCORE_W_SPREAD * max(spread% - SCORE_SPREAD_FREE, 0)
#
# Worked examples at these weights: a LABT-like mover (1.5%/min, $600K/min,
# 40% gap, 0.62% spread) scores +5.50; a dead name (0.05%/min, $20K/min, 5%
# gap, 3% spread) scores -2.83; a fast riser on no volume with a 6% spread
# scores -2.67 (junk correctly suppressed by the dollar and spread terms).
# --------------------------------------------------------------------------
SCORE_REFRESH_SEC = 30.0          # sweep cadence (live test: 20 symbols filled in 1.4s)
SCORER_POOL_TTL_SEC = 300.0       # drop a candidate this long after it left every scan list
# Sized to fit the union of all subscribed lists (4 lists x 50 rows measured
# ~151 unique symbols live in afterhours) so the cap doesn't trim arbitrarily
# among same-refresh candidates. Sweeps may stretch past SCORE_REFRESH_SEC at
# this size (~16-19s per 100 measured); the re-entry guard just skips a tick.
SCORER_POOL_MAX = 150
SCORER_SNAPSHOT_CHUNK = 40        # snapshots in flight at once (stay under mkt-data lines)
SCORER_HISTORY_KEEP_SEC = 240.0   # rolling window of readings kept per symbol
SCORER_MIN_SPAN_SEC = 20.0        # min seconds between oldest/newest reading before scoring
SCORE_W_MOVE = 2.0                # points per +1% price move per minute
SCORE_W_DOLLAR = 1.5              # points per decade of $/min above the reference
SCORE_DOLLAR_REF_PER_MIN = 50_000.0  # $/min that scores 0 dollar-term points
SCORE_W_GAP = 1.0                 # max points from the gap-vs-prior-close bonus
SCORE_GAP_CAP_PCT = 30.0          # gap% at which the gap bonus saturates
SCORE_W_SPREAD = 1.0              # points lost per 1% of spread over the free allowance
SCORE_SPREAD_FREE_PCT = 0.5       # spread% under this costs nothing
SCORE_FAST_MOVE_PCT_PER_MIN = 2.0 # move%/min that flags a candidate as fast-lane
SCORER_TOP_DISPLAY = 10           # rows shown in the observation table

# Sweep history survives restarts within the same trading day (the user
# restarts often mid-session); readings are junk across days since IB's
# volume tick resets overnight, so the cache is date-stamped and discarded
# on the first sweep of a new day.
SCORER_STATE_FILE = "./cache/scorer_history.json"

# --------------------------------------------------------------------------
# Spread filter
# --------------------------------------------------------------------------
MAX_SPREAD_PCT = 1.5      # % of mid price
SPREAD_HARD_REJECT = True  # False = flag with a warning style, True = drop from display entirely

# --------------------------------------------------------------------------
# Float reference (local file you maintain -- IBKR has no float filter, and
# this account has no Reuters Fundamentals subscription entitling the
# closest alternative, reqFundamentalData/generic tick 258)
# --------------------------------------------------------------------------
FLOAT_REFERENCE_FILE = "./float_reference.csv"   # columns: symbol,float_shares -- always wins over the auto-fetched value
FLOAT_CEILING_SHARES = 20_000_000
FLOAT_HARD_REJECT = False  # False = flag oversized/unknown float, True = drop

# Auto-fetched fallback for any symbol not in FLOAT_REFERENCE_FILE, via a
# direct HTTP call to Yahoo Finance (floatref.py) -- deliberately not the
# yfinance library, which pulls in ~160MB of pandas/numpy/curl_cffi for one
# JSON field. Cached to disk since float share counts change rarely
# (buybacks/offerings, not day to day).
FLOAT_CACHE_FILE = "./cache/float_cache.json"
FLOAT_CACHE_MAX_AGE_DAYS = 7.0

# --------------------------------------------------------------------------
# Halt detection
# --------------------------------------------------------------------------
# Tick type 49 = "Halted": 0 not halted, 1 general halt, 2 volatility halt.
# TWS pushes this automatically on a subscribed symbol when it applies -- it is NOT
# requestable via reqMktData's genericTickList (doing so gets the whole request
# rejected with error 321), so there is no config knob for it beyond this comment.
HALT_RESUME_RECENT_MIN = 15  # flag as "recently resumed" for this many minutes after resume

# Volatility (LULD) halts run on a fairly standard clock: 5 minutes, commonly
# extended to ~10. The HALTED flag shows an estimated time remaining against
# whichever tier the halt hasn't outlived yet; past the last tier (or for
# general halts, which have no standard clock) it shows elapsed time only.
# Estimates only -- actual reopen times vary, and a symbol subscribed
# mid-halt starts its clock at first observation (undercounting elapsed).
HALT_EXPECTED_DURATIONS_MIN = (5.0, 10.0)

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
