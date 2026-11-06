#!/usr/bin/env python3
"""trader-tradingview: feed TradingView MCP bars to the trader-* tools.

Claude fetches bars with the TradingView connector (get_ohlcv). Big results are
saved to a file by Claude Code automatically; small ones Claude writes to a file
itself. This tool turns that file into what the other skills read:

    # US stocks: a Yahoo-format chart JSON for any tool's --offline flag
    python3 tv.py convert RESULT.txt --symbol TSLA --interval 5m --out trader/tv/TSLA.json
    python3 ../trader-entry/entry.py check TSLA --offline trader/tv/TSLA.json
    python3 ../trader-timemachine/timemachine.py run --offline trader/tv/TSLA.json

    # crypto: trader-crypto reads TradingView JSON directly, this just tidies it
    python3 tv.py convert RESULT.txt --symbol BTC --crypto --out trader/tv/BTC.json

    python3 tv.py info RESULT.txt          # what is in the file
    python3 tv.py symbol TSLA BTC BRK.B    # TradingView symbol to ask for

Stocks keep regular hours only (9:30 a.m. to 4:00 p.m. New York) unless you
pass --all-hours, and get previousClose from the prior session's last bar.

Standard library only. Python 3.8+.
"""
import argparse
import datetime as dt
import gzip
import json
import os
import sys

STEP = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "1D": 86400}
YAHOO_INTERVAL = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "4h": "4h", "1D": "1d"}
CRYPTO_QUOTES = ("USDT", "USDC", "USD")
KNOWN_CRYPTO = {"BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC", "BCH", "TRX",
                "SUI", "NEAR", "APT", "UNI", "ATOM", "XLM", "HBAR", "TON", "PEPE", "SHIB", "ARB", "OP"}


class TVError(Exception):
    pass


def read(path):
    if path == "-":
        return sys.stdin.read()
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def unwrap(doc):
    for _ in range(3):
        if isinstance(doc, list) and doc and isinstance(doc[0], dict) and "text" in doc[0] and "t" not in doc[0]:
            doc = json.loads(doc[0]["text"])
        elif isinstance(doc, dict) and "content" in doc and isinstance(doc["content"], list):
            doc = doc["content"]
        elif isinstance(doc, dict) and "result" in doc and "bars" not in doc:
            doc = doc["result"]
        else:
            break
    return doc


def load(path):
    try:
        doc = unwrap(json.loads(read(path)))
    except (OSError, ValueError) as e:
        raise TVError("could not read %s as JSON: %s" % (path, e))
    if isinstance(doc, dict) and doc.get("success") is False:
        raise TVError("the TradingView call failed: %s" % (doc.get("error") or doc))
    src = doc.get("bars") if isinstance(doc, dict) else doc
    if not isinstance(src, list) or not src:
        raise TVError("%s has no bars (want the get_ohlcv result with summary=false)" % path)
    bars, seen = [], set()
    for b in src:
        try:
            t = int(b["t"])
            t = t // 1000 if t > 10 ** 11 else t
            row = {"t": t, "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]), "c": float(b["c"]),
                   "v": float(b.get("v") or 0)}
        except (KeyError, TypeError, ValueError):
            continue
        if t not in seen:
            seen.add(t)
            bars.append(row)
    bars.sort(key=lambda b: b["t"])
    meta = {k: doc.get(k) for k in ("symbol", "interval")} if isinstance(doc, dict) else {}
    return meta, bars


