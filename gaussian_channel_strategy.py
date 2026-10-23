#!/usr/bin/env python3
"""
Gaussian Channel Strategy v3.3 — offline replica of the TradingView strategy.

Indicator: DonovanWall's Gaussian Channel (Ehlers N-pole Gaussian filter of the
source plus a filtered True Range band).

Default inputs (match the TV settings):
    Source hlc3 | Poles 4 | Period 144 | Range Multiplier 1.414
    Reduced Lag off | Fast Response off | Shorts off
    Start 2018-01-01 | capital 1000 | 100% of equity | commission 0.1%
    slippage 1 tick (0.01) | orders fill at next bar open

Rules (long only):
    entry : close crosses above the upper band (inside the date range)
    exit  : close crosses back below the upper band
    shorts (optional): close crosses below the lower band, cover on cross back above

Usage:
    python gaussian_channel_strategy.py btc_1d.csv [--trades out.csv]
CSV needs columns time (unix seconds), open, high, low, close.
"""
import argparse
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Binomial coefficients C(i, k) used by the 9x pole filter
def _binom(n, k):
    return math.comb(n, k) if 0 <= k <= n else 0


def gaussian_pole(src: np.ndarray, alpha: float, poles: int) -> np.ndarray:
    """One N-pole Gaussian filter pass, identical to f_filt9x in the Pine script.

    f = a^N * s + sum_{k=1..N} (-1)^(k+1) * C(N,k) * (1-a)^k * f[k]
    nz() semantics: NaN inputs and missing history are treated as 0.
    """
    x = 1.0 - alpha
    coefs = [((-1) ** (k + 1)) * _binom(poles, k) * x ** k for k in range(1, poles + 1)]
    a_n = alpha ** poles
    out = np.zeros(len(src))
    for t in range(len(src)):
        s = src[t] if not np.isnan(src[t]) else 0.0
        f = a_n * s
        for k, c in enumerate(coefs, start=1):
            if t - k >= 0:
                f += c * out[t - k]
        out[t] = f
    return out


def gaussian_channel(df: pd.DataFrame, source="hlc3", poles=4, period=144,
                     mult=1.414, reduced_lag=False, fast_response=False) -> pd.DataFrame:
    h, l, c = df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()
    if source == "hlc3":
        src = (h + l + c) / 3.0
    elif source == "hl2":
        src = (h + l) / 2.0
    elif source == "ohlc4":
        src = (df["open"].to_numpy() + h + l + c) / 4.0
    else:
        src = df[source].to_numpy()

    prev_c = np.concatenate([[np.nan], c[:-1]])
    tr = np.where(np.isnan(prev_c), h - l,
                  np.maximum(h - l, np.maximum(np.abs(h - prev_c), np.abs(l - prev_c))))

    beta = (1 - math.cos(4 * math.asin(1) / period)) / (math.pow(1.414, 2 / poles) - 1)
    alpha = -beta + math.sqrt(beta * beta + 2 * beta)
    lag = int((period - 1) / (2 * poles))

    if reduced_lag:
        src = src + (src - pd.Series(src).shift(lag).to_numpy())
        tr = tr + (tr - pd.Series(tr).shift(lag).to_numpy())

    filt_n = gaussian_pole(src, alpha, poles)
    filt_tr_n = gaussian_pole(tr, alpha, poles)
    if fast_response:
        filt = (filt_n + gaussian_pole(src, alpha, 1)) / 2
        filt_tr = (filt_tr_n + gaussian_pole(tr, alpha, 1)) / 2
    else:
        filt, filt_tr = filt_n, filt_tr_n

    out = df.copy()
    out["filt"] = filt
    out["hband"] = filt + filt_tr * mult
    out["lband"] = filt - filt_tr * mult
    return out


@dataclass
class Trade:
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    qty: float
    pnl: float
    ret_pct: float
    bars: int


