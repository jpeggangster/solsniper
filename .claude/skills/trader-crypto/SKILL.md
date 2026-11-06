---
name: trader-crypto
description: >-
  Crypto Day Trader (analysis only). The day-trading pipeline for 24/7 crypto
  markets: scans coins for setups (7-day closing-high breakout, oversold and
  oversold bounce on daily RSI, reclaimed 20-day average, opening-range
  breakout, volume vs the 7-day norm), times an opening-range or level
  breakout from a UTC session open, plans the stop, target and flat-by time,
  replays the rest of the session 10,000 times from the coin's own bars plus
  every past session the rule fired, and sizes the position in fractional
  units to your budget and max loss. NEVER builds or places an order:
  trader-risk blocks crypto and trader-broker never sees this. Reads
  TradingView MCP bars (via trader-tradingview), Yahoo or Coinbase. Use when
  the user says "scan crypto", "is BTC breaking out", "crypto setups", "plan a
  SOL trade", "where's my stop on ETH", "replay this crypto trade", "how much
  BTC should I buy", or names a coin (BTC, ETH, SOL, ...) with any trader-*
  request.
argument-hint: "scan | entry SYMBOL | plan | replay | size"
---

# trader-crypto

The same method as the stock pack (scan, time the entry, plan the exit,
replay the odds, size it), rebuilt for a market that never closes. It is
**analysis only**: no ticket, no order JSON, no broker call. If the user asks
to buy, say plainly that crypto orders are off in this pack, and show the
plan and size so they can place it themselves wherever they trade.

`${CLAUDE_SKILL_DIR}` is this skill's folder. Run from the project root:
everything lands in `trader/crypto/`.

```bash
python3 "${CLAUDE_SKILL_DIR}/crypto.py" scan                                # top 20 coins, 1h bars
python3 "${CLAUDE_SKILL_DIR}/crypto.py" scan --symbols BTC,ETH,SOL --write   # -> trader/crypto/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/crypto.py" entry BTC --write                    # ORB from 00:00 UTC -> entry.json
python3 "${CLAUDE_SKILL_DIR}/crypto.py" plan --stop or-mid --target 2R --write   # -> plan.json
python3 "${CLAUDE_SKILL_DIR}/crypto.py" replay --write                       # 10,000 sessions + history
python3 "${CLAUDE_SKILL_DIR}/crypto.py" size --budget 2000 --max-loss 20     # units; not an order
```

## Sessions

Crypto has no opening bell, so the tool makes one: a 24-hour session starting
at `--session-open` (00:00 UTC by default). Useful alternatives:

- `--session-open 13:30`: the US stock open in winter (13:30 UTC = 9:30 a.m.
  New York in standard time; 13:30 becomes 12:30 in daylight time).
- `--session-open 08:00`: the London open.

The opening range is the first `--or-minutes` (60 by default). Entries stop
`--entries-hours` after the open (12 by default). The plan goes flat
`--flat-after` hours after the open (23.75 by default). Pass the same
`--session-open` to `entry`. `plan` and `replay` read it back from the files.

## Data, best first

1. **TradingView connector** (the most reliable source, and the only one when
   Yahoo is blocked). Follow the trader-tradingview skill: call `get_ohlcv`
   with `BINANCE:BTCUSDT`, interval `5m` (or `15m`), count up to 5000. When
   the result is saved to a file, pass that path straight in:
   `--bars /path/to/saved-result.txt`. trader-crypto reads the raw
   TradingView JSON, so no conversion is needed. For `scan`, save one file
   per coin into a folder (`trader/tv/BTC.json`, ...) and pass `--bars trader/tv`.
2. **Yahoo** (`BTC-USD`), fetched automatically when no `--bars` is given.
   5-minute history goes back about 60 days.
3. **Coinbase** public candles as the fallback.

`replay` wants weeks of history at the plan's interval: 5000 5-minute bars is
about 17 days, 5000 15-minute bars about 52 days. Use the same file for
`entry` and `replay`.

## Reading the output

- `entry`: FORMING (the range is still building), WAITING (with the distance
  to the level), TRIGGERED (time and price of the first close above), or
  EXPIRED (no close above before the entry cutoff).
- `plan` refuses a stop at or above the entry, one more than 30 % away, and
  one too tight to survive the spread (`--min-stop-pct`, 0.15 % by default).
- `replay`: the shares of target first, stop first and still open at the
  flat-by time, the average and spread of R, then the same rule on every past
  full session. Report the verdict as it is printed. A bar that touches both
  the stop and the target counts as the stop.
- `size`: units rounded down to `--unit-step` (0.0001 by default), limited
  by the max loss per trade and by the position cap (`--max-position-pct`, 50 %
  of the budget). It also reads `crypto.budget` and `crypto.max_loss_per_trade`
  from `trader/config.json`.

## Rules

- Never write an order, call a broker or exchange tool, or suggest a way
  around the crypto block in trader-risk. Analysis only, as the user chose.
- Crypto trades around the clock and gaps hard on news: a stop is a plan,
  not a guarantee. The time machine is a simulation from past bars, not a
  prediction.
- Not financial advice. Most day traders lose money, and leverage makes that
  worse. This tool assumes spot trading with no leverage.
