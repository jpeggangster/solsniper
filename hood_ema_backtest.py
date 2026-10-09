"""
12/21 EMA cross backtest for Robinhood Chain (pons v2 curves).
---------------------------------------------------------------
Companion to ema_cross_strategy.py, which runs the same idea on Solana 5s bars.
This one answers a narrower question: on pons curves, do TICK bars beat 5s TIME
bars, and does either clear costs?

Why tick bars. Measured on-chain, the median live pons curve trades once every
~77s. An EMA21 on 5s bars needs 21 bars = 105s, so at the median roughly 1.5
real trades land inside the whole warmup and the other ~19 bars are gap-fill —
the EMAs end up crossing over interpolation rather than over the market. A tick
bar closes every N trades instead, so 21 bars always means 21 bars of real
trades however fast or slow they arrive.

Tick data is reconstructed from the curve's own events rather than recorded
forward, so a backtest can run on history that already exists:

    BUY  0xec36bf…  (quoteIn,  tokensOut, fee, 0)
    SELL 0x8113d7…  (tokensIn, quoteOut,  fee, 0)

price = quote / tokens, and `fee` is the curve's cut — observed at exactly 1%
of the quote on every sample, which is where CURVE_FEE_BPS comes from rather
than from a doc.

Usage:
  python3.14 hood_ema_backtest.py                 # sample live curves, compare bar types
  python3.14 hood_ema_backtest.py --sweep         # grid over bar size / periods
  python3.14 hood_ema_backtest.py --hours 3       # widen the history window
"""

import argparse
import os
import statistics
import sys
from typing import Optional

from web3 import Web3

import pons_curve as pc

RPC = os.getenv("HOOD_RPC", "https://rpc.mainnet.chain.robinhood.com")
w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 60}))

BUY_TOPIC = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
SELL_TOPIC = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"

# ─── COST MODEL (all measured, not assumed) ──────────────────────────────────
CURVE_FEE_BPS = float(os.getenv("HOOD_CURVE_FEE_BPS", "100"))   # 1%, read off the events
GAS_PER_TX_ETH = float(os.getenv("HOOD_GAS_PER_TX_ETH", "0.000051"))
POSITION_ETH = float(os.getenv("HOOD_BT_POSITION_ETH", "0.01"))

# ─── STRATEGY DEFAULTS ───────────────────────────────────────────────────────
FAST = int(os.getenv("EMA_FAST", "12"))
SLOW = int(os.getenv("EMA_SLOW", "21"))
TICKS_PER_BAR = int(os.getenv("HOOD_TICKS_PER_BAR", "10"))
TIME_BAR_SEC = int(os.getenv("HOOD_TIME_BAR_SEC", "5"))
MIN_EDGE_BPS = float(os.getenv("EMA_MIN_EDGE_BPS", "15"))

BLOCK_SEC = 0.103


def _words(log) -> list[int]:
    d = log["data"]
    d = d.hex() if hasattr(d, "hex") else d
    d = d[2:] if d.startswith("0x") else d
    return [int(d[i:i + 64], 16) for i in range(0, len(d), 64)]


def _topic0(log) -> str:
    t = log["topics"][0].hex()
    return t if t.startswith("0x") else "0x" + t


