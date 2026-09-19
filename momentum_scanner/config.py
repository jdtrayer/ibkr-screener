"""
Single source of truth for every tunable threshold in the scanner.

Nothing outside this file should hardcode a number that a user might
reasonably want to tune. If you're about to write a magic number in
scanner.py / rvol.py / filters.py, it probably belongs here instead.
"""
from __future__ import annotations

import json
from pathlib import Path
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# Secrets -- API keys live in ./secrets.json (gitignored), never hardcoded
# here or read from the shell environment. Missing file/key both resolve to
# None (fail open, same as every other optional data source in this app) so
# a fresh checkout without secrets.json still runs -- just without whatever
# feature needs that key.
# --------------------------------------------------------------------------
def _load_secret(key: str) -> str | None:
    try:
        secrets = json.loads(Path("./secrets.json").read_text())
    except FileNotFoundError:
        return None
    except Exception:
        return None
    return secrets.get(key) or None


EQUIBLES_API_KEY = _load_secret("equibles_api_key")

# --------------------------------------------------------------------------
# IB connection
# --------------------------------------------------------------------------
IB_HOST = "127.0.0.1"
IB_PORT = 7497          # 7497 = TWS paper, 7496 = TWS live, 4002 = Gateway paper, 4001 = Gateway live
IB_CLIENT_ID = 17

# Order pad safety interlock, independent of tunables.order_pad_dry_run: a
# real submission is refused outright unless IB_PORT is a paper-trading
# port, so switching dry run off can never reach a live account by mistake
# (or by config.py drifting) -- see app.py's _on_pad_fire.
ORDER_PAD_PAPER_PORTS = frozenset({7497, 4002})

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

# Trailing-window $ volume floor -- MIN_DOLLAR_VOLUME above is session-
# CUMULATIVE, so a symbol that had a real burst earlier in the session keeps
# clearing it (and the RVOL floor) forever after, even once it's gone
# completely quiet -- "a move without volume isn't a move" only holds if you
# keep checking. This looks only at volume traded in the trailing
# RECENT_VOLUME_WINDOW_SEC. Starting point (user's own number for the
# regular-session floor, motivated by SHOE showing ~zero volume for the
# prior ~15min on 2026-09-10 despite still clearing both cumulative floors)
# -- validate live and adjust, same as MIN_DOLLAR_VOLUME_EXTENDED_HOURS.
RECENT_VOLUME_WINDOW_SEC = 900.0  # 15 minutes
MIN_RECENT_DOLLAR_VOLUME = 100_000.0
MIN_RECENT_DOLLAR_VOLUME_EXTENDED_HOURS = 25_000.0

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

# If a whole attempt-wave above is exhausted, the failure is usually premarket
# historical-data congestion rather than something specific to the symbol
# (observed hitting a different symbol most sessions), so it's worth one more
# wave after a longer cooldown before writing the symbol off as RVOL-blind
# for the rest of the session.
RVOL_BASELINE_MAX_WAVES = 2
RVOL_BASELINE_WAVE_DELAY_SEC = 90.0

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
# Trend direction -- seed values for the runtime-mutable Tunables object.
# A slower, zoomed-out companion to spike detection above: compares oldest
# vs newest price over a multi-minute window to show up/down/sideways
# context next to SPIKE×N, without gating or suppressing that flag (a fast
# spike and a longer-term downtrend aren't mutually exclusive -- see
# trend.py).
# --------------------------------------------------------------------------
TREND_WINDOW_SEC = 180.0      # lookback window for the up/down/sideways arrow
TREND_FLAT_PCT = 1.0          # move within +-this % over the window counts as sideways

# --------------------------------------------------------------------------
# Position sizing -- seed values for the runtime-mutable Tunables object.
# Stop distance is derived from the symbol's own recent volatility and
# spread FIRST, then the position is sized off that -- the reverse of the
# old fixed-$-position scalp sizing this replaced, which fixed the position
# size and let stop distance fall out as a byproduct (dangerously tight on
# wide-spread/volatile names: a 2-cent spread could get a stop under a
# single spread wide, getting taken out by bid-ask bounce alone). See
# sizing.py's module docstring for the full formula.
# --------------------------------------------------------------------------
RISK_USD = 10.0
ATR_MULTIPLIER = 1.0
# Live-validated 2026-09-15: at MIN_SPREADS=4, round-trip spread cost is a
# structural 2/min_spreads = 50% of risk_usd whenever the spread floor binds
# (the common case, since ATR only overrides it on genuinely high-vol
# names) -- 8 puts that floor at 25%, which is at least a tradeable
# proposition rather than guaranteed-red on every spread-floor-bound row.
MIN_SPREADS = 8
R_MULTIPLE = 2.0
MAX_POSITION_USD = 800.0  # backstop/buying-power cap only -- NOT a sizing input
MIN_SHARES = 10
# Configurable estimate for exchange/regulatory pass-through fees (not
# modeled individually) -- see COMMISSION_* below for the modeled IBKR
# broker commission itself.
PASS_THROUGH_PER_SHARE = 0.0002

