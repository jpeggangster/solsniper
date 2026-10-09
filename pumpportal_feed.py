"""
Real-time Solana swap tick feed over PumpPortal's websocket.

One connection multiplexes every tracked mint. Each trade message becomes one
price tick, which is what makes 5-second candles possible at all — REST polling
DexScreener tops out around one sample per 10-15s, an order of magnitude too
slow for a 5s bar.

Requires PUMPPORTAL_API_KEY on a wallet funded with >= 0.02 SOL; without the key
the server accepts the connection but refuses subscribeTokenTrade. Verified
working against the key in .env.

Message shape (pump bonding-curve pool):
  {signature, mint, traderPublicKey, txType: buy|sell, tokenAmount, solAmount,
   vTokensInBondingCurve, vSolInBondingCurve, marketCapSol, pool}

Price is taken from the reserve ratio (vSol/vTokens) when present — that is the
pool's mid price after the swap, and is not skewed by which side traded. For
graduated tokens on AMM pools those fields are absent, so the executed price
(solAmount/tokenAmount) is used instead; it carries a small fee-side bias but is
the only thing available there.
"""

import asyncio
import json
import logging
import os
import time
from typing import Callable, Iterable, Optional

import websockets

log = logging.getLogger(__name__)

WS_BASE = "wss://pumpportal.fun/api/data"
RECONNECT_MIN, RECONNECT_MAX = 1.0, 30.0


def price_from_trade(msg: dict) -> Optional[float]:
    """Price in SOL per token, or None if the message isn't a usable trade."""
    vs, vt = msg.get("vSolInBondingCurve"), msg.get("vTokensInBondingCurve")
    if vs and vt and vt > 0:
        return vs / vt
    sol, tok = msg.get("solAmount"), msg.get("tokenAmount")
    if sol and tok and tok > 0:
        return sol / tok
    return None


