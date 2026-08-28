# IBKR Momentum Scanner

Real-time momentum screener against IBKR TWS/Gateway. Run with `python3 main.py`
(see `main.py` for connection details). This doc covers the table columns and
the runtime-adjustable tunables in the sidebar panel.

## Table columns

Columns appear in this order on screen: Sym, Flags, Price, RVOL, $Vol, Spread%, Float, Scalp, Src.
Each row is separated by a horizontal rule (`show_lines=True`) to make wide rows easier to track.

| Column | Meaning | Calculation |
|---|---|---|
| **Sym** | Ticker symbol | Colored by RVOL tier (see below) |
| **Flags** | See [Flags](#flags) below | |
| **Price** | Last trade price | Live `last` tick |
| **RVOL** | Relative volume vs. this time-of-day's historical norm | `session volume so far / expected cumulative volume at this many minutes into the session`, where "expected" is interpolated from an empirical 20-trading-day curve of average cumulative volume by 5-minute bucket, built per symbol per session type (`rvol.py`). A bucket is only trusted if ≥5 of the 20 days had data for it (`RVOL_MIN_SAMPLE_DAYS`); untrusted buckets contribute 0, which can make RVOL `None` early in a session for thinly-traded names. This is *not* a naive "volume ÷ elapsed time" ratio — it accounts for volume being front-loaded near the open. |
| **$Vol** | Dollar volume traded this session | `Price × session volume` |
| **Spread%** | Bid-ask spread as a % of the midpoint | `(ask − bid) / mid × 100` |
| **Float** | Shares outstanding available to trade | Looked up from `float_reference.csv`, a file you maintain yourself (IBKR has no float filter). `?` means the symbol isn't in that file. |
| **Scalp** | Rough "does this deserve a closer look" sizing — see [Scalp sizing](#scalp-sizing) below | |
| **Src** | Which IBKR scan surfaced the symbol | `HOT_BY_VOLUME` or `TOP_PERC_GAIN` (`scanner.py`) |

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
   - `$Vol` ≥ `MIN_DOLLAR_VOLUME` ($5,000,000) — hard gate, not configurable at runtime
   - Spread% ≤ `MAX_SPREAD_PCT` (1.5%) **or** `SPREAD_HARD_REJECT` is off (it is, by default — over-threshold spread just gets the `WIDE` flag instead of being hidden)
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
| `HALTED` | Symbol is currently halted (IBKR tick 49 = 1 general halt or 2 volatility halt) |
| `RESUMED` | Halt resumed within the last `HALT_RESUME_RECENT_MIN` (15) minutes — flags the post-halt catalyst window |
| `WIDE` | Spread% is over `MAX_SPREAD_PCT` (1.5%) — cosmetic unless `SPREAD_HARD_REJECT` is enabled |
| `FLOAT` | Float is over `FLOAT_CEILING_SHARES` (20M) — cosmetic unless `FLOAT_HARD_REJECT` is enabled |

### Scalp sizing

The Scalp column is a rough, at-a-glance heuristic (`spikes.scalp_sizing()`), not a
risk-managed trade plan: it assumes the current pace continues and treats the spike
window's low as support, neither of which is guaranteed. It's meant to answer "is
this worth pulling up the chart/L2/T&S for, or should I wait" — not to hand you a
hard entry/stop.

```
window_min   = lowest price in the live spike-detection window
move_pct     = (price − window_min) / window_min
shares       = scalp_target_usd / (price × move_pct)
target_price = price × (1 + move_pct)
stop_price   = window_min
```

Displayed as `{shares}sh {target:.2f}tgt {stop:.2f}stp` — postfixed units (`sh`,
`tgt`, `stp`) to match the rest of the table's own convention (`3.0x`, `6.4M`, `10.0M`),
not a chosen abbreviation. Shows `-` when there's not yet enough live tick history,
or when there's no room between price and the window low (flat/no momentum).

The share count also drives a color cue: bold green if ≤500 shares (small size
already gets you there — attractive), dim if >2000 shares (you'd need serious size
for the same target off this pace — probably a wait), plain otherwise. Purely
visual, no filtering — every row still shows regardless of this cue.

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

### Scalp

| Label | Field | Default | Range | Step | Meaning |
|---|---|---|---|---|---|
| Scalp target $ | `scalp_target_usd` | $20 | $5–$200 | $5 | Dollar profit target used to compute the shares/target/stop shown in the [Scalp](#scalp-sizing) column |

## Related fixed thresholds (`config.py`, restart required)

These aren't runtime-tunable but directly affect what you see: `PRICE_MIN`/`PRICE_MAX`
($1–$15, applied scanner-side), `SCANNER_SHARE_VOLUME_ABOVE` (300k shares, coarse
scanner-side pre-filter), `MIN_DOLLAR_VOLUME` ($5M display floor), `RVOL_DISPLAY_FLOOR`
(1.5x), `MAX_SPREAD_PCT`/`SPREAD_HARD_REJECT`, `FLOAT_CEILING_SHARES`/`FLOAT_HARD_REJECT`,
`MAX_LIVE_SYMBOLS` (25 concurrent live-data subscriptions), `TOP_DISPLAY_ROWS` (20), and
`SORT_REFRESH_SEC` (8s row re-sort cadence).