# --------------------------------------------------------------------------
# IBKR commission -- Pro TIERED pricing specifically (US stocks, per order
# leg). Would be silently wrong under IBKR Pro FIXED or any other account
# tier. Plain constants, not tunables: this is the broker's fee schedule,
# not a risk knob a user would reasonably want to adjust mid-session.
# --------------------------------------------------------------------------
COMMISSION_PER_SHARE = 0.0035
COMMISSION_MIN_PER_ORDER = 0.35
COMMISSION_MAX_PCT_OF_TRADE = 0.01

# --------------------------------------------------------------------------
# Intraday ATR (sizing.py's stop-distance formula) -- real 1-min OHLC bars
# via a keepUpToDate reqHistoricalData subscription per live symbol (see
# atr.py), not a polling loop: one call opens the subscription and IB
# streams bar updates into the same BarDataList from then on, no further
# calls needed. N=5 bars (~5min lookback) is a short intraday window by
# design (scalp timeframe, not swing) -- fixed, not a tunable, since
# changing the window meaningfully changes what's being measured, unlike
# ATR_MULTIPLIER above.
# --------------------------------------------------------------------------
ATR_LOOKBACK_BARS = 5
ATR_BAR_SIZE = "1 min"
# Mirrors HISTORICAL_FETCH_CONCURRENCY/HISTORICAL_FETCH_MIN_INTERVAL_SEC --
# guards the *initial* fetch of a new ATR subscription against IB's
# historical-data pacing limit if several symbols get admitted within a
# couple seconds of each other. Steady-state bar updates stream in via the
# open subscription's own event and never touch this throttle again.
ATR_FETCH_CONCURRENCY = 5
ATR_FETCH_MIN_INTERVAL_SEC = 1.5

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

# A symbol re-qualifying via IB's scan rank (e.g. HOT_BY_VOLUME's whole-day-
# cumulative-volume ranking never resetting -- see session_scoped_volume)
# despite having genuinely zero current activity wastes a live slot and an
# RVOL-baseline rebuild every re-entry cycle. Confirmed live 2026-09-02: TYA
# cycled admit -> bump -> cooldown -> re-admit 10+ times in ~3.5 hours,
# always at dollar volume near zero (never a borderline dip), always
# immediately hidden by the RVOL display floor. DEAD_HOLD_SEC seeds the
# runtime-mutable Tunables; DEAD_DV_FRACTION does not (an engineering
# threshold, not something to tune mid-session).
DEAD_DV_FRACTION = 0.02   # dollar volume at/below this fraction of the session floor counts as genuinely dead, not just weak
DEAD_HOLD_SEC = 45 * 60.0  # how long a genuinely-dead eviction holds the symbol out, instead of the normal SLOT_REENTRY_COOLDOWN_SEC

# --------------------------------------------------------------------------
# Manual non-tradable list (sidebar panel) -- a user-triggered hold-out,
# distinct from every automatic filter/eviction mechanism above. There's no
# IBKR-queryable "not tradable" signal (broker-side restrictions, e.g. no
# borrow) -- see backlog_2026_09_02 item #1 -- so this is a plain manual
# add, expiring at the start of the next trading day (not a rolling 24h --
# a symbol marked Friday afternoon stays held over the weekend and clears
# Monday, per session.next_trading_day; no holiday calendar) so a stale
# entry doesn't silently suppress a symbol forever. Persisted to disk so it
# survives an app restart mid-session (see momentum_scanner/app.py's
# _load_non_tradable / _save_non_tradable).
# --------------------------------------------------------------------------
NON_TRADABLE_STATE_FILE = "./cache/non_tradable.json"

