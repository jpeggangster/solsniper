#!/usr/bin/env python3
"""
Resolve AIXBT trending tokens -> the TradingView symbol with the deepest usable chart.

Input  (stdin) : JSON list of {"name","symbol","trend","assets":[{"chain","address"}]}
Output (stdout): {"picks":[...], "skipped":[...]}; progress table on stderr.

Goal is the chart with the MOST DATA, but "most data" alone is a trap: tickers
collide across chains. AIXBT's "Buy The Cat" has ticker BTC, so the deepest BTC
chart is Bitcoin's 2013 one; a 2026 Solana memecoin named STONK collides with a
dormant 2020 Ethereum STONK. Both would be silently wrong.

So each token is anchored to its real on-chain birth (DexScreener, by contract
address). For a token born within the last year, any chart starting materially
before that birth belongs to a different asset and is discarded. For older
tokens the on-chain age is only a lower bound (pools migrate), so no hard clamp.

Remaining candidates are then required to be usable charts -- USD-ish quote and
real volume -- and the deepest survivor wins.
"""
import json, os, re, sys, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from validate_symbols import resolves

SCANNER = "https://scanner.tradingview.com/crypto/scan?label-product=popup-screener-crypto"
DEXSCREENER = "https://api.dexscreener.com/latest/dex/tokens/"
COLS = ["base_currency", "exchange", "centralization", "currency_id",
        "blockchain-id", "first_bar_time", "24h_vol|5"]

DAY = 86400
NEW_TOKEN_AGE = 90 * DAY     # only genuinely fresh tokens get the collision clamp
CLAMP_GRACE = 45 * DAY       # a chart may predate on-chain birth by this much (CEX pre-listing)
MIN_CEX_VOL_USD = 200_000
MIN_DEPTH_DAYS = 3           # a chart younger than this is not worth watching
SCAN_LIMIT = 1000            # must exceed the busiest ticker's listing count
# A real spot/perp line, not a dated future or option (DERIBIT:BTCUSD18U2026).
CEX_TICKER_RE = r"^{t}(USDT|USDC|USD)(\.P)?$"

# A DEX pool line is BASE+QUOTE with the pool-address suffix already stripped;
# the quote is the chain's gas/stable token, not a USD pair.
DEX_PAIR_RE = r"^([A-Z0-9]+?)(WETH|WBNB|WAVAX|WBTC|WSOL|WPOL|ETH|SOL|BNB|AVAX|USDT|USDC|USD)$"

# Usable quote currencies: a deep BTCMXN chart is still a bad chart.
GOOD_QUOTES = {"USD", "USDT", "USDC", "XTVCUSDT", "XTVCUSDC", "XTVCUSD"}

# Explicit venue order. The watchlist this feeds is overwhelmingly BINANCE spot,
# so a pick that matches that convention sits alongside what is already there
# instead of reading as a stray duplicate line.
VENUE_ORDER = ["BINANCE", "COINBASE", "BYBIT", "OKX", "KRAKEN", "BITSTAMP", "UPBIT",
               "KUCOIN", "BITGET", "GATE", "MEXC", "HTX", "BINGX", "CRYPTOCOM",
               "BITFINEX"]

CHAIN_MAP = {
    "ethereum": "ethereum", "solana": "solana", "base": "base",
    "arbitrum-one": "arbitrum", "arbitrum-nova": "arbitrum",
    "optimistic-ethereum": "optimism", "polygon-pos": "polygon",
    "binance-smart-chain": "bsc", "avalanche": "avalanche", "unichain": "unichain",
    "sui": "sui", "hyperevm": "hyperevm", "abstract": "abstract",
    "berachain": "berachain", "blast": "blast", "linea": "linea",
    "scroll": "scroll", "sonic": "sonic", "mantle": "mantle", "zksync": "zksync",
    "celo": "celo", "cronos": "cronos", "fantom": "fantom", "monad": "monad",
    "ink": "ink", "world-chain": "world-chain", "apechain": "apechain",
    "ronin": "ronin", "xdai": "gnosis", "near-protocol": "near",
    "katana": "katana", "plume-network": "plume", "soneium": "soneium",
    "taiko": "taiko", "zora-network": "zora",
}
# DexScreener chain slugs differ again from AIXBT's.
DS_CHAIN = {"ethereum": "ethereum", "solana": "solana", "base": "base",
            "arbitrum-one": "arbitrum", "binance-smart-chain": "bsc",
            "polygon-pos": "polygon", "optimistic-ethereum": "optimism",
            "avalanche": "avalanche", "sui": "sui", "unichain": "unichain"}


