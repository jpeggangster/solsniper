# 12/21 EMA Cross Bot — 5-second timeframe

Automated Solana memecoin strategy: go long when EMA12 crosses above EMA21 on
5-second bars, exit when it crosses back below (or a stop fires).

## Files

| File | Role |
|---|---|
| `ema_cross_strategy.py` | Indicator + entry/exit rules. Tick-driven, wall-clock-aligned bars. |
| `pumpportal_feed.py` | Real-time swap ticks over PumpPortal's websocket. |
| `ema_cross_bot.py` | The bot: watchlist, reconciliation, paper/live execution. |
| `backtest_ema_cross.py` | Replays recorded ticks; reports PnL net of fees. |

These are standalone — `solana_sniper_bot.py` and `hood_sniper_bot.py` are
untouched and keep running as before.

## Why a tick feed, not price polls

A 5-second bar cannot be built from a REST poll. `solana_sniper_bot.py` samples
DexScreener every 15s — less than one point per bar. This bot subscribes to
PumpPortal's websocket trade stream and folds individual swaps into bars, so a
"5s candle" is an actual 5s candle.

Requires `PUMPPORTAL_API_KEY` on a wallet holding **≥ 0.02 SOL** — the free
connection is accepted but `subscribeTokenTrade` is refused without it. The key
already in `.env` works.

**One socket only.** Trades, new launches and migrations are all multiplexed
over a single websocket. PumpPortal asks for one connection per key and warns
that opening several risks a ban; running three concurrently here measurably cut
throughput to about a third. `pumpportal_feed.py` owns the only connection —
don't add another.

Price per tick is the pool's mid after the swap (`vSolInBondingCurve /
vTokensInBondingCurve`), which is not skewed by which side traded. Graduated
tokens on AMM pools don't expose reserves, so those fall back to the executed
price (`solAmount / tokenAmount`).

## Running

```bash
# paper trade tokens that just graduated to pump-amm (recommended universe)
/opt/homebrew/bin/python3.14 ema_cross_bot.py --from-migrations

# watch specific mints
/opt/homebrew/bin/python3.14 ema_cross_bot.py --mints <MINT1> <MINT2>

# replay what was recorded
/opt/homebrew/bin/python3.14 backtest_ema_cross.py
/opt/homebrew/bin/python3.14 backtest_ema_cross.py --sweep