def fetch_ticks(curve: str, from_block: int, to_block: int) -> list[dict]:
    """Reconstruct executed trades for a curve from its own events."""
    try:
        logs = w3.eth.get_logs({"address": w3.to_checksum_address(curve),
                                "fromBlock": from_block, "toBlock": to_block,
                                "topics": [[BUY_TOPIC, SELL_TOPIC]]})
    except Exception as e:
        print(f"  getLogs failed for {curve[:12]}…: {str(e)[:80]}")
        return []

    ticks = []
    for lg in sorted(logs, key=lambda l: (l["blockNumber"], l["logIndex"])):
        wds = _words(lg)
        if len(wds) < 3:
            continue
        fee = wds[2]
        if _topic0(lg) == BUY_TOPIC:
            # quoteIn includes the fee, so the raw ratio sits ~1% ABOVE mid
            quote, tokens, side = wds[0], wds[1], "buy"
            mid_quote = quote - fee
        else:
            # quoteOut is net of the fee, so the raw ratio sits ~1% BELOW mid
            tokens, quote, side = wds[0], wds[1], "sell"
            mid_quote = quote + fee
        if quote <= 0 or tokens <= 0 or mid_quote <= 0:
            continue
        # Use mid, not the executed price. Otherwise the series carries a
        # synthetic ~2% bid-ask bounce purely from which side happened to
        # trade, and a fast EMA cross reads that noise as signal.
        ticks.append({"block": lg["blockNumber"], "price": mid_quote / tokens,
                      "raw_price": quote / tokens,
                      "side": side, "quote_eth": quote / 1e18})
    # block timestamps, interpolated from block height (blocks are ~0.103s)
    for t in ticks:
        t["ts"] = t["block"] * BLOCK_SEC
    return ticks


# ─── BAR BUILDERS ────────────────────────────────────────────────────────────
def tick_bars(ticks: list[dict], n: int) -> list[float]:
    """Close a bar every n trades. Every bar is real trades — no gap fill."""
    out, buf = [], []
    for t in ticks:
        buf.append(t["price"])
        if len(buf) >= n:
            out.append(buf[-1])
            buf = []
    return out


def time_bars(ticks: list[dict], sec: int) -> list[float]:
    """Wall-clock bars, gap-filled flat — the same shape ema_cross_strategy uses."""
    if not ticks:
        return []
    out = []
    start = ticks[0]["ts"]
    cur = start + sec
    last = ticks[0]["price"]
    i = 0
    end = ticks[-1]["ts"]
    guard = 0
    while cur <= end and guard < 20000:
        guard += 1
        while i < len(ticks) and ticks[i]["ts"] <= cur:
            last = ticks[i]["price"]
            i += 1
        out.append(last)          # empty bar repeats the last close
        cur += sec
    return out


# ─── STRATEGY ────────────────────────────────────────────────────────────────
def run_strategy(closes: list[float], fast: int, slow: int,
                 min_edge_bps: float) -> list[dict]:
    """Long-only EMA cross. Returns closed round trips with gross returns."""
    if len(closes) < slow + 2:
        return []
    fa, sa = 2 / (fast + 1), 2 / (slow + 1)
    ef = es = closes[0]
    trades, entry = [], None
    prev_diff = 0.0
    for i, c in enumerate(closes):
        ef = c * fa + ef * (1 - fa)
        es = c * sa + es * (1 - sa)
        if i < slow:
            prev_diff = ef - es
            continue
        diff = ef - es
        edge_ok = abs(diff) / c * 10_000 >= min_edge_bps if c else False
        if entry is None:
            if prev_diff <= 0 < diff and edge_ok:
                entry = c
        else:
            if prev_diff >= 0 > diff:
                trades.append({"entry": entry, "exit": c, "gross": c / entry - 1})
                entry = None
        prev_diff = diff
    return trades


def apply_costs(trades: list[dict], creator_tax_bps: float) -> list[dict]:
    """Net out curve fee, creator tax (both sides) and gas."""
    c = (CURVE_FEE_BPS + creator_tax_bps) / 10_000.0
    gas_drag = 2 * GAS_PER_TX_ETH / POSITION_ETH
    for t in trades:
        t["net"] = (1 + t["gross"]) * (1 - c) ** 2 - 1 - gas_drag
    return trades


