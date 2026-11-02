"""
mos_paper.py — paper tests: does MadeOnSol data help find profitable memecoins?

Nothing here trades. It records decisions, follows prices afterwards, and
reports whether the MadeOnSol gates/signals separated winners from losers.

    python mos_paper.py track            # follow prices of every logged token (run 24/7)
    python mos_paper.py feeds [secs]     # track + paper-trade MadeOnSol's own feeds (run instead of track)
    python mos_paper.py report           # print the test results

Where the data comes from:
  * solana_sniper_bot.py → PowerAnalyzer.analyze() calls record_verdict() for
    every CA it judges — bought or skipped — with the MadeOnSol snapshot frozen
    at decision time and what the pre-MadeOnSol pipeline would have done.
  * `feeds` logs first sightings from /kol/coordination, /deployer-hunter/alerts
    and /kol/first-touches as their own sources (test 5).
  * `track` polls DexScreener (no key) every minute for 24h per token.

Tests in the report:
  1  coverage        how much data, how many MadeOnSol fields actually filled
  2  gates           did each MadeOnSol gate block rugs or winners?
  3  buckets         does risk / bundle / KOL consensus line up with returns?
  4  head-to-head    old pipeline vs new pipeline, simulated PnL with costs
  5  feeds           MadeOnSol feeds as entry signals, same simulator
"""

import asyncio
import json
import os
import sqlite3
import statistics
import sys
import time
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mos_paper.db")
MADEONSOL_BASE = "https://madeonsol.com/api/v1"
DEXSCREENER_TOKENS = "https://api.dexscreener.com/latest/dex/tokens/"

TRACK_SEC = 24 * 3600          # follow each token for 24h
POLL_SEC = 60
HORIZONS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "24h": 86400}
RUG_LIQ_DROP = 0.8             # liquidity down 80%+ from entry (or pair gone) = rug

# Simulator: one fixed, deliberately simple exit rule applied identically to every
# source so the comparison is about entries, not exits.
SIM_TP = 1.00                  # +100% take profit
SIM_SL = -0.40                 # -40% stop
SIM_MAX_HOLD = 4 * 3600        # time stop
SIM_COST = 0.03                # per side: slippage + fees + priority fee, as a fraction


# ----------------------------- storage --------------------------------------

def _db():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("""CREATE TABLE IF NOT EXISTS entries(
        id INTEGER PRIMARY KEY, source TEXT, mint TEXT, ts REAL,
        kol TEXT, tweet_id TEXT,
        price0 REAL, liq0 REAL, mcap0 REAL,
        new_buy INTEGER, new_size REAL, new_score REAL,
        legacy_buy INTEGER, legacy_size REAL, legacy_score REAL,
        gates TEXT, legacy_gates TEXT, breakdown TEXT, mos TEXT, meta TEXT,
        done INTEGER DEFAULT 0,
        UNIQUE(source, mint))""")
    db.execute("""CREATE TABLE IF NOT EXISTS prices(
        entry_id INTEGER, ts REAL, price REAL, liq REAL)""")
    db.execute("CREATE INDEX IF NOT EXISTS prices_entry ON prices(entry_id, ts)")
    db.commit()
    return db


