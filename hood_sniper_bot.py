"""
Telegram Robinhood-Chain Memecoin Sniper Bot
---------------------------------------------
Monitors Telegram chats for Robinhood Chain (HOOD, chainId 4663) token
addresses and auto-buys them with ETH via the KyberSwap aggregator —
the same tokens Axiom lists under its HOOD chain tab.

There is NO automatic take-profit and NO automatic stop-loss, by design.
The bot buys and holds. You close positions yourself with /sell or /sellall
sent to the notify bot in Telegram.

Robinhood Wallet has no trading API — it is a self-custody wallet. This bot
trades that wallet by loading its exported private key and swapping directly
on-chain. The Robinhood app is never in the loop.

Tokens are routed by venue, because a fresh pons launch is invisible to both
DexScreener and the aggregator until it graduates:

  pons v2 curve (pre-graduation)  -> curve.buy() / curve.sell()   [pons_curve.py]
  graduated / everything else     -> KyberSwap aggregator

Execution path (all endpoints verified live):
  DexScreener  chainId "robinhood"                  -> price / liquidity
  KyberSwap    aggregator-api.kyberswap.com/robinhood -> route + calldata
  Pons factory 0x7eD598...EC7e                       -> curve lookup + trade
  RPC          rpc.mainnet.chain.robinhood.com       -> sign + broadcast

SETUP:
  /opt/homebrew/bin/python3.14 -m pip install --break-system-packages web3 telethon requests python-dotenv

Add to .env (alongside your existing sniper keys):
  TELEGRAM_API_ID=...            (already set)
  TELEGRAM_API_HASH=...          (already set)
  NOTIFY_BOT_TOKEN=...           (already set)
  NOTIFY_CHAT_ID=...             (already set)
  HOOD_PRIVATE_KEY=0x...         <- Robinhood Wallet key, EXPORTED FROM THE APP
  HOOD_MONITOR_CHATS=-100123,... (comma-separated numeric group IDs)
  HOOD_ALLOWED_SENDERS=Poopsy0x,alonzocooks   (only these users can trigger a buy;
                                               blank = anyone in the monitored chats)
  HOOD_BUY_ETH=0.01              (ETH per trade)
  HOOD_MAX_ETH_PER_TRADE=0.05    (hard safety cap)
  HOOD_MIN_LIQ_USD=15000         (skip tokens with thinner pools)
  HOOD_MIN_VOL24_USD=25000       (skip tokens with no real 24h volume)
  HOOD_SLIPPAGE_BPS=500          (500 = 5%)
  HOOD_PRIORITY_FEE_GWEI=0       (Orbit sequencer is FCFS — a tip buys nothing)
  HOOD_MAX_TX_FEE_ETH=0.001      (hard ceiling per tx; aborts rather than overpay)
  HOOD_DRY_RUN=true              <- SET TO false WHEN YOU WANT IT TO SPEND REAL ETH
  HOOD_PONS_ETH_ONLY=true        (skip pons launches quoted in anything but ETH)
  HOOD_MAX_ENTRY_TAX_BPS=1500    (measured buy tax ceiling; snipe tax starts at 9900)
  HOOD_MAX_CREATOR_TAX_BPS=1000  (per-launch creator tax ceiling)
  HOOD_MIN_CURVE_ETH=0.05        (min real ETH in a pons curve before buying)
  HOOD_MAX_GAS_GWEI=2.0          (hard cap on gas price; TX aborted if network price×1.25 exceeds this)

Run:
  nohup /opt/homebrew/bin/python3.14 /Users/jpreddy/sniper/hood_sniper_bot.py \
      >> /Users/jpreddy/sniper/hood_sniper.log 2>&1 &
"""

import asyncio
import json
import logging
import logging.handlers
import os
import re
import shutil
import sys
import threading
import time
from typing import Optional

import requests
from dotenv import load_dotenv
from eth_account import Account
from telethon import TelegramClient, events
from web3 import Web3

import pons_curve as pons

load_dotenv()

# Only the file handler writes hood_sniper.log. Under launchd/nohup stdout is
# redirected to that same file, so a StreamHandler would write every line twice
# (it did: 239 MB in six days). The console handler is therefore attached only
# when running interactively, and the file is size-capped.
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hood_sniper.log")
_handlers = [logging.handlers.RotatingFileHandler(
    _LOG_PATH, maxBytes=int(os.getenv("HOOD_LOG_MAX_MB", "20")) * 1024 * 1024,
    backupCount=int(os.getenv("HOOD_LOG_BACKUPS", "3")))]
if sys.stdout.isatty():
    _handlers.append(logging.StreamHandler())

