#!/usr/bin/env python3
"""trader-crypto: 24/7 crypto day-trade analysis. ANALYSIS ONLY.

Nothing here builds, sends or places an order. trader-risk blocks crypto by
design and trader-broker never sees anything this tool writes.

    python3 crypto.py scan                                  # the default top-20 coins, 1h bars
    python3 crypto.py scan --symbols BTC,ETH,SOL --interval 1h
    python3 crypto.py entry BTC                             # opening-range breakout from 00:00 UTC
    python3 crypto.py entry SOL --session-open 13:30        # range from the US stock open instead
    python3 crypto.py plan --stop or-mid --target 2R        # -> trader/crypto/plan.json
    python3 crypto.py replay                                # 10,000 simulated sessions + history
    python3 crypto.py size --budget 2000 --max-loss 20      # units, not an order

Data, in this order:
  --bars PATH   a saved bar file: TradingView MCP get_ohlcv JSON ({"bars":[{t,o,h,l,c,v}]}),
                a Yahoo chart JSON, or a plain list of {t,o,h,l,c,v}. .gz is fine. A folder
                is searched for BTC.json, BTC-USD.json, BTCUSDT.json, BINANCE_BTCUSDT.json.
  Yahoo         chart JSON for BTC-USD style tickers (free, unofficial)
  Coinbase      public candles for BTC-USD style products (fallback)

Times are UTC. A crypto "session" is 24 hours starting at --session-open (00:00
UTC by default). Pass --now "YYYY-MM-DD HH:MM" (UTC) to replay a past moment.

Standard library only. Python 3.8+.
"""
import argparse
import datetime as dt
import gzip
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill-crypto/1.0)"
STEP = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}
YAHOO_DAYS = {"1m": 7, "5m": 59, "15m": 59, "1h": 720}
DEFAULT_UNIVERSE = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "DOT",
                    "LTC", "BCH", "TRX", "SUI", "NEAR", "APT", "UNI", "ATOM", "XLM", "HBAR"]
# Yahoo lists a few coins under numbered tickers to tell them apart from older namesakes.
YAHOO_ALIAS = {"SUI": "SUI20947-USD", "APT": "APT21794-USD", "UNI": "UNI7083-USD",
               "TON": "TON11419-USD", "PEPE": "PEPE24478-USD"}
QUOTES = ("USDT", "USDC", "USD")
OUT_DIR = os.path.join("trader", "crypto")
NOT_AN_ORDER = "ANALYSIS ONLY: this is not an order. Crypto orders are blocked by design."


class CryptoError(Exception):
    pass


# ------------------------------------------------------------------ symbols

def base_of(sym):
    """BTC, btc-usd, BTCUSDT, BINANCE:BTCUSDT, COINBASE:BTC-USD -> BTC."""
    s = sym.strip().upper()
    if ":" in s:
        s = s.split(":", 1)[1]
    for alias_base, ticker in YAHOO_ALIAS.items():
        if s == ticker:
            return alias_base
    for sep in ("-", "/", "_"):
        if sep in s:
            return s.split(sep, 1)[0]
    for q in QUOTES:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


def yahoo_ticker(base):
    return YAHOO_ALIAS.get(base, base + "-USD")


def tradingview_symbol(base):
    return "BINANCE:%sUSDT" % base


# ------------------------------------------------------------------ time

def utc_now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def ts(d):
    return int((d - dt.datetime(1970, 1, 1)).total_seconds())


def from_ts(t):
    return dt.datetime(1970, 1, 1) + dt.timedelta(seconds=int(t))


def fmt(t):
    return from_ts(t).strftime("%Y-%m-%d %H:%M UTC")


def parse_now(text):
    if not text:
        return ts(utc_now())
    for f in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return ts(dt.datetime.strptime(text.strip().replace("Z", ""), f))
        except ValueError:
            pass
    raise CryptoError('--now wants "YYYY-MM-DD HH:MM" in UTC, not %r' % text)


def parse_hm(text):
    try:
        h, m = text.split(":")
        h, m = int(h), int(m)
        assert 0 <= h < 24 and 0 <= m < 60
        return h * 3600 + m * 60
    except (ValueError, AssertionError):
        raise CryptoError("--session-open wants HH:MM in UTC, not %r" % text)


def session_start(t, open_sec):
    """Start (unix seconds) of the 24h session holding t."""
    day = t - t % 86400
    s = day + open_sec
    return s if s <= t else s - 86400


# ------------------------------------------------------------------ data

def _read(path):
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return fh.read()
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _unwrap(doc):
    """Saved MCP tool results can come wrapped as [{"type":"text","text":"{...}"}]."""
    for _ in range(3):
        if isinstance(doc, list) and doc and isinstance(doc[0], dict) and "text" in doc[0] and "t" not in doc[0]:
            doc = json.loads(doc[0]["text"])
        elif isinstance(doc, dict) and "result" in doc and isinstance(doc["result"], (list, dict)) \
                and "bars" not in doc and "chart" not in doc:
            doc = doc["result"]
        elif isinstance(doc, dict) and "content" in doc and isinstance(doc["content"], list):
            doc = doc["content"]
        else:
            break
    return doc