# --------------------------------------------------------------------------
# ETF/fund exclusion (backlog_2026_09_02 item #3) -- unlike the manual
# non-tradable list above, this IS an IBKR-queryable signal:
# ContractDetails.stockType, fetched once per admission via
# reqContractDetailsAsync alongside contract qualification. Confirmed live
# 2026-09-03 on this account's data entitlements: NVD/SPY/TQQQ all return
# stockType="ETF" reliably, vs "COMMON" for AAPL -- so this is a real
# category-level filter, not a per-symbol list like #1. ETNs (exchange-
# traded notes) are excluded alongside ETFs -- same derivative/fund
# character, not a single-name equity. A symbol whose stockType lookup
# fails (API blip) is NOT excluded -- fail open, since this is a
# secondary check on an already-qualified contract, not core admission
# logic.
# --------------------------------------------------------------------------
EXCLUDE_STOCK_TYPES = frozenset({"ETF", "ETN"})

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
# Short interest (short_interest.py) -- % of float sold short, via Equibles'
# REST API (api.equibles.com, requires EQUIBLES_API_KEY in secrets.json),
# a reseller of FINRA's own semi-monthly short interest reports. Chosen
# over Nasdaq's free per-symbol endpoint, which is Nasdaq-listed only
# (confirmed live: blank for NYSE names) -- Equibles covers both exchanges.
# The underlying number only updates twice a month regardless of source
# (FINRA settlement dates), so this cache TTL is set to roughly that
# cadence rather than implying it can be fresher than the data actually is.
# --------------------------------------------------------------------------
SHORT_INTEREST_CACHE_FILE = "./cache/short_interest_cache.json"
SHORT_INTEREST_CACHE_MAX_AGE_DAYS = 7.0

# --------------------------------------------------------------------------
# Country reference (country.py) -- issuer country -> flag emoji hint in the
# Flags column, prototype/experimental. Same underlying gap as float above:
# IBKR's ContractDetails has no country field on this account (Reuters
# Fundamentals-gated). Source is Nasdaq's public screener download endpoint,
# which is NOT authoritative (spot-checked wrong for at least one symbol --
# see country.py's module docstring) so this is a heads-up, not ground truth.
# One bulk fetch covers the whole listed universe, cached whole and refreshed
# on this TTL rather than incrementally per symbol.
# --------------------------------------------------------------------------
COUNTRY_CACHE_FILE = "./cache/country_cache.json"
COUNTRY_CACHE_MAX_AGE_DAYS = 1.0

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
MAX_LIVE_SYMBOLS = 30

# Of MAX_LIVE_SYMBOLS, this many are reserved for the Tier-1 scorer's own
# top candidates (scorer.py's ranked(), which includes the pool-only lists --
# see scanner.py's pool_only_profiles_for_session) instead of the scan-rank
# persistence gate. Concrete motivating case: BTQ on 2026-09-02 was a real
# live mover (rank 35 on HIGH_STVOLUME_5MIN) that never cracked the top 50 of
# HOT_BY_VOLUME/TOP_PERC_GAIN, so it never got a live tick subscription at
# all under the scan-rank-only gate.
SCORER_RESERVED_SLOTS = 3

# A scorer candidate must hold a top-SCORER_RESERVED_SLOTS rank for this many
# consecutive sweeps before it earns a reserved slot, unless it's flagged
# fast_lane (see SCORE_FAST_MOVE_PCT_PER_MIN), which admits immediately --
# same anti-flicker/fast-lane split as spike detection.
SCORER_ADMIT_SWEEPS = 2

# A candidate must clear this score to be considered for a reserved slot at
# all -- score 0 is the flat baseline (average $/min flow, zero move), so
# without a floor a reserved slot can end up occupied by pure noise-fill
# (observed live 2026-09-02: WOOF admitted at score -0.03, below flat, only
# because the prior occupant had decayed further). 1.0 keeps genuinely
# moving names (e.g. 0.5%/min move at 2x reference $/min flow = 1.45) while
# excluding noise-fill; if nothing clears it, reserved slots sit empty
# rather than being force-filled.
SCORER_ADMIT_MIN_SCORE = 1.0

