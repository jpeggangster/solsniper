"""
Paper-trade the 12/21 EMA cross on a Robinhood Chain token — market-cap view.
------------------------------------------------------------------------------
Spends nothing. Same tick sources, bars and cross rules the live bot uses
(hood_ema.py), so what it prints is what the bot would have done.

Everything is reported as MARKET CAP rather than price, to match the Axiom /
TradingView view. Market cap is derived on-chain:

    price(quote per token) = (sqrtPriceX96 / 2^96)^2 * 10^(dec_token - dec_quote)
    market cap             = totalSupply / 10^dec_token * price

For pons tokens the quote is usually USDG, so market cap is already in dollars.

Usage:
  python3.14 hood_paper.py 0x<token> [--size 0.01] [--seconds 600]
"""

import argparse
import os
import sys
import time
from datetime import datetime

from web3 import Web3

import hood_ema as ema

ERC20 = [
    {"name": "decimals", "inputs": [], "outputs": [{"type": "uint8"}],
     "stateMutability": "view", "type": "function"},
    {"name": "symbol", "inputs": [], "outputs": [{"type": "string"}],
     "stateMutability": "view", "type": "function"},
    {"name": "totalSupply", "inputs": [], "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
]

w3 = ema.w3


def erc20(a):
    return w3.eth.contract(address=w3.to_checksum_address(a), abi=ERC20)


def mc_factor(token: str, quote: str) -> float:
    """Multiplier turning a raw sqrtPrice ratio into a market cap."""
    t = erc20(token)
    dec_t = t.functions.decimals().call()
    supply = t.functions.totalSupply().call() / (10 ** dec_t)
    dec_q = erc20(quote).functions.decimals().call() if quote else 18
    return supply * (10 ** (dec_t - dec_q))


def fmt_mc(v: float) -> str:
    if v >= 1e9:
        return f"${v/1e9:.2f}B"
    if v >= 1e6:
        return f"${v/1e6:.2f}M"
    if v >= 1e3:
        return f"${v/1e3:.1f}K"
    return f"${v:.0f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("token")
    ap.add_argument("--size", type=float, default=0.01)
    ap.add_argument("--seconds", type=int, default=900)
    a = ap.parse_args()

    tok = w3.to_checksum_address(a.token)
    sym = erc20(tok).functions.symbol().call()
    stream = ema.TokenStream(tok)
    quote = getattr(stream, "quote_token", None)
    k = mc_factor(tok, quote)

    print(f"PAPER — {sym} {tok}")
    print(f"venue {stream.venue} | quote {quote}")
    print(f"EMA{ema.FAST}/{ema.SLOW} on {ema.BAR_SEC}s bars | min edge {ema.MIN_EDGE_BPS} bps "
          f"| signals gated until {ema.MIN_SIGNAL_BARS} bars")
    print("-" * 96)

    st = ema.backfill_state(stream)
    if st is None:
        print("no trades yet — waiting for the first tick")
        st = None

    pos = None
    trades = []
    end = time.time() + a.seconds
    last_line = 0.0

    while time.time() < end:
        ticks = stream.poll()
        if st is None:
            if ticks:
                st = ema.backfill_state(stream)
            time.sleep(2)
            continue

        sigs = ema.ingest(st, ticks, time.time())
        f_mc = st["ema_fast"] * k
        s_mc = st["ema_slow"] * k
        px_mc = st["bar_close"] * k
        spread = (st["ema_fast"] / st["ema_slow"] - 1) * 100 if st["ema_slow"] else 0

        for sig in sigs:
            ts = datetime.now().strftime("%H:%M:%S")
            if sig == "enter" and pos is None:
                pos = {"mc": px_mc, "t": time.time()}
                print(f"{ts}  🟢 BUY  at {fmt_mc(px_mc):>9} | "
                      f"EMA12 {fmt_mc(f_mc):>9} > EMA21 {fmt_mc(s_mc):>9} ({spread:+.2f}%)")
            elif sig == "exit" and pos:
                chg = px_mc / pos["mc"] - 1
                held = time.time() - pos["t"]
                trades.append(chg)
                print(f"{ts}  🔴 SELL at {fmt_mc(px_mc):>9} | entry {fmt_mc(pos['mc']):>9} | "
                      f"{chg:+.1%} in {held:.0f}s | cum {sum(trades):+.1%} over {len(trades)}")
                pos = None

        if time.time() - last_line >= 10:
            last_line = time.time()
            state = "🟢 12>21" if st["ema_fast"] > st["ema_slow"] else "🔴 12<21"
            hold = f"HOLD from {fmt_mc(pos['mc'])} ({px_mc/pos['mc']-1:+.1%})" if pos else "flat"
            ready = "" if st["bars"] >= ema.MIN_SIGNAL_BARS else \
                    f" [warming {st['bars']}/{ema.MIN_SIGNAL_BARS}]"
            print(f"{datetime.now():%H:%M:%S}  MC {fmt_mc(px_mc):>9} | "
                  f"EMA12 {fmt_mc(f_mc):>9} EMA21 {fmt_mc(s_mc):>9} | {state} {spread:+6.2f}% | "
                  f"{len(ticks):>3} trades | {hold}{ready}")
        time.sleep(2)

    print("-" * 96)
    if trades:
        wins = sum(1 for t in trades if t > 0)
        print(f"paper result: {len(trades)} round trips | cumulative {sum(trades):+.1%} | "
              f"win rate {wins/len(trades)*100:.0f}%  (gross of fees)")
    else:
        print("paper result: no completed round trips")
    if pos:
        print(f"still holding from {fmt_mc(pos['mc'])}")


if __name__ == "__main__":
    main()