# live (real SOL)
/opt/homebrew/bin/python3.14 ema_cross_bot.py --live --mints <MINT>
```

Add mints from another process by writing `ema_watchlist.json` — it's re-read
every 5s:

```json
{"mints": ["So11111111111111111111111111111111111111112"]}
```

## Target state, not signals

The tracker exposes `want_long` (a target) rather than one-shot buy/sell
signals. The bot compares it to the position it actually holds and closes the
gap. A failed sell simply stays desired and retries on the next tick, and a
restart re-derives intent instead of losing it.

## Parameters

All env-overridable, no code edits needed:

| Var | Default | Meaning |
|---|---|---|
| `EMA_BAR_SEC` | 5 | Bar size in seconds |
| `EMA_FAST` / `EMA_SLOW` | 12 / 21 | EMA periods (60s / 105s at 5s bars) |
| `EMA_MIN_EDGE_BPS` | 15 | Cross must separate by 0.15% to count |
| `EMA_MIN_HOLD_BARS` | 3 | Ignore cross-down for 15s after entry |
| `EMA_COOLDOWN_BARS` | 6 | 30s flat before re-entering |
| `EMA_MAX_HOLD_BARS` | 360 | 30min time stop |
| `EMA_STOP_LOSS_PCT` | 12 | Hard stop from entry |
| `EMA_TRAIL_PCT` | 18 | Trailing stop from peak (0 disables) |
| `EMA_BUY_AMOUNT_SOL` | 0.18 | Size per trade |
| `EMA_MAX_CONCURRENT` | 5 | Max simultaneous positions |
| `EMA_MIN_TICKS` | 30 | Real swaps required before any entry |
| `EMA_MIN_TICK_DENSITY` | 0.5 | Min avg ticks/bar — blocks mostly-gap-filled charts |
| `EMA_MIN_MCAP_SOL` | 40 | Min market cap to enter |
| `EMA_FEE_PCT_PER_SIDE` | 1.5 | Cost model: fees |
| `EMA_SLIPPAGE_EST_PCT` | 1.0 | Cost model: slippage |

The guards (`MIN_EDGE_BPS`, `MIN_HOLD_BARS`, `COOLDOWN_BARS`) exist because the
raw 12/21 cross on a 5s chart fires constantly. Without them the bot pays a
round trip on every wiggle.

## The cost problem — read before going live

A round trip costs roughly **5%**: pump.fun's swap fee plus PumpPortal's fee on
each side (~1.5%/side), plus slippage on thin liquidity (~1%/side), plus the
priority fee. Paper mode charges exactly this, which is why paper PnL here is
lower than a naive backtest would show.

At 5s bars a 12/21 cross fires often. **The average firing has to clear ~5%
net** or the strategy loses money while looking right on the chart. That is the
single thing to check before `--live`, and it's what `backtest_ema_cross.py`
reports (`avg net` vs `avg gross` — the gap between those two columns is the
cost).

### Measured on live data (51 fresh pump.fun launches, 2026-09-10)

| | |
|---|---|
| Median trade history per token | **52 seconds** |
| Tokens surviving the 105s warmup | **7 / 51 (14%)** |
| Typical tick rate | ~0.3 swaps/sec, i.e. many empty 5s bars |

**86% of brand-new launches are dead before the indicator is even valid.** This
isn't a bug in the bot — EMA21 on 5s bars needs 21 bars, full stop. It means the
choice of *which tokens to point this at* matters more than the parameters.

That is why `--from-migrations` is the recommended source: tokens graduating to
pump-amm have already proven sustained volume, and there are ~40/hour. Use
`--auto-discover` (every new launch) only to gather tick data for backtesting.

Tick density matters for the same reason. On a barely-traded token most 5s bars
are gap-filled flat at the last close, so the EMAs partly measure silence and
the "cross" is an artifact of the fill, not of price. The entry gates
(`EMA_MIN_TICKS`, `EMA_MIN_TICK_DENSITY`, `EMA_MIN_MCAP_SOL`) block buys on
those; exits are never gated, so a position can always be closed.

Stops (`STOP_LOSS_PCT`, `TRAIL_PCT`) are evaluated on **every tick**, not on bar
close — 5 seconds is a long time in a memecoin dump.

## Restart behaviour

Open positions are re-read from `ema_cross_positions.json` on startup, the mints
re-subscribed, and the trackers marked long — so stops and the next cross-down
still apply instead of the position sitting unmanaged.

A position opened in `--live` mode will **not** be closed by a process running
in paper mode; the bot refuses and logs loudly rather than deleting a real
position from the ledger while the tokens are still on-chain. Failed sells back
off exponentially (2s up to 60s) instead of retrying on every tick.

## What the backtest actually showed (2026-09-10, ~20k live ticks, 99 tokens)

| | |
|---|---|
| Trades | 11 |
| Win rate | 45% |
| Avg PnL / trade | **+10.6% net** (+16.2% gross) |
| Median PnL | **−3.6%** |
| Best / worst | +85% / −55% |

Read those two middle rows together. The mean is positive and the median is
negative: **the typical trade loses a little and a couple of rare winners pay
for all of them.** That is a plausible profile for memecoin momentum, but it is
not the same claim as "this strategy wins".

And the sample is far too small to conclude anything either way — during
testing, four extra trades moved the total from +1.6% to +121%. The backtester
now prints a warning whenever `n < 30` or the mean and median disagree in sign.
**Get to 100+ trades in paper mode before going live.**

Other measured results:

- **Faster EMAs are worse.** 5/15 on 5s bars: 21 trades, 29% win rate, +1.5% avg
  net. More crossings means more round trips means more cost drag.
- **Slower bars barely trade.** 15s and 30s produced almost no trades — 21 bars
  of warmup is 315s and 630s, longer than most tokens live. The 5s timeframe is
  forced by token lifespan, not chosen for its edge.
- **Stops don't cap losses.** One −12% stop realised **−54.9%**. A stop fires on
  the first tick at or below the level, and on thin liquidity that tick can be
  far below it. The stop bounds *when* you exit, not *how much* you lose. Sizing
  is the only real protection.

## Wallet sharing

`solana_sniper_bot.py` and `hood_sniper_bot.py` use the same wallet. In `--live`
mode do not point this bot at a mint another bot is holding — both would try to
manage the same balance. State is kept in `ema_cross_positions.json`, separate
from `positions.json`, so the files don't collide.
