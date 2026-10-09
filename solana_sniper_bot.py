"""
Telegram + Twitter/X Solana Sniper Bot
---------------------------------------
Monitors Telegram chats AND Twitter/X handles for Solana CAs,
auto-buys via PumpPortal, and auto-sells at tiered TPs or stop-loss.

SETUP:
  pip install telethon solana requests python-dotenv twscrape

Create a .env file with:
  TELEGRAM_API_ID=your_api_id
  TELEGRAM_API_HASH=your_api_hash
  WALLET_PRIVATE_KEY=your_base58_private_key
  HELIUS_RPC=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY
  MONITOR_CHATS=chat1,chat2         (comma-separated numeric group IDs)
  PUMPPORTAL_API_KEY=your_key

  # Twitter/X monitoring (optional)
  TWITTER_HANDLES=handle1,handle2   (comma-separated, no @ needed)
  TWITTER_USERNAME=your_twitter_login
  TWITTER_PASSWORD=your_twitter_password
  TWITTER_EMAIL=your_twitter_email
  TWITTER_EMAIL_PASSWORD=          (leave blank if not using email 2FA)
  TWITTER_POLL_SEC=60              (how often to check for new tweets)
"""

import asyncio
import re
import os
import time
import logging
import requests
from typing import Optional
from dotenv import load_dotenv
from telethon import TelegramClient, events
from solders.keypair import Keypair  # type: ignore
from solders.pubkey import Pubkey  # type: ignore
from solana.rpc.types import TxOpts
import base58
from power_analyzer import PowerAnalyzer, KolLedger, DELAY_WINDOW_SEC
import ema_strategy

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("sniper.log")]
)
log = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_API_ID   = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
WALLET_PRIVATE_KEY   = os.getenv("WALLET_PRIVATE_KEY", "")
RPC_URL              = os.getenv("HELIUS_RPC", "https://api.mainnet-beta.solana.com")
MONITOR_CHATS        = os.getenv("MONITOR_CHATS", "")  # blank = all chats
PUMPPORTAL_API_KEY   = os.getenv("PUMPPORTAL_API_KEY", "")

# Twitter/X monitoring
TWITTER_HANDLES        = os.getenv("TWITTER_HANDLES", "")
TWITTER_USERNAME       = os.getenv("TWITTER_USERNAME", "")
TWITTER_PASSWORD       = os.getenv("TWITTER_PASSWORD", "")
TWITTER_EMAIL          = os.getenv("TWITTER_EMAIL", "")
TWITTER_EMAIL_PASSWORD = os.getenv("TWITTER_EMAIL_PASSWORD", "")
TWITTER_POLL_SEC       = int(os.getenv("TWITTER_POLL_SEC", "60"))

BUY_AMOUNT_SOL    = 0.18         # SOL per trade (used as fallback / default size)
SLIPPAGE_PCT      = 25           # 25% slippage
PRIORITY_FEE_SOL  = 0.001        # priority fee in SOL
CANDLE_POLL_SEC   = 15           # how often to poll price while building live 1m candles
WATCH_TIMEOUT_SEC = 3600         # give up watching for an EMA55 breakout after 1 hour

# ─── REGEX: Solana CA (base58, 32-44 chars, not a known non-CA) ───────────────
CA_PATTERN = re.compile(r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b')

# ─── STATE ────────────────────────────────────────────────────────────────────
POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "positions.json")

SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")

def save_positions():
    import json
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f)
    except Exception as e:
        log.warning(f"Failed to save positions: {e}")

def load_positions() -> dict:
    import json
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE, "r") as f:
                data = json.load(f)
            log.info(f"Loaded {len(data)} open position(s) from disk: {list(data.keys())}")
            return data
    except Exception as e:
        log.warning(f"Failed to load positions: {e}")
    return {}

def save_seen():
    import json
    try:
        with open(SEEN_FILE, "w") as f:
            json.dump(list(already_seen), f)
    except Exception as e:
        log.warning(f"Failed to save seen: {e}")