def parse_bars(text, where="bars"):
    """TradingView get_ohlcv / Yahoo chart / plain list -> sorted [{t,o,h,l,c,v}]."""
    try:
        doc = _unwrap(json.loads(text))
    except ValueError:
        raise CryptoError("%s is not JSON" % where)
    rows = []
    if isinstance(doc, dict) and "chart" in doc:
        res = (doc["chart"] or {}).get("result") or []
        if not res:
            raise CryptoError("%s: Yahoo returned no data" % where)
        r = res[0]
        q = (((r.get("indicators") or {}).get("quote")) or [{}])[0]
        for i, t in enumerate(r.get("timestamp") or []):
            c = (q.get("close") or [None])[i] if i < len(q.get("close") or []) else None
            if t is None or c is None:
                continue

            def g(k, i=i, c=c):
                arr = q.get(k) or []
                v = arr[i] if i < len(arr) else None
                return c if v is None else v
            rows.append({"t": int(t), "o": float(g("open")), "h": float(g("high")), "l": float(g("low")),
                         "c": float(c), "v": float(q.get("volume")[i] or 0) if q.get("volume") else 0.0})
    else:
        src = doc.get("bars") if isinstance(doc, dict) else doc
        if not isinstance(src, list):
            raise CryptoError("%s: no bars found (want get_ohlcv JSON with a 'bars' list)" % where)
        for b in src:
            try:
                t = int(b["t"])
                if t > 10 ** 11:   # milliseconds
                    t //= 1000
                rows.append({"t": t, "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]),
                             "c": float(b["c"]), "v": float(b.get("v") or 0)})
            except (KeyError, TypeError, ValueError):
                continue
    seen, out = set(), []
    for b in sorted(rows, key=lambda b: b["t"]):
        if b["t"] not in seen:
            seen.add(b["t"])
            out.append(b)
    if not out:
        raise CryptoError("%s holds no usable bars" % where)
    return out


def bars_file(path, base):
    if os.path.isdir(path):
        names = [base, base + "-USD", base + "USDT", base + "USD", "BINANCE_" + base + "USDT",
                 "COINBASE_" + base + "USD", yahoo_ticker(base)]
        for n in names:
            for ext in (".json", ".json.gz"):
                p = os.path.join(path, n + ext)
                if os.path.exists(p):
                    return p
        raise CryptoError("no bar file for %s in %s (want %s.json)" % (base, path, base))
    if not os.path.exists(path):
        raise CryptoError("no such file: %s" % path)
    return path


def _get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(5)
                continue
            raise
    raise CryptoError("rate limited: %s" % url)


def fetch_yahoo(base, interval, days):
    days = min(days, YAHOO_DAYS[interval])
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=%dd&interval=%s" % (
        yahoo_ticker(base), days, interval)
    return parse_bars(_get(url), "Yahoo %s" % yahoo_ticker(base))


def fetch_coinbase(base, interval, days, now):
    gran = STEP[interval]
    end, start_wanted, rows = now, now - days * 86400, []
    for _ in range(60):   # 300 candles per call
        start = max(start_wanted, end - gran * 300)
        url = "https://api.exchange.coinbase.com/products/%s-USD/candles?granularity=%d&start=%s&end=%s" % (
            base, gran, from_ts(start).isoformat() + "Z", from_ts(end).isoformat() + "Z")
        data = json.loads(_get(url))
        if not isinstance(data, list) or not data:
            break
        rows += [{"t": int(r[0]), "l": float(r[1]), "h": float(r[2]), "o": float(r[3]),
                  "c": float(r[4]), "v": float(r[5])} for r in data]
        if start <= start_wanted:
            break
        end = start
        time.sleep(0.35)
    return parse_bars(json.dumps(rows), "Coinbase %s-USD" % base)


def load(sym, interval, days, path=None, now=None):
    base = base_of(sym)
    if path:
        f = bars_file(path, base)
        bars = parse_bars(_read(f), f)
        src = f
    else:
        errors = []
        bars = None
        for name, fn in (("Yahoo", lambda: fetch_yahoo(base, interval, days)),
                         ("Coinbase", lambda: fetch_coinbase(base, interval, days, now or ts(utc_now())))):
            try:
                bars, src = fn(), name
                break
            except (CryptoError, urllib.error.URLError, OSError, ValueError) as e:
                errors.append("%s: %s" % (name, e))
        if bars is None:
            raise CryptoError("no data for %s (%s). Save TradingView bars with the trader-tradingview "
                              "skill and pass --bars FILE." % (base, "; ".join(errors)))
    step = infer_step(bars)
    if step != STEP[interval]:
        name = {v: k for k, v in STEP.items()}.get(step, "%ds" % step)
        raise CryptoError("%s has %s bars, not %s: pass --interval %s" % (src, name, interval, name))
    if now is not None:
        bars = [b for b in bars if b["t"] + step <= now]   # completed bars only
    if not bars:
        raise CryptoError("%s has no completed bars before that time" % src)
    return base, bars, src


def infer_step(bars):
    gaps = {}
    for a, b in zip(bars, bars[1:]):
        d = b["t"] - a["t"]
        if d > 0:
            gaps[d] = gaps.get(d, 0) + 1
    return max(gaps, key=gaps.get) if gaps else 0


# ------------------------------------------------------------------ math

def rp(x):
    """Round a price to 6 significant digits (BTC at 75,890.0 and PEPE at 0.0000123456)."""
    if x == 0 or not math.isfinite(x):
        return x
    return round(x, max(0, 5 - int(math.floor(math.log10(abs(x))))))


def pct(a, b):
    return (a / b - 1.0) * 100.0 if b else 0.0


