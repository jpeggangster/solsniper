"""
12/21 EMA crossover bot on a 5-second timeframe — Solana memecoins.

    ema_cross_strategy.py   the indicator + entry/exit rules
    pumpportal_feed.py      real-time swap ticks (what makes 5s bars real)
    ema_cross_bot.py        this file: watchlist, reconciliation, execution

Runs in PAPER mode by default. Paper fills are charged the same fees and
slippage a live trade would pay, so the PnL it prints is comparable to reality
rather than flattering. Go live only after a paper session shows positive
net-of-cost PnL over a decent sample.

  # paper trade the current watchlist, recording ticks for later backtests
  /opt/homebrew/bin/python3.14 ema_cross_bot.py --auto-discover

  # watch specific mints
  /opt/homebrew/bin/python3.14 ema_cross_bot.py --mints <MINT1> <MINT2>

  # live trading (real SOL — read the warning it prints)
  /opt/homebrew/bin/python3.14 ema_cross_bot.py --live --mints <MINT>

Watchlist file (ema_watchlist.json) is re-read every few seconds, so other
processes can add mints to it while this is running:
    {"mints": ["<mint>", ...]}

NOTE ON WALLET SHARING: solana_sniper_bot.py and hood_sniper_bot.py use the same
wallet. In --live mode, do not point this bot at a mint another bot is already
holding — both would try to manage the same balance.
"""

import argparse
import asyncio
import json
import logging
import os
import time
from typing import Optional

import requests
from dotenv import load_dotenv

import ema_cross_strategy as strat
from pumpportal_feed import PumpPortalFeed

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

# ─── CONFIG ───────────────────────────────────────────────────────────────────
PUMPPORTAL_API_KEY = os.getenv("PUMPPORTAL_API_KEY", "")
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")
RPC_URL            = os.getenv("HELIUS_RPC", "https://api.mainnet-beta.solana.com")
NOTIFY_BOT_TOKEN   = os.getenv("NOTIFY_BOT_TOKEN", "")
NOTIFY_CHAT_ID     = os.getenv("NOTIFY_CHAT_ID", "")

BUY_AMOUNT_SOL   = float(os.getenv("EMA_BUY_AMOUNT_SOL", "0.18"))
SLIPPAGE_PCT     = int(os.getenv("EMA_SLIPPAGE_PCT", "25"))
PRIORITY_FEE_SOL = float(os.getenv("EMA_PRIORITY_FEE_SOL", "0.001"))
MAX_CONCURRENT   = int(os.getenv("EMA_MAX_CONCURRENT", "5"))
WATCH_TIMEOUT    = int(os.getenv("EMA_WATCH_TIMEOUT_SEC", "1800"))  # drop cold tokens

# Entry quality gates. Measured live: of 60 freshly-created pump.fun tokens only
# ~4 stayed alive long enough to warm up a 105s indicator. On a barely-traded
# token most 5s bars are gap-filled flat, so the EMAs end up measuring silence
# and the "cross" is an artifact. These gates require the token to be genuinely
# active before any real money is committed.
MIN_TICKS_FOR_ENTRY = int(os.getenv("EMA_MIN_TICKS", "30"))     # real swaps seen
MIN_MCAP_SOL        = float(os.getenv("EMA_MIN_MCAP_SOL", "40"))
MIN_TICKS_PER_BAR   = float(os.getenv("EMA_MIN_TICK_DENSITY", "0.5"))  # avg ticks/bar

# What a round trip actually costs. Charged against paper fills so paper PnL is
# honest: pump.fun swap fee + PumpPortal fee on each side, plus realistic
# slippage on thin memecoin liquidity.
FEE_PCT_PER_SIDE      = float(os.getenv("EMA_FEE_PCT_PER_SIDE", "1.5"))
SLIPPAGE_EST_PER_SIDE = float(os.getenv("EMA_SLIPPAGE_EST_PCT", "1.0"))
COST_PER_SIDE = (FEE_PCT_PER_SIDE + SLIPPAGE_EST_PER_SIDE) / 100

WATCHLIST_FILE = os.path.join(HERE, "ema_watchlist.json")
POSITIONS_FILE = os.path.join(HERE, "ema_cross_positions.json")   # separate from positions.json
TICKS_FILE     = os.path.join(HERE, "ema_ticks.jsonl")            # replay data for backtests
TRADES_FILE    = os.path.join(HERE, "ema_cross_trades.jsonl")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(os.path.join(HERE, "ema_cross.log")),
              logging.StreamHandler()],
)
log = logging.getLogger("ema_cross")
logging.getLogger("websockets").setLevel(logging.WARNING)

