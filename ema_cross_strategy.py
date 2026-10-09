"""
Fast/slow EMA crossover engine (default 12/21) on a configurable bar size
(default 5 seconds), driven by real trade ticks rather than price polls.

Why tick-driven: a 5-second bar cannot be built from a 10-15s REST poll of
DexScreener — you would be sampling less than one point per bar. This engine
takes individual swap ticks (see pumpportal_feed.py) and folds them into
wall-clock-aligned bars, so a "5s candle" is an actual 5s candle.

Signal model — the tracker exposes a TARGET state, not one-shot signals:

    t["want_long"]   True  -> caller should be holding
                     False -> caller should be flat

The caller compares want_long against the position it actually holds and acts
on the difference. This is deliberate: one-shot "buy"/"sell" signals get lost
when a trade fails or the process restarts, whereas a target state is
idempotent — a failed sell simply stays desired until it succeeds.

Entry:  fast EMA crosses ABOVE slow EMA on a closed bar, with a minimum
        separation (MIN_EDGE_BPS) so noise-level touches don't fire, and only
        after COOLDOWN_BARS have passed since the last exit.
Exit:   fast EMA crosses BELOW slow EMA on a closed bar (after MIN_HOLD_BARS),
        or a hard stop / trailing stop / time stop — the stops are evaluated on
        every tick, not just on bar close, because a 5s bar is a long time in a
        memecoin.

Gaps matter: a token with no trades for 30s must still advance its EMAs, or a
stalled chart would freeze the indicator and the cross would fire late off
stale data. Empty bars are filled flat at the last close (see _advance_to).
"""

from collections import deque
from typing import Optional
import os
import time

# ─── PARAMETERS (env-overridable) ─────────────────────────────────────────────
BAR_SEC       = int(os.getenv("EMA_BAR_SEC", "5"))       # 5-second timeframe
FAST_PERIOD   = int(os.getenv("EMA_FAST", "12"))         # 12 bars = 60s
SLOW_PERIOD   = int(os.getenv("EMA_SLOW", "21"))         # 21 bars = 105s

# Whipsaw guards. On a 5s chart the raw 12/21 cross fires constantly, and every
# round trip costs PumpPortal fees + priority fee + slippage. These exist to
# make the average firing clear that cost; tune them against backtest output.
MIN_EDGE_BPS   = float(os.getenv("EMA_MIN_EDGE_BPS", "15"))   # fast must beat slow by 0.15%
MIN_HOLD_BARS  = int(os.getenv("EMA_MIN_HOLD_BARS", "3"))     # ignore cross-down for 15s after entry
COOLDOWN_BARS  = int(os.getenv("EMA_COOLDOWN_BARS", "6"))     # 30s flat before re-entering
MAX_HOLD_BARS  = int(os.getenv("EMA_MAX_HOLD_BARS", "360"))   # 30min time stop

STOP_LOSS_PCT  = float(os.getenv("EMA_STOP_LOSS_PCT", "12"))  # hard stop from entry
TRAIL_PCT      = float(os.getenv("EMA_TRAIL_PCT", "18"))      # trail from peak; 0 disables

FAST_ALPHA = 2 / (FAST_PERIOD + 1)
SLOW_ALPHA = 2 / (SLOW_PERIOD + 1)
MAX_BARS   = 600                      # bounded per-token history
MAX_GAP_FILL_BARS = 720               # don't fabricate more than an hour of flat bars


def new_tracker(mint: str, size_sol: float, source: str = "") -> dict:
    return {
        "mint": mint,
        "size_sol": size_sol,
        "source": source,
        "closes": deque(maxlen=MAX_BARS),
        "fast": None,
        "slow": None,
        "prev_fast": None,
        "prev_slow": None,
        # current forming bar
        "bar_start": None,
        "bar_o": None, "bar_h": None, "bar_l": None, "bar_c": None,
        # target state the caller acts on
        "want_long": False,
        "entry_price": None,
        "entry_bar": None,
        "peak_price": None,
        "exit_bar": None,
        "exit_reason": None,
        # counters
        "bars_seen": 0,
        "ticks": 0,
        "last_price": None,
        "last_tick_ts": None,
        "created_at": time.time(),
    }


