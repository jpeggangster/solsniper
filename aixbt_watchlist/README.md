# AIXBT trending → TradingView watchlist

Every 4 hours, pull the tokens AIXBT is currently trending and add each one to the
TradingView watchlist `AIXBT_TRENDING_WATCHLIST` (id `346089787`), using the chart
with the most historical data — any chain, CEX or DEX.

- Routine: `~/.claude/scheduled-tasks/aixbt-trending-watchlist/SKILL.md`
- Schedule: `23 2,6,10,14,18,22 * * *` (offset 2h from `dex-memes-watchlist-refresh`,
  which fights over the same active-watchlist state)
- Resolver: `resolve_pairs.py`
- Symbol validator: `validate_symbols.py`
- Run history: `run.log`

## Usage

```bash
/opt/homebrew/bin/python3.14 resolve_pairs.py /tmp/existing.json < /tmp/aixbt_tokens.json > /tmp/picks.json
```

stdin: `[{"name","symbol","trend","assets":[{"chain","address"}]}]` straight from AIXBT
`list_projects`. Optional argv[1]: JSON array of symbols already on the watchlist.
stdout: `{"picks":[...],"skipped":[...]}`; a readable table goes to stderr.

## Why this is not just "sort by first_bar_time"

Chart depth comes from TradingView's scanner field `first_bar_time`, so "most data"
is measured, not guessed. But taking the deepest chart for a ticker is wrong often
enough to be dangerous:

- AIXBT trends a Solana memecoin called **Buy The Cat** whose ticker is `BTC`. The
  deepest `BTC` chart is Bitcoin's, from 2013.
- A 2026 Solana **STONK** shares its ticker with a dormant 2020 Ethereum STONK.

Both resolve silently and look perfectly reasonable in a watchlist. So each token is
anchored to its real on-chain birth (DexScreener, by contract address) and any chart
starting materially before that birth is rejected as a different asset.

Three refinements that came out of testing against live data:

1. **The DexScreener match is validated by symbol.** Bittensor's AIXBT "ethereum"
   address is a bridged representation whose pools are symboled `NIL`; trusting it
   dated a 2023 token to 2025 and threw away its real charts. Pools whose
   `baseToken.symbol` differs from the ticker no longer count.
2. **The clamp only applies to tokens younger than 90 days.** For older tokens the
   on-chain pool age is just a lower bound (pools migrate and get recreated).
3. **Dated derivatives are excluded.** With old charts clamped away, `BTC` otherwise
   resolved to `DERIBIT:BTCUSD18U2026`, a dated Bitcoin future — recent first bar,
   still the wrong asset. CEX lines must match `TICKER + USD/USDT/USDC` with an
   optional `.P`.

A pick is flagged `collision_risk: true` when AIXBT gave contract addresses but no
pool could be confirmed under that ticker, meaning the clamp never ran. It is a
"could not verify", not a "known wrong" — `LIT` (Lighter vs Litentry) is the live
example.

## Resolvable != listed in the scanner

A symbol can sit in TradingView's scanner and still be unchartable — it shows **red**
in the watchlist with no data. The usual cause is a spot line for a token that only
trades as a perp on that venue: Binance has `VVVUSDT.P` but no `VVVUSDT`.

`validate_symbols.py` checks this against TradingView's symbol-search endpoint,
requiring an exact `exchange:ticker` match. It found precisely the five red lines the
user reported (`BINANCE:VVVUSDT`, `BINANCE:USELESSUSDT`, `BINANCE:MONUSDT`,
`BYBIT:CASHCATUSDT`, `UNISWAP4BASE:LAPTOPUSDC_89F4F6.USD`), all replaced by their
working `.P` equivalents.

The resolver now walks down its ranking to the next-best candidate that resolves,
rather than returning the top-ranked one blindly. A network failure is reported as
`unknown`, never as "dead" — we do not delete a symbol because a request timed out.

## Removing symbols

There is no MCP tool for removal. Use the REST endpoint through `ui_evaluate`:

`POST /api/v1/symbols_list/custom/346089787/remove/?unsafe=true` with a JSON array of
symbols and the `X-CSRFToken` cookie value.

It removes only the named symbols. **Never rewrite the whole symbol array** — other
processes write to this list concurrently (it grew 47 → 56 mid-session during
development), so a read-modify-write would silently discard their additions.

## Selection order

Among candidates that survive filtering (USD-ish quote, real volume, chain match for
DEX pools):

1. venue rank — `VENUE_ORDER`, BINANCE first
2. spot before perp
3. deepest chart
4. highest 24h volume

Venue and spot outrank depth deliberately. Pure depth-first picked `POLONIEX:BTCUSDT`
over Binance and `BITSO:BTCMXN` (peso-quoted) for a handful of extra bars, and the
existing watchlist is almost entirely BINANCE spot.

Deduping is by **base ticker**, not exact symbol, so a list already holding
`BINANCE:TAOUSDT` does not also collect `BYBIT:TAOUSDT.P`.

## Expect skips

Brand-new Solana memecoins are frequently not indexed by TradingView at all — KNOTS
and NearKat both returned zero pools while trending in AIXBT's top 15. There is no
chart to add, so they are skipped rather than approximated. The watchlist therefore
fills mostly with the more established trending names.
