"""
EMA55 crossover strategy, adapted from the TR-B2S-Crypto-18 long routine's
buy/exit logic, applied to Solana memecoins on a 1-minute timeframe.

Hybrid data source:
  - Historical seed: real 1m OHLCV from DexPaprika's public API
    (https://api.dexpaprika.com, free, no key). Verified solid for OLD data
    (~100 clean sequential candles from hours-old history), but empirically
    UNRELIABLE for recent windows — two overlapping "last N minutes" queries
    returned different, non-overlapping single candles in testing. So only
    candles older than SAFETY_MARGIN_MINUTES are used to seed EMA state.
  - Live monitoring: 1-minute candles built in-process from live DexScreener
    price polls (ingest_price), continuing the same EMA series forward from
    wherever the backfilled seed leaves off. This is what actually drives
    buy/sell decisions, so it deliberately does NOT depend on DexPaprika's
    flaky recent-data window.

A token with 55+ minutes of real trading history gets a usable EMA55 almost
immediately (backfilled). A brand-new token still needs to wait out its own
age in real time — that's a hard floor set by the indicator, not the data source.

Rules (mirrors the routine's LONG exit/entry logic, 1:1):
  - EXIT (close 100%) when the latest CLOSED candle's close is BELOW the EMA55.
  - ENTER when the latest CLOSED candle's close crossed ABOVE the EMA55
    (close is above EMA, and the previous candle's close was at or below its EMA).
"""

from collections import deque
from datetime import datetime, timedelta, timezone
import time
from typing import Optional
import requests

DEXPAPRIKA_BASE = "https://api.dexpaprika.com"
CANDLE_INTERVAL = "1m"
CANDLE_SEC = 60                     # live in-process bar duration — matches the backfilled interval
EMA_PERIOD = 55
EMA_ALPHA = 2 / (EMA_PERIOD + 1)
BACKFILL_MINUTES = EMA_PERIOD + 10  # small buffer past the minimum needed
SAFETY_MARGIN_MINUTES = 5           # exclude DexPaprika's unreliable recent tail; live ticking covers the gap
MAX_CANDLES = 300                   # bounded per-token history


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_pool(ca: str) -> Optional[str]:
    """Find the highest-volume Solana pool for a token CA. Returns pool address or None."""
    try:
        r = requests.get(
            f"{DEXPAPRIKA_BASE}/networks/solana/tokens/{ca}/pools",
            params={"limit": 1, "order_by": "volume_usd", "sort": "desc"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        pools = r.json().get("pools") or []
        return pools[0]["id"] if pools else None
    except Exception:
        return None


def fetch_ohlcv(pool_address: str, start_iso: str, interval: str = CANDLE_INTERVAL) -> list:
    """Fetch closed candles from `start_iso` onward. Returns [] on any failure."""
    try:
        r = requests.get(
            f"{DEXPAPRIKA_BASE}/networks/solana/pools/{pool_address}/ohlcv",
            params={"start": start_iso, "interval": interval},
            timeout=15,
        )
        if r.status_code != 200:
            return []
        return r.json() or []
    except Exception:
        return []


def new_tracker(size_sol: float, kol_handle: str = "", pool_address: Optional[str] = None) -> dict:
    return {
        "pool_address": pool_address,
        "candles": deque(maxlen=MAX_CANDLES),
        "ema": None,
        "prev_close": None,
        "bar_start": None,
        "bar_o": None, "bar_h": None, "bar_l": None, "bar_c": None,
        "in_position": False,
        "size_sol": size_sol,
        "kol_handle": kol_handle,
        "added_at": time.time(),
    }


def _fold_close(t: dict, close: float) -> Optional[str]:
    """Fold one closed bar's close into EMA state. Returns a signal if produced."""
    t["candles"].append(close)
    closes = list(t["candles"])

    if len(closes) < EMA_PERIOD:
        new_ema = None
    elif len(closes) == EMA_PERIOD:
        new_ema = sum(closes) / EMA_PERIOD
    else:
        new_ema = close * EMA_ALPHA + t["ema"] * (1 - EMA_ALPHA)

    signal = None
    if new_ema is not None:
        if not t["in_position"]:
            if t["ema"] is not None and t["prev_close"] is not None \
               and close > new_ema and t["prev_close"] <= t["ema"]:
                signal = "buy"
        else:
            if close < new_ema:
                signal = "sell"

    t["prev_close"] = close
    t["ema"] = new_ema
    return signal


def backfill(t: dict) -> bool:
    """Seed EMA state from real DexPaprika history, excluding the unreliable
    recent tail. Live ticking (ingest_price) picks up from here. Returns True
    if any candles were folded in."""
    if not t["pool_address"]:
        return False
    now = datetime.now(timezone.utc)
    start = _iso(now - timedelta(minutes=BACKFILL_MINUTES))
    cutoff = _iso(now - timedelta(minutes=SAFETY_MARGIN_MINUTES))
    candles = [c for c in fetch_ohlcv(t["pool_address"], start) if c["time_close"] <= cutoff]
    for c in candles:
        _fold_close(t, c["close"])
    return len(candles) > 0


def ingest_price(t: dict, price: float, now: Optional[float] = None) -> Optional[str]:
    """Feed a live price tick, building 1-minute bars in-process on top of
    whatever EMA state backfill() seeded. Returns a signal if a bar just
    closed and produced one, else None.
    """
    now = now if now is not None else time.time()

    if t["bar_start"] is None:
        t["bar_start"] = now
        t["bar_o"] = t["bar_h"] = t["bar_l"] = t["bar_c"] = price
        return None

    t["bar_h"] = max(t["bar_h"], price)
    t["bar_l"] = min(t["bar_l"], price)
    t["bar_c"] = price

    if now - t["bar_start"] < CANDLE_SEC:
        return None

    signal = _fold_close(t, t["bar_c"])

    # start next bar
    t["bar_start"] = now
    t["bar_o"] = t["bar_h"] = t["bar_l"] = t["bar_c"] = price

    return signal
