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

BUY_AMOUNT_SOL    = 0.18         # SOL per trade
STOP_LOSS_PCT     = -0.60        # sell 100% at -60%
SLIPPAGE_PCT      = 25           # 25% slippage
PRIORITY_FEE_SOL  = 0.001        # priority fee in SOL
POLL_INTERVAL_SEC = 10           # how often to check price for open positions

# Tiered take-profit: (gain_pct, sell_fraction_of_remaining)
# gain_pct  = required gain from entry (1.0 = +100%)
# sell_frac = fraction of current token balance to sell at this level
TAKE_PROFIT_LEVELS = [
    {"gain_pct": 1.00, "sell_frac": 0.50},  # +100% → sell 50%
    {"gain_pct": 2.00, "sell_frac": 0.33},  # +200% → sell 33%
    {"gain_pct": 3.00, "sell_frac": 0.25},  # +300% → sell 25%
    {"gain_pct": 5.00, "sell_frac": 0.16},  # +500% → sell 16%
]

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
            "tp_hit": [False] * len(TAKE_PROFIT_LEVELS),
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

# ─── POSITION MONITOR ─────────────────────────────────────────────────────────
async def monitor_positions():
    while True:
        await asyncio.sleep(POLL_INTERVAL_SEC)
        for ca, pos in list(positions.items()):
            current_price = get_price_usd(ca)
            if current_price is None:
                continue
            buy_price = pos["buy_price"]
            if buy_price is None or buy_price == 0:
                pos["buy_price"] = current_price
                continue

            change = (current_price - buy_price) / buy_price
            log.info(f"📊 {ca[:8]}... | entry=${buy_price:.6f} | now=${current_price:.6f} | {change*100:+.1f}%")

            # Stop-loss: sell everything
            if change <= STOP_LOSS_PCT:
                sell_token(ca, f"STOP LOSS {change*100:+.1f}%", sell_frac=1.0)
                continue

            # Tiered take-profits (check highest first so we don't double-fire lower ones)
            for i in range(len(TAKE_PROFIT_LEVELS) - 1, -1, -1):
                if pos["tp_hit"][i]:
                    continue
                tp = TAKE_PROFIT_LEVELS[i]
                if change >= tp["gain_pct"]:
                    label = f"TP{i+1} +{tp['gain_pct']*100:.0f}%"
                    success = sell_token(ca, label, sell_frac=tp["sell_frac"])
                    if success:
                        pos["tp_hit"][i] = True  # only mark done after confirmed sell
                        save_positions()
                    break  # only fire one TP per poll cycle

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
            f"📊 <b>Score {verdict.score:.0f}/100</b> — buying {verdict.size_sol} SOL\n"
            f"<code>{ca}</code>\n{breakdown}"
        )
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, buy_token, ca, verdict.size_sol)

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

async def main():
    log.info("🚀 Sniper bot running (Twitter-only mode)...")
    asyncio.ensure_future(monitor_positions())
    await monitor_twitter()  # runs forever; keeps the loop alive

if __name__ == "__main__":
    asyncio.run(main())