logging.basicConfig(
    level=getattr(logging, os.getenv("HOOD_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
log = logging.getLogger(__name__)
logging.getLogger("telethon").setLevel(logging.WARNING)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_API_ID   = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
NOTIFY_BOT_TOKEN  = os.getenv("NOTIFY_BOT_TOKEN", "")
NOTIFY_CHAT_ID    = os.getenv("NOTIFY_CHAT_ID", "")

HOOD_PRIVATE_KEY   = os.getenv("HOOD_PRIVATE_KEY", "")
HOOD_RPC           = os.getenv("HOOD_RPC", "https://rpc.mainnet.chain.robinhood.com")
MONITOR_CHATS      = os.getenv("HOOD_MONITOR_CHATS", "")   # blank = all chats
# Only these senders can trigger a buy. Blank = anyone in the monitored chats.
ALLOWED_SENDERS    = os.getenv("HOOD_ALLOWED_SENDERS", "")

BUY_AMOUNT_ETH     = float(os.getenv("HOOD_BUY_ETH", "0.01"))
MAX_ETH_PER_TRADE  = float(os.getenv("HOOD_MAX_ETH_PER_TRADE", "0.05"))
MIN_LIQ_USD        = float(os.getenv("HOOD_MIN_LIQ_USD", "15000"))
MIN_VOL24_USD      = float(os.getenv("HOOD_MIN_VOL24_USD", "25000"))
SLIPPAGE_BPS       = int(os.getenv("HOOD_SLIPPAGE_BPS", "500"))
DRY_RUN            = os.getenv("HOOD_DRY_RUN", "true").lower() != "false"

# Pons v2 bonding-curve launches (pre-graduation). See pons_curve.py.
PONS_ETH_ONLY       = os.getenv("HOOD_PONS_ETH_ONLY", "true").lower() != "false"
MAX_ENTRY_TAX_BPS   = int(os.getenv("HOOD_MAX_ENTRY_TAX_BPS", "1500"))
MAX_CREATOR_TAX_BPS = int(os.getenv("HOOD_MAX_CREATOR_TAX_BPS", "1000"))
MIN_CURVE_ETH       = float(os.getenv("HOOD_MIN_CURVE_ETH", "0.05"))
SNIPE_TAX_RETRIES   = int(os.getenv("HOOD_SNIPE_TAX_RETRIES", "4"))
SNIPE_TAX_WAIT_SEC  = float(os.getenv("HOOD_SNIPE_TAX_WAIT_SEC", "2"))


# Exits are the 12/21 EMA cross on 5s bars (hood_ema_live), not a TP ladder.
# There is no take-profit, no trailing stop, no stop-loss and no moon bag: the
# position is held until the fast EMA closes below the slow one, then sold in
# full. Entries still come from the Telegram allowlist.
#
# The cross needs SLOW bars to warm up (21 x 5s = 105s) before it can fire, so a
# freshly bought position has no automated exit for its first ~2 minutes.
# Tiered take-profit ladder. First rung sells TP_FIRST_FRAC at +TP_FIRST_PCT,
# then every further +TP_STEP_PCT sells TP_STEP_FRAC. Fractions are of the
# CURRENT remaining balance, not of the original position, so the ladder never
# runs out of tokens. There is deliberately NO stop-loss: it sells into
# strength only, and a token that falls is held until you /sell it.
TP_ENABLED    = os.getenv("HOOD_TP_ENABLED", "true").lower() != "false"
TP_FIRST_PCT  = float(os.getenv("HOOD_TP_FIRST_PCT", "100"))
TP_FIRST_FRAC = float(os.getenv("HOOD_TP_FIRST_FRAC", "50")) / 100.0
TP_STEP_PCT   = float(os.getenv("HOOD_TP_STEP_PCT", "100"))
TP_STEP_FRAC  = float(os.getenv("HOOD_TP_STEP_FRAC", "10")) / 100.0
TP_MAX_TIER   = int(os.getenv("HOOD_TP_MAX_TIER", "50"))
TP_POLL_SEC   = int(os.getenv("HOOD_TP_POLL_SEC", "15"))
HEARTBEAT_MIN = float(os.getenv("HOOD_HEARTBEAT_MIN", "10"))

# Connection supervision. Telethon reconnects on its own, but a hard drop ends
# run_until_disconnected() and would otherwise kill the process silently.
WATCHDOG_POLL_SEC   = int(os.getenv("HOOD_WATCHDOG_POLL_SEC", "15"))
MAX_DISCONNECT_SEC  = int(os.getenv("HOOD_MAX_DISCONNECT_SEC", "60"))
RECONNECT_DELAY_SEC = int(os.getenv("HOOD_RECONNECT_DELAY_SEC", "5"))

# Buys and take-profit sells run in the same executor pool, so two transactions
# could be built concurrently, read the same "pending" nonce, and collide —
# which is exactly what killed the INFINITI buy ("nonce too low: tx 57 state 58").
# Every send is serialised and re-nonced on collision.
NONCE_RETRIES  = int(os.getenv("HOOD_NONCE_RETRIES", "3"))
REQUOTE_TRIES  = int(os.getenv("HOOD_REQUOTE_TRIES", "2"))
_TX_LOCK = threading.Lock()

CHAIN_ID        = 4663
KYBER_BASE      = "https://aggregator-api.kyberswap.com/robinhood/api/v1"
DEXSCREENER     = "https://api.dexscreener.com/latest/dex/tokens"
DEXSCREENER_CHAIN = "robinhood"
NATIVE_ETH      = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"  # Kyber native sentinel
GAS_LIMIT_BUFFER  = 1.3
GAS_PRICE_BUFFER  = 1.25

# Fees. Robinhood Chain is an Arbitrum Orbit rollup: the sequencer orders by
# arrival time, not by fee, and the chain reports max_priority_fee = 0. A tip
# buys no position here the way PRIORITY_FEE_SOL does on Solana, so the default
# is 0 and MAX_TX_FEE_ETH acts as a ceiling that aborts an overpriced send
# (a full swap currently costs ~0.00005 ETH, so 0.001 is ~20x headroom).
PRIORITY_FEE_GWEI = float(os.getenv("HOOD_PRIORITY_FEE_GWEI", "0"))
MAX_TX_FEE_ETH    = float(os.getenv("HOOD_MAX_TX_FEE_ETH", "0.001"))
_max_gwei         = float(os.getenv("HOOD_MAX_GAS_GWEI", "2.0"))
MAX_GAS_PRICE_WEI = int(_max_gwei * 1e9)   # convert to wei for direct comparison
DEDUPE_WINDOW_SEC = 300     # ignore the same token reposted within this window
RECEIPT_TIMEOUT   = 180

# Addresses that show up constantly in chat but are never the thing to buy.
BLOCKED = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0x0bd7d308f8e1639fab988df18a8011f41eacad73",  # WETH
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168",  # USDG
    "0x6131b5fae19ea4f9d964eac0408e4408b66337b5",  # KyberSwap router
    "0x000000000022d473030f116ddee9f6b43ac78ba3",  # Permit2
    "0x8876789976decbfcbbbe364623c63652db8c0904",  # Uniswap UniversalRouter
    "0xcaf681a66d020601342297493863e78c959e5cb2",  # Uniswap SwapRouter02
}

ADDR_PATTERN = re.compile(r"\b0x[a-fA-F0-9]{40}\b")

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
POSITIONS_FILE = os.path.join(BASE_DIR, "hood_positions.json")
SEEN_FILE      = os.path.join(BASE_DIR, "hood_seen.json")
# Manual watches. Anything listed here is watched for an EMA cross-up REGARDLESS
# of the liquidity/volume gates — an explicit override, so the gates still guard
# everything that arrives automatically from Telegram.
WATCHLIST_FILE = os.path.join(BASE_DIR, "hood_watchlist.json")
SESSION_NAME   = os.path.join(BASE_DIR, "hood_session")
SOLANA_SESSION = os.path.join(BASE_DIR, "sniper_session.session")

ERC20_ABI = [
    {"name": "decimals",  "inputs": [], "outputs": [{"type": "uint8"}],  "stateMutability": "view", "type": "function"},
    {"name": "symbol",    "inputs": [], "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
    {"name": "balanceOf", "inputs": [{"name": "a", "type": "address"}], "outputs": [{"type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"name": "allowance", "inputs": [{"name": "o", "type": "address"}, {"name": "s", "type": "address"}],
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"name": "approve",   "inputs": [{"name": "s", "type": "address"}, {"name": "v", "type": "uint256"}],
     "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
]

MAX_UINT = 2 ** 256 - 1
MAX_UINT160 = 2 ** 160 - 1

# Uniswap's Permit2. Kyber's V4 calldata pulls tokens through this rather than
# through a direct router allowance, so approving only the router produces
# "TransferHelper: TRANSFER_FROM_FAILED" on every sell — which is exactly what
# trapped the 咿 position on 2026-09-13 for 45 minutes across four exit attempts.
PERMIT2 = "0x000000000022D473030F116dDEE9F6B43aC78BA3"
PERMIT2_ABI = [
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "address", "name": "token"}, {"type": "address", "name": "spender"},
                {"type": "uint160", "name": "amount"}, {"type": "uint48", "name": "expiration"}],
     "outputs": []},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address", "name": "user"}, {"type": "address", "name": "token"},
                {"type": "address", "name": "spender"}],
     "outputs": [{"type": "uint160", "name": "amount"}, {"type": "uint48", "name": "expiration"},
                 {"type": "uint48", "name": "nonce"}]},
]

# ─── WEB3 / WALLET ────────────────────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(HOOD_RPC, request_kwargs={"timeout": 30}))
ACCT = Account.from_key(HOOD_PRIVATE_KEY) if HOOD_PRIVATE_KEY else None

HTTP = requests.Session()
HTTP.headers.update({
    # Kyber's edge rejects default python user-agents with a 403.
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "x-client-id": "hood-sniper",
})

# ─── STATE ────────────────────────────────────────────────────────────────────
def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"Failed to load {path}: {e}")
    return default


def save_positions():
    try:
        clean = {k: {ik: iv for ik, iv in v.items() if not ik.startswith("_")}
                 for k, v in positions.items()}
        with open(POSITIONS_FILE, "w") as f:
            json.dump(clean, f, indent=2)
    except Exception as e:
        log.warning(f"Failed to save positions: {e}")


def save_seen():
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(sorted(already_seen), f)
    except Exception as e:
        log.warning(f"Failed to save seen: {e}")


positions: dict = _load_json(POSITIONS_FILE, {})
already_seen: set = set(_load_json(SEEN_FILE, []))
_last_trigger: dict = {}
PAUSED = False