# New York time without zoneinfo: US daylight time runs from the second Sunday of
# March, 2 a.m., to the first Sunday of November, 2 a.m.
def _nth_sunday(year, month, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def eastern(t):
    utc = dt.datetime(1970, 1, 1) + dt.timedelta(seconds=t)
    y = utc.year
    start = dt.datetime.combine(_nth_sunday(y, 3, 2), dt.time(7))   # 2 a.m. EST in UTC
    end = dt.datetime.combine(_nth_sunday(y, 11, 1), dt.time(6))    # 2 a.m. EDT in UTC
    return utc + dt.timedelta(hours=-4 if start <= utc < end else -5)


def regular_hours(bars, step):
    out = []
    for b in bars:
        et = eastern(b["t"])
        m = et.hour * 60 + et.minute
        if et.weekday() < 5 and 9 * 60 + 30 <= m and m + step // 60 <= 16 * 60:
            out.append(b)
    return out


def infer_step(bars):
    gaps = {}
    for a, b in zip(bars, bars[1:]):
        d = b["t"] - a["t"]
        if d > 0:
            gaps[d] = gaps.get(d, 0) + 1
    return max(gaps, key=gaps.get) if gaps else 0


def yahoo_ticker(sym, crypto):
    s = sym.strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    if crypto:
        for q in CRYPTO_QUOTES:
            if s.endswith(q) and len(s) > len(q) and "-" not in s:
                s = s[: -len(q)]
        return s.split("-")[0] + "-USD"
    return s.replace(".", "-")


def to_yahoo(bars, symbol, interval, prev_close=None, crypto=False):
    last = bars[-1]
    if prev_close is None and not crypto:
        today = eastern(last["t"]).date()
        before = [b for b in bars if eastern(b["t"]).date() < today]
        prev_close = before[-1]["c"] if before else None
    if prev_close is None and crypto:
        day_ago = [b for b in bars if b["t"] <= last["t"] - 86400]
        prev_close = day_ago[-1]["c"] if day_ago else None
    meta = {"symbol": symbol, "currency": "USD", "dataGranularity": YAHOO_INTERVAL[interval],
            "exchangeTimezoneName": "UTC" if crypto else "America/New_York",
            "instrumentType": "CRYPTOCURRENCY" if crypto else "EQUITY",
            "regularMarketTime": last["t"], "regularMarketPrice": last["c"],
            "regularMarketVolume": int(sum(b["v"] for b in bars if eastern(b["t"]).date() == eastern(last["t"]).date())),
            "previousClose": prev_close, "chartPreviousClose": prev_close,
            "source": "TradingView MCP via trader-tradingview"}
    return {"chart": {"result": [{"meta": meta, "timestamp": [b["t"] for b in bars],
                                  "indicators": {"quote": [{"open": [b["o"] for b in bars],
                                                            "high": [b["h"] for b in bars],
                                                            "low": [b["l"] for b in bars],
                                                            "close": [b["c"] for b in bars],
                                                            "volume": [int(b["v"]) for b in bars]}]}}],
                      "error": None}}


def cmd_convert(a):
    meta, bars = load(a.input)
    interval = a.interval or meta.get("interval")
    if interval not in STEP:
        raise TVError("give --interval (one of %s)" % ", ".join(STEP))
    step = infer_step(bars)
    if step and step != STEP[interval]:
        raise TVError("the bars are %ds apart, not %s" % (step, interval))
    crypto = a.crypto or yahoo_ticker(a.symbol, False).split("-")[0] in KNOWN_CRYPTO and not a.stock
    if not crypto and not a.all_hours and STEP[interval] < 86400:
        n = len(bars)
        bars = regular_hours(bars, STEP[interval])
        dropped = n - len(bars)
    else:
        dropped = 0
    if not bars:
        raise TVError("no bars left (regular hours only: pass --all-hours to keep the rest)")
    sym = yahoo_ticker(a.symbol, crypto)
    doc = {"symbol": a.symbol.upper(), "interval": interval, "bars": bars} if crypto and not a.yahoo \
        else to_yahoo(bars, sym, interval, a.prev_close, crypto)
    out = a.out or os.path.join("trader", "tv", "%s.json" % (base_name(sym) if crypto else sym))
    d = os.path.dirname(out)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    first, last = eastern(bars[0]["t"]), eastern(bars[-1]["t"])
    print("wrote %s: %d %s bars of %s, %s to %s %s%s" % (
        out, len(bars), interval, sym, (first if not crypto else utc(bars[0]["t"])).strftime("%Y-%m-%d %H:%M"),
        (last if not crypto else utc(bars[-1]["t"])).strftime("%Y-%m-%d %H:%M"), "UTC" if crypto else "New York",
        ", dropped %d extended-hours bars" % dropped if dropped else ""))
    sessions = len({eastern(b["t"]).date() for b in bars})
    if not crypto and interval == "5m" and sessions < 21:
        print("note: %d sessions; the time machine wants about 60 (ask get_ohlcv for count 5000)" % sessions)
    return 0


def base_name(sym):
    return sym.split("-")[0]


def utc(t):
    return dt.datetime(1970, 1, 1) + dt.timedelta(seconds=t)


def cmd_info(a):
    meta, bars = load(a.input)
    step = infer_step(bars)
    print("%s %s: %d bars, %ds apart, %s to %s UTC, last close %s" % (
        meta.get("symbol") or "?", meta.get("interval") or "?", len(bars), step,
        utc(bars[0]["t"]).strftime("%Y-%m-%d %H:%M"), utc(bars[-1]["t"]).strftime("%Y-%m-%d %H:%M"), bars[-1]["c"]))
    return 0


def cmd_symbol(a):
    for s in a.symbols:
        u = s.upper()
        if ":" in u:
            print("%-8s %s (already TradingView format)" % (s, u))
        elif u.split("-")[0].split("/")[0] in KNOWN_CRYPTO or u.endswith("-USD") or u.endswith("USDT"):
            b = yahoo_ticker(u, True).split("-")[0]
            print("%-8s BINANCE:%sUSDT  (or COINBASE:%sUSD)" % (s, b, b))
        else:
            print("%-8s NASDAQ:%s or NYSE:%s ... confirm with search_symbols(\"%s\")" % (
                s, u.replace("-", "."), u.replace("-", "."), u))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="TradingView MCP bars -> trader-* tools")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("convert", help="a saved get_ohlcv result -> a chart file")
    p.add_argument("input", help="the saved get_ohlcv result (or - for stdin)")
    p.add_argument("--symbol", required=True, help="ticker the tools will use: TSLA, BRK.B, BTC")
    p.add_argument("--interval", help="1m 5m 15m 30m 1h 4h 1D (default: from the file)")
    p.add_argument("--out")
    p.add_argument("--crypto", action="store_true", help="24/7 bars, keep every hour")
    p.add_argument("--stock", action="store_true", help="treat as a stock even if the name looks like a coin")
    p.add_argument("--yahoo", action="store_true", help="crypto: write Yahoo chart format too")
    p.add_argument("--all-hours", action="store_true", help="stocks: keep pre- and post-market bars")
    p.add_argument("--prev-close", type=float, help="override the previous close")
    p.set_defaults(fn=cmd_convert)
    p = sub.add_parser("info", help="what a saved result holds")
    p.add_argument("input")
    p.set_defaults(fn=cmd_info)
    p = sub.add_parser("symbol", help="TradingView symbols for tickers")
    p.add_argument("symbols", nargs="+")
    p.set_defaults(fn=cmd_symbol)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except TVError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
