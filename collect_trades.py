"""
Accumulate individual trades for a Solana token so sub-minute candles can be
built from them.

GeckoTerminal's free trades endpoint returns only the LAST 300 trades. On an
active memecoin that is about three minutes — less than the 315s an EMA21 on
15s bars needs just to warm up. Polling it on a timer and de-duplicating by
tx_hash turns that rolling window into continuous history, forward from now.

  /opt/homebrew/bin/python3.14 collect_trades.py <CA> --minutes 25

Writes gt_ticks_<ca8>.jsonl ({ts, price, tx, kind, usd}), append-only and safe
to re-run; duplicates are dropped on load. Then:

  /opt/homebrew/bin/python3.14 analyze_token_ema.py <CA> --bar-sec 15 \
      --ticks gt_ticks_<ca8>.jsonl
"""

import argparse
import calendar
import json
import os
import sys
import time

import requests

GT = "https://api.geckoterminal.com/api/v2"
HDR = {"accept": "application/json"}
HERE = os.path.dirname(os.path.abspath(__file__))


def best_pool(ca: str):
    r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", timeout=25)
    r.raise_for_status()
    pairs = [p for p in (r.json().get("pairs") or [])
             if p.get("baseToken", {}).get("address") == ca]
    if not pairs:
        raise SystemExit(f"No DexScreener pair for {ca}")
    return max(pairs, key=lambda p: (p.get("liquidity", {}).get("usd") or 0))


def iso_to_epoch(s: str) -> float:
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def poll(pool: str, ca: str):
    """Last ~300 trades as {ts, price(SOL per token), tx, kind, usd}."""
    r = requests.get(f"{GT}/networks/solana/pools/{pool}/trades", headers=HDR, timeout=30)
    if r.status_code != 200:
        print(f"  poll HTTP {r.status_code}", file=sys.stderr)
        return []
    out = []
    for d in r.json().get("data", []):
        a = d["attributes"]
        # Orient the price to "SOL per token" regardless of trade direction.
        if a.get("from_token_address") == ca:
            px = a.get("price_from_in_currency_token")
        else:
            px = a.get("price_to_in_currency_token")
        if px is None:
            continue
        try:
            px = float(px)
        except (TypeError, ValueError):
            continue
        if px <= 0:
            continue
        out.append({"ts": iso_to_epoch(a["block_timestamp"]), "price": px,
                    "tx": a.get("tx_hash"), "kind": a.get("kind"),
                    "usd": a.get("volume_in_usd")})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ca")
    ap.add_argument("--minutes", type=float, default=25)
    ap.add_argument("--every", type=int, default=60, help="poll interval seconds")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    p = best_pool(args.ca)
    pool = p["pairAddress"]
    out = args.out or os.path.join(HERE, f"gt_ticks_{args.ca[:8]}.jsonl")

    seen = set()
    if os.path.exists(out):
        with open(out) as f:
            for line in f:
                try:
                    seen.add(json.loads(line)["tx"])
                except Exception:
                    pass
    print(f"{p['baseToken']['symbol']} pool {pool} ({p['dexId']})")
    print(f"collecting {args.minutes:g} min -> {out}  ({len(seen)} already on file)")

    deadline = time.time() + args.minutes * 60
    fh = open(out, "a", buffering=1)
    polls = 0
    try:
        while time.time() < deadline:
            new = 0
            for t in poll(pool, args.ca):
                if t["tx"] in seen:
                    continue
                seen.add(t["tx"])
                fh.write(json.dumps(t) + "\n")
                new += 1
            polls += 1
            left = max(deadline - time.time(), 0)
            print(f"  poll {polls}: +{new} new, {len(seen)} total, {left/60:.1f} min left")
            if left <= 0:
                break
            time.sleep(min(args.every, left))
    finally:
        fh.close()
    print(f"done: {len(seen)} unique trades -> {out}")


if __name__ == "__main__":
    main()