# ─── NOTIFY ───────────────────────────────────────────────────────────────────
def notify(msg: str):
    if not NOTIFY_BOT_TOKEN or not NOTIFY_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/sendMessage",
            json={"chat_id": NOTIFY_CHAT_ID, "text": msg, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Notify failed: {e}")


# ─── PRICE / VETTING (DexScreener, chainId "robinhood") ──────────────────────
# Robinhood Chain is full of spoofed pools that report enormous fake liquidity
# on near-zero volume (one CASHCAT pool advertised $2.1B liquidity, $51 of 24h
# volume, and a price 27 orders of magnitude off). Picking the deepest pool
# blindly walks straight into those, so every price is anchored to the median
# across pools and outliers are discarded before anything is used.
PRICE_OUTLIER_FACTOR = 5.0


_PAIRS_TTL = float(os.getenv("HOOD_PAIRS_TTL_SEC", "10"))
_pairs_cache: dict = {}


def _robinhood_pairs(token: str) -> list[dict]:
    """Robinhood-Chain pairs for a token, with price outliers stripped out.

    Cached briefly: a single take-profit poll asks for the same token's pairs
    three times (price, pair, market cap), which tripled both the DexScreener
    traffic and the log volume.
    """
    hit = _pairs_cache.get(token)
    if hit and time.time() - hit[0] < _PAIRS_TTL:
        return hit[1]
    try:
        r = HTTP.get(f"{DEXSCREENER}/{token}", timeout=12)
        raw = (r.json() or {}).get("pairs") or []
    except Exception as e:
        log.warning(f"DexScreener lookup failed for {token}: {e}")
        return []


    pairs = []
    for p in raw:
        if p.get("chainId") != DEXSCREENER_CHAIN:
            continue
        try:
            price = float(p.get("priceUsd"))
        except (TypeError, ValueError):
            continue
        if price > 0:
            p["_price"] = price
            pairs.append(p)
    if not pairs:
        _pairs_cache[token] = (time.time(), [])
        return []

    prices = sorted(p["_price"] for p in pairs)
    median = prices[len(prices) // 2]
    sane = [
        p for p in pairs
        if median / PRICE_OUTLIER_FACTOR <= p["_price"] <= median * PRICE_OUTLIER_FACTOR
    ]
    dropped = len(pairs) - len(sane)
    if dropped:
        log.debug(f"{token}: ignored {dropped} pool(s) with outlier prices (median ${median})")
    sane.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
    _pairs_cache[token] = (time.time(), sane)
    return sane


def get_pair(token: str) -> Optional[dict]:
    """Deepest credible Robinhood-Chain pair, or None if it isn't trading there."""
    pairs = _robinhood_pairs(token)
    return pairs[0] if pairs else None


def get_price_usd(token: str) -> Optional[float]:
    """Median price across credible pools — robust to a single spoofed pool.

    Falls back to the bonding curve for pre-graduation pons tokens, which have
    no DexScreener listing yet.
    """
    pairs = _robinhood_pairs(token)
    if pairs:
        prices = sorted(p["_price"] for p in pairs)
        return prices[len(prices) // 2]
    info = pons_route(token)
    return curve_price_usd(info) if info else None


WETH_ADDR = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
USDG_ADDR = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"
_eth_usd_cache = {"value": None, "at": 0.0}


def eth_usd() -> Optional[float]:
    """ETH price in USD, cached for a minute. Needed to price curve tokens,
    which are quoted in ETH and have no DexScreener entry at all."""
    if _eth_usd_cache["value"] and time.time() - _eth_usd_cache["at"] < 60:
        return _eth_usd_cache["value"]
    for pair in _robinhood_pairs(WETH_ADDR):
        if (pair.get("baseToken") or {}).get("address", "").lower() == WETH_ADDR.lower():
            _eth_usd_cache.update(value=pair["_price"], at=time.time())
            return pair["_price"]
    return _eth_usd_cache["value"]


def pons_route(token: str) -> Optional[dict]:
    """Pons launch record if this token still trades on its bonding curve.

    Returns None for non-pons tokens and for graduated ones — both of those go
    through the aggregator instead.
    """
    info = pons.lookup(w3, token)
    if not info or info["graduated"]:
        return None
    return info


def curve_price_usd(info: dict) -> Optional[float]:
    st = pons.state(w3, info["curve"])
    if not st or not st["price_quote"]:
        return None
    if info["native_eth"]:
        usd = eth_usd()
        return st["price_quote"] * usd if usd else None
    if info["pair_token"].lower() == USDG_ADDR.lower():
        return st["price_quote"]          # USDG is pegged to $1
    return None


def is_contract(token: str) -> bool:
    try:
        return w3.eth.get_code(w3.to_checksum_address(token)) not in (b"", "0x")
    except Exception:
        return False


def erc20(token: str):
    return w3.eth.contract(address=w3.to_checksum_address(token), abi=ERC20_ABI)


def token_symbol(token: str) -> str:
    try:
        return erc20(token).functions.symbol().call()
    except Exception:
        return token[:10]


def token_balance(token: str) -> int:
    try:
        return erc20(token).functions.balanceOf(ACCT.address).call()
    except Exception as e:
        log.warning(f"balanceOf failed for {token}: {e}")
        return 0


# ─── KYBERSWAP AGGREGATOR ────────────────────────────────────────────────────
def kyber_route(token_in: str, token_out: str, amount_in_wei: int) -> Optional[dict]:
    try:
        r = HTTP.get(
            f"{KYBER_BASE}/routes",
            params={"tokenIn": token_in, "tokenOut": token_out, "amountIn": str(amount_in_wei)},
            timeout=20,
        )
        body = r.json()
        if r.status_code != 200 or body.get("code") != 0:
            log.error(f"Kyber route error {r.status_code}: {str(body)[:300]}")
            return None
        return body["data"]["routeSummary"]
    except Exception as e:
        log.error(f"Kyber route failed: {e}")
        return None


def kyber_build(route_summary: dict) -> Optional[dict]:
    try:
        r = HTTP.post(
            f"{KYBER_BASE}/route/build",
            json={
                "routeSummary": route_summary,
                "sender": ACCT.address,
                "recipient": ACCT.address,
                "slippageTolerance": SLIPPAGE_BPS,
                "source": "hood-sniper",
            },
            timeout=20,
        )
        body = r.json()
        if r.status_code != 200 or body.get("code") != 0:
            log.error(f"Kyber build error {r.status_code}: {str(body)[:300]}")
            return None
        return body["data"]
    except Exception as e:
        log.error(f"Kyber build failed: {e}")
        return None


# ─── TX SENDING ───────────────────────────────────────────────────────────────
def apply_fees(tx: dict) -> tuple[bool, str]:
    """Set EIP-1559 fees and refuse to send if the ceiling would be breached."""
    try:
        base = w3.eth.get_block("latest").get("baseFeePerGas") or w3.eth.gas_price
    except Exception:
        base = w3.eth.gas_price
    tip = int(PRIORITY_FEE_GWEI * 1e9)
    max_fee = int(base * 2 * GAS_PRICE_BUFFER) + tip

    tx.pop("gasPrice", None)
    tx["maxFeePerGas"] = max_fee
    tx["maxPriorityFeePerGas"] = tip
    tx["type"] = 2

    worst = max_fee * int(tx.get("gas") or 0) / 1e18
    if worst > MAX_TX_FEE_ETH:
        return False, (f"worst-case fee {worst:.6f} ETH exceeds ceiling "
                       f"{MAX_TX_FEE_ETH} ETH (gas {tx.get('gas')}, "
                       f"maxFee {max_fee/1e9:.4f} gwei)")
    return True, ""


def preflight(tx: dict) -> tuple[bool, str]:
    """eth_call the exact transaction before broadcasting.

    A revert caught here costs nothing; broadcasting it costs the full gas. The
    CLANKER buy burned 597k gas to find out the router would reject it.
    """
    call = {"from": tx["from"], "to": tx["to"], "data": tx["data"],
            "value": hex(int(tx.get("value", 0)))}   # fees omitted on purpose
    try:
        r = w3.provider.make_request("eth_call", [call, "latest"])
    except Exception as e:
        return True, f"preflight unavailable ({e})"      # don't block on RPC hiccups
    if "error" in r:
        return False, str(r["error"].get("message", r["error"]))[:200]
    return True, ""


def send_tx(tx: dict, skip_preflight: bool = False) -> Optional[str]:
    """Sign, broadcast and await a receipt. Serialised so nonces can't collide."""
    with _TX_LOCK:
        for attempt in range(1, NONCE_RETRIES + 1):
            try:
                tx["chainId"] = CHAIN_ID
                tx["nonce"] = w3.eth.get_transaction_count(ACCT.address, "pending")
                tx.setdefault("gasPrice", int(w3.eth.gas_price * GAS_PRICE_BUFFER))
                if tx["gasPrice"] > MAX_GAS_PRICE_WEI:
                    gwei = tx["gasPrice"] / 1e9
                    log.warning(f"Gas price {gwei:.4f} gwei exceeds cap {_max_gwei} gwei — TX aborted")
                    notify(f"⛔ TX aborted — gas {gwei:.4f} gwei > cap {_max_gwei} gwei (set HOOD_MAX_GAS_GWEI to raise)")
                    return None
                if "gas" not in tx:
                    tx["gas"] = int(w3.eth.estimate_gas(tx) * GAS_LIMIT_BUFFER)

                if not skip_preflight:
                    ok, why = preflight(tx)
                    if not ok:
                        log.error(f"❌ Preflight says this would revert — not sending: {why}")
                        return None

                signed = w3.eth.account.sign_transaction(tx, HOOD_PRIVATE_KEY)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            except Exception as e:
                msg = str(e).lower()
                if any(k in msg for k in ("nonce too low", "already known",
                                          "replacement transaction underpriced")):
                    log.warning(f"Nonce collision (attempt {attempt}/{NONCE_RETRIES}): {e}")
                    tx.pop("nonce", None)
                    time.sleep(1.0)
                    continue
                log.error(f"TX failed: {e}")
                return None

            h = tx_hash.hex()
            if not h.startswith("0x"):
                h = "0x" + h
            log.info(f"TX sent: https://robinhoodchain.blockscout.com/tx/{h}")
            try:
                receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=RECEIPT_TIMEOUT)
            except Exception as e:
                log.error(f"No receipt for {h}: {e}")
                return None
            if receipt.status != 1:
                log.error(f"TX reverted on chain: {h} (gas {receipt.gasUsed}/{tx['gas']})")
                return None
            return h

        log.error(f"Gave up after {NONCE_RETRIES} nonce collisions")
        return None


def _permit2_ready(token: str, spender: str, needed: int) -> bool:
    """Approve the Permit2 path: ERC20 -> Permit2, then Permit2 -> router."""
    p2 = w3.to_checksum_address(PERMIT2)
    spender = w3.to_checksum_address(spender)
    try:
        c = erc20(token)
        if c.functions.allowance(ACCT.address, p2).call() < needed:
            log.info(f"Approving {token_symbol(token)} -> Permit2...")
            tx = c.functions.approve(p2, MAX_UINT).build_transaction(
                {"from": ACCT.address,
                 "nonce": w3.eth.get_transaction_count(ACCT.address, "pending"),
                 "chainId": CHAIN_ID})
            for k in ("gasPrice", "maxFeePerGas", "maxPriorityFeePerGas", "gas"):
                tx.pop(k, None)
            if send_tx(tx) is None:
                return False

        pc = w3.eth.contract(address=p2, abi=PERMIT2_ABI)
        amount, expiry, _ = pc.functions.allowance(ACCT.address, w3.to_checksum_address(token),
                                                   spender).call()
        if amount >= needed and expiry > time.time() + 300:
            return True

        log.info(f"Approving Permit2 -> router for {token_symbol(token)}...")
        exp = int(time.time()) + 365 * 24 * 3600
        tx = pc.functions.approve(w3.to_checksum_address(token), spender,
                                  MAX_UINT160, exp).build_transaction(
            {"from": ACCT.address,
             "nonce": w3.eth.get_transaction_count(ACCT.address, "pending"),
             "chainId": CHAIN_ID})
        for k in ("gasPrice", "maxFeePerGas", "maxPriorityFeePerGas", "gas"):
            tx.pop(k, None)
        return send_tx(tx) is not None
    except Exception as e:
        log.error(f"Permit2 approval failed for {token}: {e}")
        return False


def ensure_allowance(token: str, spender: str, needed: int) -> bool:
    """Make the router able to pull `needed` tokens, by either mechanism.

    Both paths are approved because which one a route uses is not visible from
    the quote: V2/V3 routes pull on a direct allowance, V4 routes pull through
    Permit2. Gas here is ~$0.13, so covering both once per token is cheap
    insurance against an exit that cannot execute.
    """
    try:
        c = erc20(token)
        spender_cs = w3.to_checksum_address(spender)
        if c.functions.allowance(ACCT.address, spender_cs).call() < needed:
            log.info(f"Approving {token_symbol(token)} -> router {spender_cs}...")
            tx = c.functions.approve(spender_cs, MAX_UINT).build_transaction(
                {"from": ACCT.address,
                 "nonce": w3.eth.get_transaction_count(ACCT.address, "pending"),
                 "chainId": CHAIN_ID})
            for k in ("gasPrice", "maxFeePerGas", "maxPriorityFeePerGas", "gas"):
                tx.pop(k, None)
            if send_tx(tx) is None:
                return False
    except Exception as e:
        log.error(f"Router approval failed for {token}: {e}")
        return False

    return _permit2_ready(token, spender, needed)


# ─── BUY / SELL ───────────────────────────────────────────────────────────────
def _open_position(token, symbol, venue, eth_amount, received, tx_hash, source, curve=None):
    positions[token] = {
        "symbol": symbol,
        "venue": venue,
        "curve": curve,
        "tokens": received,
        "entry_eth": eth_amount,
        "entry_price_usd": get_price_usd(token),
        "buy_time": time.time(),
        "source": source,
        "buy_tx": tx_hash,
        "peak_multiple": 1.0,
        "ema": None,
    }
    save_positions()
    p = positions[token]
    log.info(f"✅ Position opened on {venue}: {symbol} @ ${p['entry_price_usd']}")
    notify(
        f"🟢 <b>BUY</b> — {symbol} <i>({venue})</i>\n<code>{token}</code>\n"
        f"Spent: {eth_amount} ETH\nEntry: ${p['entry_price_usd']}\n"
        f"TP ladder armed — no stop-loss; close early with /sell {token}\n"
        f"🔗 robinhoodchain.blockscout.com/tx/{tx_hash}"
    )


def _buy_pons(token, symbol, amount_wei, eth_amount, info, source) -> bool:
    """Buy on the bonding curve, waiting out the snipe-tax window if needed."""
    curve_addr = info["curve"]
    is_usdg = info["pair_token"].lower() == USDG_ADDR.lower()

    if is_usdg:
        # USDG-quoted curve: convert ETH budget to a USDG amount.
        usd = eth_usd()
        if not usd:
            notify(f"❌ Cannot price ETH for USDG buy of {symbol}")
            return False
        quote_wei = int(eth_amount * usd * 1e18)

        # eth_call simulation with ERC-20 allowance state is not straightforward,
        # so we clear the snipe window by sleeping past it, then use the
        # constant-product formula to derive min_out.
        _, snipe_sec = pons.snipe_tax_params(w3)
        sleep_sec = int(snipe_sec) + 2
        log.info(f"{symbol}: USDG curve — sleeping {sleep_sec}s to clear snipe window")
        time.sleep(sleep_sec)

        st = pons.state(w3, curve_addr)
        if not st or not st["quote_reserve"] or not st["token_reserve"]:
            notify(f"❌ Could not read curve state for {symbol}")
            return False
        q, t = st["quote_reserve"], st["token_reserve"]
        ideal = t - (q * t) // (q + quote_wei)
        if ideal <= 0:
            notify(f"❌ Curve math failed for {symbol} (zero ideal output)")
            return False
        min_out = int(ideal * (1 - SLIPPAGE_BPS / 10_000))

        if DRY_RUN:
            log.info(f"[DRY RUN] would approve USDG and buy {symbol} on pons curve: "
                     f"~{ideal/1e18:,.0f} tokens")
            notify(f"🧪 <b>DRY RUN</b> — would buy {symbol} on <b>pons curve (USDG)</b>\n"
                   f"<code>{token}</code>\n"
                   f"{eth_amount} ETH ≈ {quote_wei/1e18:,.2f} USDG → ~{ideal/1e18:,.0f} tokens\n"
                   f"Set HOOD_DRY_RUN=false to trade live.")
            return False

        if not ensure_allowance(USDG_ADDR, curve_addr, quote_wei):
            notify(f"❌ USDG approval failed for {symbol}")
            return False

        before = token_balance(token)
        tx = pons.build_buy_tx(w3, curve_addr, quote_wei, min_out,
                               ACCT.address, ACCT.address, native_eth=False)
        tx_hash = send_tx(tx)
        if not tx_hash:
            notify(f"❌ <b>BUY FAILED</b> — {symbol} (pons curve / USDG)\n<code>{token}</code>")
            return False

        _open_position(token, symbol, "pons/usdg", eth_amount,
                       token_balance(token) - before, tx_hash, source, curve=curve_addr)
        return True

    # ── ETH-quoted curve (original path) ──────────────────────────────────────
    quote_wei = amount_wei

    # The factory starts the snipe tax at 9900 bps and decays it over 3s. Rather
    # than racing a timestamp, measure the real cost and wait for it to fall.
    tax = None
    for attempt in range(1, SNIPE_TAX_RETRIES + 1):
        tax = pons.effective_tax_bps(w3, curve_addr, quote_wei, ACCT.address)
        if tax is None:
            notify(f"❌ Curve buy would revert for {symbol}")
            return False
        if tax <= MAX_ENTRY_TAX_BPS:
            break
        log.info(f"{symbol}: entry tax {tax/100:.1f}% > {MAX_ENTRY_TAX_BPS/100:.1f}% "
                 f"(snipe window?) — retry {attempt}/{SNIPE_TAX_RETRIES} in {SNIPE_TAX_WAIT_SEC}s")
        time.sleep(SNIPE_TAX_WAIT_SEC)
    else:
        shown = f"{tax/100:.1f}%" if tax is not None else "unknown"
        notify(f"⛔ Skipped {symbol} — entry tax stayed at {shown} "
               f"(cap {MAX_ENTRY_TAX_BPS/100:.1f}%)")
        return False

    expected = pons.simulate_buy(w3, curve_addr, quote_wei, ACCT.address)
    if not expected:
        notify(f"❌ Curve simulation failed for {symbol}")
        return False
    min_out = int(expected * (1 - SLIPPAGE_BPS / 10_000))

    if DRY_RUN:
        usd = eth_usd()
        log.info(f"[DRY RUN] would buy {symbol} on pons curve: {expected/1e18:,.0f} tokens, tax {tax/100:.1f}%")
        notify(f"🧪 <b>DRY RUN</b> — would buy {symbol} on <b>pons curve</b>\n<code>{token}</code>\n"
               f"{eth_amount} ETH -> {expected/1e18:,.0f} tokens"
               f"{f' (~${eth_amount*usd:,.2f})' if usd else ''}\n"
               f"Entry tax: {tax/100:.1f}%\nSet HOOD_DRY_RUN=false to trade live.")
        return False

    before = token_balance(token)
    tx = pons.build_buy_tx(w3, curve_addr, quote_wei, min_out, ACCT.address, ACCT.address)
    tx_hash = send_tx(tx)
    if not tx_hash:
        notify(f"❌ <b>BUY FAILED</b> — {symbol} (pons curve)\n<code>{token}</code>")
        return False

    _open_position(token, symbol, "pons", eth_amount, token_balance(token) - before,
                   tx_hash, source, curve=curve_addr)
    return True


def _buy_kyber(token, symbol, amount_wei, eth_amount, source) -> bool:
    built = None
    for attempt in range(1, REQUOTE_TRIES + 1):
        route = kyber_route(NATIVE_ETH, token, amount_wei)
        if not route:
            notify(f"❌ No route for {symbol} <code>{token}</code>")
            return False
        candidate = kyber_build(route)
        if not candidate:
            notify(f"❌ Route build failed for {symbol}")
            return False
        probe = {"from": ACCT.address,
                 "to": w3.to_checksum_address(candidate["routerAddress"]),
                 "data": candidate["data"],
                 "value": int(candidate.get("transactionValue") or amount_wei)}
        ok, why = preflight(probe)
        if ok:
            built = candidate
            break
        log.warning(f"{symbol}: quote {attempt}/{REQUOTE_TRIES} would revert ({why}) — re-quoting")
    if not built:
        log.error(f"{symbol}: every quote would revert — skipping")
        notify(f"❌ <b>BUY ABORTED</b> — {symbol}\nRoute kept reverting in preflight "
               f"(likely slippage or a transfer tax). No gas spent.")
        return False

    if DRY_RUN:
        log.info(f"[DRY RUN] would buy {symbol}: out={built['amountOut']} (${built['amountOutUsd']})")
        notify(f"🧪 <b>DRY RUN</b> — would buy {symbol}\n<code>{token}</code>\n"
               f"{eth_amount} ETH -> ${built['amountOutUsd']}\nSet HOOD_DRY_RUN=false to trade live.")
        return False

    before = token_balance(token)
    tx_hash = send_tx({
        "from": ACCT.address,
        "to": w3.to_checksum_address(built["routerAddress"]),
        "data": built["data"],
        "value": int(built.get("transactionValue") or amount_wei),
        "gas": int(int(built.get("gas") or 600000) * GAS_LIMIT_BUFFER),
    })
    if not tx_hash:
        notify(f"❌ <b>BUY FAILED</b> — {symbol}\n<code>{token}</code>")
        return False

    _open_position(token, symbol, "kyber", eth_amount, token_balance(token) - before,
                   tx_hash, source)
    return True


def buy_token(token: str, eth_amount: float, source: str = "telegram") -> bool:
    token = w3.to_checksum_address(token)
    symbol = token_symbol(token)

    if eth_amount <= 0 or eth_amount > MAX_ETH_PER_TRADE:
        log.warning(f"Refusing buy of {symbol}: {eth_amount} ETH outside (0, {MAX_ETH_PER_TRADE}]")
        notify(f"⚠️ Refused {symbol} — size {eth_amount} ETH exceeds cap {MAX_ETH_PER_TRADE}")
        return False

    amount_wei = w3.to_wei(eth_amount, "ether")
    balance = w3.eth.get_balance(ACCT.address)
    if balance < amount_wei and not DRY_RUN:
        msg = f"Insufficient ETH: have {w3.from_wei(balance,'ether')}, need {eth_amount}"
        log.error(msg)
        notify(f"❌ {msg}")
        return False

    info = pons_route(token)
    venue = "pons curve" if info else "kyber"
    log.info(f"🟢 BUYING {symbol} ({token}) — {eth_amount} ETH via {venue} [{source}]")

    if info:
        return _buy_pons(token, symbol, amount_wei, eth_amount, info, source)
    return _buy_kyber(token, symbol, amount_wei, eth_amount, source)


def _sell_pons(token, symbol, amount, info, reason, fraction) -> bool:
    curve = info["curve"]
    est = pons.quote_out_estimate(w3, curve, amount)
    min_out = int(est * (1 - SLIPPAGE_BPS / 10_000)) if est else 0

    if DRY_RUN:
        usd = eth_usd()
        eth_out = (est or 0) / 1e18
        log.info(f"[DRY RUN] would sell {symbol} on curve for ~{eth_out:.6f} ETH")
        notify(f"🧪 <b>DRY RUN</b> — would sell {symbol} on <b>pons curve</b> for "
               f"~{eth_out:.6f} ETH{f' (${eth_out*usd:,.2f})' if usd else ''}")
        return False

    if not ensure_allowance(token, curve, amount):
        notify(f"❌ Approval failed for {symbol} — cannot sell")
        return False

    tx = pons.build_sell_tx(w3, curve, amount, min_out, ACCT.address, ACCT.address)
    tx_hash = send_tx(tx)
    if not tx_hash:
        notify(f"❌ <b>SELL FAILED</b> — {symbol} (pons curve)")
        return False
    return tx_hash


def _sell_kyber(token, symbol, amount, reason, fraction):
    route = kyber_route(token, NATIVE_ETH, amount)
    if not route:
        notify(f"❌ No sell route for {symbol}")
        return False
    built = kyber_build(route)
    if not built:
        notify(f"❌ Sell build failed for {symbol}")
        return False

    if DRY_RUN:
        log.info(f"[DRY RUN] would sell {symbol} for ${built['amountOutUsd']}")
        notify(f"🧪 <b>DRY RUN</b> — would sell {symbol} for ${built['amountOutUsd']}")
        return False

    if not ensure_allowance(token, built["routerAddress"], amount):
        notify(f"❌ Approval failed for {symbol} — cannot sell")
        return False

    tx_hash = send_tx({
        "from": ACCT.address,
        "to": w3.to_checksum_address(built["routerAddress"]),
        "data": built["data"],
        "value": int(built.get("transactionValue") or 0),
        "gas": int(int(built.get("gas") or 600000) * GAS_LIMIT_BUFFER),
    })
    if not tx_hash:
        notify(f"❌ <b>SELL FAILED</b> — {symbol}")
        return False
    return tx_hash


def sell_token(token: str, reason: str, fraction: float = 1.0) -> bool:
    token = w3.to_checksum_address(token)
    pos = positions.get(token)
    symbol = (pos or {}).get("symbol") or token_symbol(token)

    held = token_balance(token)
    if held <= 0:
        log.warning(f"Nothing to sell for {symbol}")
        notify(f"⚠️ No {symbol} balance to sell")
        positions.pop(token, None)
        save_positions()
        return False

    amount = int(held * max(0.0, min(1.0, fraction)))
    if amount <= 0:
        notify(f"⚠️ Sell fraction too small for {symbol}")
        return False

    info = pons_route(token)
    venue = "pons curve" if info else "kyber"
    log.info(f"🔴 SELLING {symbol} — {fraction*100:.0f}% via {venue} ({reason})")

    entry = (pos or {}).get("entry_price_usd")
    now_price = get_price_usd(token)

    tx_hash = (_sell_pons(token, symbol, amount, info, reason, fraction) if info
               else _sell_kyber(token, symbol, amount, reason, fraction))
    if not tx_hash:
        return False

    pnl = ""
    if entry and now_price:
        pnl = f"\nPnL: <b>{(now_price/entry - 1)*100:+.1f}%</b>"

    if fraction >= 1.0:
        positions.pop(token, None)
    elif pos:
        pos["tokens"] = token_balance(token)
    save_positions()

    log.info(f"✅ Sold {symbol} on {venue} ({reason})")
    notify(
        f"🔴 <b>SELL</b> — {symbol} <i>({venue})</i>\n<code>{token}</code>\n"
        f"{fraction*100:.0f}% sold{pnl}\nReason: {reason}\n"
        f"🔗 robinhoodchain.blockscout.com/tx/{tx_hash}"
    )
    return True


# ─── TAKE-PROFIT LADDER ──────────────────────────────────────────────────────
# Tier 1 fires at 1 + TP_FIRST_PCT/100 (default 2.0x) and sells 50%.
# Tier k>1 fires every further TP_STEP_PCT (default 3.0x, 4.0x, …) selling 10%.
def tier_multiple(tier: int) -> float:
    return 1.0 + TP_FIRST_PCT / 100.0 + (tier - 1) * (TP_STEP_PCT / 100.0)


def tier_fraction(tier: int) -> float:
    return TP_FIRST_FRAC if tier == 1 else TP_STEP_FRAC


def tier_for_multiple(mult: float) -> int:
    """Highest tier reached at this multiple (0 = none)."""
    tier = 0
    for t in range(1, TP_MAX_TIER + 1):
        if mult >= tier_multiple(t):
            tier = t
        else:
            break
    return tier


def combined_fraction(from_tier: int, to_tier: int) -> float:
    """One sell fraction covering every tier from from_tier..to_tier.

    A fast mover can cross several rungs between polls; collapsing them into a
    single sell keeps it to one transaction instead of one per rung.
    """
    remaining = 1.0
    for t in range(from_tier, to_tier + 1):
        remaining *= (1.0 - tier_fraction(t))
    return 1.0 - remaining


async def check_take_profit(token: str):
    pos = positions.get(token)
    if not pos:
        return
    loop = asyncio.get_event_loop()

    entry = pos.get("entry_price_usd")
    if not entry:
        price_now = await loop.run_in_executor(None, get_price_usd, token)
        if price_now:
            pos["entry_price_usd"] = price_now
            save_positions()
            log.info(f"{pos.get('symbol')}: backfilled entry price ${price_now}")
        return

    price = await loop.run_in_executor(None, get_price_usd, token)
    if not price:
        return

    mult = price / entry
    pos["peak_multiple"] = max(pos.get("peak_multiple", 1.0), mult)

    tier = tier_for_multiple(mult)
    last = pos.get("tp_tier", 0)
    if tier <= last:
        return

    frac = combined_fraction(last + 1, tier)
    reason = f"TP tier {tier} ({mult:.2f}x)"
    log.info(f"🎯 {pos.get('symbol')} hit {mult:.2f}x — selling {frac*100:.0f}% ({reason})")
    notify(f"🎯 <b>TAKE PROFIT</b> — {pos.get('symbol')} at <b>{mult:.2f}x</b>\n"
           f"Selling {frac*100:.0f}% of remaining (tier {tier})")

    ok = await loop.run_in_executor(None, sell_token, token, reason, frac)
    if ok and token in positions:
        positions[token]["tp_tier"] = tier
        positions[token]["tokens"] = token_balance(token)
        save_positions()
    elif not ok:
        log.warning(f"TP sell failed for {token} — tier {tier} will retry next poll")


async def monitor_positions():
    """Poll open positions and run the take-profit ladder. No stop-loss."""
    if not TP_ENABLED:
        log.warning("TP ladder DISABLED — positions have no automated exit at all")
        return
    rungs = ", ".join(f"{tier_multiple(t):.0f}x->{tier_fraction(t)*100:.0f}%" for t in range(1, 5))
    log.info(f"🎯 TP ladder active: {rungs}, … (fractions are of remaining balance) — no stop-loss")
    last_beat = 0.0
    while True:
        await asyncio.sleep(TP_POLL_SEC)
        for token in list(positions.keys()):
            try:
                await check_take_profit(token)
            except Exception as e:
                log.warning(f"TP check failed for {token}: {e}")

        if time.time() - last_beat >= HEARTBEAT_MIN * 60:
            last_beat = time.time()
            if positions:
                summary = ", ".join(
                    f"{p.get('symbol','?')} {p.get('peak_multiple',1.0):.2f}x"
                    f"{'/t' + str(p.get('tp_tier')) if p.get('tp_tier') else ''}"
                    for p in list(positions.values())[:8])
                extra = f" (+{len(positions)-8} more)" if len(positions) > 8 else ""
            else:
                summary, extra = "no open positions", ""
            try:
                bal = w3.eth.get_balance(ACCT.address) / 1e18
            except Exception:
                bal = float("nan")
            log.info(f"💓 alive — {len(positions)} position(s), {bal:.4f} ETH | {summary}{extra}")


# ─── SIGNAL HANDLING ──────────────────────────────────────────────────────────
def extract_tokens(text: str) -> list[str]:
    out = []
    for m in ADDR_PATTERN.findall(text or ""):
        if m.lower() in BLOCKED:
            continue
        try:
            out.append(w3.to_checksum_address(m))
        except Exception:
            continue
    return out


def vet_pons(token: str, info: dict, eth_amount: float) -> tuple[bool, str]:
    """Gates for a token still on its pons bonding curve.

    Liquidity and volume mean nothing here — there is no pool yet — so this
    checks the curve's own state instead, and measures the real cost of entry
    rather than trusting the advertised tax.
    """
    is_usdg = info["pair_token"].lower() == USDG_ADDR.lower()
    if not info["native_eth"] and not is_usdg:
        return False, f"unsupported quote asset {info['pair_token'][:10]}…"
    if info["creator_tax_bps"] > MAX_CREATOR_TAX_BPS:
        return False, f"creator tax {info['creator_tax_bps']/100:.1f}% above {MAX_CREATOR_TAX_BPS/100:.1f}%"

    st = pons.state(w3, info["curve"])
    if not st:
        return False, "could not read curve state"

    if info["native_eth"]:
        real_quote = st["real_quote_reserve"] / 1e18
        if real_quote < MIN_CURVE_ETH:
            return False, f"curve holds {real_quote:.4f} ETH, below {MIN_CURVE_ETH} ETH"
        detail = (f"pons curve {real_quote:.3f}/{st['graduation_threshold']/1e18:.2f} ETH "
                  f"({st['progress']*100:.1f}% to graduation)")
    else:
        # USDG-quoted: compare reserve to an ETH-equivalent USD floor
        real_usdg = st["real_quote_reserve"] / 1e18
        usd = eth_usd() or 1800.0
        min_usd = MIN_CURVE_ETH * usd
        if real_usdg < min_usd:
            return False, f"curve holds ${real_usdg:,.0f} USDG, below ${min_usd:,.0f} equivalent"
        grad_usd = st["graduation_threshold"] / 1e18
        detail = (f"pons curve ${real_usdg:,.0f}/${grad_usd:,.0f} USDG "
                  f"({st['progress']*100:.1f}% to graduation)")

    return True, f"{detail}, creator tax {info['creator_tax_bps']/100:.1f}%"


def vet_token(token: str, eth_amount: float = None) -> tuple[bool, str]:
    """Cheap sanity gates before spending ETH. Returns (ok, reason)."""
    if not is_contract(token):
        return False, "not a contract (likely a wallet address)"

    info = pons_route(token)
    if info:
        return vet_pons(token, info, eth_amount or BUY_AMOUNT_ETH)

    pairs = _robinhood_pairs(token)
    if not pairs:
        return False, "no credible Robinhood Chain pair on DexScreener"
    pair = pairs[0]
    liq = float((pair.get("liquidity") or {}).get("usd") or 0)
    if liq < MIN_LIQ_USD:
        return False, f"liquidity ${liq:,.0f} below ${MIN_LIQ_USD:,.0f}"
    vol24 = float((pair.get("volume") or {}).get("h24") or 0)
    if vol24 < MIN_VOL24_USD:
        return False, f"24h volume ${vol24:,.0f} below ${MIN_VOL24_USD:,.0f}"
    return True, f"liq ${liq:,.0f} | 24h vol ${vol24:,.0f} on {pair.get('dexId')}"


async def handle_signal(token: str, source: str):
    loop = asyncio.get_event_loop()

    if PAUSED:
        log.info(f"Paused — ignoring {token}")
        return
    if token in positions:
        log.info(f"Already holding {token}")
        return

    now = time.time()
    if now - _last_trigger.get(token, 0) < DEDUPE_WINDOW_SEC:
        log.info(f"Deduped repeat mention of {token}")
        return
    _last_trigger[token] = now

    if token in already_seen:
        log.info(f"Already processed {token} earlier")
        return
    already_seen.add(token)
    save_seen()

    ok, reason = await loop.run_in_executor(None, vet_token, token)
    if not ok:
        log.info(f"Skipped {token} — {reason}")
        return

    log.info(f"📡 Signal: {token} from {source} — {reason}")

    notify(f"📡 <b>Signal</b> — <code>{token}</code>\nFrom: {source}\n{reason}\n"
           f"Buying {BUY_AMOUNT_ETH} ETH...")
    await loop.run_in_executor(None, buy_token, token, BUY_AMOUNT_ETH, source)


# ─── TELEGRAM LISTENER ────────────────────────────────────────────────────────
# ─── SENDER ALLOWLIST ─────────────────────────────────────────────────────────
# Usernames are resolved to numeric IDs once at startup and matched by ID
# thereafter, so a caller renaming themselves can't silently disable the filter
# (and can't be impersonated by someone claiming the freed username).
ALLOWED_NAMES: set = {u.strip().lstrip("@").lower()
                      for u in ALLOWED_SENDERS.split(",") if u.strip()}
ALLOWED_IDS: set = set()


async def resolve_allowed_senders(client):
    """Map configured usernames to user IDs. Unresolved names still match by name."""
    if not ALLOWED_NAMES:
        log.warning("⚠️ HOOD_ALLOWED_SENDERS is empty — ANY sender in the monitored "
                    "chats can trigger a buy")
        return
    ALLOWED_IDS.clear()
    resolved, failed = [], []
    for name in sorted(ALLOWED_NAMES):
        try:
            ent = await client.get_entity(name)
            ALLOWED_IDS.add(ent.id)
            resolved.append(f"@{name} -> {ent.id}")
        except Exception as e:
            failed.append(f"@{name} ({type(e).__name__})")
    if resolved:
        log.info("🔒 Buy allowlist: " + ", ".join(resolved))
    if failed:
        log.warning("⚠️ Could not resolve " + ", ".join(failed) +
                    " — will match on username instead")
        notify("⚠️ <b>Allowlist</b> — could not resolve " + ", ".join(failed) +
               "\nFalling back to username matching for those.")


async def sender_allowed(event) -> tuple[bool, str]:
    """(allowed, label). Label identifies the poster for logs and alerts."""
    sender_id = getattr(event, "sender_id", None)

    sender = None
    try:
        sender = await event.get_sender()
    except Exception:
        pass
    username = (getattr(sender, "username", "") or "").lower()
    label = f"@{username}" if username else f"id:{sender_id}"

    if not ALLOWED_NAMES:
        return True, label
    if sender_id is not None and sender_id in ALLOWED_IDS:
        return True, label
    if username and username in ALLOWED_NAMES:
        return True, label
    return False, label


def _chat_filter():
    if not MONITOR_CHATS.strip():
        return None
    ids = []
    for part in MONITOR_CHATS.split(","):
        part = part.strip()
        if part:
            ids.append(int(part))
    return ids


async def _connection_watchdog(client) -> None:
    """Force a full reconnect if Telethon stays down past MAX_DISCONNECT_SEC.

    Returning from here (after disconnect()) unblocks run_until_disconnected(),
    which lets the supervisor rebuild the client from scratch.
    """
    down_since = None
    while True:
        await asyncio.sleep(WATCHDOG_POLL_SEC)
        try:
            connected = client.is_connected()
        except Exception:
            connected = False

        if connected:
            if down_since:
                log.info(f"✅ Telegram reconnected after {time.time()-down_since:.0f}s")
                notify("✅ <b>Reconnected</b> — sniper is listening again")
            down_since = None
            continue

        down_since = down_since or time.time()
        down = time.time() - down_since
        log.warning(f"⚠️ Telegram disconnected for {down:.0f}s")
        if down >= MAX_DISCONNECT_SEC:
            log.error(f"Down {down:.0f}s >= {MAX_DISCONNECT_SEC}s — forcing full reconnect")
            notify(f"🔄 <b>Reconnecting</b> — Telegram down {down:.0f}s, rebuilding session")
            try:
                await client.disconnect()
            except Exception as e:
                log.warning(f"disconnect() during forced reconnect: {e}")
            return


async def monitor_telegram():
    """Listen for signals, rebuilding the client for as long as the bot runs."""
    # Reuse the authorised Solana session so there's no second phone-code login.
    if not os.path.exists(SESSION_NAME + ".session") and os.path.exists(SOLANA_SESSION):
        shutil.copyfile(SOLANA_SESSION, SESSION_NAME + ".session")
        log.info("Copied sniper_session -> hood_session (reusing existing Telegram auth)")

    chats = _chat_filter()
    attempt = 0

    while True:
        client = None
        watchdog = None
        try:
            client = TelegramClient(
                SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH,
                connection_retries=None,      # retry forever at the Telethon layer
                retry_delay=RECONNECT_DELAY_SEC,
                auto_reconnect=True,
                request_retries=5,
            )

            @client.on(events.NewMessage(chats=chats))
            async def _(event):
                text = event.raw_text or ""
                tokens = extract_tokens(text)
                if not tokens:
                    return

                allowed, who = await sender_allowed(event)
                if not allowed:
                    log.info(f"🚫 Ignoring {len(tokens)} address(es) from {who} — not on the buy allowlist")
                    return

                for token in tokens:
                    try:
                        await handle_signal(token, f"tg:{who}")
                    except Exception as e:
                        log.warning(f"Signal handling error for {token}: {e}")

            await client.start()
            await resolve_allowed_senders(client)
            if attempt:
                log.info(f"👂 Telegram reconnected (attempt {attempt}) on {chats or 'ALL chats'}")
            else:
                log.info(f"👂 Telegram listening on {chats or 'ALL chats'}")
            attempt = 0

            watchdog = asyncio.ensure_future(_connection_watchdog(client))
            await client.run_until_disconnected()
            log.warning("Telegram listener stopped — reconnecting")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            attempt += 1
            log.error(f"Telegram listener crashed (attempt {attempt}): {type(e).__name__}: {e}")
            if attempt == 1:
                notify(f"⚠️ <b>Telegram listener dropped</b>\n{type(e).__name__}: {e}\nRetrying...")
        finally:
            if watchdog and not watchdog.done():
                watchdog.cancel()
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

        # Back off a little on repeated failures, but never give up.
        delay = min(RECONNECT_DELAY_SEC * max(1, attempt), 60)
        log.info(f"Reconnecting to Telegram in {delay}s...")
        await asyncio.sleep(delay)


# ─── CONTROL COMMANDS (via the notify bot) ───────────────────────────────────
def _tg_get_updates(offset: Optional[int]) -> list:
    try:
        params = {"timeout": 20}
        if offset is not None:
            params["offset"] = offset
        r = requests.get(
            f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/getUpdates",
            params=params, timeout=25,
        )
        return r.json().get("result", [])
    except Exception as e:
        log.warning(f"Control poll failed: {e}")
        return []


HELP = (
    "<b>Commands</b>\n"
    "/buy &lt;addr&gt; [eth] — buy now, skipping vetting gates\n"
    "/sell &lt;addr&gt; [pct] — sell all or part of a position\n"
    "/sellall — close every open position\n"
    "/watch &lt;addr&gt; [eth] — buy a token now, bypassing the vetting gates\n"
    "/positions — open positions with live PnL\n"
    "/price &lt;addr&gt; — current price and liquidity\n"
    "/balance — wallet ETH balance\n"
    "/size &lt;eth&gt; — change default buy size\n"
    "/pause /resume — stop or restart auto-buying\n"
    "/status — current settings"
)


async def handle_control_command(text: str):
    global PAUSED, BUY_AMOUNT_ETH
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]
    arg = parts[1] if len(parts) > 1 else ""
    loop = asyncio.get_event_loop()

    def as_addr(a):
        try:
            return w3.to_checksum_address(a)
        except Exception:
            return None

    if cmd in ("/help", "/start"):
        notify(HELP)

    elif cmd == "/buy":
        token = as_addr(arg)
        if not token:
            notify("⚠️ Usage: /buy &lt;0x address&gt; [eth]")
            return
        if token in positions:
            notify(f"⚠️ Already holding <code>{token}</code>")
            return
        try:
            size = float(parts[2]) if len(parts) > 2 else BUY_AMOUNT_ETH
        except ValueError:
            notify("⚠️ eth amount must be a number")
            return
        already_seen.add(token)
        save_seen()
        await loop.run_in_executor(None, buy_token, token, size, "manual /buy")

    elif cmd == "/sell":
        token = as_addr(arg)
        if not token:
            notify("⚠️ Usage: /sell &lt;0x address&gt; [pct]")
            return
        try:
            pct = float(parts[2]) if len(parts) > 2 else 100.0
        except ValueError:
            notify("⚠️ pct must be a number")
            return
        await loop.run_in_executor(None, sell_token, token, "manual /sell", pct / 100.0)

    elif cmd == "/watch":
        token = as_addr(arg)
        if not token:
            notify("⚠️ Usage: /watch &lt;0x address&gt; [eth]")
            return
        try:
            size = float(parts[2]) if len(parts) > 2 else BUY_AMOUNT_ETH
        except ValueError:
            notify("⚠️ eth amount must be a number")
            return
        already_seen.add(token)
        save_seen()
        notify(f"🟢 Buying {size} ETH of <code>{token}</code> (manual /watch, gates bypassed)")
        await loop.run_in_executor(None, buy_token, token, size, "manual /watch")

    elif cmd == "/sellall":
        if not positions:
            notify("ℹ️ No open positions")
            return
        notify(f"🔴 Closing {len(positions)} position(s)...")
        for token in list(positions.keys()):
            await loop.run_in_executor(None, sell_token, token, "manual /sellall", 1.0)

    elif cmd == "/positions":
        if not positions:
            notify("ℹ️ No open positions")
            return
        lines = []
        for token, p in positions.items():
            price = await loop.run_in_executor(None, get_price_usd, token)
            entry = p.get("entry_price_usd")
            if price and entry:
                pnl = f"{(price/entry - 1)*100:+.1f}%"
            else:
                pnl = "n/a"
            held = time.time() - p.get("buy_time", time.time())
            tier = p.get("tp_tier", 0)
            nxt = tier_multiple(tier + 1)
            lines.append(
                f"<b>{p.get('symbol','?')}</b> {pnl} <i>({p.get('venue','?')})</i>\n"
                f"<code>{token}</code>\n"
                f"  entry ${entry} -> now ${price} | {p.get('entry_eth')} ETH | {held/3600:.1f}h\n"
                f"  peak {p.get('peak_multiple',1.0):.2f}x | TP tier {tier} done | "
                f"next rung {nxt:.0f}x sells {tier_fraction(tier+1)*100:.0f}%"
            )
        notify("📊 <b>Open positions</b>\n\n" + "\n\n".join(lines))

    elif cmd == "/price":
        token = as_addr(arg)
        if not token:
            notify("⚠️ Usage: /price &lt;0x address&gt;")
            return
        pair = await loop.run_in_executor(None, get_pair, token)
        if not pair:
            notify(f"⚠️ No Robinhood Chain pair for <code>{token}</code>")
            return
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        notify(
            f"<b>{pair.get('baseToken',{}).get('symbol','?')}</b>\n"
            f"Price: ${pair.get('priceUsd')}\nLiquidity: ${liq:,.0f}\n"
            f"DEX: {pair.get('dexId')} | 24h vol: ${float((pair.get('volume') or {}).get('h24') or 0):,.0f}"
        )

    elif cmd == "/balance":
        bal = await loop.run_in_executor(None, lambda: w3.eth.get_balance(ACCT.address))
        notify(f"💰 <code>{ACCT.address}</code>\n{w3.from_wei(bal,'ether'):.6f} ETH on Robinhood Chain")

    elif cmd == "/size":
        try:
            size = float(arg)
        except ValueError:
            notify("⚠️ Usage: /size &lt;eth&gt;")
            return
        if size <= 0 or size > MAX_ETH_PER_TRADE:
            notify(f"⚠️ Size must be in (0, {MAX_ETH_PER_TRADE}] — raise HOOD_MAX_ETH_PER_TRADE to go higher")
            return
        BUY_AMOUNT_ETH = size
        notify(f"✅ Buy size set to {size} ETH")

    elif cmd == "/pause":
        PAUSED = True
        notify("⏸ Auto-buying paused (manual /buy and /sell still work)")

    elif cmd == "/resume":
        PAUSED = False
        notify("▶️ Auto-buying resumed")

    elif cmd == "/status":
        notify(
            f"<b>Status</b>\n"
            f"Mode: {'🧪 DRY RUN' if DRY_RUN else '🔴 LIVE'}\n"
            f"Auto-buy: {'paused' if PAUSED else 'active'}\n"
            f"Size: {BUY_AMOUNT_ETH} ETH (cap {MAX_ETH_PER_TRADE})\n"
            f"Min liquidity: ${MIN_LIQ_USD:,.0f} | min 24h vol: ${MIN_VOL24_USD:,.0f}\n"
            f"Slippage: {SLIPPAGE_BPS/100:.1f}%\n"
            f"Pons: ETH-only {PONS_ETH_ONLY} | max entry tax {MAX_ENTRY_TAX_BPS/100:.1f}% | "
            f"max creator tax {MAX_CREATOR_TAX_BPS/100:.1f}% | min curve {MIN_CURVE_ETH} ETH\n"
            f"Open positions: {len(positions)}\n"
            f"Buy allowlist: {', '.join('@'+n for n in sorted(ALLOWED_NAMES)) if ALLOWED_NAMES else 'ANYONE ⚠️'}\n"
            f"Entry: immediate on an allowlisted Telegram call\n"
            f"Exit: {tier_multiple(1):.0f}x sells {TP_FIRST_FRAC*100:.0f}%, then every "
            f"+{TP_STEP_PCT:.0f}% sells {TP_STEP_FRAC*100:.0f}% of remaining\n"
            f"  stop-loss: none\n"
            f"Fees: tip {PRIORITY_FEE_GWEI} gwei | ceiling {MAX_TX_FEE_ETH} ETH/tx"
        )


async def monitor_control_commands():
    if not NOTIFY_BOT_TOKEN or not NOTIFY_CHAT_ID:
        log.info("Control commands disabled — NOTIFY_BOT_TOKEN/NOTIFY_CHAT_ID not set")
        return
    loop = asyncio.get_event_loop()
    offset = None
    log.info("🎮 Listening for control commands...")
    while True:
        try:
            updates = await loop.run_in_executor(None, _tg_get_updates, offset)
        except Exception as e:
            log.warning(f"Control poll crashed: {e}; retrying in {RECONNECT_DELAY_SEC}s")
            await asyncio.sleep(RECONNECT_DELAY_SEC)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("channel_post")
            if not msg:
                continue
            if str(msg.get("chat", {}).get("id")) != str(NOTIFY_CHAT_ID):
                continue
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            try:
                await handle_control_command(text)
            except Exception as e:
                log.warning(f"Control command error ({text!r}): {e}")
                notify(f"⚠️ Command error: {e}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    if not ACCT:
        raise SystemExit("HOOD_PRIVATE_KEY is not set in .env — nothing to trade with.")
    if not w3.is_connected():
        raise SystemExit(f"Cannot reach Robinhood Chain RPC at {HOOD_RPC}")
    if w3.eth.chain_id != CHAIN_ID:
        raise SystemExit(f"RPC reports chain {w3.eth.chain_id}, expected {CHAIN_ID}")

    bal = w3.from_wei(w3.eth.get_balance(ACCT.address), "ether")
    mode = "🧪 DRY RUN — no ETH will be spent" if DRY_RUN else "🔴 LIVE TRADING"
    log.info(f"🚀 HOOD sniper starting — {mode}")
    log.info(f"Wallet {ACCT.address} — {bal:.6f} ETH — {len(positions)} open position(s)")
    notify(
        f"🚀 <b>HOOD sniper online</b>\n{mode}\n"
        f"<code>{ACCT.address}</code>\n{bal:.6f} ETH\n"
        f"Size {BUY_AMOUNT_ETH} ETH\n"
        f"Entry: immediate on call | Exit: {tier_multiple(1):.0f}x sells "
        f"{TP_FIRST_FRAC*100:.0f}%, then +{TP_STEP_PCT:.0f}% sells {TP_STEP_FRAC*100:.0f}% each\n"
        f"No stop-loss\n"
        f"Allowlist: {', '.join('@'+n for n in sorted(ALLOWED_NAMES)) if ALLOWED_NAMES else 'ANYONE ⚠️'}\n"
        f"Auto-reconnect: forced after {MAX_DISCONNECT_SEC}s offline\n"
        f"Send /help for commands"
    )

    asyncio.ensure_future(monitor_control_commands())
    asyncio.ensure_future(monitor_positions())
    await monitor_telegram()


if __name__ == "__main__":
    asyncio.run(main())