TRACKED: dict[str, dict] = {}
POSITIONS: dict[str, dict] = {}
PENDING: set[str] = set()          # mints with a trade in flight — see reconcile()
DISCOVERED: list[tuple[str, str]] = []   # firehose finds, drained by discovery_drain
STATS = {"closed": 0, "wins": 0, "net_sol": 0.0, "net_pct": 0.0}
LIVE = False
RECORD_TICKS = True
_tick_fh = None


# ─── PERSISTENCE ──────────────────────────────────────────────────────────────
def save_positions() -> None:
    try:
        with open(POSITIONS_FILE, "w") as f:
            json.dump(POSITIONS, f, indent=2)
    except Exception as e:
        log.error(f"save_positions failed: {e}")


def load_positions() -> dict:
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def record_trade(rec: dict) -> None:
    try:
        with open(TRADES_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        log.error(f"record_trade failed: {e}")


def notify(msg: str) -> None:
    if not NOTIFY_BOT_TOKEN or not NOTIFY_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{NOTIFY_BOT_TOKEN}/sendMessage",
            json={"chat_id": NOTIFY_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"notify failed: {e}")


# ─── EXECUTION ────────────────────────────────────────────────────────────────
_keypair = None


def keypair():
    global _keypair
    if _keypair is None:
        import base58
        from solders.keypair import Keypair  # type: ignore
        _keypair = Keypair.from_bytes(base58.b58decode(WALLET_PRIVATE_KEY))
    return _keypair


def pumpportal_trade(action: str, mint: str, amount, denominated_in_sol: bool = True) -> Optional[str]:
    """Build via PumpPortal trade-local, sign locally, send through Helius."""
    from solders.transaction import VersionedTransaction  # type: ignore
    from solana.rpc.api import Client  # type: ignore
    from solana.rpc.types import TxOpts

    try:
        resp = requests.post(
            "https://pumpportal.fun/api/trade-local",
            json={
                "publicKey": str(keypair().pubkey()),
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
            log.error(f"PumpPortal {resp.status_code}: {resp.text[:200]}")
            return None
        tx = VersionedTransaction.from_bytes(resp.content)
        signed = VersionedTransaction(tx.message, [keypair()])
        result = Client(RPC_URL).send_raw_transaction(bytes(signed), opts=TxOpts(skip_preflight=True))
        sig = str(result.value)
        log.info(f"TX https://solscan.io/tx/{sig}")
        return sig
    except Exception as e:
        log.error(f"trade failed: {e}")
        return None


def do_buy(mint: str, t: dict) -> bool:
    price = t["last_price"]
    if not price:
        return False
    size = t["size_sol"]

    if LIVE:
        sig = pumpportal_trade("buy", mint, size, denominated_in_sol=True)
        if not sig:
            log.error(f"BUY FAILED {mint}")
            return False
    else:
        sig = "PAPER"

    fill = price * (1 + COST_PER_SIDE)          # pay the spread on the way in
    POSITIONS[mint] = {
        "entry_price": fill,
        "signal_price": price,
        "entry_time": time.time(),
        "size_sol": size,
        "sig": sig,
        "live": LIVE,
        "sell_failures": 0,
    }
    save_positions()
    strat.set_entry_price(t, fill)
    tag = "LIVE" if LIVE else "PAPER"
    log.info(f"🟢 BUY [{tag}] {mint[:10]}… @ {fill:.4e} SOL  size {size} SOL  ({strat.status(t)})")
    notify(f"🟢 <b>BUY [{tag}]</b> — EMA{strat.FAST_PERIOD}/{strat.SLOW_PERIOD} cross ({strat.BAR_SEC}s)\n"
           f"<code>{mint}</code>\nEntry: {fill:.4e} SOL | {size} SOL")
    record_trade({"ts": time.time(), "side": "buy", "mint": mint, "price": fill,
                  "signal_price": price, "size_sol": size, "live": LIVE, "sig": sig})
    return True


def do_sell(mint: str, t: dict, reason: str) -> bool:
    pos = POSITIONS.get(mint)
    if not pos:
        return True
    price = t["last_price"]

    if pos.get("live") and not LIVE:
        # A real position is open but this process is in paper mode. Closing it
        # in the ledger would abandon actual tokens on-chain, so refuse and keep
        # saying so until a human deals with it.
        log.error(f"⚠️  {mint} holds a LIVE position but this process is PAPER. "
                  f"Not closing it. Restart with --live, or sell it manually.")
        return False

    if LIVE and pos.get("live"):
        sig = pumpportal_trade("sell", mint, "100%", denominated_in_sol=False)
        if not sig:
            pos["sell_failures"] = pos.get("sell_failures", 0) + 1
            pos["retry_after"] = time.time() + min(2 ** pos["sell_failures"], 60)
            save_positions()
            log.error(f"SELL FAILED {mint} (attempt {pos['sell_failures']}) — "
                      f"retrying in {pos['retry_after'] - time.time():.0f}s")
            return False
    else:
        sig = "PAPER"

    fill = price * (1 - COST_PER_SIDE) if price else pos["entry_price"]
    pnl_pct = (fill - pos["entry_price"]) / pos["entry_price"] * 100
    pnl_sol = pos["size_sol"] * pnl_pct / 100
    held = time.time() - pos["entry_time"]

    tag = "LIVE" if pos.get("live") else "PAPER"
    emoji = "🟩" if pnl_pct >= 0 else "🟥"
    log.info(f"{emoji} SELL [{tag}] {mint[:10]}… @ {fill:.4e}  "
             f"PnL {pnl_pct:+.2f}% ({pnl_sol:+.4f} SOL)  held {held:.0f}s  — {reason}")
    notify(f"{emoji} <b>SELL [{tag}]</b> — {reason}\n<code>{mint}</code>\n"
           f"PnL: <b>{pnl_pct:+.2f}%</b> ({pnl_sol:+.4f} SOL) | held {held:.0f}s")
    record_trade({"ts": time.time(), "side": "sell", "mint": mint, "price": fill,
                  "reason": reason, "pnl_pct": pnl_pct, "pnl_sol": pnl_sol,
                  "held_sec": held, "live": pos.get("live"), "sig": sig})

    STATS["closed"] += 1
    STATS["wins"] += 1 if pnl_pct > 0 else 0
    STATS["net_sol"] += pnl_sol
    STATS["net_pct"] += pnl_pct

    POSITIONS.pop(mint, None)
    save_positions()
    return True


# ─── RECONCILIATION ───────────────────────────────────────────────────────────
def entry_gate(t: dict) -> tuple[bool, str]:
    """Is this token active enough to trade? Exits are never gated — once in a
    position we always want the ability to get out."""
    if t["ticks"] < MIN_TICKS_FOR_ENTRY:
        return False, f"only {t['ticks']} ticks (need {MIN_TICKS_FOR_ENTRY})"
    density = t["ticks"] / max(t["bars_seen"], 1)
    if density < MIN_TICKS_PER_BAR:
        return False, f"tick density {density:.2f}/bar (need {MIN_TICKS_PER_BAR}) - mostly gap-filled"
    mcap = t.get("mcap_sol")
    if MIN_MCAP_SOL > 0 and mcap is not None and mcap < MIN_MCAP_SOL:
        return False, f"mcap {mcap:.0f} SOL (need {MIN_MCAP_SOL:.0f})"
    return True, ""


async def _execute(mint: str, t: dict, want_long: bool) -> None:
    """Run the blocking trade off the event loop, then release the mint."""
    try:
        if want_long:
            await asyncio.to_thread(do_buy, mint, t)
        else:
            await asyncio.to_thread(do_sell, mint, t, t.get("exit_reason") or "target flat")
    except Exception as e:
        log.error(f"execute {mint[:10]}…: {e}", exc_info=True)
    finally:
        PENDING.discard(mint)


def reconcile(mint: str, t: dict) -> None:
    """Drive the actual position toward the strategy's target state.

    Idempotent — a failed trade just leaves the gap open and the next tick
    retries it. The trade itself runs in a worker thread: PumpPortal's build
    call plus the RPC send take seconds, and doing that inline would stall the
    websocket feed, which on a 5s timeframe means missing whole bars.
    """
    if mint in PENDING:
        return                       # trade already in flight; don't double-fire
    holding = mint in POSITIONS
    if holding and time.time() < POSITIONS[mint].get("retry_after", 0):
        return                       # backing off after a failed sell
    if t["want_long"] and not holding:
        if len(POSITIONS) + len(PENDING) >= MAX_CONCURRENT:
            return
        ok, why = entry_gate(t)
        if not ok:
            log.debug(f"skip buy {mint[:10]}… — {why}")
            return
    elif t["want_long"] == holding:
        return                       # already in the desired state

    PENDING.add(mint)
    asyncio.get_running_loop().create_task(_execute(mint, t, t["want_long"]))


# ─── WATCHLIST ────────────────────────────────────────────────────────────────
def read_watchlist() -> list[str]:
    try:
        with open(WATCHLIST_FILE) as f:
            data = json.load(f)
        return [m for m in data.get("mints", []) if isinstance(m, str)]
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning(f"watchlist unreadable: {e}")
        return []


async def add_mints(feed: PumpPortalFeed, mints: list[str], source: str = "") -> None:
    fresh = [m for m in mints if m not in TRACKED]
    if not fresh:
        return
    for m in fresh:
        TRACKED[m] = strat.new_tracker(m, BUY_AMOUNT_SOL, source=source)
    await feed.subscribe(fresh)
    log.info(f"👀 tracking +{len(fresh)} ({source or 'manual'}) — "
             f"{len(TRACKED)} total, warmup {strat.warmup_bars_needed() * strat.BAR_SEC}s")


# ─── TASKS ────────────────────────────────────────────────────────────────────
def on_tick(mint: str, price: float, ts: float, msg: dict) -> None:
    t = TRACKED.get(mint)
    if t is None:
        return
    if RECORD_TICKS and _tick_fh:
        _tick_fh.write(json.dumps({"ts": ts, "mint": mint, "price": price,
                                   "side": msg.get("txType"),
                                   "sol": msg.get("solAmount"),
                                   "mcap": msg.get("marketCapSol")}) + "\n")
    t["mcap_sol"] = msg.get("marketCapSol") or t.get("mcap_sol")
    if strat.ingest_tick(t, price, ts):
        reconcile(mint, t)


async def bar_clock(feed: PumpPortalFeed) -> None:
    """Close elapsed bars even when a token has gone quiet, and retire tokens
    that never went anywhere. Ticks alone can't advance a dead chart."""
    while True:
        await asyncio.sleep(1.0)
        now = time.time()
        for mint, t in list(TRACKED.items()):
            try:
                if strat.flush(t, now):
                    reconcile(mint, t)
            except Exception as e:
                log.error(f"flush {mint[:10]}…: {e}", exc_info=True)

            if mint not in POSITIONS and now - t["created_at"] > WATCH_TIMEOUT:
                TRACKED.pop(mint, None)
                await feed.unsubscribe([mint])
                log.info(f"⌛ dropped {mint[:10]}… — no trade in {WATCH_TIMEOUT}s")


async def watchlist_poller(feed: PumpPortalFeed) -> None:
    while True:
        await add_mints(feed, read_watchlist(), source="watchlist")
        await asyncio.sleep(5)


def make_discovery_handler(feed: PumpPortalFeed, label: str, max_tracked: int):
    """Handle a firehose message from the shared socket by queueing the mint.

    subscribeMigration  — tokens that just graduated to pump-amm. These have
        already proven sustained volume, which is what this strategy needs: a
        12/21 EMA on 5s bars takes 105s just to warm up, and measured on live
        data only 14% of brand-new tokens trade for that long at all.

    subscribeNewToken   — every launch. ~86% are dead before the indicator is
        even valid, so this is a data-gathering source, not a trading one.
    """
    def handler(msg: dict) -> None:
        mint = msg.get("mint")
        if not mint or len(TRACKED) >= max_tracked:
            return
        tag = msg.get("symbol") or msg.get("pool") or "?"
        DISCOVERED.append((mint, f"{label}:{tag}"))
    return handler


async def discovery_drain(feed: PumpPortalFeed) -> None:
    """Subscribe mints found by the firehose handlers. The handlers themselves
    are sync callbacks off the socket, so they only queue; the awaiting happens
    here."""
    while True:
        await asyncio.sleep(0.5)
        if not DISCOVERED:
            continue
        batch, DISCOVERED[:] = list(DISCOVERED), []
        by_source: dict[str, list[str]] = {}
        for mint, source in batch:
            by_source.setdefault(source, []).append(mint)
        for source, mints in by_source.items():
            await add_mints(feed, mints, source=source)


async def heartbeat(feed: PumpPortalFeed) -> None:
    while True:
        await asyncio.sleep(60)
        ready = sum(1 for t in TRACKED.values() if strat.is_ready(t))
        if STATS["closed"]:
            pnl_line = (f" | closed {STATS['closed']}, {STATS['wins']}/{STATS['closed']} green, "
                        f"avg {STATS['net_pct'] / STATS['closed']:+.2f}%, "
                        f"net {STATS['net_sol']:+.4f} SOL")
        else:
            pnl_line = " | no closed trades yet"
        log.info(f"💓 tracking {len(TRACKED)} ({ready} warm) | open {len(POSITIONS)} | "
                 f"{feed.stats['ticks']} ticks, {feed.stats['reconnects']} reconnects{pnl_line}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main() -> None:
    global LIVE, RECORD_TICKS, _tick_fh, POSITIONS

    ap = argparse.ArgumentParser(description="12/21 EMA cross bot on 5s bars")
    ap.add_argument("--live", action="store_true", help="trade real SOL (default: paper)")
    ap.add_argument("--mints", nargs="*", default=[], help="mints to watch immediately")
    ap.add_argument("--from-migrations", action="store_true",
                    help="track tokens graduating to pump-amm (proven volume — recommended)")
    ap.add_argument("--auto-discover", action="store_true",
                    help="track every new pump.fun launch (~86%% die before the 105s warmup; "
                         "use for gathering tick data, not for trading)")
    ap.add_argument("--max-tracked", type=int, default=40)
    ap.add_argument("--no-record", action="store_true", help="don't write ema_ticks.jsonl")
    args = ap.parse_args()

    LIVE = args.live
    RECORD_TICKS = not args.no_record
    POSITIONS = load_positions()

    if LIVE:
        if not WALLET_PRIVATE_KEY:
            raise SystemExit("--live needs WALLET_PRIVATE_KEY in .env")
        log.warning("=" * 68)
        log.warning("LIVE MODE — real SOL. Other bots share this wallet; make sure")
        log.warning("they are not managing the same mints.")
        log.warning("=" * 68)
    else:
        log.info("PAPER MODE — no real orders. Fills charged "
                 f"{COST_PER_SIDE * 100:.1f}%/side ({COST_PER_SIDE * 200:.1f}% round trip).")

    log.info(f"Strategy: EMA{strat.FAST_PERIOD}/EMA{strat.SLOW_PERIOD} cross on {strat.BAR_SEC}s bars "
             f"| warmup {strat.SLOW_PERIOD * strat.BAR_SEC}s | edge>={strat.MIN_EDGE_BPS:.0f}bps "
             f"| hold>={strat.MIN_HOLD_BARS} bars | cooldown {strat.COOLDOWN_BARS} bars "
             f"| stop -{strat.STOP_LOSS_PCT:g}% trail -{strat.TRAIL_PCT:g}%")

    if RECORD_TICKS:
        _tick_fh = open(TICKS_FILE, "a", buffering=1)
        log.info(f"recording ticks -> {TICKS_FILE} (replay with backtest_ema_cross.py)")

    feed = PumpPortalFeed(PUMPPORTAL_API_KEY, on_tick)
    if args.from_migrations:
        feed.on_migration = make_discovery_handler(feed, "migrated", args.max_tracked)
    if args.auto_discover:
        feed.on_new_token = make_discovery_handler(feed, "new", args.max_tracked)

    tasks = [
        asyncio.create_task(feed.run()),
        asyncio.create_task(bar_clock(feed)),
        asyncio.create_task(watchlist_poller(feed)),
        asyncio.create_task(discovery_drain(feed)),
        asyncio.create_task(heartbeat(feed)),
    ]

    await asyncio.sleep(1.5)
    if POSITIONS:
        # Open positions from a previous run still need an exit. Re-subscribe to
        # them and mark the tracker long, so the stops and the next cross-down
        # apply instead of the position sitting unmanaged.
        resumed = list(POSITIONS.keys())
        await add_mints(feed, resumed, source="resumed")
        for m in resumed:
            t = TRACKED.get(m)
            if t:
                t["want_long"] = True
                strat.set_entry_price(t, POSITIONS[m]["entry_price"])
        log.warning(f"resumed {len(resumed)} open position(s) from {os.path.basename(POSITIONS_FILE)}: "
                    f"{', '.join(m[:8] + '…' for m in resumed)}")

    # Everything shares one websocket — PumpPortal asks for a single connection
    # per key and multiple sockets get throttled (measured) or banned.
    if args.from_migrations:
        await feed.subscribe_stream("subscribeMigration")
    if args.auto_discover:
        await feed.subscribe_stream("subscribeNewToken")
    if args.mints:
        await add_mints(feed, args.mints, source="cli")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        if _tick_fh:
            _tick_fh.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutting down")