def atr(bars, n=14):
    if len(bars) < 2:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])) for a, b in zip(bars, bars[1:])]
    trs = trs[-n:]
    return sum(trs) / len(trs)


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for a, b in zip(closes[:n], closes[1:n + 1]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for a, b in zip(closes[n:], closes[n + 1:]):
        d = b - a
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def daily(bars, open_sec=0):
    """Group bars into 24h sessions -> [{s,o,h,l,c,v,n}]."""
    out = {}
    for b in bars:
        s = session_start(b["t"], open_sec)
        d = out.get(s)
        if d is None:
            out[s] = {"s": s, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"], "n": 1}
        else:
            d["h"], d["l"], d["c"] = max(d["h"], b["h"]), min(d["l"], b["l"]), b["c"]
            d["v"] += b["v"]
            d["n"] += 1
    return [out[k] for k in sorted(out)]


# ------------------------------------------------------------------ the rule

def opening_range(bars, s, or_sec, step):
    rng = [b for b in bars if s <= b["t"] < s + or_sec]
    if len(rng) < max(1, (or_sec // step) // 2):
        return None
    hi, lo = max(b["h"] for b in rng), min(b["l"] for b in rng)
    return {"high": hi, "low": lo, "mid": (hi + lo) / 2, "bars": rng,
            "avg_v": sum(b["v"] for b in rng) / len(rng)}


def find_trigger(bars, s, or_end, level, step, entries_sec, min_volume_x=None, avg_v=0):
    for b in bars:
        if b["t"] < or_end or b["t"] >= s + 86400:
            continue
        close_t = b["t"] + step
        if close_t > s + entries_sec:
            return None
        if b["c"] > level and (not min_volume_x or (avg_v and b["v"] >= min_volume_x * avg_v)):
            return b
    return None


def evaluate(bars, now, open_sec, or_min, step, setup="orb", level=None, min_volume_x=None, entries_h=12.0):
    s = session_start(now, open_sec)
    or_sec, entries_sec = or_min * 60, int(entries_h * 3600)
    or_end = s + or_sec
    res = {"session_start": fmt(s), "session_start_ts": s, "setup": setup, "or_minutes": or_min,
           "no_entries_after": fmt(s + entries_sec)}
    last = bars[-1]
    res["last"] = {"time": fmt(last["t"] + step), "close": last["c"]}
    if setup == "orb" and now < or_end:
        res.update(status="forming", detail="the %d-minute range closes at %s" % (or_min, fmt(or_end)))
        return res
    rng = opening_range(bars, s, or_sec, step) if setup == "orb" else None
    if setup == "orb":
        if rng is None:
            res.update(status="no-data", detail="too few bars in the opening range")
            return res
        res["range"] = {"high": rng["high"], "low": rng["low"], "mid": rp(rng["mid"])}
        level = rng["high"]
    elif level is None:
        raise CryptoError("--setup level needs --level PRICE")
    start = or_end if setup == "orb" else s
    res["level"] = level
    trig = find_trigger(bars, s, start, level, step, entries_sec, min_volume_x, rng["avg_v"] if rng else 0)
    if trig:
        res.update(status="triggered", trigger={"time": fmt(trig["t"] + step), "time_ts": trig["t"] + step,
                                                "price": trig["c"]},
                   detail="first %s close above %s" % (fmt_interval(step), rp(level)))
    elif now >= s + entries_sec:
        res.update(status="expired", detail="no close above %s before %s" % (rp(level), fmt(s + entries_sec)))
    else:
        res.update(status="waiting", distance_pct=round(pct(level, last["c"]), 3),
                   detail="last close %s, needs a close above %s" % (rp(last["c"]), rp(level)))
    return res


def fmt_interval(step):
    return {v: k for k, v in STEP.items()}.get(step, "%ds" % step)


# ------------------------------------------------------------------ plan

def stop_price(kind, entry, rng, bars_to_entry):
    if kind == "or-mid":
        if not rng:
            raise CryptoError("or-mid needs an opening range (use --stop pct:2 or atr:1.5)")
        return rng["mid"]
    if kind == "or-low":
        if not rng:
            raise CryptoError("or-low needs an opening range")
        return rng["low"]
    if kind.startswith("pct:"):
        return entry * (1 - float(kind[4:]) / 100)
    if kind.startswith("atr:"):
        a = atr(bars_to_entry)
        if not a:
            raise CryptoError("not enough bars for an ATR stop")
        return entry - float(kind[4:]) * a
    try:
        return float(kind)
    except ValueError:
        raise CryptoError("--stop wants or-mid, or-low, pct:N, atr:N or a price, not %r" % kind)


def make_plan(entry_doc, stop_kind="or-mid", target="2R", flat_h=23.75, min_stop_pct=0.15, bars=None):
    if entry_doc.get("status") != "triggered" and "entry" not in entry_doc:
        raise CryptoError("no triggered entry to plan (status: %s). Pass --entry PRICE to plan anyway."
                          % entry_doc.get("status"))
    entry = float(entry_doc.get("entry") or entry_doc["trigger"]["price"])
    rng = entry_doc.get("range")
    t0 = entry_doc.get("trigger", {}).get("time_ts") or entry_doc.get("entry_ts")
    upto = [b for b in (bars or []) if t0 is None or b["t"] < t0]
    stop = rp(stop_price(stop_kind, entry, rng, upto))
    if stop >= entry:
        raise CryptoError("the stop %s is not below the entry %s" % (stop, entry))
    risk = entry - stop
    if risk / entry * 100 > 30:
        raise CryptoError("the stop %s is %.1f%% under the entry %s: that is not a day-trade stop, "
                          "check the number" % (stop, risk / entry * 100, entry))
    if risk / entry * 100 < min_stop_pct:
        raise CryptoError("the stop is only %.3f%% under the entry: too tight to survive the spread "
                          "(min %.2f%%, change with --min-stop-pct)" % (risk / entry * 100, min_stop_pct))
    if target.upper().endswith("R"):
        r_mult = float(target[:-1])
        tgt = rp(entry + r_mult * risk)
    else:
        tgt = float(target)
        r_mult = (tgt - entry) / risk
    if tgt <= entry:
        raise CryptoError("the target %s is not above the entry %s" % (tgt, entry))
    s = entry_doc.get("session_start_ts")
    flat = (s + int(flat_h * 3600)) if s is not None else ((t0 or 0) + int(flat_h * 3600))
    return {"symbol": entry_doc.get("symbol"), "side": "long", "entry": entry, "stop": stop, "target": tgt,
            "risk_per_unit": rp(risk), "risk_pct": round(risk / entry * 100, 3), "r_multiple": round(r_mult, 2),
            "stop_rule": stop_kind, "entry_time": fmt(t0) if t0 else None, "entry_ts": t0,
            "flat_by": fmt(flat), "flat_by_ts": flat, "session_start_ts": s,
            "or_minutes": entry_doc.get("or_minutes"), "setup": entry_doc.get("setup"),
            "range": rng, "interval": entry_doc.get("interval"), "note": NOT_AN_ORDER}


# ------------------------------------------------------------------ replay

def resolve(o, h, l, c, stop, target):
    """One bar against the brackets. A bar that touches both counts as the stop (conservative)."""
    if o <= stop:
        return "stop", o
    if o >= target:
        return "target", o
    if l <= stop:
        return "stop", stop
    if h >= target:
        return "target", target
    return None, c


def replay(plan, bars, step, open_sec, sims=10000, seed=None):
    entry, stop, target = plan["entry"], plan["stop"], plan["target"]
    risk = entry - stop
    t0, flat = plan["entry_ts"], plan["flat_by_ts"]
    s_now = session_start(t0, open_sec)
    past = [b for b in bars if session_start(b["t"], open_sec) < s_now]
    if not past:
        raise CryptoError("no history before the entry's session to replay from")
    # bar shapes per slot (seconds after the session open): gap from previous close, then h, l, c vs open
    pool, prev = {}, None
    for b in past:
        if prev is not None and b["t"] - prev["t"] == step and b["o"] > 0:
            slot = (b["t"] - session_start(b["t"], open_sec))
            pool.setdefault(slot, []).append((b["o"] / prev["c"], b["h"] / b["o"], b["l"] / b["o"], b["c"] / b["o"]))
        prev = b
    everything = [x for v in pool.values() for x in v]
    days = len({session_start(b["t"], open_sec) for b in past})
    slots = [(t - s_now) for t in range(t0, flat, step)]
    if not slots:
        raise CryptoError("the entry is at or after the flat-by time")
    rnd = random.Random(seed)
    hit = {"target": 0, "stop": 0, "open": 0}
    rs, closes = [], []
    for _ in range(sims):
        p, outcome = entry, None
        for slot in slots:
            choices = pool.get(slot) or everything
            g, hr, lr, cr = choices[rnd.randrange(len(choices))]
            o = p * g
            outcome, p = resolve(o, o * hr, o * lr, o * cr, stop, target)
            if outcome:
                break
        hit[outcome or "open"] += 1
        rs.append((p - entry) / risk)
        if not outcome:
            closes.append(p)
    rs.sort()
    mc = {"sims": sims, "seed": seed, "history_sessions": days,
          "target_first_pct": round(100.0 * hit["target"] / sims, 1),
          "stop_first_pct": round(100.0 * hit["stop"] / sims, 1),
          "open_at_flat_pct": round(100.0 * hit["open"] / sims, 1),
          "avg_r": round(sum(rs) / sims, 3), "median_r": round(rs[sims // 2], 3),
          "p10_r": round(rs[sims // 10], 3), "p90_r": round(rs[9 * sims // 10], 3)}
    return mc, history(plan, past, step, open_sec)


def history(plan, past, step, open_sec):
    """Re-run the same rule on every past session in the data."""
    if plan.get("setup") != "orb" or not plan.get("or_minutes"):
        return {"note": "history replay covers the opening-range rule only"}
    or_sec = plan["or_minutes"] * 60
    entries_sec = int(plan.get("entries_h", 12) * 3600)
    flat_off = plan["flat_by_ts"] - plan["session_start_ts"]
    r_mult = plan["r_multiple"]
    by_s = {}
    for b in past:
        by_s.setdefault(session_start(b["t"], open_sec), []).append(b)
    out = {"sessions": 0, "fired": 0, "target": 0, "stop": 0, "neither": 0, "r": []}
    for s in sorted(by_s):
        day = by_s[s]
        if len(day) < (86400 // step) * 0.9:
            continue   # partial session
        out["sessions"] += 1
        rng = opening_range(day, s, or_sec, step)
        if not rng:
            continue
        trig = find_trigger(day, s, s + or_sec, rng["high"], step, entries_sec)
        if not trig:
            continue
        e = trig["c"]
        rule = plan["stop_rule"]
        try:
            st = stop_price(rule, e, rng, [b for b in day if b["t"] <= trig["t"]]) \
                if not rule.replace(".", "", 1).isdigit() else e * (1 - plan["risk_pct"] / 100)
        except CryptoError:
            st = e * (1 - plan["risk_pct"] / 100)
        if st >= e:
            continue
        tg = e + r_mult * (e - st)
        out["fired"] += 1
        res, px = None, e
        for b in day:
            if b["t"] <= trig["t"] or b["t"] >= s + flat_off:
                continue
            res, px = resolve(b["o"], b["h"], b["l"], b["c"], st, tg)
            if res:
                break
        out[res or "neither"] += 1
        out["r"].append((px - e) / (e - st))
    rs = out.pop("r")
    out["avg_r"] = round(sum(rs) / len(rs), 3) if rs else None
    return out


def verdict(mc, hist):
    avg = mc["avg_r"]
    if avg > 0.15:
        v = "the simulated odds lean your way"
    elif avg < -0.15:
        v = "the simulated odds lean against you"
    else:
        v = "close to a coin flip"
    if hist.get("fired", 0) >= 5 and hist.get("avg_r") is not None and (hist["avg_r"] > 0) != (avg > 0):
        v += "; the past sessions disagree, so trust neither much"
    elif hist.get("fired", 0) < 5:
        v += "; too few past setups to confirm it"
    return v


# ------------------------------------------------------------------ size

def size(plan, budget, max_loss, max_position_pct=50.0, unit_step=0.0001):
    entry, risk = plan["entry"], plan["risk_per_unit"]

    def down(x):
        return math.floor(x / unit_step + 1e-9) * unit_step
    limits = {"max loss per trade": down(max_loss / risk),
              "position cap (%g%% of budget)" % max_position_pct: down(budget * max_position_pct / 100 / entry)}
    decider = min(limits, key=limits.get)
    units = limits[decider]
    if units <= 0:
        raise CryptoError("no size fits: %s allows 0 units" % decider)
    dec = max(0, -int(math.floor(math.log10(unit_step))))
    return {"symbol": plan.get("symbol"), "units": round(units, dec), "decided_by": decider,
            "notional": round(units * entry, 2), "loss_at_stop": round(units * risk, 2),
            "gain_at_target": round(units * (plan["target"] - entry), 2),
            "budget": budget, "max_loss": max_loss, "note": NOT_AN_ORDER}


# ------------------------------------------------------------------ AIXBT context

STABLES = {"USDT", "USDC", "USD1", "DAI", "USDE", "FDUSD", "TUSD", "PYUSD", "USDS"}
RISK_WORDS = re.compile(r"\b(unlock\w*|hack\w*|exploit\w*|drain\w*|stolen|delist\w*|lawsuit|subpoena\w*|"
                        r"nuked|depeg\w*|insolven\w*|security (flaw|findings|incident)|rug\w*)\b", re.I)
ACCESS = re.compile(r"\b(withdraw\w*|deposit\w*|trading|swaps?)\b", re.I)
BLOCKED = re.compile(r"\b(halt\w*|paus\w*|suspend\w*|disabl\w*|frozen|freez\w*|reopen\w*|restor\w*|closed)\b", re.I)


def is_risk(headline):
    h = headline or ""
    return bool(RISK_WORDS.search(h) or (ACCESS.search(h) and BLOCKED.search(h)))


def load_aixbt(path):
    """AIXBT list_projects / get_project output, raw or compact -> {SYMBOL: context}.

    Raw: {"data": [ {symbol, rank, trend, rankBand, intel:[{headline, grade, detectedAt}]} ]}
    Compact: {"BTC": {"rank": 25, "trend": 6.8, "band": "strong", "intel": ["headline", ...]}}
    Extra headlines from list_intel can sit in a top-level "intel" list of {ticker, headline, ...}."""
    try:
        doc = _unwrap(json.loads(_read(path)))
    except (OSError, ValueError) as e:
        raise CryptoError("could not read the AIXBT file %s: %s" % (path, e))
    rows = doc.get("data") if isinstance(doc, dict) and "data" in doc else doc
    if isinstance(rows, dict) and "symbol" in rows:
        rows = [rows]
    out = {}

    def add(sym, ctx):
        sym = (sym or "").upper()
        if not sym or sym in STABLES:
            return
        cur = out.setdefault(sym, {"rank": None, "trend": None, "band": None, "intel": []})
        for k in ("rank", "trend", "band"):
            if ctx.get(k) is not None and (cur[k] is None or (k == "rank" and ctx[k] < cur[k])):
                cur[k] = ctx[k]
        for h in ctx.get("intel") or []:
            if h and h not in cur["intel"]:
                cur["intel"].append(h)

    if isinstance(rows, list):
        for r in rows:
            if not isinstance(r, dict):
                continue
            intel = [(i.get("headline") if isinstance(i, dict) else i) for i in (r.get("intel") or [])]
            add(r.get("symbol") or r.get("ticker"),
                {"rank": r.get("rank"), "trend": r.get("trend"), "band": r.get("rankBand") or r.get("band"),
                 "intel": intel})
    elif isinstance(rows, dict):
        for sym, r in rows.items():
            if sym == "intel" or not isinstance(r, dict):
                continue
            add(sym, {"rank": r.get("rank"), "trend": r.get("trend"), "band": r.get("band") or r.get("rankBand"),
                      "intel": [(i.get("headline") if isinstance(i, dict) else i) for i in (r.get("intel") or [])]})
    if isinstance(doc, dict) and isinstance(doc.get("intel"), list):
        for i in doc["intel"]:
            if isinstance(i, dict):
                for t in (i.get("tickers") or [i.get("ticker") or i.get("symbol")]):
                    add(t, {"intel": [i.get("headline")]})
    for ctx in out.values():
        ctx["risk"] = [h for h in ctx["intel"] if is_risk(h)]
        if ctx["trend"] is not None:
            ctx["trend"] = round(float(ctx["trend"]), 1)
    return out


def aixbt_universe(ctx, limit=25):
    ranked = sorted((c["rank"] if c["rank"] is not None else 10 ** 6, s) for s, c in ctx.items())
    return [s for _, s in ranked][:limit]


# ------------------------------------------------------------------ scan

def scan_one(base, bars, step, open_sec, now, or_min):
    d = daily(bars, open_sec)
    closes = [x["c"] for x in d]
    last = bars[-1]["c"]
    day_ago = [b for b in bars if b["t"] <= bars[-1]["t"] - 86400]
    chg24 = pct(last, day_ago[-1]["c"]) if day_ago else None
    hits = []
    prior = [b for b in bars if bars[-1]["t"] - 8 * 86400 < b["t"] <= bars[-1]["t"] - 86400]
    if prior and last > max(b["c"] for b in prior):
        hits.append("7-day closing-high breakout")
    r = rsi(closes[:-1] + [last]) if len(closes) > 15 else None
    if r is not None and r < 30:
        hits.append("oversold (daily RSI %.0f)" % r)
        if chg24 and chg24 > 0:
            hits.append("oversold bounce")
    if len(closes) >= 21:
        sma_now = sum(closes[-20:]) / 20
        sma_prev = sum(closes[-21:-1]) / 20
        if last > sma_now and closes[-2] <= sma_prev:
            hits.append("reclaimed 20-day average")
    v24 = sum(b["v"] for b in bars if b["t"] > bars[-1]["t"] - 86400)
    v7 = sum(b["v"] for b in bars if bars[-1]["t"] - 8 * 86400 < b["t"] <= bars[-1]["t"] - 86400) / 7
    vol_x = v24 / v7 if v7 else None
    orb = None
    if or_min * 60 >= step:
        e = evaluate(bars, now, open_sec, or_min, step, entries_h=12)
        orb = e["status"]
        if orb == "triggered":
            hits.append("opening-range breakout (%s)" % e["trigger"]["time"][11:16])
    return {"symbol": base, "tradingview": tradingview_symbol(base), "last": last,
            "change_24h_pct": round(chg24, 2) if chg24 is not None else None,
            "daily_rsi": round(r, 1) if r is not None else None,
            "volume_x_7d": round(vol_x, 2) if vol_x else None, "orb": orb, "setups": hits}


# ------------------------------------------------------------------ cli

def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def read_json(path, what):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except OSError:
        raise CryptoError("no %s at %s: run the step before this one" % (what, path))


def cmd_scan(a):
    now = parse_now(a.now)
    open_sec = parse_hm(a.session_open)
    ctx = load_aixbt(a.aixbt) if a.aixbt else {}
    if a.symbols == "aixbt":
        if not ctx:
            raise CryptoError("--symbols aixbt needs --aixbt FILE")
        syms = aixbt_universe(ctx, a.aixbt_top)
    else:
        syms = [s for s in (a.symbols.split(",") if a.symbols else DEFAULT_UNIVERSE) if s.strip()]
    rows, errs = [], []
    for s in syms:
        try:
            base, bars, _ = load(s, a.interval, a.days, a.bars, now if a.now else None)
            row = scan_one(base, bars, STEP[a.interval], open_sec, now, a.or_minutes)
            if ctx:
                c = ctx.get(base) or {}
                row["aixbt"] = {"rank": c.get("rank"), "trend": c.get("trend"), "band": c.get("band"),
                                "intel": (c.get("intel") or [])[:3], "risk": c.get("risk") or []}
            rows.append(row)
        except CryptoError as e:
            errs.append("%s: %s" % (base_of(s), e))
        if not a.bars:
            time.sleep(0.4)
    def rank_of(r):
        x = (r.get("aixbt") or {}).get("rank")
        return x if x is not None else 10 ** 6
    rows.sort(key=lambda r: (-len(r["setups"]), rank_of(r), -(r["volume_x_7d"] or 0)))
    if a.json:
        print(json.dumps({"rows": rows, "errors": errs}, indent=2))
    else:
        print("Crypto scan, %s bars, session from %s UTC, as of %s" % (a.interval, a.session_open, fmt(now)))
        ax = bool(ctx)
        print("%-6s %14s %8s %6s %6s %s %s" % ("coin", "last", "24h %", "RSI", "vol x",
                                              " aixbt" if ax else "", " setups"))
        for r in rows:
            c = r.get("aixbt") or {}
            tag = ""
            if ax:
                tag = ("#%d" % c["rank"]) if c.get("rank") is not None else "-"
                tag = "%6s" % (tag + ("!" if c.get("risk") else ""))
            print("%-6s %14s %8s %6s %6s %s  %s" % (
                r["symbol"], rp(r["last"]), "" if r["change_24h_pct"] is None else "%+.2f" % r["change_24h_pct"],
                "" if r["daily_rsi"] is None else "%.0f" % r["daily_rsi"],
                "" if r["volume_x_7d"] is None else "%.2f" % r["volume_x_7d"], tag, ", ".join(r["setups"]) or "-"))
        flagged = [r for r in rows if (r.get("aixbt") or {}).get("risk")]
        if flagged:
            print("\nAIXBT event risk (! above). Read before trusting any setup:")
            for r in flagged:
                for h in r["aixbt"]["risk"][:3]:
                    print("  %-6s %s" % (r["symbol"], h))
        if ax:
            print("AIXBT rank = attention trend (#1 = most). Context, not a signal.")
        for e in errs:
            print("  skipped " + e)
    if a.write:
        write_json(a.write, {"as_of": fmt(now), "interval": a.interval, "session_open_utc": a.session_open,
                             "hits": [r for r in rows if r["setups"]], "rows": rows})
        print("wrote %s" % a.write)
    return 0 if rows else 1


def cmd_entry(a):
    now = parse_now(a.now)
    base, bars, src = load(a.symbol, a.interval, 3, a.bars, now if a.now else None)
    res = evaluate(bars, now, parse_hm(a.session_open), a.or_minutes, STEP[a.interval], a.setup,
                   a.level, a.min_volume_x, a.entries_hours)
    res.update(symbol=base, interval=a.interval, source=src, session_open_utc=a.session_open,
               entries_h=a.entries_hours, checked_at=fmt(now))
    if a.json:
        print(json.dumps(res, indent=2))
    else:
        print("%s %s: %s" % (base, res["setup"].upper(), res["status"].upper()))
        print("  session from %s, entries until %s" % (res["session_start"], res["no_entries_after"]))
        if res.get("range"):
            print("  %d-min range: %s - %s (mid %s)" % (a.or_minutes, rp(res["range"]["low"]),
                                                       rp(res["range"]["high"]), res["range"]["mid"]))
        print("  " + res["detail"])
        if res.get("trigger"):
            print("  entry %s at %s" % (rp(res["trigger"]["price"]), res["trigger"]["time"]))
    if a.aixbt:
        c = load_aixbt(a.aixbt).get(base)
        res["aixbt"] = c
        if not a.json:
            if c:
                print("  AIXBT: rank %s, trend %s%s" % (c.get("rank") or "-", c.get("trend") or "-",
                                                      ", %s" % c["band"] if c.get("band") else ""))
                for h in c["risk"][:3]:
                    print("  ! event risk: %s" % h)
                for h in [x for x in c["intel"] if x not in c["risk"]][:2]:
                    print("  - %s" % h)
            else:
                print("  AIXBT: no context for %s in %s" % (base, a.aixbt))
    if a.write and res["status"] == "triggered":
        write_json(a.write, res)
        print("wrote %s" % a.write)
    return 0


def cmd_plan(a):
    doc = read_json(a.entry_file, "entry") if a.entry is None else {
        "entry": a.entry, "symbol": base_of(a.symbol or "?"), "status": "manual", "setup": "manual",
        "entry_ts": parse_now(a.now), "session_start_ts": session_start(parse_now(a.now), parse_hm(a.session_open))}
    bars = None
    if a.stop.startswith("atr:"):
        _, bars, _ = load(doc.get("symbol") or a.symbol, doc.get("interval") or a.interval, 3, a.bars)
    p = make_plan(doc, a.stop, a.target, a.flat_after, a.min_stop_pct, bars)
    p["entries_h"] = doc.get("entries_h", 12)
    p["session_open_utc"] = doc.get("session_open_utc", a.session_open)
    p["interval"] = doc.get("interval") or a.interval
    if a.json:
        print(json.dumps(p, indent=2))
    else:
        print("%s long plan (%s)" % (p["symbol"], NOT_AN_ORDER))
        print("  entry  %s" % rp(p["entry"]))
        print("  stop   %s  (%s, %s a unit, %.2f%%)" % (p["stop"], p["stop_rule"], p["risk_per_unit"], p["risk_pct"]))
        print("  target %s  (%sR)" % (p["target"], p["r_multiple"]))
        print("  flat by %s either way" % p["flat_by"])
    if a.write:
        write_json(a.write, p)
        print("wrote %s" % a.write)
    return 0


def cmd_replay(a):
    p = read_json(a.plan, "plan")
    if not p.get("entry_ts"):
        raise CryptoError("the plan has no entry time to replay from")
    interval = p.get("interval") or a.interval
    open_sec = parse_hm(p.get("session_open_utc") or a.session_open)
    _, bars, src = load(p["symbol"], interval, a.days, a.bars, p["entry_ts"])
    mc, hist = replay(p, bars, STEP[interval], open_sec, a.sims, a.seed)
    v = verdict(mc, hist)
    out = {"symbol": p["symbol"], "plan": {k: p[k] for k in ("entry", "stop", "target", "flat_by")},
           "monte_carlo": mc, "history": hist, "verdict": v, "source": src,
           "caveat": "a simulation from past bars, not a prediction"}
    if a.json:
        print(json.dumps(out, indent=2))
    else:
        print("%s time machine: %d sessions from %d past days of %s bars (seed %s)" % (
            p["symbol"], mc["sims"], mc["history_sessions"], interval, mc["seed"]))
        print("  target first   %5.1f %%" % mc["target_first_pct"])
        print("  stop first     %5.1f %%" % mc["stop_first_pct"])
        print("  open at flat   %5.1f %%" % mc["open_at_flat_pct"])
        print("  average R %+.2f, median %+.2f, 10th-90th pct %+.2f to %+.2f" % (
            mc["avg_r"], mc["median_r"], mc["p10_r"], mc["p90_r"]))
        if "fired" in hist:
            print("  history: the rule fired %d times in %d full past sessions: %d target, %d stop, %d neither%s" % (
                hist["fired"], hist["sessions"], hist["target"], hist["stop"], hist["neither"],
                "" if hist["avg_r"] is None else ", average R %+.2f" % hist["avg_r"]))
        else:
            print("  history: " + hist["note"])
        print("  verdict: " + v)
        print("  (a simulation, not a prediction)")
    if a.write:
        write_json(a.write, out)
        print("wrote %s" % a.write)
    return 0


def cmd_size(a):
    p = read_json(a.plan, "plan")
    cfg = {}
    if os.path.exists(a.config):
        cfg = read_json(a.config, "config").get("crypto", {})
    budget = a.budget if a.budget is not None else cfg.get("budget")
    max_loss = a.max_loss if a.max_loss is not None else cfg.get("max_loss_per_trade")
    if budget is None or max_loss is None:
        raise CryptoError("give --budget and --max-loss (or set crypto.budget and crypto.max_loss_per_trade "
                          "in %s)" % a.config)
    r = size(p, float(budget), float(max_loss), a.max_position_pct, a.unit_step)
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        print("%s size: %s units (decided by %s)" % (r["symbol"], r["units"], r["decided_by"]))
        print("  notional $%s, loss at the stop $%s, gain at the target $%s" % (
            r["notional"], r["loss_at_stop"], r["gain_at_target"]))
        print("  " + NOT_AN_ORDER)
    if a.write:
        write_json(a.write, r)
        print("wrote %s" % a.write)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="24/7 crypto day-trade analysis (no orders).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, interval="5m"):
        p.add_argument("--interval", choices=sorted(STEP), default=interval)
        p.add_argument("--bars", metavar="PATH", help="saved bar file or folder (TradingView get_ohlcv JSON ok)")
        p.add_argument("--session-open", default="00:00", help="session start, HH:MM UTC (default 00:00)")
        p.add_argument("--now", help='pretend it is this UTC time, "YYYY-MM-DD HH:MM"')
        p.add_argument("--json", action="store_true")

    p = sub.add_parser("scan", help="setups across a list of coins")
    common(p, "1h")
    p.add_argument("--symbols", help='comma list (default: top 20 by market cap), or "aixbt" for the AIXBT ranking')
    p.add_argument("--aixbt", metavar="FILE", help="saved AIXBT list_projects / get_project JSON (or compact)")
    p.add_argument("--aixbt-top", type=int, default=25, help="with --symbols aixbt: how many ranked coins")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--or-minutes", type=int, default=60)
    p.add_argument("--write", nargs="?", const=os.path.join(OUT_DIR, "watchlist.json"))
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("entry", help="opening-range or level breakout status")
    common(p)
    p.add_argument("symbol")
    p.add_argument("--setup", choices=["orb", "level"], default="orb")
    p.add_argument("--or-minutes", type=int, default=60)
    p.add_argument("--level", type=float)
    p.add_argument("--min-volume-x", type=float)
    p.add_argument("--aixbt", metavar="FILE", help="saved AIXBT context for this coin")
    p.add_argument("--entries-hours", type=float, default=12, help="no new entries this long after the open")
    p.add_argument("--write", nargs="?", const=os.path.join(OUT_DIR, "entry.json"))
    p.set_defaults(fn=cmd_entry)

    p = sub.add_parser("plan", help="stop, target and flat-by time")
    common(p)
    p.add_argument("--entry-file", default=os.path.join(OUT_DIR, "entry.json"))
    p.add_argument("--entry", type=float, help="plan from this price instead of entry.json")
    p.add_argument("--symbol", help="with --entry")
    p.add_argument("--stop", default="or-mid", help="or-mid | or-low | pct:N | atr:N | PRICE")
    p.add_argument("--target", default="2R", help="NR or a price")
    p.add_argument("--flat-after", type=float, default=23.75, help="hours after the session open")
    p.add_argument("--min-stop-pct", type=float, default=0.15)
    p.add_argument("--write", nargs="?", const=os.path.join(OUT_DIR, "plan.json"))
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("replay", help="the time machine")
    common(p)
    p.add_argument("--plan", default=os.path.join(OUT_DIR, "plan.json"))
    p.add_argument("--days", type=int, default=59)
    p.add_argument("--sims", type=int, default=10000)
    p.add_argument("--seed", type=int, default=19)
    p.add_argument("--write", nargs="?", const=os.path.join(OUT_DIR, "replay.json"))
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("size", help="units for your budget and max loss (not an order)")
    p.add_argument("--plan", default=os.path.join(OUT_DIR, "plan.json"))
    p.add_argument("--budget", type=float)
    p.add_argument("--max-loss", type=float)
    p.add_argument("--max-position-pct", type=float, default=50.0)
    p.add_argument("--unit-step", type=float, default=0.0001, help="smallest unit you can buy")
    p.add_argument("--config", default=os.path.join("trader", "config.json"))
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const=os.path.join(OUT_DIR, "size.json"))
    p.set_defaults(fn=cmd_size)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except CryptoError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