def _get(url, data=None, retries=3, timeout=30):
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            if i == retries - 1:
                print(f"    ! request failed {url[:60]}: {e}", file=sys.stderr)
                return None
            time.sleep(2 * (i + 1))
    return None


def onchain_birth(assets, ticker):
    """Earliest pool creation across the token's contract addresses (epoch secs).

    Only pools whose baseToken symbol matches `ticker` count. AIXBT sometimes
    lists a bridged representation -- Bittensor's "ethereum" address resolves to
    pools symboled NIL -- and trusting that would date a 2023 token to 2025 and
    then discard its real charts as impostors.
    """
    earliest = None
    for a in (assets or [])[:4]:
        addr = a.get("address")
        if not addr or not addr.startswith(("0x", "1", "2", "3", "4", "5", "6",
                                            "7", "8", "9", "A", "B", "C", "D",
                                            "E", "F", "G", "H", "J", "K", "L",
                                            "M", "N", "P", "Q", "R", "S", "T",
                                            "U", "V", "W", "X", "Y", "Z")):
            continue
        d = _get(DEXSCREENER + addr, retries=2, timeout=20)
        for p in (d or {}).get("pairs") or []:
            sym = ((p.get("baseToken") or {}).get("symbol") or "").upper()
            if sym != ticker.upper():
                continue
            ts = p.get("pairCreatedAt")
            if ts:
                ts //= 1000
                earliest = ts if earliest is None else min(earliest, ts)
        time.sleep(0.25)
    return earliest


def scan(ticker):
    body = json.dumps({
        "columns": COLS,
        "filter2": {"operator": "and", "operands": [
            {"expression": {"left": "base_currency", "operation": "equal", "right": ticker}}]},
        "range": [0, SCAN_LIMIT], "markets": ["crypto"],
    }).encode()
    j = _get(SCANNER, data=body) or {}
    rows = j.get("data") or []
    total = j.get("totalCount")
    # A truncated scan silently loses the deepest line: SHIB has 126 listings and
    # at range 100 the 2021 BINANCE:SHIBUSDT fell off, handing the pick to a 2024
    # SHIBUSDC. Shout rather than quietly return the wrong "deepest" chart.
    if total and total > len(rows):
        print(f"    ! scan truncated for {ticker}: {len(rows)}/{total}", file=sys.stderr)
    return rows


def venue_rank(row):
    if row["centralization"] != "cex":
        return len(VENUE_ORDER) + 1          # a DEX pool only when no CEX exists
    ex = (row.get("exchange") or "").upper()
    return VENUE_ORDER.index(ex) if ex in VENUE_ORDER else len(VENUE_ORDER)


def is_perp(row):
    return row["symbol"].endswith(".P")


def resolve(tok):
    ticker = (tok.get("symbol") or "").strip().upper()
    if not ticker:
        return None, "no ticker (project has no tradeable token)"

    assets = tok.get("assets") or []
    tv_chains = {CHAIN_MAP.get(a.get("chain"), a.get("chain")) for a in assets}
    birth = onchain_birth(assets, ticker)
    now = time.time()
    clamp = birth - CLAMP_GRACE if (birth and now - birth < NEW_TOKEN_AGE) else None

    rows = [dict(zip(COLS, r["d"]), symbol=r["s"]) for r in scan(ticker)]
    rows = [r for r in rows if r.get("first_bar_time")]
    if not rows:
        return None, "no TradingView listings with chart data"

    pat = re.compile(CEX_TICKER_RE.format(t=re.escape(ticker)))
    kept, why = [], {}
    for r in rows:
        line = r["symbol"].split(":", 1)[-1]
        if r["centralization"] == "dex":
            if tv_chains and r.get("blockchain-id") not in tv_chains:
                why[r["symbol"]] = "wrong chain"; continue
            # DEX pools quote in the chain's gas token; TradingView's ".USD"
            # variant is the one worth charting.
            if not line.endswith(".USD") and r.get("currency_id") not in GOOD_QUOTES:
                why[r["symbol"]] = f'quote {r.get("currency_id")}'; continue
        else:
            if (r.get("24h_vol|5") or 0) < MIN_CEX_VOL_USD:
                why[r["symbol"]] = "illiquid"; continue
            if not pat.match(line):
                why[r["symbol"]] = "dated/exotic contract"; continue
        if clamp and r["first_bar_time"] < clamp:
            why[r["symbol"]] = "predates token (ticker collision)"; continue
        if (now - r["first_bar_time"]) < MIN_DEPTH_DAYS * DAY:
            why[r["symbol"]] = "chart too new"; continue
        kept.append(r)

    if not kept:
        sample = list(why.items())[:3]
        return None, f"no usable chart among {len(rows)} listings ({sample})"

    # Reputable venue, then spot over perp, then the deepest chart. Sorting on
    # depth alone picks oddities (POLONIEX over BINANCE) for a few extra bars.
    kept.sort(key=lambda r: (venue_rank(r), is_perp(r),
                             -(now - r["first_bar_time"]), -(r.get("24h_vol|5") or 0)))

    # Being in the scanner does not mean TradingView can chart it -- those are the
    # symbols that show red in the watchlist with no data. Walk down the ranking
    # to the next-best candidate that actually resolves.
    best, rejected = None, []
    for cand in kept[:12]:
        if resolves(cand["symbol"]):
            best = cand
            break
        rejected.append(cand["symbol"])
    if best is None:
        return None, (f"no resolvable chart among {len(kept)} candidates"
                      f" (unresolvable: {rejected[:3]})")
    best["_skipped_dead"] = rejected
    best["_n"], best["_kept"], best["_birth"], best["_clamped"] = len(rows), len(kept), birth, bool(clamp)
    # AIXBT listed contract addresses but DexScreener could not confirm a pool
    # under this ticker, so the collision clamp never ran. The pick may belong to
    # an older token sharing the ticker (LIT is both Lighter and Litentry).
    best["_unverified"] = bool(assets) and birth is None
    return best, None


