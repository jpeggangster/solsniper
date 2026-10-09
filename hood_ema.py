"""
12/21 EMA cross on 5s bars for Robinhood Chain — shared library.
-----------------------------------------------------------------
Tick sources and the cross state machine. hood_sniper_bot drives this for both
entries and exits; hood_ema_backtest.py replays history through the same rules.

LIBRARY ONLY — deliberately has no runner. The previous standalone version
cold-started its EMAs (fast and slow both seeded to the first price), which is
NOT the EMA12/21 a chart computes over real history: from identical seeds the
fast line diverges on a few minutes of local data and can cross up during a
downtrend. It did exactly that on 2026-09-13, buying 0x3dfa…6761 against a
clearly bearish chart and losing 29% in ten seconds. Always arm state with
backfill_state(), never with a bare new_state() on live data.

Ticks come from chain events, not price polls. A 5s bar cannot be built from a
15s DexScreener poll, and on this chain there is no public websocket (both
endpoints refuse the upgrade), so this polls eth_getLogs for new blocks every
POLL_SEC and folds each swap into a bar. Blocks are ~0.1s, so a 2s poll still
resolves every individual trade.

Two venues, because a pons token changes venue when it graduates:
  * pre-graduation  -> the curve's own BUY/SELL events, priced at MID
                       (quoteIn includes the fee, quoteOut is net of it, so raw
                       executed prices carry a synthetic ~2% bid-ask bounce)
  * graduated       -> Uniswap V3 Swap events on the deepest pool, priced from
                       sqrtPriceX96

WHAT THE EVIDENCE ACTUALLY SUPPORTS — read before --live:
On 0x3dfa…6761 over 3.8h and 788 reconstructed trades, 5s 12/21 with NO edge
filter returned +2.07% average net over 23 round trips (+47.6% total) after
1% curve fee/side, creator tax and gas. That is ONE token, which itself did
+1035% over the same window — buy-and-hold beat the strategy roughly 20x — and
20 of 88 swept configs were positive, so some of that ranking is chance.
MIN_EDGE_BPS=15 (the Solana default) turned the same token NEGATIVE by
suppressing entries. Treat the parameters as fitted, not established.

Usage:
  python3.14 hood_ema_live.py --tokens 0x3dfa...6761          # paper
  python3.14 hood_ema_live.py --watchlist hood_ema_watch.json # paper, reloaded live
  python3.14 hood_ema_live.py --tokens 0x... --live --size 0.01
"""

import json
import logging
from eth_abi import encode as _abi_encode
import logging.handlers
import os
import sys
import time
from typing import Optional

from dotenv import load_dotenv
from web3 import Web3

import pons_curve as pc

load_dotenv()

RPC = os.getenv("HOOD_RPC", "https://rpc.mainnet.chain.robinhood.com")
w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 30}))

BUY_TOPIC = "0xec36bf571f136799e8dc0b0b8bea4b04d8bd3d43de838aab0d5fc21d4cbfc455"
SELL_TOPIC = "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df"
def _topic(sig: str) -> str:
    h = w3.keccak(text=sig).hex()
    return h if h.startswith("0x") else "0x" + h


V3_SWAP = _topic("Swap(address,address,int256,int256,uint160,uint128,int24)")
# Pons graduates into Uniswap V4, which is a singleton: there is no per-pool
# contract, so DexScreener reports a 32-byte pool ID where an address would go
# and every swap is emitted by the PoolManager with that ID as topic1.
V4_SWAP = _topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
V4_POOL_MANAGER = w3.to_checksum_address(
    os.getenv("HOOD_V4_POOL_MANAGER", "0x8366a39CC670B4001A1121B8F6A443A643e40951"))

BAR_SEC = int(os.getenv("HOOD_EMA_BAR_SEC", "5"))
FAST = int(os.getenv("HOOD_EMA_FAST", "12"))
SLOW = int(os.getenv("HOOD_EMA_SLOW", "21"))
# Deliberately 0: the Solana default of 15 bps made this token net negative.
MIN_EDGE_BPS = float(os.getenv("HOOD_EMA_MIN_EDGE_BPS", "0"))
POLL_SEC = float(os.getenv("HOOD_EMA_POLL_SEC", "2"))
MAX_GAP_BARS = int(os.getenv("HOOD_EMA_MAX_GAP_BARS", "720"))

CURVE_FEE_BPS = 100.0
GAS_PER_TX_ETH = 0.000051

log = logging.getLogger("hood_ema")


def setup_logging():
    h = [logging.handlers.RotatingFileHandler(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "hood_ema_live.log"),
        maxBytes=20 * 1024 * 1024, backupCount=3)]
    if sys.stdout.isatty():
        h.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s", handlers=h)


# ─── TICK SOURCES ────────────────────────────────────────────────────────────
def _words(lg) -> list[int]:
    d = lg["data"]
    d = d.hex() if hasattr(d, "hex") else d
    d = d[2:] if d.startswith("0x") else d
    return [int(d[i:i + 64], 16) for i in range(0, len(d), 64)]


