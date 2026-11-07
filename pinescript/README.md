# Pine Script Strategy Library

TradingView strategies built and reviewed in chat. Every file is a Pine **v6 `strategy()`**,
so it shows results in the Strategy Tester. None of these has been compiled in TradingView
by the assistant; paste into the Pine Editor to check.

Common conventions across the library:
- Alerts use `alert()` (create alerts with **"alert() function calls only"**); strategies ignore `alertcondition()`.
- Oscillators live in the script's own pane; price-chart lines use `force_overlay=true` so the price scale isn't squashed.
- Where SL/TP are used, they're locked at entry and repeat signals in the same direction are ignored.
- Default sizing is 10% of equity unless noted (some keep the original 100%).

| # | File | Type | Indicators | Intended TF | Exits (default) |
|---|------|------|-----------|-------------|-----------------|
| 1 | `01_supertrend_fisher_klinger_30m.pine` | Trend + momentum | Supertrend, Fisher Transform, Klinger | 30m | Supertrend flip |
| 2 | `02_aroon_vortex_volosc_10m.pine` | Breakout | Aroon, Vortex, Volume Oscillator | 10m | 1% SL / 2% TP, opposite signal |
| 3 | `03_bb_adx_mfi_ranging_5m.pine` | Mean reversion | Bollinger Bands, ADX < 20, MFI | 5m | TP at BB basis, 1% SL |
| 4 | `04_vwap_bands_stochrsi_rvol.pine` | Mean reversion | Session VWAP ±2σ bands, Stoch RSI, RVOL | Intraday | TP at VWAP, 1% SL |
| 5 | `05_kc_cmo_rvol_scalp_3m.pine` | Breakout scalp | Keltner Channels, CMO, RVOL | 3m | 1×ATR SL / 2×ATR TP |
| 6 | `06_vwap_rsi_volume_5m.pine` | VWAP reclaim | VWAP, RSI 14 (30/70), Volume MA 30 | 5m | 0.5% SL / 1% TP, VWAP exit |
| 7 | `07_bb_stochrsi_volume_scalper_1m.pine` | Mean reversion scalp | Bollinger Bands, Stoch RSI (25/75), Volume MA 15 | 1m | TP at BB basis, 0.3% SL |
| 8 | `08_bb_rsi_range_breakout_exit_1h.pine` | Mean reversion | Bollinger Bands, RSI, RSI breakout exit | BTC 1H | TP at basis, SL beyond band, RSI 20/80 bail-out (100% equity) |
| 9 | `09_bb_adx_rsi_range.pine` | Mean reversion | Bollinger Bands, ADX < 22, RSI | Any | Basis / RSI 50 exit, 2% SL, 1.5R (100% equity) |
| 10 | `10_smi_ema_cloud_pullback.pine` | Trend pullback | SMI, 50/200 EMA cloud | 1H–4H | Swing SL, 2.5R TP |
| 11 | `11_bb_kc_squeeze_breakout.pine` | Squeeze breakout | BB inside KC, 200 EMA, Volume | BTC 15m/1H | Breakout-candle SL, 1.5R TP |
| 12 | `12_donchian_adx_volume_breakout.pine` | Trend | Donchian 20, ADX > 25, Volume | BTC 1H/4H | Opposite band SL, 1.5R, mid-band exit |
| 13 | `13_bb_rsi_divergence.pine` | Mean reversion | Bollinger Bands 30, RSI, simple divergence | Any | Mid-band exit, optional % SL (100% equity) |
| 14 | `14_donchian_lwti_volume_v2.pine` | Breakout | Donchian 96, LWTI, Volume MA 30, HTF EMA, ADX | 5m → try 1H | Trailing DC-middle SL, 2R, breakeven at +1R |
| 15 | `15_rsi_vwap_extreme_reversal.pine` | Reversal | RSI of VWAP, Extreme Reversal candle (3× range) | Intraday | SL beyond extreme, 2R or VWAP |

## Key fixes made to the originals

- **#1, #4, #9**: missing `*` operators (stripped by copy/paste) restored.
- **#13**: `ta.bb()` returns `[middle, upper, lower]`; the original unpacked it in the wrong order, so shorts fired above the *middle* band and longs exited at the *upper* band.
- **#10, #11**: repeat signals were silently moving an open trade's SL/TP.
- **#12**: one unprotected bar after entry (exit placed a bar late); "breakout" actually traded mid-band crosses (both modes now available).
- **#14**: original "LWTI" was a smoothed stochastic; TradingView-style LWTI added as default.
- **#15**: original same-bar logic was nearly impossible (huge red candle vs. rising RSI-VWAP); "Window" signal mode added.

## Backtest results on record

### #14 Donchian + LWTI + Volume — **v1** (before v2 changes)
INDEX:BTCUSD, 5m, 24 Aug – 7 Nov 2026, 278 trades, 10% equity per trade, 0.05% commission per side.

| Metric | Value |
|---|---|
| Net | −$328 (−3.3% of account) |
| Win rate | 26.3% (needed 35.6% to break even) |
| Avg win / avg loss | +$8.13 / −$4.50 (1.81) |
| Profit factor | 0.64 |
| Max drawdown | $376 (~3.8% of account; ~1/3 of per-trade size) |
| Commission | $272 (83% of the loss); pre-fee result ≈ −$56 |
| Longs / shorts | −$168 / −$161 |
| Max losing streak | 17 |

Diagnosis: entry had ~zero edge in a ranging market; 5m costs dominated; wick-triggered
breakouts; 36 losers had been ≥ +0.5% in profit. v2 adds close-confirmation, 1H EMA trend
filter, ADX filter, breakeven stop, cooldown, and "Stop"/"TP" exit labels. **v2 not yet backtested.**

Other strategies: results were reviewed by the user in TradingView but not shared here.
Add them below as they come in.