def existing_tickers(path):
    """Base tickers already on the watchlist, so we add tokens not duplicates.

    A list holding BINANCE:TAOUSDT does not need BYBIT:TAOUSDT.P as well.
    """
    try:
        raw = json.load(open(path))
    except Exception:
        return set(), set()
    lines = {str(s).upper() for s in raw if not str(s).startswith("###")}
    bases = set()
    for line in lines:
        tail = line.split(":", 1)[-1].split(".")[0]
        t = tail.split("_")[0]
        # DEX pool lines (UNISWAPBASE:CRCLWETH_15C77D.USD) quote in the chain's
        # gas token, so the USD-quote strip below never fires and the token reads
        # as untracked -- which is how a second, shallower CRCL chart got picked.
        if "_" in tail:
            m = re.match(DEX_PAIR_RE, t)
            if m:
                bases.add(m.group(1)); continue
        for q in ("USDT", "USDC", "USD"):
            if t.endswith(q) and len(t) > len(q):
                bases.add(t[: -len(q)]); break
    return lines, bases


def main():
    tokens = json.load(sys.stdin)
    have_lines, have_bases = existing_tickers(sys.argv[1]) if len(sys.argv) > 1 else (set(), set())
    picks, skips = [], []
    for tok in tokens:
        label = f'{tok.get("name")} ({tok.get("symbol") or "-"})'
        tk = (tok.get("symbol") or "").upper()
        if tk and tk in have_bases:
            skips.append({"token": label, "reason": "already on watchlist"})
            print(f"HAVE  {label:30} already tracked", file=sys.stderr)
            continue
        best, err = resolve(tok)
        if err:
            skips.append({"token": label, "reason": err})
            print(f"SKIP  {label:30} {err}", file=sys.stderr)
            continue
        if best["symbol"].upper() in have_lines:
            skips.append({"token": label, "reason": "already on watchlist"})
            print(f"HAVE  {label:30} {best['symbol']}", file=sys.stderr)
            continue
        days = int((time.time() - best["first_bar_time"]) / DAY)
        picks.append({
            "token": tok.get("name"), "ticker": tok.get("symbol"), "trend": tok.get("trend"),
            "symbol": best["symbol"], "venue": best["exchange"], "type": best["centralization"],
            "chain": best.get("blockchain-id"),
            "first_bar": time.strftime("%Y-%m-%d", time.gmtime(best["first_bar_time"])),
            "days_of_data": days, "vol_24h_usd": best.get("24h_vol|5"),
            "onchain_birth": (time.strftime("%Y-%m-%d", time.gmtime(best["_birth"]))
                              if best["_birth"] else None),
            "collision_clamp": best["_clamped"],
            "collision_risk": best["_unverified"],
            "skipped_unresolvable": best.get("_skipped_dead") or [],
            "candidates": best["_n"], "usable": best["_kept"],
        })
        print(f'PICK  {label:30} {best["symbol"]:38} {best["centralization"]:3} '
              f'{days:5}d  ({best["_kept"]}/{best["_n"]} usable'
              f'{", clamped" if best["_clamped"] else ""}'
              f'{", UNVERIFIED ticker" if best["_unverified"] else ""}'
              f'{", skipped %d dead" % len(best["_skipped_dead"]) if best["_skipped_dead"] else ""})',
              file=sys.stderr)
        time.sleep(0.3)
    json.dump({"picks": picks, "skipped": skips}, sys.stdout, indent=2)


if __name__ == "__main__":
    main()
