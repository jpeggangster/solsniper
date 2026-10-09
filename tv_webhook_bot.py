"""
TradingView Alert -> Solana Auto-Buy Webhook Bot
-------------------------------------------------
Listens for TradingView webhook alerts (e.g. Bot2Slow 4H buy signal on any
memecoin chart) and auto-buys the token on Solana via PumpPortal.

You put the Solana CA + position size directly in the TradingView alert
message when you create the alert. This bot doesn't need a symbol->CA
mapping — you decide the CA per-alert, per-coin, at alert-creation time.

SETUP:
  pip install flask requests python-dotenv solders solana base58

Add to .env (alongside your existing sniper bot keys):
  WALLET_PRIVATE_KEY=...        (already set — reused from solana_sniper_bot.py)
  HELIUS_RPC=...                (already set)
  PUMPPORTAL_API_KEY=...        (already set)
  NOTIFY_BOT_TOKEN=...          (already set — Telegram alert bot)
  NOTIFY_CHAT_ID=...            (already set)
  TV_WEBHOOK_SECRET=<pick a long random string>
  TV_WEBHOOK_PORT=7071          (optional, defaults to 7071)
  TV_MAX_SOL_PER_TRADE=1.0      (optional safety cap, defaults to 1.0 SOL)

TradingView alert message body (Message box in the alert dialog), example:
  {"secret": "<TV_WEBHOOK_SECRET>", "ca": "<solana token mint address>", "sol": 0.25, "symbol": "ANSEM"}

Webhook URL (once ngrok is running — see start_tv_webhook.sh):
  https://<your-ngrok-subdomain>.ngrok-free.app/tv-webhook

Run:
  /opt/homebrew/bin/python3.14 /Users/jpreddy/sniper/tv_webhook_bot.py
"""

import base64
import json
import logging
import os
import time
from typing import Optional

