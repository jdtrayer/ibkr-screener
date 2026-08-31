# IBKR Momentum Scanner

Real-time momentum screener against IBKR TWS/Gateway. Run with `python3 main.py`
(see `main.py` for connection details). This doc covers the table columns and
the runtime-adjustable tunables in the sidebar panel.

## Table columns

Columns appear in this order on screen: Sym, Flags, Price, RVOL, $Vol, Spread%, Float, Shares, Target, Stop.
Each row is separated by a horizontal rule (`show_lines=True`) to make wide rows easier to track.

| Column | Meaning | Calculation |
|---|---|---|
| **Sym** | Ticker symbol | Colored by RVOL tier (see below) |
| **Flags** | See [Flags](#flags) below | |
| **Price** | Last trade price | Live `last` tick |
| **RVOL** | Relative volume vs. this time-of-day's historical norm | `session volume so far / expected cumulative volume at this many minutes into the session`, where "expected" is interpolated from an empirical 20-trading-day curve of average cumulative volume by 5-minute bucket, built per symbol per session type (`rvol.py`). A bucket is only trusted if ≥5 of the 20 days had data for it (`RVOL_MIN_SAMPLE_DAYS`); untrusted buckets contribute 0, which can make RVOL `None` early in a session for thinly-traded names. This is *not* a naive "volume ÷ elapsed time" ratio — it accounts for volume being front-loaded near the open. |
| **$Vol** | Dollar volume traded this session | `Price × session volume` |
| | | `session volume` here is `SymbolState.session_volume` (`models.py`): IBKR's raw volume tick never resets at session boundaries (it's one running total from premarket through the close through afterhours), so this is deliberately volume *since we started watching this symbol this session* (a snapshot is taken on its first live tick and subtracted from every reading after). Without this, a stock that did big volume hours ago in an earlier session leg reads as still-hot RVOL/$Vol long after it's gone quiet — e.g. a stock that spikes premarket and then trades nothing would otherwise still show a huge $Vol and RVOL well into the regular session, purely off the stale premarket total. One consequence: a symbol admitted a few minutes into a session (not exactly at its start) will slightly *undercount* for those first few minutes, since volume traded before we were watching it is invisible to us — the opposite direction of error, and much smaller. |
| **Spread%** | Bid-ask spread as a % of the midpoint | `(ask − bid) / mid × 100` |
| **Float** | Shares outstanding available to trade | Looked up from `float_reference.csv`, a file you maintain yourself — an entry there always wins. For anything not in it, `floatref.get_float()` fetches from Yahoo Finance directly (IBKR has no float data on a standard account -- confirmed via `Error 10358` against both `reqFundamentalData` and generic tick 258, which need a Reuters Fundamentals subscription this account doesn't have) and caches the result to `cache/float_cache.json` for `FLOAT_CACHE_MAX_AGE_DAYS` (7 days). `?` means neither source has a value yet (a fresh symbol's first Yahoo lookup takes a moment) or Yahoo genuinely has no float data for it. |
| **Shares** | Rough "does this deserve a closer look" sizing — see [Scalp sizing](#scalp-sizing) below | `scalp_position_usd / price` |
| **Target** | Scalp target price | See [Scalp sizing](#scalp-sizing) below |
| **Stop** | Scalp stop price | See [Scalp sizing](#scalp-sizing) below |

### RVOL color tiers (`config.RVOL_TIERS`)

| RVOL | Style |
|---|---|
| ≥ 10.0x | bold white on red |
| ≥ 5.0x | bold black on green (bright) |
| ≥ 3.0x | bold green |
| ≥ 2.0x | green |
| ≥ 0.0x | white |
| unknown | dim white |

### What has to be true for a row to show at all

Two gates apply, in order:

1. **Persistence** (`filters.PersistenceTracker`) — before a symbol is even live-subscribed, it must rank in the top `persistence_top_n` of a scanner refresh for `persistence_required` *consecutive* refreshes (a short miss is forgiven within `persistence_reset_sec`; see [Tunables](#tunables)).
2. **Display filter** (`filters.display_reason`) — once live-subscribed, every one of these must pass:
   - RVOL is known and ≥ `RVOL_DISPLAY_FLOOR` (1.5x) — hard gate, not configurable at runtime
   - `$Vol` ≥ the session's $ floor — hard gate, not configurable at runtime. `MIN_DOLLAR_VOLUME` ($5,000,000) applies to the regular session; `MIN_DOLLAR_VOLUME_EXTENDED_HOURS` ($500,000) applies to premarket/afterhours (`filters.min_dollar_volume_for`). These are genuinely different bars, not the same number applied to a smaller window: extended-hours liquidity is thin by nature, so a $5M floor there would filter out nearly everything, while $500K still rules out a single stray print (e.g. 3,500 shares at ~$6 is ~$21K) — a starting point, worth validating against a real premarket/afterhours session
   - Spread% ≤ `MAX_SPREAD_PCT` (1.5%) **or** `SPREAD_HARD_REJECT` is off (it's on by default — over-threshold spread is hidden entirely, not just flagged; wide spread is a direct cost against a scalp's target and stop, so it's treated the same as failing the $Vol floor)
   - Float ≤ `FLOAT_CEILING_SHARES` (20,000,000) **or unknown** **or** `FLOAT_HARD_REJECT` is off (it is, by default — oversized float just gets the `FLOAT` flag instead of being hidden)

Rows that pass are capped to the top `TOP_DISPLAY_ROWS` (20). Row *order* is by RVOL
descending, but it's only recomputed every `SORT_REFRESH_SEC` (8s, `config.py`) instead
of on every redraw — cell values (price, RVOL, flags, scalp numbers) still update live
every 2s, but rows hold their position between resorts instead of jumping around on
minor RVOL noise. A symbol that newly qualifies between resorts still appears
immediately, just not necessarily in its final sorted position until the next resort.

### Flags

| Flag | Meaning |
|---|---|
| `SPIKE×N` | N spike events in the trailing lookback window — see [Spike detection](#spike-detection) |
| `HALTED` | Symbol is currently halted (IBKR tick 49 = 1 general halt or 2 volatility halt). Volatility (LULD) halts show elapsed plus an estimated time remaining against the standard 5-minute clock, then the ~10-minute extension (`HALTED 3:12 ~1:48 left`, tiers in `HALT_EXPECTED_DURATIONS_MIN`); general halts and halts that outlive both tiers show elapsed only, since there's no standard clock to count against. Estimates are `~` because reopen times vary, and a symbol subscribed mid-halt starts its clock at first observation |
| `RESUMED` | Halt resumed within the last `HALT_RESUME_RECENT_MIN` (15) minutes — flags the post-halt catalyst window |
| `WIDE` | Spread% is over `MAX_SPREAD_PCT` (1.5%) — with `SPREAD_HARD_REJECT` on by default, a row showing this flag is about to drop off the table (and becomes bump-eligible for a live slot — see [Live-slot occupancy](#live-slot-occupancy)) |
| `FLOAT` | Float is over `FLOAT_CEILING_SHARES` (20M) — cosmetic unless `FLOAT_HARD_REJECT` is enabled |

### Scalp sizing

The Scalp column is a rough, at-a-glance heuristic (`spikes.scalp_sizing()`), not a
risk-managed trade plan. It's meant to answer "is this worth pulling up the
chart/L2/T&S for, or should I wait" — not to hand you a hard entry/stop.

It's worked forward purely from the current price and three tunables — **no
dependency on recent price history or technical levels at all**:

```
shares       = scalp_position_usd worth of shares at the current price
reward/share = scalp_target_usd / shares   (profit if target is hit, using the full position)
target_price = price + reward/share
risk/share   = reward/share / scalp_rr_ratio
stop_price   = price − risk/share
```

An earlier version derived the stop from a recent technical low (first the fast
spike-detection window, then a separate longer lookback). Both were wrong in the
same way: whenever the stock hadn't pulled back much — common during a strong,
uninterrupted move — the technical low ended up a penny or two below price, which
isn't actually informative about a sane stop distance, and sometimes produced no
stop at all. Solving forward from position size + target + R:R instead means every
row gets a consistent, sane stop distance (e.g. ~3.3% at the $300/$20/2:1 defaults)
regardless of what the stock's recent chart looks like.

Displayed as `{shares}sh {target:.2f}tgt {stop:.2f}stp` — postfixed units (`sh`,
`tgt`, `stp`) to match the rest of the table's own convention (`3.0x`, `6.4M`, `10.0M`),
not a chosen abbreviation. Shows `-` only when price itself isn't known yet, when the
implied share count would be zero, or when the implied stop would be ≤ 0 (a very
low-priced stock with an aggressive R:R/target combination).

## Tunables

Adjustable live from the sidebar's `+`/`-` buttons (`tunables.py`, `controls.py`) —
no restart needed. Three groups, each backed by one shared `Tunables` instance
that `PersistenceTracker`, the spike logic, and scalp sizing read directly.

### Persistence

| Label | Field | Default | Range | Step | Meaning |
|---|---|---|---|---|---|
| Persist req | `persistence_required` | 3 | 1–10 | 1 | Consecutive qualifying scan refreshes needed before a symbol is promoted to live tracking |
| Persist top-N | `persistence_top_n` | 15 | 5–50 | 1 | A symbol must rank in the top N of a scan refresh to count as "qualifying" that cycle |
| Persist reset | `persistence_reset_sec` | 45s | 10–300s | 5s | How long a symbol can drop out of the top-N before its streak resets to 0 (forgives a single missed/jittery refresh) |

### Spike

| Label | Field | Default | Range | Step | Meaning |
|---|---|---|---|---|---|
| Spike thresh | `spike_threshold_pct` | 3.0% | 0.5–20% | 0.5% | Minimum price move within the detection window to count as a spike |
| Spike window | `spike_window_sec` | 20s | 5–120s | 5s | The detection window itself, *and* the cooldown before the same symbol can trigger another spike |
| Spike lookback | `spike_lookback_sec` | 10m | 1–60m | 1m | Trailing window over which spike events are counted for the `SPIKE×N` flag |
| Spike quiet | `spike_quiet_sec` | 5m | 1–30m | 1m | How long with no new spike **and** no new session high before a symbol that has spiked is evicted from live tracking |

#### Spike detection algorithm (`spikes.py`)

- Every live tick appends `(time, price)` to a rolling window pruned to `spike_window_sec`.
- `move_pct = (price − min(price in window)) / min(price in window) × 100`. If `move_pct ≥ spike_threshold_pct` **and** at least `spike_window_sec` has passed since the last recorded spike (cooldown), a new spike event is recorded.
- `SPIKE×N` counts events still within the trailing `spike_lookback_sec`.
- A symbol becomes eligible for eviction once it has spiked at least once **and** both of the following hold for `spike_quiet_sec`: no new spike, and no new session high. This is independent of the persistence streak — a symbol can be evicted purely for going quiet after spiking.

### Live-slot occupancy

| Label | Field | Default | Range | Step | Meaning |
|---|---|---|---|---|---|
| Slot cooldown | `slot_reentry_cooldown_sec` | 300s | 0–1800s | 60s | How long a bumped symbol is barred from re-taking a live slot (0 disables the cooldown) |
| Live slots | `max_live_symbols` | 25 | 5–100 | 5 | How many symbols can hold a live `reqMktData` subscription at once |

`max_live_symbols` caps how many symbols can hold a live `reqMktData`
subscription at once — clearing the persistence/scanner gate only earns a
symbol a *ticket*, not a guaranteed slot. When the pool is full and a new
symbol qualifies, `filters.bump_candidate()` looks for the weakest current
occupant that's failing either the `$Vol` floor or the spread ceiling and
evicts *that one* to make room — never a symbol that's clearing both.

This is **demand-driven only, with no idle timer**: an occupant failing
`$Vol` or spread keeps its slot indefinitely as long as nothing better is
waiting for it — it's just hidden from the table (see [What has to be true
for a row to show](#what-has-to-be-true-for-a-row-to-show-at-all)). It's
only evicted the instant a newly-qualified symbol needs the room. Two
guards apply before a symbol is even eligible to be bumped, both fixed
(`config.py`, restart required): `SLOT_BUMP_WARMUP_SEC` (60s grace period
after subscribing, so a symbol with no data yet isn't judged) and
`SLOT_BUMP_SPIKE_HOLD_SEC` (120s — a symbol that spiked recently is exempt,
so a transient dip in dollar volume or a momentarily wide print mid-move
doesn't cost it the slot). Weakness on the two axes is normalized to "how
many threshold-multiples past the line," so whichever signal is
proportionally worse wins the eviction regardless of which one it is.

The table caption shows `Live slots: X/N` (N = `max_live_symbols`) and, when
nonzero, two separate counts — deliberately not merged into one, since they
need different fixes:
- `N waiting for a slot` — cleared persistence, genuinely blocked by a full
  pool with nothing bump-eligible. Raising `max_live_symbols` admits these.
- `N in re-entry cooldown` — cleared persistence but barred from re-taking a
  slot for `slot_reentry_cooldown_sec` after being bumped. Unrelated to
  capacity — raising `max_live_symbols` does **not** admit these, only
  waiting out the cooldown does. `scanner.log` logs each one once ("qualified
  but held out by re-entry cooldown (Ns remaining)") the moment it's blocked.

### Scalp

| Label | Field | Default | Range | Step | Meaning |
|---|---|---|---|---|---|
| Scalp size $ | `scalp_position_usd` | $300 | $50–$5000 | $50 | Position size (`shares × price`) used at the current price — your buying-power target for the trade |
| Scalp target $ | `scalp_target_usd` | $20 | $5–$200 | $5 | Dollar profit target if the target price is hit, using that position size |
| Scalp R:R | `scalp_rr_ratio` | 2.0:1 | 1.0–5.0 | 0.5 | Reward:risk multiple — sets the stop distance as `(target − price) / scalp_rr_ratio` |

### Tier-1 snapshot scorer (observation)

A second table under the main one, rendered by the snapshot scorer
(`scorer.py`) — the first slice of the two-tier redesign in which the IB scan
lists provide *membership only* and the ranking is computed by us. Every
`SCORE_REFRESH_SEC` (30s) the full candidate pool (up to `SCORER_POOL_MAX` =
150 — deliberately not gated by the persistence top-N) is batch-snapshotted
and scored. The pool is the union of ALL rows of every subscribed scan list —
the two admission lists plus **pool-only discovery lists**
(`scanner.pool_only_profiles_for_session`): `HIGH_STVOLUME_5MIN` in every
session, and `TOP_AFTER_HOURS_PERC_GAIN` in afterhours. Pool-only rows never
feed the persistence/admission gate — partly to keep the live pipeline
unchanged while the scorer is under observation, and partly because
`HIGH_STVOLUME_5MIN` ranks *absolute* 5-minute volume, so in-price-band
megacaps (Ford made its top 5 in live testing) would otherwise qualify for
live slots; the scorer's velocity-vs-dollars math handles them, the top-N
gate would not.

```
score = 2.0 × move%/min                 (price velocity between sweeps)
      + 1.5 × log10($/min ÷ $50K)       (short-window dollar flow, per decade)
      + 1.0 × min(gap% / 30, 1)         (saturating prior-close-gap context)
      − 1.0 × max(spread% − 0.5, 0)     (quote-cost penalty)
```

Worked examples at the default weights: a mover doing +1.5%/min on $600K/min
with a 40% gap and 0.62% spread scores **+5.50**; a dead name (+0.05%/min,
$20K/min, 3% spread) scores **−2.83**; a fast riser on no volume with a 6%
spread scores **−2.67**. `⚡` marks a fast-lane candidate (≥ 2%/min right now).

This table is **observation only** — it does not affect admission, eviction,
or the main table. Weights and cadences live in `config.py` (restart to
change). Sweep history is cached to `cache/scorer_history.json` and survives
same-day restarts; it's discarded on the first sweep of a new trading day.

## Related fixed thresholds (`config.py`, restart required)

These aren't runtime-tunable but directly affect what you see: `PRICE_MIN`/`PRICE_MAX`
($1–$15, applied scanner-side), `SCANNER_SHARE_VOLUME_ABOVE` (300k shares, coarse
scanner-side pre-filter), `MIN_DOLLAR_VOLUME` ($5M regular-session display floor) and
`MIN_DOLLAR_VOLUME_EXTENDED_HOURS` ($500K premarket/afterhours floor — see [What has
to be true for a row to show](#what-has-to-be-true-for-a-row-to-show-at-all)),
`RVOL_DISPLAY_FLOOR` (1.5x), `MAX_SPREAD_PCT`/`SPREAD_HARD_REJECT` (hard-reject on by default),
`FLOAT_CEILING_SHARES`/`FLOAT_HARD_REJECT`, `FLOAT_CACHE_FILE`/`FLOAT_CACHE_MAX_AGE_DAYS`
(7-day cache for the Yahoo float fallback — see the Float column above),
`SLOT_BUMP_WARMUP_SEC`/`SLOT_BUMP_SPIKE_HOLD_SEC`
(bump-eligibility guards — see [Live-slot occupancy](#live-slot-occupancy)),
`TOP_DISPLAY_ROWS` (20), and `SORT_REFRESH_SEC` (8s row re-sort cadence).