def _topic0(lg) -> str:
    t = lg["topics"][0].hex()
    return t if t.startswith("0x") else "0x" + t


PONS_HOOK = w3.to_checksum_address(
    os.getenv("HOOD_PONS_HOOK", "0xE5e702641Ea86F4ae6cC3cDaeD2B886f976Be044"))


def derive_v4_pool(token: str):
    """Compute a graduated pons token's V4 pool ID from its launch record.

    poolId = keccak(abi.encode(currency0, currency1, fee, tickSpacing, hooks))
    with currency0/currency1 ordered by address. Returns (poolId, quoteToken).
    """
    try:
        rec = pc.factory(w3).functions.getLaunchedToken(
            w3.to_checksum_address(token)).call()
    except Exception:
        return None
    if not rec[14] or rec[10] != pc.PHASE_POOL_CREATED:
        return None
    tok, quote, fee, spacing = rec[0], rec[4], rec[6], rec[7]
    a, b_ = sorted([tok.lower(), quote.lower()])
    c0, c1 = w3.to_checksum_address(a), w3.to_checksum_address(b_)
    pid = w3.keccak(_abi_encode(["address", "address", "uint24", "int24", "address"],
                                [c0, c1, fee, spacing, PONS_HOOK]))
    pid = pid.hex()
    pid = pid if pid.startswith("0x") else "0x" + pid
    return pid, w3.to_checksum_address(quote)


def deepest_pool(token: str) -> Optional[dict]:
    """Deepest Robinhood-Chain Uniswap pool, for graduated tokens."""
    import requests
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{token}",
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        pairs = [p for p in (r.json().get("pairs") or [])
                 if p.get("chainId") == "robinhood"
                 and len(p.get("pairAddress") or "") in (42, 66)]
    except Exception:
        return None
    if not pairs:
        return None
    pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
    return pairs[0]


class TokenStream:
    """Resolves a token to a venue and yields executed trades as ticks.

    Three venues, because a pons token moves between them:
      pons curve  -> the curve's BUY/SELL events, priced at mid
      uniswap v3  -> Swap on the pool contract
      uniswap v4  -> Swap on the singleton PoolManager, filtered by pool ID
    """

    def __init__(self, token: str):
        self.token = w3.to_checksum_address(token)
        self.info = pc.lookup(w3, self.token)
        self.invert = False

        if self.info and not self.info["graduated"]:
            self.venue = "pons-curve"
            self.address = w3.to_checksum_address(self.info["curve"])
            self.topics = [[BUY_TOPIC, SELL_TOPIC]]
            self.last_block = w3.eth.block_number
            return

        # For a graduated pons token the canonical venue is the locked V4 pool
        # created at graduation. Prefer the derived pool ID over DexScreener,
        # which often indexes a thinner side pool (13 bars of history vs 345
        # swaps on the real one, measured on XL).
        derived = derive_v4_pool(self.token) if self.info else None
        if derived:
            pool_id, quote = derived
            self.venue = "uniswap-v4:pons"
            self.address = V4_POOL_MANAGER
            self.topics = [[V4_SWAP], [pool_id]]
            self.sqrt_word = 2
            self.quote_token = quote
            self.invert = self.token.lower() > quote.lower()
            self.last_block = w3.eth.block_number
            return

        p = deepest_pool(self.token)
        if not p:
            raise RuntimeError("no liquid pool found")
        pool = p.get("pairAddress") or ""
        quote = (p.get("quoteToken") or {}).get("address", "")
        self.quote_token = quote
        self.invert = bool(quote) and self.token.lower() > quote.lower()

        if len(pool) == 42:
            self.venue = f"uniswap-v3:{p.get('dexId')}"
            self.address = w3.to_checksum_address(pool)
            self.topics = [[V3_SWAP]]
            self.sqrt_word = 2
        elif len(pool) == 66:
            self.venue = f"uniswap-v4:{p.get('dexId')}"
            self.address = V4_POOL_MANAGER
            self.topics = [[V4_SWAP], [pool]]
            self.sqrt_word = 2
        else:
            raise RuntimeError(f"unrecognised pool identifier {pool[:24]}…")
        self.last_block = w3.eth.block_number

    def poll(self) -> list[dict]:
        head = w3.eth.block_number
        if head <= self.last_block:
            return []
        ticks = self.fetch(self.last_block + 1, head)
        self.last_block = head
        return ticks

    def fetch(self, frm: int, to: int) -> list[dict]:
        try:
            logs = w3.eth.get_logs({"address": self.address, "fromBlock": frm,
                                    "toBlock": to, "topics": self.topics})
        except Exception as e:
            log.warning(f"{self.token[:10]}… getLogs: {str(e)[:90]}")
            return []

        ticks = []
        for lg in sorted(logs, key=lambda l: (l["blockNumber"], l["logIndex"])):
            wd = _words(lg)
            if self.venue == "pons-curve":
                if len(wd) < 3:
                    continue
                fee = wd[2]
                if _topic0(lg) == BUY_TOPIC:
                    tokens, mid = wd[1], wd[0] - fee
                else:
                    tokens, mid = wd[0], wd[1] + fee
                if tokens <= 0 or mid <= 0:
                    continue
                ticks.append({"price": mid / tokens, "block": lg["blockNumber"]})
            else:
                if len(wd) <= self.sqrt_word:
                    continue
                sqrt_p = wd[self.sqrt_word]
                if sqrt_p <= 0:
                    continue
                price = (sqrt_p / (2 ** 96)) ** 2
                if self.invert:
                    if price == 0:
                        continue
                    price = 1 / price
                ticks.append({"price": price, "block": lg["blockNumber"]})
        return ticks