def backtest(df: pd.DataFrame, start="2018-01-01", capital=1000.0, commission=0.001,
             slippage=0.01, enable_shorts=False):
    """Bar-close signals, fills at the next bar's open (TV 'on bar close' default)."""
    d = df.reset_index(drop=True)
    times = pd.to_datetime(d["time"], unit="s")
    hband, lband = d["hband"].to_numpy(), d["lband"].to_numpy()
    close, opn = d["close"].to_numpy(), d["open"].to_numpy()
    start_ts = pd.Timestamp(start)

    equity = capital
    pos = 0          # 1 long, -1 short, 0 flat
    qty = entry_px = 0.0
    entry_i = 0
    trades, curve = [], []

    for i in range(1, len(d) - 1):
        in_range = times[i] >= start_ts
        long_sig = in_range and close[i] > hband[i] and close[i - 1] <= hband[i - 1]
        long_exit = close[i] < hband[i] and close[i - 1] >= hband[i - 1]
        short_sig = in_range and enable_shorts and close[i] < lband[i] and close[i - 1] >= lband[i - 1]
        short_exit = close[i] > lband[i] and close[i - 1] <= lband[i - 1]
        nxt = opn[i + 1]

        def close_pos():
            nonlocal equity, pos
            px = nxt - slippage if pos == 1 else nxt + slippage
            gross = (px - entry_px) * qty * pos
            fees = (entry_px + px) * qty * commission
            pnl = gross - fees
            trades.append(Trade(times[entry_i + 1], entry_px, times[i + 1], px, qty, pnl,
                                pnl / (entry_px * qty) * 100, i - entry_i))
            equity += pnl
            pos = 0

        def open_pos(side):
            nonlocal pos, qty, entry_px, entry_i
            entry_px = nxt + slippage if side == 1 else nxt - slippage
            qty = equity / (entry_px * (1 + commission))
            pos, entry_i = side, i

        if pos == 1 and (long_exit or short_sig):
            close_pos()
        elif pos == -1 and (short_exit or long_sig):
            close_pos()
        if pos == 0:
            if long_sig:
                open_pos(1)
            elif short_sig:
                open_pos(-1)

        mtm = equity + ((close[i] - entry_px) * qty * pos if pos else 0.0)
        curve.append((times[i], mtm))

    return trades, pd.DataFrame(curve, columns=["time", "equity"]).set_index("time")


def summarize(trades, curve, capital, df):
    pnl = np.array([t.pnl for t in trades])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    eq = curve["equity"]
    dd = (eq / eq.cummax() - 1).min() * 100
    first = trades[0].entry_time if trades else curve.index[0]
    bh = df.loc[pd.to_datetime(df["time"], unit="s") >= first, "close"]
    return {
        "Net profit": round(pnl.sum(), 2),
        "Net profit %": round(pnl.sum() / capital * 100, 2),
        "Total trades": len(trades),
        "Percent profitable": round(len(wins) / max(len(trades), 1) * 100, 2),
        "Profit factor": round(wins.sum() / abs(losses.sum()), 3) if len(losses) else float("inf"),
        "Avg win": round(wins.mean(), 2) if len(wins) else 0,
        "Avg loss": round(losses.mean(), 2) if len(losses) else 0,
        "Max drawdown % (close)": round(dd, 2),
        "Buy & hold %": round((bh.iloc[-1] / bh.iloc[0] - 1) * 100, 2),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv")
    ap.add_argument("--start", default="2018-01-01")
    ap.add_argument("--poles", type=int, default=4)
    ap.add_argument("--period", type=int, default=144)
    ap.add_argument("--mult", type=float, default=1.414)
    ap.add_argument("--source", default="hlc3")
    ap.add_argument("--reduced-lag", action="store_true")
    ap.add_argument("--fast-response", action="store_true")
    ap.add_argument("--shorts", action="store_true")
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--trades", help="write trade list CSV here")
    a = ap.parse_args()

    df = pd.read_csv(a.csv).sort_values("time").reset_index(drop=True)
    gc = gaussian_channel(df, a.source, a.poles, a.period, a.mult, a.reduced_lag, a.fast_response)
    trades, curve = backtest(gc, a.start, a.capital, enable_shorts=a.shorts)

    for k, v in summarize(trades, curve, a.capital, df).items():
        print(f"{k:24s} {v}")
    if a.trades:
        pd.DataFrame([t.__dict__ for t in trades]).to_csv(a.trades, index=False)
        print(f"trades -> {a.trades}")


if __name__ == "__main__":
    main()