def load_seen() -> set:
    import json
    try:
        if os.path.exists(SEEN_FILE):
            with open(SEEN_FILE, "r") as f:
                return set(json.load(f))
    except Exception as e:
        log.warning(f"Failed to load seen: {e}")
    return set()

positions: dict = load_positions()
already_seen: set = load_seen() | set(positions.keys())

# ─── EMA55(1m, DexPaprika) STRATEGY STATE ─────────────────────────────────────
# Candle/EMA state is in-memory only, not persisted — but since candles are
# fetched fresh from DexPaprika's real history on every startup (via backfill),
# a reopened position's EMA55 is live again almost immediately, not after a
# fresh warmup, as long as the token has 55+ minutes of real trading history.
TRACKED: dict = {}
for _ca, _pos in positions.items():
    _pool = ema_strategy.resolve_pool(_ca)
    _t = ema_strategy.new_tracker(size_sol=_pos.get("amount_sol", BUY_AMOUNT_SOL), pool_address=_pool)
    _t["in_position"] = True
    if _pool:
        ema_strategy.backfill(_t)
        log.info(f"Resumed {_ca} — pool={_pool} ema={_t['ema']}")
    else:
        log.warning(f"No DexPaprika pool found for {_ca} — EMA55 exit inactive until pool resolves")
    TRACKED[_ca] = _t

# ─── WALLET ───────────────────────────────────────────────────────────────────
def load_keypair() -> Keypair:
    raw = base58.b58decode(WALLET_PRIVATE_KEY)
    return Keypair.from_bytes(raw)

KEYPAIR = load_keypair() if WALLET_PRIVATE_KEY else None

# ─── PRICE VIA DEXSCREENER ────────────────────────────────────────────────────
def get_price_usd(ca: str) -> Optional[float]:
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", timeout=10)
        pairs = r.json().get("pairs") or []
        if not pairs:
            return None
        # pick highest liquidity pair
        pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0)), reverse=True)
        return float(pairs[0]["priceUsd"])
    except Exception as e:
        log.warning(f"Price fetch failed for {ca}: {e}")
        return None

# ─── PUMPPORTAL TRADE-LOCAL (sign + send ourselves) ───────────────────────────
def pumpportal_trade(action: str, mint: str, amount, denominated_in_sol: bool = True) -> Optional[str]:
    """Build tx via PumpPortal trade-local, sign with our keypair, send via Helius RPC."""
    import base64
    from solders.transaction import VersionedTransaction  # type: ignore
    from solana.rpc.api import Client  # type: ignore

    wallet_pubkey = str(KEYPAIR.pubkey())
    try:
        # 1. Get serialized transaction from PumpPortal
        resp = requests.post(
            "https://pumpportal.fun/api/trade-local",
            json={
                "publicKey": wallet_pubkey,
                "action": action,
                "mint": mint,
                "amount": amount,
                "denominatedInSol": "true" if denominated_in_sol else "false",
                "slippage": SLIPPAGE_PCT,
                "priorityFee": PRIORITY_FEE_SOL,
                "pool": "auto",
            },
            timeout=20,
        )
        if resp.status_code != 200:
            log.error(f"PumpPortal error {resp.status_code}: {resp.text}")
            return None

        # 2. Deserialize, sign, send
        tx_bytes = resp.content
        tx = VersionedTransaction.from_bytes(tx_bytes)
        tx_signed = VersionedTransaction(tx.message, [KEYPAIR])

        client = Client(RPC_URL)
        result = client.send_raw_transaction(bytes(tx_signed), opts=TxOpts(skip_preflight=True))
        sig = str(result.value)
        log.info(f"TX sent: https://solscan.io/tx/{sig}")
        return sig

    except Exception as e:
        log.error(f"PumpPortal trade failed: {e}")
        return None

