#!/usr/bin/env python3
"""
Check whether TradingView can actually resolve a symbol.

A symbol can sit in the scanner and still be unresolvable on a chart -- it shows
red in the watchlist with no data. The usual cause is a spot line for a token
that only trades as a perp on that venue: Binance has VVVUSDT.P but no VVVUSDT.

Usage:  validate_symbols.py SYM [SYM ...]      # or one per line on stdin
Output: JSON {"valid":[...], "invalid":[...]}, readable table on stderr.
"""
import json, sys, time, urllib.parse, urllib.request

SEARCH = "https://symbol-search.tradingview.com/symbol_search/v3/"
HDRS = {"User-Agent": "Mozilla/5.0", "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/"}
_cache = {}


def _strip(s):
    return (s or "").replace("<em>", "").replace("</em>", "").upper()


def resolves(symbol, retries=3):
    """True if TradingView lists this exact exchange:ticker pair."""
    symbol = symbol.upper()
    if symbol in _cache:
        return _cache[symbol]
    exchange, _, ticker = symbol.partition(":")
    if not ticker:
        _cache[symbol] = False
        return False
    qs = urllib.parse.urlencode({"text": ticker, "exchange": exchange,
                                 "hl": "false", "lang": "en", "domain": "production"})
    ok = None
    for i in range(retries):
        try:
            req = urllib.request.Request(SEARCH + "?" + qs, headers=HDRS)
            with urllib.request.urlopen(req, timeout=20) as r:
                found = json.load(r).get("symbols") or []
            ok = any(_strip(s.get("symbol")) == ticker for s in found)
            break
        except Exception as e:
            if i == retries - 1:
                print(f"  ! validate failed {symbol}: {e}", file=sys.stderr)
                ok = None                      # unknown, not proven invalid
            else:
                time.sleep(1.5 * (i + 1))
    _cache[symbol] = ok
    return ok


def main():
    syms = sys.argv[1:] or [l.strip() for l in sys.stdin if l.strip()]
    syms = [s for s in syms if not s.startswith("###")]
    valid, invalid, unknown = [], [], []
    for s in syms:
        r = resolves(s)
        (valid if r else (unknown if r is None else invalid)).append(s)
        print(f'{"OK  " if r else ("?   " if r is None else "DEAD")} {s}', file=sys.stderr)
        time.sleep(0.2)
    json.dump({"valid": valid, "invalid": invalid, "unknown": unknown},
              sys.stdout, indent=2)


if __name__ == "__main__":
    main()