# --------------------------------------------------------------------------
# News/catalyst detection (backlog item #12) -- pull-only via
# reqHistoricalNewsAsync, periodically checking scorer-pool symbols that
# don't have a headline yet (see momentum_scanner/news.py). A genericTick
# 292 live-push path was tried and rejected: confirmed live it delivers a
# broad-market news firehose to every active tick-news subscription rather
# than symbol-scoped headlines on this account's entitlements, so
# reqHistoricalNewsAsync (which takes a conId directly) is the only
# mechanism that's actually symbol-correct. Also confirmed live that its
# startDateTime bound isn't reliably honored server-side, so news.py
# enforces the current-trading-day scope client-side instead -- see
# news.py's module docstring.
# --------------------------------------------------------------------------
NEWS_PULL_INTERVAL_SEC = 45.0     # how often to re-check pool symbols with no news yet
NEWS_FETCH_CONCURRENCY = 5        # mirrors HISTORICAL_FETCH_CONCURRENCY
NEWS_FETCH_MIN_INTERVAL_SEC = 1.5 # mirrors HISTORICAL_FETCH_MIN_INTERVAL_SEC
# totalResults passed to reqHistoricalNewsAsync -- since startDateTime isn't
# honored server-side, this is the only thing bounding how far back a
# result set can reach before today's own headlines get crowded out by
# older ones; sized generously since a low-float small-cap (this scanner's
# actual target) is far less newsy than the AAPL/TSLA/NVDA symbols used to
# validate this live, which each had ~10 headlines in a single afternoon.
NEWS_HEADLINES_PER_PULL = 20
NEWS_FEED_DISPLAY_ROWS = 100  # cap on the scrollable news feed panel's row count

# Sweep state (recorded headlines/sentiment/feed) survives restarts within
# the same trading day, same pattern/motivation as SCORER_STATE_FILE (the
# user restarts often mid-session). This is about UX continuity, not pull
# efficiency -- a pull is already cheap and self-limiting (once a symbol has
# a headline it's never re-pulled today, and startDateTime isn't honored
# server-side anyway, so there's no cheaper "resume" query to make) -- what
# it actually avoids is the news feed panel and sentiment badges going blank
# on restart until the next sweep cycle re-discovers everything. Date-stamped
# and discarded on a new trading day, same as scorer_history.json.
NEWS_STATE_FILE = "./cache/news_history.json"

# --------------------------------------------------------------------------
# Headline sentiment classification (backlog #12 fast-follow) -- local via
# FinBERT (see momentum_scanner/sentiment.py), not an LLM API: no API key/
# billing dependency, and headlines are already one line so there's nothing
# to summarize, only classify. Confirmed live on this machine: 50-70ms/
# headline (CPU), ~5.6s warm model load (~440MB one-time download, cached
# under ~/.cache/huggingface).
# --------------------------------------------------------------------------
NEWS_SENTIMENT_MODEL = "ProsusAI/finbert"
# Confirmed live: FinBERT is strong on genuine per-company headlines
# (0.90+ confidence on clear cases) but a generic market-wide roundup
# headline ("Stock Market Today: Nasdaq Posts Back-To-Back Gains") was
# misclassified at only 0.53 confidence -- barely above the 3-way random
# baseline (0.33). Below this floor, treat the call as neutral rather than
# paint a wrong color.
NEWS_SENTIMENT_CONFIDENCE_FLOOR = 0.6

# --------------------------------------------------------------------------
# Refresh cadence
# --------------------------------------------------------------------------
DISPLAY_REFRESH_SEC = 2.0   # rich.Live redraw cadence
TOP_DISPLAY_ROWS = 20       # rows rendered in the table

# Row ORDER is re-sorted by RVOL at this cadence instead of every redraw, so
# rows hold still while their cell values (price, RVOL, flags) keep updating
# live in place -- avoids rows jumping around every 2s on minor RVOL noise.
SORT_REFRESH_SEC = 8.0

# --------------------------------------------------------------------------
# Order pad (orderpad.py = logic, padwindow.py = the Tk window) -- a small
# bracket-entry window that sits over TWS, so the scanner's own per-symbol
# sizing can be submitted with a keypress while your eyes are on TWS's
# chart/Level 2/time-and-sales. Three states: Empty (no symbol), Loaded
# (symbol typed or sent from a scanner row; feed subscribed; sizing
# recalculating live on every tick), Armed (Loaded + F4 is live, for a
# limited time). F4 fires immediately -- no confirmation.
#
# The pad never does its own sizing arithmetic -- it calls
# sizing.compute_sizing(), the same function display.py renders the table's
# Shares/Target/Stop columns from.
# --------------------------------------------------------------------------

# Master switch for actually transmitting orders is
# tunables.Tunables.order_pad_dry_run, toggled live from a button in the
# scanner's TunablesPanel sidebar (NOT on the pad itself; the pad only
# DISPLAYS it -- see padwindow.py -- so it can never be flipped by a stray
# key while watching the tape). Default OFF: fire submits for real, gated a
# second, independent way by ORDER_PAD_PAPER_PORTS below, so a live account
# can't be reached by mistake. While ON, the fire key runs every validation
# check and logs the exact bracket it WOULD have sent without touching the
# order API at all -- and the pad's state bar says DRY RUN in every state,
# in a colour family that is never the live-armed red, because a simulated
# fire mistaken for a live one is a silent failure.