# ─── SAMPLING ────────────────────────────────────────────────────────────────
def sample_curves(hours: float, min_ticks: int, limit: int) -> list[dict]:
    latest = w3.eth.block_number
    span = int(hours * 3600 / BLOCK_SEC)
    found, seen = [], set()
    step = 15000
    start = latest
    while start > latest - span and len(found) < limit * 4:
        frm, to = max(0, start - step), start
        for l in pc.recent_launches(w3, frm, to):
            if not l["native_eth"] or l["curve"] in seen:
                continue
            seen.add(l["curve"])
            found.append(l)
        start = frm
    print(f"scanned {hours:.1f}h ({span:,} blocks) -> {len(found)} ETH-paired launches")

    # Rank by trade count and keep the densest. Taking the first N that merely
    # clear the threshold fills the sample with curves too thin to produce even
    # one bar past the EMA warmup, which tests nothing.
    scored = []
    for l in found:
        ticks = fetch_ticks(l["curve"], max(0, latest - span), latest)
        if len(ticks) >= min_ticks:
            scored.append((len(ticks), l, ticks))
    scored.sort(key=lambda x: -x[0])
    out = []
    for _, l, ticks in scored[:limit]:
        info = pc.lookup(w3, l["token"])
        out.append({"token": l["token"], "curve": l["curve"], "ticks": ticks,
                    "creator_tax_bps": (info or {}).get("creator_tax_bps", 0)})
    return out


def summarise(label: str, trades: list[dict]) -> Optional[dict]:
    if not trades:
        print(f"  {label:<22} no trades")
        return None
    g = [t["gross"] for t in trades]
    n = [t["net"] for t in trades]
    wins = sum(1 for x in n if x > 0)
    row = {"label": label, "n": len(trades),
           "gross": statistics.mean(g) * 100, "net": statistics.mean(n) * 100,
           "win": wins / len(n) * 100, "total": sum(n) * 100,
           "median_net": statistics.median(n) * 100}
    print(f"  {label:<22} {row['n']:>4} trades | gross {row['gross']:+6.2f}% | "
          f"net {row['net']:+6.2f}% | median net {row['median_net']:+6.2f}% | "
          f"win {row['win']:4.0f}% | total {row['total']:+7.1f}%")
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=1.5)
    ap.add_argument("--min-ticks", type=int, default=40)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--sweep", action="store_true")
    a = ap.parse_args()

    print(f"RPC {RPC} | chain {w3.eth.chain_id}")
    print(f"costs: curve fee {CURVE_FEE_BPS/100:.2f}%/side + creator tax + "
          f"gas {GAS_PER_TX_ETH} ETH/tx on a {POSITION_ETH} ETH position "
          f"({2*GAS_PER_TX_ETH/POSITION_ETH*100:.2f}% drag)\n")

    curves = sample_curves(a.hours, a.min_ticks, a.limit)
    if not curves:
        print("no curves with enough trades in the window")
        return
    tot = sum(len(c["ticks"]) for c in curves)
    print(f"usable curves: {len(curves)} | total reconstructed trades: {tot:,}")
    print(f"trades per curve: median {statistics.median([len(c['ticks']) for c in curves]):.0f}, "
          f"max {max(len(c['ticks']) for c in curves)}\n")

    configs = [("tick bars x10", "tick", 10), ("tick bars x5", "tick", 5),
               ("tick bars x20", "tick", 20), ("time bars 5s", "time", 5),
               ("time bars 15s", "time", 15)]
    if a.sweep:
        configs = [(f"tick x{n}", "tick", n) for n in (3, 5, 8, 10, 15, 20, 30)] + \
                  [(f"time {s}s", "time", s) for s in (5, 10, 15, 30, 60)]

    print(f"EMA {FAST}/{SLOW}, min edge {MIN_EDGE_BPS} bps\n")
    results = []
    for label, kind, size in configs:
        allt = []
        for c in curves:
            closes = (tick_bars(c["ticks"], size) if kind == "tick"
                      else time_bars(c["ticks"], size))
            t = run_strategy(closes, FAST, SLOW, MIN_EDGE_BPS)
            allt += apply_costs(t, c["creator_tax_bps"])
        r = summarise(label, allt)
        if r:
            results.append(r)

    if results:
        best = max(results, key=lambda r: r["total"])
        print(f"\nbest total: {best['label']} ({best['total']:+.1f}% over {best['n']} trades)")
        pos = [r for r in results if r["net"] > 0]
        print("configs with positive average net:",
              ", ".join(r["label"] for r in pos) if pos else "NONE")


if __name__ == "__main__":
    main()
