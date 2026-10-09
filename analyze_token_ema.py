"""
Report every EMA crossover for a Solana token on a chosen timeframe.

  /opt/homebrew/bin/python3.14 analyze_token_ema.py <CA>                  # 1m history
  /opt/homebrew/bin/python3.14 analyze_token_ema.py <CA> --bar-sec 15     # 15s, needs ticks
  /opt/homebrew/bin/python3.14 analyze_token_ema.py <CA> --bar-sec 15 --ticks gt_ticks.jsonl

DATA AVAILABILITY — this is the whole difficulty with sub-minute analysis:

  1-minute and coarser   GeckoTerminal OHLCV, free, ~1000 candles of history.
  Sub-minute (15s)       No free historical source exists. GeckoTerminal has
                         `timeframe=second` but it is Analyst-plan-only (401);
                         Birdeye needs a key; DexPaprika's floor is 1m and its
                         keyless quota is exhausted; Helius address-indexing on
                         these pools is ~2% signal (the rest is unrelated
                         Meteora/router traffic).

  So for 15s this builds bars from individual trades. GeckoTerminal's trades
  endpoint returns only the last 300 — on an active token that is ~3 minutes,
  less than the 21 bars (315s) an EMA21 needs to warm up. collect_trades.py
  polls that endpoint and accumulates them, which is how you get real 15s
  history: forward from when you start, not backward.
"""

import argparse
import json
import os
import time

import requests

GT = "https://api.geckoterminal.com/api/v2"
HDR = {"accept": "application/json"}


def best_pool(ca: str) -> dict:
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", timeout=25)
    r.raise_for_status()
    pairs = [p for p in (r.json().get("pairs") or [])
             if p.get("baseToken", {}).get("address") == ca]
    if not pairs:
        raise SystemExit(f"No DexScreener pair for {ca}")
    return max(pairs, key=lambda p: (p.get("liquidity", {}).get("usd") or 0))


def gt_ohlcv(pool: str, minutes_agg: int, limit: int) -> list:
    """[(ts, o,h,l,c, vol)] oldest-first, in USD."""
    r = requests.get(f"{GT}/networks/solana/pools/{pool}/ohlcv/minute",
                     params={"aggregate": minutes_agg, "limit": limit},
                     headers=HDR, timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"GeckoTerminal OHLCV {r.status_code}: {r.text[:200]}")
    rows = r.json()["data"]["attributes"]["ohlcv_list"]
    return sorted([tuple(x) for x in rows], key=lambda x: x[0])