# How far above the price at F4 the parent (entry) order may fill, as a
# fraction of the fire-time stop distance. NOT a guard on firing -- the pad
# recalculates live, so there is no earlier price to have drifted from -- but
# the price of the entry order itself: a marketable limit rather than a
# market order, so a fast run-up between the last tick and the fill can't
# drag the entry past the risk the pad displayed. Expressed against stop
# distance, not a fixed %/cents, so it self-scales: a volatile wide-stop
# name gets proportionally more room than a tight one.
ORDER_PAD_ENTRY_SLIPPAGE_FRACTION = 0.25

# Refuse to fire on a symbol that hasn't ticked in this long (seed for
# tunables.order_pad_max_quote_age_sec). Short by design: this is a scalp
# pad, and every number on it is computed from the last tick, so a name
# whose feed has gone quiet is one whose displayed shares/stop/target can't
# be trusted. Measured per ticker UPDATE, not per trade, so a quiet small
# cap outside regular hours can legitimately exceed it between updates.
ORDER_PAD_MAX_QUOTE_AGE_SEC = 2.0

# How long an F2 arm lasts before the pad silently returns to Loaded (seed
# for tunables.order_pad_arm_timeout_sec). Deliberately no warning as it
# runs down: a countdown alarm creates urgency to enter.
ORDER_PAD_ARM_TIMEOUT_SEC = 300.0

# Ceiling for the pad's own editable risk $. Mirrors the risk_usd tunable's
# own upper bound (tunables.TUNABLE_SPECS) -- typing 2500 for 25 is a
# one-keystroke mistake, and max_position_usd is the only other thing that
# would catch it.
ORDER_PAD_MAX_RISK_USD = 500.0

# The protective stop is submitted as STP LMT, not a plain STP -- confirmed
# live 2026-09-17 (TURB) and against IBKR's own docs that a plain STP order
# is NOT eligible to trigger outside regular trading hours on US stocks
# (unlike STP LMT, which can be); the parent/target legs stay LMT, which IS
# RTH-eligible.
#
# The LIMIT price is the real, risk-math stop (fill_price - stop_distance) --
# see orderpad.recompute_bracket_exit. The TRIGGER sits ABOVE it, so the
# order wakes and starts working before price actually reaches the intended
# stop rather than only once it's already there. Fixed at 2026-09-17 (order
# 28420: trigger 1.94/limit 1.92 had this backwards -- the computed stop
# was used as the TRIGGER with limit a flat $0.02 below it, so every stop
# fill realized ~2 cents worse than risk_usd priced in). Proportional to the
# stock's own volatility (stop_distance already blends ATR and the spread
# floor) rather than a flat cent amount, so a wide-spread/volatile name gets
# proportionally more room to actually trigger and fill.
STOP_TRIGGER_LEAD_PCT = 0.25

# Window position, so the pad comes back where you left it over TWS instead
# of wherever the WM decides. Position only -- the window sizes itself to its
# content. Same cache/ + plain-JSON convention as NON_TRADABLE_STATE_FILE.
ORDER_PAD_STATE_FILE = "./cache/order_pad_window.json"
ORDER_PAD_DEFAULT_POSITION = "+40+40"

# The toggle key (Loaded <-> Armed) is pressed in the pad; in the scanner TUI
# the same key LOADS the row under the cursor into the pad (never arms it).
# Fire is pad-only. Two different key-name conventions, unavoidably: Textual
# spells function keys lowercase, Tk uses X keysyms (padwindow.py upper-cases
# ORDER_PAD_TOGGLE_KEY for its own bind). The pad is a normal (not
# always-on-top) window: keys work when it is visible and focused, and not
# otherwise.
#
# Both are deliberately function keys rather than letters: the sidebar's
# non-tradable Input swallows printable keys whenever it has focus, which
# would silently eat a press. F3 is deliberately skipped between them so a
# slipped finger on the toggle key can't land on fire.
ORDER_PAD_TOGGLE_KEY = "f2"   # Textual binding in the TUI; Tk keysym F2 in the pad
ORDER_PAD_FIRE_KEY = "F4"     # Tk keysym, pressed in the pad

# One JSON-lines file per trading day (see order_history.py) -- kept out of
# cache/ since it's not disposable state to rebuild, it's a record meant to
# be read (by a human or another agent checking the pricing math), same
# reasoning that keeps it out of scanner.log too.
ORDER_HISTORY_DIR = "./logs/orders"
