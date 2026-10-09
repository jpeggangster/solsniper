"""
Structure-based exit engine for HOOD positions.
------------------------------------------------
Replaces the fixed "+100% sells 10%" rungs once a position is de-risked at 2x.
After that the remainder is governed by structure and flow rather than by
round-number gains:

  1. EMA cross      EMA12 crossing below EMA21 on closed 5m bars -> sell a chunk
  2. Higher-low     a 5m close below the last confirmed higher low -> sell the rest
  3. MC levels      approaching a round market cap (1M/2M/5M/…) -> sell a chunk
  4. Volume dry-up  price flat while volume shrinks (distribution) -> sell the rest
  5. Moon bag       never auto-sell below a floor % of the original position

Two constraints shaped this, both discovered rather than assumed:

  * There is no OHLCV backfill for Robinhood Chain. DexPaprika (used by
    ema_strategy.py for Solana) has no robinhood network and now returns HTTP
    402 without a key, and DexScreener exposes no public candle endpoint. So
    bars are built in-process from price polls, and the EMAs are seeded from the
    first close instead of a warm history. Early crosses are therefore noisier
    than they would be on a backfilled series.
  * Crosses are evaluated on CLOSED bars only, so a signal cannot repaint. With
    a 15s poll the sell fires within 15s of the 5m bar closing.

The tracker is plain JSON-serialisable state, stored inside the position record,
so a bot restart keeps its EMA and swing-low history instead of starting blind.
"""

from typing import Optional

CANDLE_SEC = 300          # 5m bars
EMA_FAST = 12
EMA_SLOW = 21
MAX_CANDLES = 120

# Higher lows are found with a zigzag, not a 3-bar fractal. During a parabolic
# leg every bar's low is above the previous one, so a fractal finds no local
# minimum at all and the rule never arms. A zigzag instead marks a trough only
# once price has retraced SWING_PULLBACK_PCT and then recovered by the same
# amount — which is what a trader actually reads as a higher low.
SWING_PULLBACK_PCT = 8.0

# Round market-cap levels memecoins tend to stall at.
MC_LEVELS = [1e6, 2e6, 5e6, 1e7, 2e7, 5e7, 1e8, 2e8, 5e8, 1e9]

# Volume dry-up: this many consecutive shrinking bars while price stays inside
# FLAT_RANGE_PCT reads as distribution.
VOL_DECLINE_BARS = 3
FLAT_RANGE_PCT = 12.0


def new_trail(price: float, now: float) -> dict:
    return {
        "closes": [],
        "lows": [],
        "vols": [],
        "ema_fast": None,
        "ema_slow": None,
        "prev_fast": None,
        "prev_slow": None,
        "swing_lows": [],
        "last_higher_low": None,
        "zz_dir": 0,
        "zz_high": price,
        "zz_low": price,
        "mc_seen": False,
        "mc_hit": [],
        "fired": [],
        "bar_start": now,
        "bar_o": price, "bar_h": price, "bar_l": price, "bar_c": price,
        "bar_vol_start": None,
        "bars": 0,
    }


def _ema(prev: Optional[float], value: float, period: int) -> float:
    if prev is None:
        return value                      # seed on first close — no history available
    k = 2.0 / (period + 1)
    return value * k + prev * (1 - k)


def _update_swings(t: dict, close: float):
    """Zigzag: confirm a trough once price retraces then recovers by the threshold."""
    th = SWING_PULLBACK_PCT / 100.0

    if t["zz_dir"] == 0:
        t["zz_dir"], t["zz_high"], t["zz_low"] = 1, close, close
        return

    if t["zz_dir"] == 1:                       # riding a leg up
        if close > t["zz_high"]:
            t["zz_high"] = close
            t["zz_low"] = close                # any trough tracked so far is void
        elif close <= t["zz_high"] * (1 - th):
            t["zz_dir"], t["zz_low"] = -1, close
        return

    # riding a leg down — the lowest point is the candidate trough
    if close < t["zz_low"]:
        t["zz_low"] = close
    elif close >= t["zz_low"] * (1 + th):
        trough = t["zz_low"]
        prev = t["swing_lows"][-1] if t["swing_lows"] else None
        t["swing_lows"].append(trough)
        if prev is None or trough > prev:
            t["last_higher_low"] = trough      # only a HIGHER low arms the rule
        t["zz_dir"], t["zz_high"] = 1, close