BLOCK_SEC = 0.1017          # measured on chain 4663


# An EMA seeded at its first value needs roughly 5x its period to converge.
# Measured on chain: with 27 bars of warmup the EMA21 was off by up to 2.33%
# and disagreed with a fully-warmed EMA on bullish-vs-bearish for 23% of bars.
# At 200 bars the disagreement is zero.
BACKFILL_BARS = int(os.getenv("HOOD_EMA_BACKFILL_BARS", "200"))
MIN_SIGNAL_BARS = int(os.getenv("HOOD_EMA_MIN_SIGNAL_BARS", "100"))


def backfill_state(stream, bars: int = None) -> Optional[dict]:
    """Warm the EMAs from chain history instead of waiting SLOW bars in real time.

    Without this a freshly bought position has no automated exit for its first
    ~105 seconds, which is a long time in a memecoin.
    """
    bars = bars or BACKFILL_BARS
    need_blocks = int(bars * BAR_SEC / BLOCK_SEC) + 300
    head = w3.eth.block_number
    ticks = stream.fetch(max(0, head - need_blocks), head)
    if not ticks:
        return None

    b0, b1 = ticks[0]["block"], ticks[-1]["block"]
    if b1 <= b0:
        return None
    try:
        t0 = w3.eth.get_block(b0).timestamp
        rate = (w3.eth.get_block(b1).timestamp - t0) / (b1 - b0)
    except Exception:
        t0, rate = time.time() - (b1 - b0) * BLOCK_SEC, BLOCK_SEC
    for t in ticks:
        t["ts"] = t0 + (t["block"] - b0) * rate

    st = new_state(ticks[0]["price"], ticks[0]["ts"])
    cur = ticks[0]["ts"] + BAR_SEC
    i = 0
    closed = 0
    end = ticks[-1]["ts"]
    while cur <= end:
        while i < len(ticks) and ticks[i]["ts"] <= cur:
            st["bar_close"] = ticks[i]["price"]
            i += 1
        _fold(st, st["bar_close"])
        closed += 1
        cur += BAR_SEC
    # hand over to live ticking from now
    st["bar_end"] = time.time() + BAR_SEC
    stream.last_block = head
    st["backfilled_bars"] = closed
    return st


# ─── STRATEGY ────────────────────────────────────────────────────────────────
def new_state(price: float, now: float) -> dict:
    return {"ema_fast": None, "ema_slow": None, "prev_diff": None,
            "bar_end": now + BAR_SEC, "bar_close": price,
            "bars": 0, "want_long": False, "entry": None, "trades": []}


def _fold(st: dict, close: float) -> Optional[str]:
    fa, sa = 2 / (FAST + 1), 2 / (SLOW + 1)
    st["ema_fast"] = close if st["ema_fast"] is None else close * fa + st["ema_fast"] * (1 - fa)
    st["ema_slow"] = close if st["ema_slow"] is None else close * sa + st["ema_slow"] * (1 - sa)
    st["bars"] += 1
    diff = st["ema_fast"] - st["ema_slow"]
    prev = st["prev_diff"]
    st["prev_diff"] = diff
    if st["bars"] < max(SLOW + 1, MIN_SIGNAL_BARS) or prev is None:
        return None      # not converged yet — its own error would exceed the signal
    edge_ok = (abs(diff) / close * 10_000) >= MIN_EDGE_BPS if close else False
    if not st["want_long"] and prev <= 0 < diff and edge_ok:
        return "enter"
    if st["want_long"] and prev >= 0 > diff:
        return "exit"
    return None


def ingest(st: dict, ticks: list[dict], now: float) -> list[str]:
    """Fold ticks into wall-clock 5s bars. Empty bars repeat the last close so a
    quiet token still advances its EMAs instead of freezing the indicator."""
    signals = []
    for t in ticks:
        st["bar_close"] = t["price"]
    filled = 0
    while now >= st["bar_end"] and filled < MAX_GAP_BARS:
        sig = _fold(st, st["bar_close"])
        if sig:
            signals.append(sig)
        st["bar_end"] += BAR_SEC
        filled += 1
    return signals