def bars_from_ticks(path: str, mint_pool: str, bar_sec: int) -> list:
    """Build OHLC bars from accumulated trades (collect_trades.py output)."""
    ticks = []
    seen = set()
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("tx") in seen:
                continue
            seen.add(r.get("tx"))
            ticks.append((r["ts"], r["price"]))
    ticks.sort()
    if not ticks:
        return []
    out, cur = [], None
    for ts, px in ticks:
        b = (int(ts) // bar_sec) * bar_sec
        if cur is None:
            cur = [b, px, px, px, px, 1]
            continue
        if b == cur[0]:
            cur[2] = max(cur[2], px); cur[3] = min(cur[3], px); cur[4] = px; cur[5] += 1
            continue
        out.append(tuple(cur))
        for g in range(1, (b - cur[0]) // bar_sec):
            out.append((cur[0] + g * bar_sec, cur[4], cur[4], cur[4], cur[4], 0))
        cur = [b, px, px, px, px, 1]
    out.append(tuple(cur))
    return out


def emas(closes, fast, slow):
    af, as_ = 2 / (fast + 1), 2 / (slow + 1)
    f = s = None
    F, S = [], []
    for i, c in enumerate(closes):
        n = i + 1
        f = None if n < fast else (sum(closes[:fast]) / fast if n == fast else c * af + f * (1 - af))
        s = None if n < slow else (sum(closes[:slow]) / slow if n == slow else c * as_ + s * (1 - as_))
        F.append(f); S.append(s)
    return F, S


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ca")
    ap.add_argument("--bar-sec", type=int, default=60)
    ap.add_argument("--fast", type=int, default=12)
    ap.add_argument("--slow", type=int, default=21)
    ap.add_argument("--limit", type=int, default=300, help="candles (OHLCV mode)")
    ap.add_argument("--ticks", default=None, help="accumulated trades file for sub-minute bars")
    ap.add_argument("--cost-per-side", type=float, default=1.5)
    args = ap.parse_args()

    p = best_pool(args.ca)
    pool = p["pairAddress"]
    print(f"{p['baseToken']['symbol']} — {p['baseToken']['name']}")
    print(f"  {args.ca}")
    print(f"  pool {pool} ({p['dexId']})   liq ${(p.get('liquidity',{}).get('usd') or 0):,.0f}"
          f"   fdv ${(p.get('fdv') or 0):,.0f}")
    print(f"  price ${p.get('priceUsd')}   24h vol ${(p.get('volume',{}).get('h24') or 0):,.0f}")
    c = p.get("priceChange", {})
    print(f"  change  m5 {c.get('m5')}%   h1 {c.get('h1')}%   h6 {c.get('h6')}%   h24 {c.get('h24')}%")

    if args.bar_sec < 60:
        if not args.ticks or not os.path.exists(args.ticks):
            raise SystemExit(
                f"\n{args.bar_sec}s bars need trade-level data, which no free API serves "
                f"historically.\nRun the collector first, then pass --ticks:\n"
                f"  /opt/homebrew/bin/python3.14 collect_trades.py {args.ca} --minutes 25\n"
                f"  /opt/homebrew/bin/python3.14 analyze_token_ema.py {args.ca} "
                f"--bar-sec {args.bar_sec} --ticks gt_ticks_{args.ca[:8]}.jsonl")
        bars = bars_from_ticks(args.ticks, pool, args.bar_sec)
        unit, src = f"{args.bar_sec}s", f"accumulated trades ({args.ticks})"
    else:
        agg = args.bar_sec // 60
        rows = gt_ohlcv(pool, agg, args.limit)
        bars = [(t, o, h, l, cl, 1) for t, o, h, l, cl, _v in rows]
        unit, src = f"{args.bar_sec//60}m", "GeckoTerminal OHLCV"

    if len(bars) <= args.slow:
        raise SystemExit(f"\nOnly {len(bars)} bars — EMA{args.slow} needs more than {args.slow}.")

    closes = [b[4] for b in bars]
    F, S = emas(closes, args.fast, args.slow)
    traded = sum(1 for b in bars if b[5] > 0)
    span = (bars[-1][0] - bars[0][0]) / 60

    print(f"\nsource: {src}")
    print(f"{len(bars)} x {unit} bars over {span:.0f} min"
          + (f"  ({len(bars)-traded} empty/gap-filled)" if traded < len(bars) else ""))
    print(f"warmup: EMA{args.slow} = {args.slow} bars = {args.slow*args.bar_sec}s\n")

    crosses = []
    for i in range(1, len(bars)):
        pf, ps, cf, cs = F[i-1], S[i-1], F[i], S[i]
        if None in (pf, ps, cf, cs):
            continue
        if pf <= ps and cf > cs:
            crosses.append((i, bars[i][0], "golden", closes[i], (cf-cs)/cs*1e4))
        elif pf >= ps and cf < cs:
            crosses.append((i, bars[i][0], "death", closes[i], (cf-cs)/cs*1e4))

    def tf(x):
        return time.strftime("%H:%M:%S", time.localtime(x))

    print(f"=== EMA{args.fast}/{args.slow} CROSSES on {unit} bars — {len(crosses)} found ===\n")
    h = f"{'#':>3}  {'time':>8}  {'cross':<6}  {'price':>13}  {'edge':>7}   {'move to next':>16}"
    print(h); print("-" * len(h))
    for n, (i, t, d, px, e) in enumerate(crosses, 1):
        if n < len(crosses):
            nx = crosses[n]
            mv = (nx[3]-px)/px*100
            note = f"{mv:+7.2f}% / {nx[1]-t:>4}s"
        else:
            note = f"{(closes[-1]-px)/px*100:+7.2f}% (open)"
        print(f"{n:>3}  {tf(t):>8}  {'▲ UP' if d=='golden' else '▼ DOWN':<6}  "
              f"{px:>13.6g}  {e:>+6.0f}b   {note:>16}")

    cost = args.cost_per_side/100
    trades, entry = [], None
    for i, t, d, px, e in crosses:
        if d == "golden" and entry is None:
            entry = (t, px)
        elif d == "death" and entry:
            en, ex = entry[1]*(1+cost), px*(1-cost)
            trades.append(((t-entry[0]), (px-entry[1])/entry[1]*100, (ex-en)/en*100))
            entry = None

    print(f"\n=== LONG-ONLY ROUND TRIPS (up-cross → next down-cross, {args.cost_per_side:g}%/side) ===\n")
    if not trades:
        print("  none completed")
    else:
        for i, (held, g, n_) in enumerate(trades, 1):
            print(f"  {i:>2}.  held {held:>5}s   {g:+7.2f}% gross   {n_:+7.2f}% net")
        nets = [x[2] for x in trades]
        wins = [x for x in nets if x > 0]
        comp = 1.0
        for x in nets:
            comp *= (1 + x/100)
        gain = sum(wins); pain = abs(sum(x for x in nets if x <= 0))
        print(f"\n  {len(trades)} trades | {len(wins)} green ({len(wins)/len(trades)*100:.0f}%)")
        print(f"  mean {sum(nets)/len(nets):+.2f}%   median {sorted(nets)[len(nets)//2]:+.2f}%")
        print(f"  compounded {(comp-1)*100:+.1f}%   profit factor {gain/pain if pain else float('inf'):.2f}")
        if entry:
            print(f"  still open since {tf(entry[0])}: {(closes[-1]-entry[1])/entry[1]*100:+.2f}%")


if __name__ == "__main__":
    main()