import base58
import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from solana.rpc.api import Client  # type: ignore
from solana.rpc.types import TxOpts
from solders.keypair import Keypair  # type: ignore
from solders.transaction import VersionedTransaction  # type: ignore

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("tv_webhook.log"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")
RPC_URL = os.getenv("HELIUS_RPC", "https://api.mainnet-beta.solana.com")
PUMPPORTAL_API_KEY = os.getenv("PUMPPORTAL_API_KEY", "")
NOTIFY_BOT_TOKEN = os.getenv("NOTIFY_BOT_TOKEN", "")
NOTIFY_CHAT_ID = os.getenv("NOTIFY_CHAT_ID", "")

TV_WEBHOOK_SECRET = os.getenv("TV_WEBHOOK_SECRET", "")
TV_WEBHOOK_PORT = int(os.getenv("TV_WEBHOOK_PORT", "7071"))
TV_MAX_SOL_PER_TRADE = float(os.getenv("TV_MAX_SOL_PER_TRADE", "1.0"))

SLIPPAGE_PCT = 25
PRIORITY_FEE_SOL = 0.001
DEDUPE_WINDOW_SEC = 90  # ignore repeat webhook fires for the same CA within this window

POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tv_positions.json")

if not TV_WEBHOOK_SECRET:
    raise SystemExit("TV_WEBHOOK_SECRET is not set in .env — refusing to start an unauthenticated public webhook.")

# ─── WALLET ───────────────────────────────────────────────────────────────────
def load_keypair() -> Keypair:
    raw = base58.b58decode(WALLET_PRIVATE_KEY)
    return Keypair.from_bytes(raw)


KEYPAIR = load_keypair() if WALLET_PRIVATE_KEY else None

# ─── STATE ────────────────────────────────────────────────────────────────────
def load_positions() -> dict:
    try:
        if os.path.exists(POSITIONS_FILE):
            with open(POSITIONS_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        log.warning(f"Failed to load positions: {e}")
    return {}


def save_positions():
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(positions, f, indent=2)
    except Exception as e:
        log.warning(f"Failed to save positions: {e}")


positions: dict = load_positions()
_last_trigger: dict = {}  # ca -> unix timestamp of last accepted webhook

# ─── TELEGRAM NOTIFY ──────────────────────────────────────────────────────────
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


# ─── PUMPPORTAL TRADE-LOCAL (sign + send ourselves) ──────────────────────────
def pumpportal_trade(action: str, mint: str, amount, denominated_in_sol: bool = True) -> Optional[str]:
    wallet_pubkey = str(KEYPAIR.pubkey())
    try:
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


def get_price_usd(ca: str) -> Optional[float]:
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", timeout=10)
        pairs = r.json().get("pairs") or []
        if not pairs:
            return None
        pairs.sort(key=lambda p: float(p.get("liquidity", {}).get("usd", 0)), reverse=True)
        return float(pairs[0]["priceUsd"])
    except Exception as e:
        log.warning(f"Price fetch failed for {ca}: {e}")
        return None


def buy_token(ca: str, sol_amount: float, symbol: str = ""):
    if not PUMPPORTAL_API_KEY:
        log.error("No PumpPortal API key configured!")
        return
    if not KEYPAIR:
        log.error("No wallet configured!")
        return
    label = symbol or ca
    log.info(f"🟢 BUYING {label} ({ca}) — {sol_amount} SOL [TradingView Bot2Slow signal]")
    sig = pumpportal_trade("buy", ca, sol_amount, denominated_in_sol=True)
    if sig:
        buy_price = get_price_usd(ca)
        positions[ca] = {
            "symbol": symbol,
            "buy_price": buy_price,
            "buy_time": time.time(),
            "amount_sol": sol_amount,
            "sig": sig,
        }
        save_positions()
        log.info(f"✅ Position opened: {label} @ ${buy_price}")
        notify(
            f"🟢 <b>TV SIGNAL BUY</b> — {label}\n<code>{ca}</code>\n"
            f"Entry: <b>${buy_price}</b>\n{sol_amount} SOL\n🔗 solscan.io/tx/{sig}"
        )
    else:
        log.error(f"❌ Buy failed for {label} ({ca})")
        notify(f"❌ <b>TV SIGNAL BUY FAILED</b> — {label}\n<code>{ca}</code>")


# ─── SOLANA MINT ADDRESS SANITY CHECK ────────────────────────────────────────
def looks_like_mint(ca: str) -> bool:
    if not (32 <= len(ca) <= 44):
        return False
    try:
        base58.b58decode(ca)
        return True
    except Exception:
        return False


# ─── FLASK APP ────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/tv-webhook", methods=["POST"])
def tv_webhook():
    raw = request.get_data(as_text=True)
    payload = request.get_json(silent=True)
    if payload is None:
        try:
            payload = json.loads(raw)
        except Exception:
            log.warning(f"Rejected webhook — could not parse body: {raw[:300]!r}")
            return jsonify({"ok": False, "error": "invalid body, expected JSON"}), 400

    secret = payload.get("secret", "")
    if secret != TV_WEBHOOK_SECRET:
        log.warning("Rejected webhook — bad secret")
        return jsonify({"ok": False, "error": "unauthorized"}), 403

    ca = str(payload.get("ca", "")).strip()
    symbol = str(payload.get("symbol", "")).strip()
    action = str(payload.get("action", "buy")).strip().lower()

    try:
        sol_amount = float(payload.get("sol", 0))
    except (TypeError, ValueError):
        log.warning(f"Rejected webhook — bad sol amount: {payload.get('sol')!r}")
        return jsonify({"ok": False, "error": "invalid sol amount"}), 400

    if not looks_like_mint(ca):
        log.warning(f"Rejected webhook — CA doesn't look like a Solana mint: {ca!r}")
        return jsonify({"ok": False, "error": "invalid ca"}), 400

    if sol_amount <= 0 or sol_amount > TV_MAX_SOL_PER_TRADE:
        log.warning(f"Rejected webhook — sol amount {sol_amount} outside (0, {TV_MAX_SOL_PER_TRADE}]")
        return jsonify({"ok": False, "error": f"sol amount must be in (0, {TV_MAX_SOL_PER_TRADE}]"}), 400

    if action != "buy":
        log.info(f"Ignoring non-buy action: {action}")
        return jsonify({"ok": True, "ignored": True}), 200

    now = time.time()
    last = _last_trigger.get(ca, 0)
    if now - last < DEDUPE_WINDOW_SEC:
        log.info(f"Deduped repeat webhook for {ca} ({now - last:.0f}s since last)")
        return jsonify({"ok": True, "deduped": True}), 200
    _last_trigger[ca] = now

    log.info(f"📡 TradingView signal received: {symbol or ca} — buy {sol_amount} SOL")
    notify(f"📡 <b>TV Bot2Slow signal</b> — {symbol or ca}\nBuying {sol_amount} SOL...")
    buy_token(ca, sol_amount, symbol)

    return jsonify({"ok": True}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "positions": len(positions)}), 200


if __name__ == "__main__":
    log.info(f"Starting TradingView webhook bot on port {TV_WEBHOOK_PORT}")
    app.run(host="127.0.0.1", port=TV_WEBHOOK_PORT)