def record_verdict(v, kol_handle: str = "", tweet_id: str = "", source: str = "kol_tweet"):
    """Called by PowerAnalyzer.analyze() for every verdict. Must stay cheap and never raise."""
    db = _db()
    try:
        e, lg = v.entry or {}, v.legacy or {}
        db.execute("""INSERT OR IGNORE INTO entries(source, mint, ts, kol, tweet_id,
            price0, liq0, mcap0, new_buy, new_size, new_score,
            legacy_buy, legacy_size, legacy_score, gates, legacy_gates, breakdown, mos, meta)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (source, v.mint, time.time(), kol_handle, tweet_id,
             e.get("price"), e.get("liq"), e.get("mcap"),
             int(v.buy), v.size_sol, v.score,
             int(bool(lg.get("buy"))), lg.get("size", 0.0), lg.get("score", 0.0),
             json.dumps(v.gate_failures), json.dumps(lg.get("gates", [])),
             json.dumps(v.breakdown), json.dumps(v.mos or {}), json.dumps({"notes": v.notes})))
        db.commit()
    finally:
        db.close()


def _record_feed(db, source, mint, price, liq, mcap, meta):
    cur = db.execute("""INSERT OR IGNORE INTO entries(source, mint, ts, price0, liq0, mcap0,
        new_buy, new_size, legacy_buy, legacy_size, meta)
        VALUES(?,?,?,?,?,?,1,0.25,0,0,?)""",
        (source, mint, time.time(), price, liq, mcap, json.dumps(meta)))
    return cur.rowcount


# ----------------------------- prices ---------------------------------------

async def _dex_prices(s, mints):
    """{mint: (price_usd, liquidity_usd, mcap)} from DexScreener, deepest pool per mint."""
    import aiohttp
    out = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i:i + 30]
        try:
            async with s.get(DEXSCREENER_TOKENS + ",".join(chunk),
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                d = await r.json()
        except Exception:
            continue
        for p in d.get("pairs") or []:
            if (p.get("chainId") or "solana") != "solana":
                continue
            m = (p.get("baseToken") or {}).get("address")
            liq = (p.get("liquidity") or {}).get("usd", 0) or 0
            if m in chunk and (m not in out or liq > out[m][1]):
                out[m] = (float(p.get("priceUsd") or 0) or None, liq,
                          p.get("marketCap") or p.get("fdv") or 0)
    return out


async def track_loop():
    import aiohttp
    print(f"tracking prices → {DB_PATH} (every {POLL_SEC}s, {TRACK_SEC // 3600}h per token)")
    async with aiohttp.ClientSession() as s:
        while True:
            db = _db()
            try:
                now = time.time()
                db.execute("UPDATE entries SET done=1 WHERE done=0 AND ts < ?", (now - TRACK_SEC,))
                rows = db.execute("SELECT id, mint, price0 FROM entries WHERE done=0").fetchall()
                prices = await _dex_prices(s, sorted({r[1] for r in rows})) if rows else {}
                for eid, mint, price0 in rows:
                    price, liq, mcap = prices.get(mint, (None, 0, 0))
                    if price0 is None and price:
                        # entry logged without a price (no pair yet) — first seen price is the entry
                        db.execute("UPDATE entries SET price0=?, liq0=?, mcap0=? WHERE id=?",
                                   (price, liq, mcap, eid))
                    db.execute("INSERT INTO prices VALUES(?,?,?,?)", (eid, now, price, liq))
                db.commit()
                if rows:
                    print(time.strftime("%H:%M:%S"), f"tracked {len(rows)} tokens, {len(prices)} priced")
            finally:
                db.close()
            await asyncio.sleep(POLL_SEC)


# ----------------------------- MadeOnSol feeds (test 5) ---------------------

async def feeds_loop(every_sec: int):
    import aiohttp
    key = os.getenv("MADEONSOL_API_KEY")
    if not key:
        sys.exit("MADEONSOL_API_KEY not set (.env)")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    feeds = {
        "mos:coordination":  ("/kol/coordination", {"period": "1h", "min_kols": 3, "limit": 50},
                              "coordination"),
        "mos:deployer":      ("/deployer-hunter/alerts", {"limit": 50}, "alerts"),
        "mos:first_touch":   ("/kol/first-touches", {"preset": "scout", "limit": 50}, "events"),
    }
    print(f"polling {len(feeds)} MadeOnSol feeds every {every_sec}s "
          f"(~{len(feeds) * 86400 // every_sec} requests/day of your quota)")
    async with aiohttp.ClientSession() as s:
        while True:
            for source, (path, params, key_name) in feeds.items():
                try:
                    async with s.get(MADEONSOL_BASE + path, params=params, headers=headers,
                                     timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status != 200:
                            print(source, "HTTP", r.status, (await r.text())[:120])
                            continue
                        d = await r.json()
                except Exception as e:
                    print(source, "error", e)
                    continue
                items = d.get(key_name) or []
                if source == "mos:coordination":
                    # only clusters still accumulating; distributing = KOLs already leaving
                    items = [x for x in items if x.get("signal") == "accumulating"]
                mints = [x["token_mint"] for x in items if x.get("token_mint")]
                prices = await _dex_prices(s, mints) if mints else {}
                db = _db()
                new = 0
                # skip signals older than one poll interval (+ free-tier delay): entering
                # them now at today's price would test a stale signal, not the feed
                max_age = max(every_sec, 900) + (d.get("delay_seconds") or 0)
                try:
                    for x in items:
                        m = x.get("token_mint")
                        stamp = x.get("last_buy_at") or x.get("created_at") or x.get("first_buy_at")
                        if not m or _age_sec(stamp) > max_age:
                            continue
                        price, liq, mcap = prices.get(m, (None, 0, 0))
                        meta = {k: x.get(k) for k in ("kol_count", "net_sol_flow", "coordination_score",
                                                       "avg_winrate_7d", "created_at", "first_buy_at",
                                                       "market_cap_at_alert", "market_cap_usd_at_first_buy",
                                                       "token_age_minutes", "priority")
                                if x.get(k) is not None}
                        if isinstance(x.get("first_kol"), dict):
                            meta["scout_tier"] = x["first_kol"].get("scout_tier")
                        if isinstance(x.get("deployers"), dict):
                            meta["deployer_tier"] = x["deployers"].get("tier")
                        meta["feed_delay_sec"] = d.get("delay_seconds")
                        new += _record_feed(db, source, m, price, liq, mcap, meta)
                    db.commit()
                finally:
                    db.close()
                print(time.strftime("%H:%M:%S"), source, f"{len(items)} items, {new} new")
            await asyncio.sleep(every_sec)


def _age_sec(iso: Optional[str]) -> float:
    from datetime import datetime
    try:
        return time.time() - datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return float("inf")


# ----------------------------- outcomes -------------------------------------

def _outcome(db, eid, ts0, price0, liq0):
    """Forward returns + simulated trade for one entry, from its recorded price path."""
    path = db.execute("SELECT ts, price, liq FROM prices WHERE entry_id=? ORDER BY ts",
                      (eid,)).fetchall()
    if not price0 or not path:
        return None
    o = {"age_h": (path[-1][0] - ts0) / 3600}
    for name, sec in HORIZONS.items():
        pt = next((p for t, p, _ in path if t - ts0 >= sec and p), None)
        o[name] = (pt / price0 - 1) if pt else None
    priced = [(t, p, l) for t, p, l in path if p]
    o["peak"] = max(p for _, p, _ in priced) / price0 - 1 if priced else None
    o["trough"] = min(p for _, p, _ in priced) / price0 - 1 if priced else None
    # rug: pool vanished or liquidity collapsed
    gone = sum(1 for _, p, _ in path[-5:] if p is None) >= 5
    min_liq = min((l for _, p, l in path if p), default=0)
    o["rug"] = gone or (liq0 and min_liq < liq0 * (1 - RUG_LIQ_DROP))
    # which came first: 2x or -50%
    o["hit2x"] = False
    for _, p, _ in priced:
        r = p / price0 - 1
        if r <= -0.5:
            break
        if r >= 1.0:
            o["hit2x"] = True
            break
    # simulated trade: buy at price0*(1+cost), exit on TP/SL/time, sell at price*(1-cost)
    buy = price0 * (1 + SIM_COST)
    exit_px = None
    for t, p, _ in path:
        if p is None:
            if gone:
                exit_px = 0.0  # pool gone — total loss
                break
            continue
        r = p / buy - 1
        if r >= SIM_TP or r <= SIM_SL or t - ts0 >= SIM_MAX_HOLD:
            exit_px = p
            break
    if exit_px is None:
        if o["age_h"] * 3600 < SIM_MAX_HOLD:
            o["sim"] = None    # trade still open
        else:
            exit_px = priced[-1][1] if priced else 0.0
    if exit_px is not None:
        o["sim"] = exit_px * (1 - SIM_COST) / buy - 1
    return o


# ----------------------------- report ---------------------------------------

def _pct(x):
    return "   —  " if x is None else f"{x * 100:+6.0f}%"


def _stats(rows):
    """rows: list of outcome dicts → summary line pieces."""
    n = len(rows)
    if not n:
        return "n=0"
    r1h = [o["1h"] for o in rows if o.get("1h") is not None]
    sims = [o["sim"] for o in rows if o.get("sim") is not None]
    rug = sum(1 for o in rows if o["rug"]) / n
    x2 = sum(1 for o in rows if o["hit2x"]) / n
    med = statistics.median(r1h) if r1h else None
    win = (sum(1 for x in sims if x > 0) / len(sims)) if sims else None
    avg = statistics.mean(sims) if sims else None
    return (f"n={n:<4} rug {rug * 100:4.0f}%  2x-first {x2 * 100:4.0f}%  med1h {_pct(med)}  "
            f"sim n={len(sims):<3} win {('  — ' if win is None else f'{win * 100:3.0f}%')}  "
            f"avg/trade {_pct(avg)}")


def report():
    db = _db()
    rows = db.execute("""SELECT id, source, mint, ts, price0, liq0, new_buy, new_size, legacy_buy,
        legacy_size, gates, legacy_gates, mos, meta FROM entries ORDER BY ts""").fetchall()
    data = []
    for (eid, src, mint, ts, p0, l0, nb, ns, lb, ls, gates, lgates, mos, meta) in rows:
        o = _outcome(db, eid, ts, p0, l0)
        data.append({"src": src, "mint": mint, "ts": ts, "new_buy": nb, "new_size": ns,
                     "legacy_buy": lb, "legacy_size": ls,
                     "gates": json.loads(gates or "[]"), "legacy_gates": json.loads(lgates or "[]"),
                     "mos": json.loads(mos or "{}"), "o": o})
    db.close()

    tw = [d for d in data if d["src"] == "kol_tweet"]
    done = [d for d in tw if d["o"] and d["o"]["age_h"] >= 1]

    print("=" * 96)
    print("1. COVERAGE")
    print(f"   KOL-tweet verdicts: {len(tw)}   with ≥1h of price data: {len(done)}")
    for f in ("risk_score", "dev_dumped", "bundle_pct", "kol_buyers"):
        k = sum(1 for d in tw if d["mos"].get(f) is not None)
        print(f"   MadeOnSol {f:<11} filled on {k}/{len(tw)}")
    if len(done) < 30:
        print("   ⚠ fewer than 30 finished tokens — numbers below are noise, not evidence yet")

    print("\n2. GATES — blocked vs passed (want: blocked = mostly rugs, few 2x)")
    gate_defs = {
        "MadeOnSol risk":   lambda d: any(g.startswith("MadeOnSol risk") for g in d["gates"]),
        "deployer dumped":  lambda d: any(g.startswith("deployer ") for g in d["gates"]),
        "bundle (MOS)":     lambda d: any(g.startswith("bundle bought") for g in d["gates"])
                                      and d["mos"].get("bundle_pct") is not None,
    }
    for name, hit in gate_defs.items():
        blocked = [d["o"] for d in done if hit(d)]
        passed = [d["o"] for d in done if not hit(d)]
        print(f"   {name:<16} blocked: {_stats(blocked)}")
        print(f"   {'':<16} passed:  {_stats(passed)}")

    print("\n3. BUCKETS — does the signal line up with outcomes? (want: a steady trend)")
    buckets = {
        "risk_score":   [(0, 30), (30, 50), (50, 70), (70, 101)],
        "bundle_pct":   [(0, 5), (5, 15), (15, 25), (25, 101)],
        "kol_buyers":   [(0, 1), (1, 2), (2, 4), (4, 10_000)],
    }
    for f, ranges in buckets.items():
        print(f"   {f}")
        for lo, hi in ranges:
            sel = [d["o"] for d in done
                   if d["mos"].get(f) is not None and lo <= d["mos"][f] < hi]
            print(f"     {lo:>4}–{hi if hi < 101 else '∞':<5} {_stats(sel)}")
    for label, cond in (("net KOL buying", lambda m: (m.get("kol_net_flow_sol") or 0) > 0),
                        ("net KOL selling", lambda m: "kol_buyers" in m
                         and (m.get("kol_net_flow_sol") or 0) <= 0)):
        print(f"   {label:<16} {_stats([d['o'] for d in done if cond(d['mos'])])}")

    print("\n4. HEAD-TO-HEAD — tokens each pipeline would buy, simulated "
          f"(TP {SIM_TP:+.0%}, SL {SIM_SL:+.0%}, {SIM_MAX_HOLD // 3600}h max, {SIM_COST:.0%}/side)")
    for label, key in (("old (no MOS)", "legacy_buy"), ("new (MOS)", "new_buy")):
        sel = [d for d in done if d[key]]
        print(f"   {label:<13} {_stats([d['o'] for d in sel])}  {_pnl(sel, label.startswith('new'))}")
    only_old = [d["o"] for d in done if d["legacy_buy"] and not d["new_buy"]]
    only_new = [d["o"] for d in done if d["new_buy"] and not d["legacy_buy"]]
    print(f"   MOS removed   {_stats(only_old)}   ← want these to be losers")
    print(f"   MOS added     {_stats(only_new)}")
    if len(done) >= 20:
        cut = int(len(done) * 0.7)
        for label, part in (("first 70%", done[:cut]), ("last 30%", done[cut:])):
            print(f"   {label:<10} old {_stats([d['o'] for d in part if d['legacy_buy']])}")
            print(f"   {'':<10} new {_stats([d['o'] for d in part if d['new_buy']])}")

    print("\n5. SOURCES — each feed as an entry signal, same simulator")
    for src in sorted({d["src"] for d in data}):
        sel = [d["o"] for d in data if d["src"] == src and d["o"] and d["o"]["age_h"] >= 1]
        if src == "kol_tweet":
            print(f"   {'kol_tweet (all)':<20} {_stats(sel)}")
            sel = [d["o"] for d in done if d["new_buy"]]
            src = "kol_tweet (new buys)"
        print(f"   {src:<20} {_stats(sel)}")
    print("=" * 96)


def _pnl(sel, new):
    pnl = 0.0
    for d in sel:
        sim = d["o"].get("sim")
        if sim is not None:
            pnl += sim * (d["new_size"] if new else d["legacy_size"])
    return f"PnL {pnl:+.2f} SOL"


# ----------------------------- cli ------------------------------------------

def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    except ImportError:
        pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "track":
        asyncio.run(track_loop())
    elif cmd == "feeds":
        every = int(sys.argv[2]) if len(sys.argv) > 2 else 3600

        async def both():
            await asyncio.gather(track_loop(), feeds_loop(every))
        asyncio.run(both())
    elif cmd == "report":
        report()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