class PumpPortalFeed:
    """Subscribe/unsubscribe mints at runtime; `on_tick(mint, price, ts, msg)`
    fires for every swap. Reconnects with backoff and re-subscribes on its own."""

    def __init__(self, api_key: str,
                 on_tick: Callable[[str, float, float, dict], None],
                 on_new_token: Optional[Callable[[dict], None]] = None,
                 on_migration: Optional[Callable[[dict], None]] = None):
        self.api_key = api_key
        self.on_tick = on_tick
        self.on_new_token = on_new_token
        self.on_migration = on_migration
        self.mints: set[str] = set()
        self.streams: set[str] = set()      # subscribeNewToken / subscribeMigration
        self._ws = None
        self._connected = asyncio.Event()
        self.stats = {"ticks": 0, "reconnects": 0, "last_msg_ts": None}

    async def subscribe_stream(self, method: str) -> None:
        """Add a firehose (subscribeNewToken / subscribeMigration) to this same
        connection. PumpPortal asks clients to keep a SINGLE websocket open —
        opening one socket per subscription risks having the key banned — so
        every stream is multiplexed here rather than dialing out again."""
        if method in self.streams:
            return
        self.streams.add(method)
        await self._send({"method": method})
        log.info(f"[feed] stream +{method}")

    # ─── subscription management ──────────────────────────────────────────────
    async def subscribe(self, mints: Iterable[str]) -> None:
        new = {m for m in mints if m and m not in self.mints}
        if not new:
            return
        self.mints |= new
        await self._send({"method": "subscribeTokenTrade", "keys": sorted(new)})
        log.info(f"[feed] subscribed {len(new)} mint(s), tracking {len(self.mints)}")

    async def unsubscribe(self, mints: Iterable[str]) -> None:
        gone = {m for m in mints if m in self.mints}
        if not gone:
            return
        self.mints -= gone
        await self._send({"method": "unsubscribeTokenTrade", "keys": sorted(gone)})
        log.info(f"[feed] unsubscribed {len(gone)} mint(s), tracking {len(self.mints)}")

    async def _send(self, payload: dict) -> None:
        """Best-effort send. If the socket is down the state in self.mints is
        still correct, and _resubscribe_all replays it on reconnect."""
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.send(json.dumps(payload))
        except Exception as e:
            log.warning(f"[feed] send failed ({e}); will replay on reconnect")

    async def _resubscribe_all(self) -> None:
        for method in sorted(self.streams):
            await self._send({"method": method})
        if self.mints:
            await self._send({"method": "subscribeTokenTrade", "keys": sorted(self.mints)})
            log.info(f"[feed] re-subscribed {len(self.mints)} mint(s) "
                     f"+ {len(self.streams)} stream(s) after reconnect")

    # ─── main loop ────────────────────────────────────────────────────────────
    async def run(self) -> None:
        if not self.api_key:
            raise RuntimeError(
                "PUMPPORTAL_API_KEY is required for the trade stream "
                "(wallet must hold >= 0.02 SOL)"
            )
        url = f"{WS_BASE}?api-key={self.api_key}"
        backoff = RECONNECT_MIN
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    self._connected.set()
                    backoff = RECONNECT_MIN
                    log.info("[feed] connected")
                    await self._resubscribe_all()
                    async for raw in ws:
                        self._handle(raw)
            except Exception as e:
                log.warning(f"[feed] disconnected: {type(e).__name__}: {e}")
            finally:
                self._ws = None
                self._connected.clear()
            self.stats["reconnects"] += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX)

    def _handle(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if "message" in msg:           # server ack / error, not a trade
            log.debug(f"[feed] server: {msg['message']}")
            return

        mint = msg.get("mint")
        if not mint:
            return

        # One socket carries trades, new launches and migrations — route by type.
        tx = msg.get("txType")
        if tx == "migrate":
            if self.on_migration:
                self._safe(self.on_migration, msg)
            return
        if tx == "create":
            if self.on_new_token:
                self._safe(self.on_new_token, msg)
            return

        if mint not in self.mints:
            return
        price = price_from_trade(msg)
        if price is None:
            return
        self.stats["ticks"] += 1
        self.stats["last_msg_ts"] = time.time()
        try:
            self.on_tick(mint, price, time.time(), msg)
        except Exception as e:
            log.error(f"[feed] on_tick raised for {mint}: {e}", exc_info=True)

    @staticmethod
    def _safe(fn, msg) -> None:
        try:
            fn(msg)
        except Exception as e:
            log.error(f"[feed] handler raised: {e}", exc_info=True)


async def _demo() -> None:
    """Smoke test: watch the next few freshly-created tokens and print ticks."""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    seen: dict[str, int] = {}

    def on_tick(mint, price, ts, msg):
        seen[mint] = seen.get(mint, 0) + 1
        if seen[mint] <= 3:
            print(f"  {mint[:8]}… {msg['txType']:4s} price={price:.3e} SOL  mcap={msg.get('marketCapSol', 0):.1f} SOL")

    pending: list[str] = []
    feed = PumpPortalFeed(
        os.getenv("PUMPPORTAL_API_KEY", ""), on_tick,
        on_new_token=lambda m: pending.append(m["mint"]) if len(pending) < 8 else None,
    )
    task = asyncio.create_task(feed.run())
    await asyncio.sleep(2)

    # same socket, no second dial-out
    await feed.subscribe_stream("subscribeNewToken")
    for _ in range(15):
        await asyncio.sleep(1)
        if len(pending) >= 8:
            break
    await feed.subscribe(list(pending))
    print(f"\nwatching {len(pending)} mints for 30s…\n")
    await asyncio.sleep(30)
    print(f"\ntotal ticks: {feed.stats['ticks']} across {len(seen)} mints "
          f"({feed.stats['ticks'] / 30:.1f} ticks/sec)")
    task.cancel()


if __name__ == "__main__":
    asyncio.run(_demo())