def warmup_bars_needed() -> int:
    return SLOW_PERIOD


def is_ready(t: dict) -> bool:
    return t["fast"] is not None and t["slow"] is not None


def _bar_index(ts: float) -> float:
    """Wall-clock-aligned bar start, so bars are deterministic and replayable."""
    return (int(ts) // BAR_SEC) * BAR_SEC


def _close_bar(t: dict, close: float) -> None:
    """Fold one closed bar into EMA state and re-evaluate the target position."""
    t["closes"].append(close)
    t["bars_seen"] += 1
    n = len(t["closes"])
    closes = t["closes"]

    t["prev_fast"], t["prev_slow"] = t["fast"], t["slow"]

    # Seed each EMA from its own SMA the moment enough bars exist, then step it.
    if n < FAST_PERIOD:
        t["fast"] = None
    elif n == FAST_PERIOD:
        t["fast"] = sum(list(closes)[-FAST_PERIOD:]) / FAST_PERIOD
    else:
        t["fast"] = close * FAST_ALPHA + t["fast"] * (1 - FAST_ALPHA)

    if n < SLOW_PERIOD:
        t["slow"] = None
    elif n == SLOW_PERIOD:
        t["slow"] = sum(list(closes)[-SLOW_PERIOD:]) / SLOW_PERIOD
    else:
        t["slow"] = close * SLOW_ALPHA + t["slow"] * (1 - SLOW_ALPHA)

    if t["fast"] is None or t["slow"] is None:
        return
    if t["prev_fast"] is None or t["prev_slow"] is None:
        return  # first bar where both exist has no previous relationship to cross

    edge_bps = (t["fast"] - t["slow"]) / t["slow"] * 10_000
    crossed_up = t["prev_fast"] <= t["prev_slow"] and t["fast"] > t["slow"]
    crossed_dn = t["prev_fast"] >= t["prev_slow"] and t["fast"] < t["slow"]

    if not t["want_long"]:
        cooled = t["exit_bar"] is None or (t["bars_seen"] - t["exit_bar"]) >= COOLDOWN_BARS
        if crossed_up and edge_bps >= MIN_EDGE_BPS and cooled:
            _enter(t, close)
    else:
        held = t["bars_seen"] - (t["entry_bar"] or t["bars_seen"])
        if crossed_dn and held >= MIN_HOLD_BARS:
            _exit(t, "ema cross down")
        elif held >= MAX_HOLD_BARS:
            _exit(t, f"time stop ({MAX_HOLD_BARS} bars)")


def _enter(t: dict, price: float) -> None:
    t["want_long"] = True
    t["entry_price"] = price
    t["entry_bar"] = t["bars_seen"]
    t["peak_price"] = price
    t["exit_reason"] = None


def _exit(t: dict, reason: str) -> None:
    t["want_long"] = False
    t["exit_bar"] = t["bars_seen"]
    t["exit_reason"] = reason


def set_entry_price(t: dict, price: float) -> None:
    """Override the reference entry with the actual fill, so stops measure from
    what was really paid rather than from the signal bar's close."""
    if price and price > 0:
        t["entry_price"] = price
        t["peak_price"] = max(price, t["peak_price"] or price)


def _check_stops(t: dict, price: float) -> None:
    """Stops run on every tick — a 5s bar is a long time to sit through a dump."""
    if not t["want_long"] or not t["entry_price"]:
        return
    t["peak_price"] = max(t["peak_price"] or price, price)

    if STOP_LOSS_PCT > 0:
        if price <= t["entry_price"] * (1 - STOP_LOSS_PCT / 100):
            _exit(t, f"stop loss -{STOP_LOSS_PCT:g}%")
            return
    if TRAIL_PCT > 0 and t["peak_price"]:
        if price <= t["peak_price"] * (1 - TRAIL_PCT / 100):
            _exit(t, f"trailing stop -{TRAIL_PCT:g}% from peak")


def _advance_to(t: dict, bar: float) -> None:
    """Close every bar between the current one and `bar`, filling untraded bars
    flat at the last close so the EMAs keep advancing through dead air."""
    if t["bar_start"] is None:
        return
    gap = int((bar - t["bar_start"]) // BAR_SEC)
    if gap <= 0:
        return
    if gap > MAX_GAP_FILL_BARS:
        # Token went quiet for a long stretch; the old EMA series is meaningless
        # now. Reset rather than fabricate an hour of synthetic bars.
        t["closes"].clear()
        t["fast"] = t["slow"] = t["prev_fast"] = t["prev_slow"] = None
        t["bar_start"] = None
        # Never hold through a reset: with the EMAs cleared no cross-down can
        # fire for the next SLOW_PERIOD bars, so a position would be left with
        # only the stops to protect it. An hour of silence on a memecoin you
        # hold is reason enough to be out.
        if t["want_long"]:
            _exit(t, "data gap - indicator reset")
        return

    _close_bar(t, t["bar_c"])                     # the real, traded bar
    for _ in range(gap - 1):                      # untraded bars in between
        _close_bar(t, t["bar_c"])
    t["bar_start"] = None


def ingest_tick(t: dict, price: float, ts: Optional[float] = None) -> bool:
    """Feed one trade tick. Returns True if the target state changed and the
    caller should act."""
    if price is None or price <= 0:
        return False
    ts = ts if ts is not None else time.time()
    before = t["want_long"]

    t["ticks"] += 1
    t["last_price"] = price
    t["last_tick_ts"] = ts

    _check_stops(t, price)

    bar = _bar_index(ts)
    if t["bar_start"] is None:
        t["bar_start"] = bar
        t["bar_o"] = t["bar_h"] = t["bar_l"] = t["bar_c"] = price
        return t["want_long"] != before

    if bar > t["bar_start"]:
        _advance_to(t, bar)
        t["bar_start"] = bar
        t["bar_o"] = t["bar_h"] = t["bar_l"] = t["bar_c"] = price
    else:
        t["bar_h"] = max(t["bar_h"], price)
        t["bar_l"] = min(t["bar_l"], price)
        t["bar_c"] = price

    return t["want_long"] != before


def flush(t: dict, now: Optional[float] = None) -> bool:
    """Close any bars that have elapsed without a trade. Must be called on a
    timer — with no ticks, nothing else advances the clock. Returns True if the
    target state changed."""
    now = now if now is not None else time.time()
    if t["bar_start"] is None:
        return False
    bar = _bar_index(now)
    if bar <= t["bar_start"]:
        return False
    before = t["want_long"]
    _advance_to(t, bar)
    return t["want_long"] != before


def status(t: dict) -> str:
    if not is_ready(t):
        return f"warming up {len(t['closes'])}/{SLOW_PERIOD} bars"
    edge = (t["fast"] - t["slow"]) / t["slow"] * 10_000
    side = "LONG" if t["want_long"] else "flat"
    return (f"{side} | fast {t['fast']:.3e} slow {t['slow']:.3e} "
            f"edge {edge:+.0f}bps | {t['bars_seen']} bars, {t['ticks']} ticks")


def configure(**kw) -> None:
    """Override parameters at runtime (used by backtest_ema_cross.py to sweep).
    Recomputes the smoothing constants so FAST/SLOW changes actually take."""
    global BAR_SEC, FAST_PERIOD, SLOW_PERIOD, FAST_ALPHA, SLOW_ALPHA
    global MIN_EDGE_BPS, MIN_HOLD_BARS, COOLDOWN_BARS, MAX_HOLD_BARS
    global STOP_LOSS_PCT, TRAIL_PCT

    for k, v in kw.items():
        if v is None:
            continue
        key = k.upper()
        if key not in globals():
            raise KeyError(f"unknown strategy parameter: {k}")
        globals()[key] = v

    FAST_ALPHA = 2 / (FAST_PERIOD + 1)
    SLOW_ALPHA = 2 / (SLOW_PERIOD + 1)


def params() -> dict:
    return {
        "bar_sec": BAR_SEC, "fast": FAST_PERIOD, "slow": SLOW_PERIOD,
        "min_edge_bps": MIN_EDGE_BPS, "min_hold_bars": MIN_HOLD_BARS,
        "cooldown_bars": COOLDOWN_BARS, "max_hold_bars": MAX_HOLD_BARS,
        "stop_loss_pct": STOP_LOSS_PCT, "trail_pct": TRAIL_PCT,
    }