def _volume_drying(t: dict) -> bool:
    vols = [v for v in t["vols"][-(VOL_DECLINE_BARS + 1):] if v is not None]
    if len(vols) < VOL_DECLINE_BARS + 1:
        return False
    if not all(vols[i] > vols[i + 1] for i in range(len(vols) - 1)):
        return False
    closes = t["closes"][-(VOL_DECLINE_BARS + 1):]
    if len(closes) < VOL_DECLINE_BARS + 1 or min(closes) <= 0:
        return False
    rng = (max(closes) - min(closes)) / min(closes) * 100
    return rng <= FLAT_RANGE_PCT


def check_mc_levels(t: dict, mcap: Optional[float], buffer_pct: float) -> Optional[dict]:
    """Fire once per round MC level as price approaches it from below."""
    if not mcap:
        return None
    if not t.get("mc_seen"):
        t["mc_seen"] = True
        t["mc_hit"] = [l for l in MC_LEVELS if mcap >= l * (1 - buffer_pct / 100.0)]
        return None
    for level in MC_LEVELS:
        if level in t["mc_hit"]:
            continue
        if mcap >= level * (1 - buffer_pct / 100.0):
            t["mc_hit"].append(level)
            return {"rule": "mc_level",
                    "reason": f"approaching ${level/1e6:.0f}M market cap (MC ${mcap/1e6:.2f}M)"}
    return None


def ingest(t: dict, price: float, now: float,
           mcap: Optional[float] = None, vol_m5: Optional[float] = None) -> list[dict]:
    """Feed a price tick. Returns exit signals (empty most of the time).

    MC signals can fire intrabar; EMA/structure/volume signals only on bar close.
    """
    signals = []

    mc = check_mc_levels(t, mcap, buffer_pct=2.0)
    if mc:
        signals.append(mc)

    t["bar_h"] = max(t["bar_h"], price)
    t["bar_l"] = min(t["bar_l"], price)
    t["bar_c"] = price
    if t["bar_vol_start"] is None and vol_m5 is not None:
        t["bar_vol_start"] = vol_m5

    if now - t["bar_start"] < CANDLE_SEC:
        return signals

    # ── bar close ──
    close, low = t["bar_c"], t["bar_l"]
    t["closes"].append(close)
    t["lows"].append(low)
    t["vols"].append(vol_m5)
    del t["closes"][:-MAX_CANDLES], t["lows"][:-MAX_CANDLES], t["vols"][:-MAX_CANDLES]
    t["bars"] += 1

    t["prev_fast"], t["prev_slow"] = t["ema_fast"], t["ema_slow"]
    t["ema_fast"] = _ema(t["ema_fast"], close, EMA_FAST)
    t["ema_slow"] = _ema(t["ema_slow"], close, EMA_SLOW)

    _update_swings(t, close)

    # 1. bearish EMA cross — needs a real prior state, not the seeded first bar
    if (t["prev_fast"] is not None and t["prev_slow"] is not None and t["bars"] > 2
            and t["prev_fast"] >= t["prev_slow"] and t["ema_fast"] < t["ema_slow"]):
        signals.append({"rule": "ema_cross",
                        "reason": f"EMA{EMA_FAST} crossed below EMA{EMA_SLOW} on the 5m"})

    # 2. break of the last higher low — latched, it only means something once
    hl = t["last_higher_low"]
    if hl and close < hl and "higher_low_break" not in t["fired"]:
        t["fired"].append("higher_low_break")
        signals.append({"rule": "higher_low_break",
                        "reason": f"5m close {close:.3e} below last higher low {hl:.3e}"})

    # 4. distribution — also latched; it re-fires every bar of a flat tape
    if _volume_drying(t) and "volume_dry" not in t["fired"]:
        t["fired"].append("volume_dry")
        signals.append({"rule": "volume_dry",
                        "reason": f"volume shrinking {VOL_DECLINE_BARS} bars while price flat"})

    t["bar_start"] = now
    t["bar_o"] = t["bar_h"] = t["bar_l"] = t["bar_c"] = price
    t["bar_vol_start"] = vol_m5
    return signals