# ─── TELEGRAM NOTIFICATIONS ───────────────────────────────────────────────────
def notify(msg: str):
    if not NOTIFY_BOT_TOKEN or not NOTIFY_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/sendMessage",
            json={"chat_id": NOTIFY_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Notify failed: {e}")

# ─── BUY ──────────────────────────────────────────────────────────────────────
def buy_token(ca: str, sol_amount: float = BUY_AMOUNT_SOL):
    if not PUMPPORTAL_API_KEY:
        log.error("No PumpPortal API key configured!")
        return
    log.info(f"🟢 BUYING {ca} — {sol_amount} SOL")
    sig = pumpportal_trade("buy", ca, sol_amount, denominated_in_sol=True)
    if sig:
        buy_price = get_price_usd(ca)
        positions[ca] = {
            "buy_price": buy_price,
            "buy_time": time.time(),
            "amount_sol": sol_amount,
            "sig": sig,
        }
        save_positions()
        log.info(f"✅ Position opened: {ca} @ ${buy_price}")
        notify(f"🟢 <b>BUY</b>\n<code>{ca}</code>\nEntry: <b>${buy_price}</b>\n{sol_amount} SOL\n🔗 solscan.io/tx/{sig}")
    else:
        log.error(f"❌ Buy failed for {ca}")
        notify(f"❌ <b>BUY FAILED</b>\n<code>{ca}</code>")

# ─── SELL (partial or full) ───────────────────────────────────────────────────
MAX_SELL_FAILURES = 3  # drop position after this many consecutive sell failures

def sell_token(ca: str, reason: str, sell_frac: float = 1.0) -> bool:
    """Sell `sell_frac` (0–1) of token balance. Returns True on success."""
    pct = f"{int(sell_frac * 100)}%"
    log.info(f"🔴 SELLING {pct} of {ca} — {reason}")
    sig = pumpportal_trade("sell", ca, pct, denominated_in_sol=False)
    if sig:
        log.info(f"✅ Sold {ca} ({reason}) | tx: {sig}")
        pos = positions.get(ca)
        entry = pos.get("buy_price") if pos else None
        current = get_price_usd(ca)
        pnl_str = ""
        if entry and current:
            pnl = (current - entry) / entry * 100
            pnl_str = f"\nPnL: <b>{pnl:+.1f}%</b>"
        notify(f"🔴 <b>SELL — {reason}</b>\n<code>{ca}</code>{pnl_str}\n🔗 solscan.io/tx/{sig}")
        if sell_frac >= 1.0:
            positions.pop(ca, None)
        else:
            positions[ca]["sell_failures"] = 0
        save_positions()
        return True
    else:
        pos = positions.get(ca)
        if pos is not None:
            pos["sell_failures"] = pos.get("sell_failures", 0) + 1
            if pos["sell_failures"] >= MAX_SELL_FAILURES:
                log.warning(f"⚠️ Dropping {ca} after {MAX_SELL_FAILURES} failed sell attempts — manual action may be needed")
                positions.pop(ca, None)
            save_positions()
        log.error(f"❌ Sell failed for {ca} (attempt {pos.get('sell_failures','?') if pos else '?'}/{MAX_SELL_FAILURES})")
        notify(f"⚠️ <b>SELL FAILED</b> — {reason}\n<code>{ca}</code>")
        return False

# ─── WATCHLIST + POSITION MONITOR (EMA55 / 1m via DexPaprika) ─────────────────
async def add_to_watchlist(ca: str, size_sol: float, kol_handle: str):
    """Resolve the token's pool, backfill real 1m history, then watch for an EMA55 upcross."""
    if ca in TRACKED:
        return
    loop = asyncio.get_event_loop()
    pool = await loop.run_in_executor(None, ema_strategy.resolve_pool, ca)
    t = ema_strategy.new_tracker(size_sol=size_sol, kol_handle=kol_handle, pool_address=pool)
    TRACKED[ca] = t

    if pool:
        await loop.run_in_executor(None, ema_strategy.backfill, t)
        status = "EMA55 ready" if t["ema"] is not None else f"only {len(t['candles'])}/{ema_strategy.EMA_PERIOD} candles so far — still warming up"
    else:
        status = "no DexPaprika pool found yet — will retry"

    log.info(f"👀 Watching {ca} for EMA55(1m) breakout — size {size_sol} SOL ({status})")
    notify(f"👀 <b>Watching for EMA55(1m) breakout</b>\n<code>{ca}</code>\nSize: {size_sol} SOL\n{status}")

async def monitor_tracked():
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(CANDLE_POLL_SEC)
        now = time.time()
        for ca, t in list(TRACKED.items()):
            if not t["pool_address"]:
                t["pool_address"] = await loop.run_in_executor(None, ema_strategy.resolve_pool, ca)
                if t["pool_address"]:
                    await loop.run_in_executor(None, ema_strategy.backfill, t)

            price = await loop.run_in_executor(None, get_price_usd, ca)
            if price is None:
                continue

            signal = ema_strategy.ingest_price(t, price, now=now)

            if signal == "buy":
                log.info(f"📈 EMA55(1m) upcross — buying {ca}")
                await loop.run_in_executor(None, buy_token, ca, t["size_sol"])
                if ca in positions:
                    t["in_position"] = True
                else:
                    TRACKED.pop(ca, None)  # buy failed, stop chasing it

            elif signal == "sell":
                log.info(f"📉 EMA55(1m) close below EMA — selling {ca}")
                success = await loop.run_in_executor(None, sell_token, ca, "EMA55(1m) close below EMA", 1.0)
                if success:
                    # Allow re-entry: keep watching, give it a fresh timeout window
                    # to find the next breakout instead of dropping it for good.
                    t["in_position"] = False
                    t["added_at"] = now
                elif ca not in positions:
                    TRACKED.pop(ca, None)  # bot gave up after repeated sell failures — needs manual attention
                # else: sell failed but position still open — leave tracked (in_position=True)
                # so the next closed candle retries the exit

            elif not t["in_position"] and now - t["added_at"] > WATCH_TIMEOUT_SEC:
                log.info(f"⌛ Gave up watching {ca} — no EMA55 breakout within {WATCH_TIMEOUT_SEC}s")
                notify(f"⌛ <b>Gave up watching</b> — no breakout\n<code>{ca}</code>")
                TRACKED.pop(ca, None)

# ─── ANALYZE + BUY PIPELINE ───────────────────────────────────────────────────
async def analyze_and_buy(ca: str, tweet_id: str, tweet_text: str, kol_handle: str):
    """Run PowerAnalyzer pipeline (with delay window), then buy if verdict says so."""
    try:
        log.info(f"🔍 Analyzing {ca} from @{kol_handle} — {DELAY_WINDOW_SEC}s delay window...")
        notify(
            f"🔍 <b>CA detected</b> from @{kol_handle}\n"
            f"<code>{ca}</code>\n"
            f"⏳ Running {DELAY_WINDOW_SEC}s analysis window..."
        )

        verdict = await PA.analyze(
            mint=ca,
            tweet_id=tweet_id,
            kol_handle=kol_handle,
            tweet_text=tweet_text,
        )

        log.info(f"📊 {verdict.summary()}")

        if not verdict.gates_passed:
            msg = "❌ <b>SKIPPED — hard gate failed</b>\n" + \
                  f"<code>{ca}</code>\n" + \
                  "\n".join(f"• {f}" for f in verdict.gate_failures)
            notify(msg)
            return

        if not verdict.buy:
            notify(
                f"⚠️ <b>SKIPPED — score too low</b>\n"
                f"<code>{ca}</code>\n"
                f"Score: {verdict.score:.0f}/100"
            )
            return

        breakdown = " | ".join(f"{k}:{v:.0f}" for k, v in verdict.breakdown.items())
        notify(
            f"📊 <b>Score {verdict.score:.0f}/100</b> — watching, size {verdict.size_sol} SOL\n"
            f"<code>{ca}</code>\n{breakdown}"
        )
        await add_to_watchlist(ca, verdict.size_sol, kol_handle)

    except Exception as e:
        log.error(f"analyze_and_buy error for {ca}: {e}")
        notify(f"⚠️ <b>Analysis error</b>\n<code>{ca}</code>\n{e}")

# ─── TWITTER/X MONITOR ────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "")  # optional: official API

NOTIFY_BOT_TOKEN = os.getenv("NOTIFY_BOT_TOKEN", "")
NOTIFY_CHAT_ID   = os.getenv("NOTIFY_CHAT_ID", "")

OPENROUTER_KEY   = os.getenv("OPENROUTER_KEY", "")  # optional: LLM lore scoring

# ─── POWER ANALYZER ───────────────────────────────────────────────────────────
_helius_key = RPC_URL.split("api-key=")[-1] if "api-key=" in RPC_URL else ""
PA = PowerAnalyzer(
    helius_key=_helius_key,
    openrouter_key=OPENROUTER_KEY or None,
    x_bearer=None,  # free tier bearer doesn't support tweet lookup
)

NITTER_INSTANCES = [
    "nitter.net",
    "nitter.privacydev.net",
    "nitter.poast.org",
    "nitter.kavin.rocks",
]

def _fetch_rss(handle: str, instance: str) -> Optional[list]:
    """Fetch Nitter RSS for handle. Returns list of (guid, text) or None on failure."""
    import xml.etree.ElementTree as ET
    try:
        url = f"https://{instance}/{handle}/rss"
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return None
        root = ET.fromstring(r.text)
        items = []
        for item in root.findall(".//item"):
            guid  = item.findtext("guid") or ""
            title = item.findtext("title") or ""
            desc  = item.findtext("description") or ""
            items.append((guid, title + " " + desc))
        return items
    except Exception:
        return None

def _best_rss(handle: str) -> Optional[tuple]:
    """Hit all Nitter instances in parallel, return (instance, items) from whichever responds first."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=len(NITTER_INSTANCES)) as pool:
        futures = {pool.submit(_fetch_rss, handle, inst): inst for inst in NITTER_INSTANCES}
        for fut in as_completed(futures):
            items = fut.result()
            if items is not None:
                return futures[fut], items
    return None

# Cache handle → user_id to avoid repeated API calls
_twitter_user_id_cache: dict = {}

def _fetch_official_api(handle: str, since_id: Optional[str] = None) -> Optional[list]:
    """Fetch tweets via official Twitter API v2. Returns list of (id, text) or None."""
    if not TWITTER_BEARER_TOKEN:
        return None
    try:
        # Resolve handle to user ID (cached)
        if handle not in _twitter_user_id_cache:
            r = requests.get(
                f"https://api.twitter.com/2/users/by/username/{handle}",
                headers={"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"},
                timeout=10,
            )
            if r.status_code != 200:
                log.warning(f"Twitter API: could not resolve @{handle}: {r.status_code}")
                return None
            _twitter_user_id_cache[handle] = r.json()["data"]["id"]

        uid = _twitter_user_id_cache[handle]
        params = {"max_results": 100, "tweet.fields": "id,text"}
        if since_id:
            params["since_id"] = since_id
        r = requests.get(
            f"https://api.twitter.com/2/users/{uid}/tweets",
            headers={"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"},
            params=params,
            timeout=10,
        )
        if r.status_code == 429:
            log.warning("Twitter API: rate limited")
            return None
        if r.status_code != 200:
            return None
        tweets = r.json().get("data") or []
        return [(t["id"], t["text"]) for t in tweets]
    except Exception as e:
        log.warning(f"Twitter API fetch error: {e}")
        return None

async def monitor_twitter():
    """Poll Twitter/X for Solana CAs and auto-buy. Uses official API if bearer token works, else Nitter RSS."""
    if not TWITTER_HANDLES.strip():
        return
    handles = [h.strip().lstrip("@") for h in TWITTER_HANDLES.split(",") if h.strip()]
    if not handles:
        return

    import urllib.parse

    # ── One-time startup probe: check if official API is actually usable ──────
    use_official_api = False
    inst = "nitter.net"
    if TWITTER_BEARER_TOKEN:
        token = urllib.parse.unquote(TWITTER_BEARER_TOKEN)
        r = requests.get(
            f"https://api.twitter.com/2/users/by/username/{handles[0]}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code == 200:
            _twitter_user_id_cache[handles[0]] = r.json()["data"]["id"]
            use_official_api = True
            log.info(f"Twitter: official API active — monitoring {[('@'+h) for h in handles]}")
        else:
            log.warning(f"Twitter API: probe returned {r.status_code} — falling back to Nitter permanently")

    if not use_official_api:
        result = _best_rss(handles[0])
        if result is None:
            log.warning("Twitter: no Nitter instance reachable — monitor exiting")
            return
        inst, _ = result
        log.info(f"Twitter: using {inst} (Nitter) — monitoring {[('@'+h) for h in handles]}")

    seen_guids:        dict = {h: set()       for h in handles}
    first_run:         dict = {h: True        for h in handles}
    last_success_time: dict = {h: time.time() for h in handles}
    nitter_was_down:   dict = {h: False       for h in handles}
    since_id:          dict = {h: None        for h in handles}

    while True:
        for handle in handles:
            try:
                # ── Fetch ──────────────────────────────────────────────────────
                items = None
                if use_official_api:
                    items = await asyncio.get_event_loop().run_in_executor(
                        None, _fetch_official_api, handle, since_id[handle]
                    )

                if items is None:                          # official failed or not enabled
                    rss = _fetch_rss(handle, inst)
                    if rss is None:
                        fallback = _best_rss(handle)
                        if fallback is None:
                            log.warning(f"Twitter: all Nitter instances down for @{handle}")
                            nitter_was_down[handle] = True
                            continue
                        inst, rss = fallback
                        log.info(f"Twitter: switched to {inst}")
                    items = rss

                # ── Recovery ───────────────────────────────────────────────────
                if nitter_was_down[handle]:
                    gap = int(time.time() - last_success_time[handle])
                    log.info(f"Twitter: back online — was down {gap}s for @{handle}")
                    nitter_was_down[handle] = False
                last_success_time[handle] = time.time()

                # ── Process new items ──────────────────────────────────────────
                new_items = [(g, t) for g, t in items if g not in seen_guids[handle]]
                if new_items:
                    for g, _ in new_items:
                        seen_guids[handle].add(g)
                    if use_official_api:
                        since_id[handle] = new_items[0][0]

                    if not first_run[handle]:
                        for guid, text in new_items:
                            for ca in extract_cas(text):
                                if ca in already_seen or ca in positions:
                                    continue
                                already_seen.add(ca)
                                save_seen()
                                log.info(f"🐦 CA from @{handle}: {ca} | tweet {guid}")
                                # Fire as background task — analysis runs for 120s, doesn't block polling
                                asyncio.ensure_future(analyze_and_buy(ca, guid, text, handle))

                    first_run[handle] = False

            except Exception as e:
                log.warning(f"Twitter poll error for @{handle}: {e}")

        await asyncio.sleep(TWITTER_POLL_SEC)

# ─── TELEGRAM LISTENER ────────────────────────────────────────────────────────
def is_valid_solana_ca(addr: str) -> bool:
    """Basic validation — correct length and base58."""
    try:
        decoded = base58.b58decode(addr)
        return len(decoded) == 32
    except Exception:
        return False

def extract_cas(text: str) -> list[str]:
    matches = CA_PATTERN.findall(text)
    return [m for m in matches if is_valid_solana_ca(m) and m.endswith("pump")]

# ─── MANUAL CONTROL COMMANDS (via the notify bot, long-polling getUpdates) ────
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
        log.warning(f"Control command poll failed: {e}")
        return []

async def handle_control_command(text: str):
    parts = text.split()
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    loop = asyncio.get_event_loop()

    if cmd == "/watch":
        if not arg or not is_valid_solana_ca(arg):
            notify("⚠️ Usage: /watch <CA> [size_sol]")
            return
        if arg in positions:
            notify(f"⚠️ Already holding a position in <code>{arg}</code>")
            return
        try:
            size = float(parts[2]) if len(parts) > 2 else BUY_AMOUNT_SOL
        except ValueError:
            notify("⚠️ size_sol must be a number")
            return
        already_seen.add(arg)
        save_seen()
        await add_to_watchlist(arg, size, kol_handle="manual")

    elif cmd == "/stop":
        if not arg:
            notify("⚠️ Usage: /stop <CA>")
            return
        had_position = arg in positions
        removed = TRACKED.pop(arg, None) is not None
        if not removed:
            notify(f"ℹ️ <code>{arg}</code> wasn't being tracked")
            return
        if had_position:
            notify(
                f"🛑 Stopped automated trading for <code>{arg}</code>\n"
                f"⚠️ Position is still OPEN with no automated exit — use /sell {arg} to close it"
            )
        else:
            notify(f"🛑 Stopped watching <code>{arg}</code>")

    elif cmd == "/sell":
        if not arg:
            notify("⚠️ Usage: /sell <CA>")
            return
        if arg not in positions:
            notify(f"⚠️ No open position in <code>{arg}</code>")
            return
        await loop.run_in_executor(None, sell_token, arg, "manual /sell command", 1.0)
        TRACKED.pop(arg, None)

async def monitor_control_commands():
    """Listen for /watch, /stop, /sell sent to the notify bot's chat."""
    if not NOTIFY_BOT_TOKEN or not NOTIFY_CHAT_ID:
        log.info("Control commands disabled — NOTIFY_BOT_TOKEN/NOTIFY_CHAT_ID not set")
        return

    loop = asyncio.get_event_loop()
    offset = None
    log.info("🎮 Listening for /watch, /stop, /sell commands...")
    while True:
        updates = await loop.run_in_executor(None, _tg_get_updates, offset)
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

# ─── FILE-BASED CONTROL CHANNEL (fallback, independent of Telegram) ───────────
# Write a JSON list of command strings (e.g. ["/watch <CA> 0.18"]) to this file;
# it's picked up within a few seconds and then cleared.
CONTROL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "control_commands.json")

def _read_control_commands() -> list:
    import json
    try:
        if not os.path.exists(CONTROL_FILE):
            return []
        with open(CONTROL_FILE, "r") as f:
            cmds = json.load(f)
        if cmds:
            with open(CONTROL_FILE, "w") as f:
                json.dump([], f)
        return cmds
    except Exception as e:
        log.warning(f"Failed to read control file: {e}")
        return []

async def monitor_control_file():
    log.info(f"📄 Watching {CONTROL_FILE} for manual commands...")
    while True:
        await asyncio.sleep(5)
        for text in _read_control_commands():
            try:
                await handle_control_command(text)
            except Exception as e:
                log.warning(f"Control file command error ({text!r}): {e}")
                notify(f"⚠️ Command error: {e}")

async def main():
    log.info("🚀 Sniper bot running (Twitter-only mode)...")
    asyncio.ensure_future(monitor_tracked())
    asyncio.ensure_future(monitor_control_commands())
    asyncio.ensure_future(monitor_control_file())
    await monitor_twitter()  # runs forever; keeps the loop alive

if __name__ == "__main__":
    asyncio.run(main())
