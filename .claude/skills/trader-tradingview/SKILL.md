---
name: trader-tradingview
description: >-
  TradingView bridge for the trader-* pack, for US stocks and crypto. Uses the
  TradingView MCP connector (get_ohlcv, get_quote, run_screener,
  search_symbols, get_technicals) as the data source when Yahoo is blocked,
  lagging or rate-limited, and turns saved get_ohlcv results into the chart
  files that trader-entry, trader-exit, trader-timemachine and trader-scanner
  read with --offline (regular hours only, previous close filled in), or
  hands them straight to trader-crypto with --bars. Also screens stocks and
  crypto with TradingView's screener. Use when the user says "use
  TradingView", "pull it from TradingView", "TradingView data", "Yahoo isn't
  working", "screen on TradingView", or when a trader-* tool cannot reach
  Yahoo.
argument-hint: "convert FILE --symbol TSLA | info FILE | symbol TICKER..."
---

# trader-tradingview

The trader-* tools read free Yahoo data. This skill swaps TradingView in.
Claude calls the TradingView connector, and `tv.py` turns what comes back into
files the tools already accept. Works for US stocks and crypto alike.

`${CLAUDE_SKILL_DIR}` is this skill's folder. Run from the project root.

## Which connector

Use whichever TradingView MCP tools are connected. The official TradingView
MCP server (https://www.tradingview.com/mcp/docs) is preferred. The
community tvremix server (`mcp__TVREMIX__*`) also works, but it shuts down on
2026-11-11 and allows 50 calls a day. If the tool names differ, look for the
equivalents of: OHLCV bars, a quote, a screener and symbol search.

## Symbols

TradingView wants `EXCHANGE:TICKER`:

| you say | ask TradingView for |
| --- | --- |
| TSLA, AAPL, NVDA | `NASDAQ:TSLA` ... |
| KO, BRK-B, JPM | `NYSE:KO`, `NYSE:BRK.B` ... |
| SPY, QQQ | `AMEX:SPY`, `NASDAQ:QQQ` |
| BTC, ETH, SOL | `BINANCE:BTCUSDT` (or `COINBASE:BTCUSD`) |

When unsure, call `search_symbols` or run `python3 "${CLAUDE_SKILL_DIR}/tv.py" symbol TSLA BTC`.

## Bars into the tools

1. Call `get_ohlcv(symbol, interval, count, summary=false)`.
   - Stocks: `5m`, count `5000` (about 64 sessions of regular hours), enough
     for the time machine's 60-day history. Entry checks only need `count` 300.
   - Crypto: `5m` (about 17 days) or `15m` (about 52 days), count `5000`.
2. A big result is saved to a file, and the tool result names its path. A
   small one comes back inline: write its JSON to `trader/tv/raw-SYMBOL.json`
   yourself. Never retype bars by hand from a summary.
3. Stocks: convert, then point any tool's `--offline` at the output:

```bash
python3 "${CLAUDE_SKILL_DIR}/tv.py" convert SAVED_FILE --symbol TSLA --interval 5m   # -> trader/tv/TSLA.json
python3 .claude/skills/trader-entry/entry.py check TSLA --offline trader/tv/TSLA.json --write
python3 .claude/skills/trader-exit/exit.py plan --write --offline trader/tv/TSLA.json
python3 .claude/skills/trader-timemachine/timemachine.py run --offline trader/tv/TSLA.json --write
```

   Add `--now "YYYY-MM-DD HH:MM"` (New York) when the bars end before right
   now, for example after the close.

4. Crypto: no conversion needed. `crypto.py ... --bars SAVED_FILE` reads
   TradingView JSON directly (see trader-crypto). `tv.py convert --crypto`
   just tidies the file into `trader/tv/BTC.json` for a scan folder.

`python3 "${CLAUDE_SKILL_DIR}/tv.py" info SAVED_FILE` shows what a file holds:
the bar count, spacing and first and last times.

## Screening, quotes, technicals

- **Stock scan:** `run_screener(market="america", sort_by="change", ...)`
  for the day's movers, or sort by `volume`. Then run trader-entry on the top
  names with TradingView bars.
- **Crypto scan:** `run_screener(market="crypto", sort_by="market_cap_calc")`
  (or `24h_vol_cmc`), then pick from those rows. Do NOT sort the whole crypto
  market by `change`: the top of that list is illiquid micro-caps with
  market caps in the thousands of dollars. Rows come back as
  `CRYPTO:BTCUSD`, which trader-crypto accepts as is. Then run trader-crypto
  `entry` / `scan` on the names that look interesting.
- **Price for sizing and alerts:** `get_quote("NASDAQ:TSLA")`. TradingView
  quotes are often fresher than Yahoo's free feed.
- **A second opinion:** `get_technicals` gives TradingView's own rating and
  moving averages. Treat it as context, not as a signal the pack relies on.

Save tool calls on the 50-a-day connector: one `get_ohlcv` with count 5000
covers entry, exit and the time machine for that symbol.

## Rules

- TradingView data feeds the analysis tools only. Orders still go through
  trader-risk and trader-broker for stocks (paper by default, never without
  the user's yes). Crypto stays analysis only.
- Check anything that matters against the broker before acting. Not
  financial advice.
