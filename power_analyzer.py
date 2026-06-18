"""
power_analyzer.py — JP Corp memecoin conviction engine for the X KOL sniper.

Pipeline:  tweet detected → DELAY WINDOW → hard gates → 100-pt power score → SOL tier → execute

Plugs into sniper.py:

    from power_analyzer import PowerAnalyzer
    pa = PowerAnalyzer(helius_key=..., openrouter_key=...)
    verdict = await pa.analyze(mint=ca, tweet_id=tid, kol_handle="alonzocooks", tweet_text=text)
    if verdict.buy:
        await execute_jupiter_swap(ca, sol_amount=verdict.size_sol)

Free/keyed APIs used:
  - DexScreener  (no key)        : price, volume, liquidity, txns, pair age
  - RugCheck     (no key)        : mint/freeze authority, LP lock, top holders, risks
  - Helius RPC   (free tier key) : holder accounts, deployer history, bundle detection
  - OpenRouter   (optional)      : lore/narrative scoring via LLM
"""

import asyncio
import json
import math
import os
import pickle
import time
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

# ----------------------------- config ---------------------------------------

DELAY_WINDOW_SEC = 120          # wait before final checks + buy (anti drain-trap)
RECHECK_INTERVAL_SEC = 30       # poll tweet liveness / dev sells during window

MIN_LIQUIDITY_USD = 10_000      # hard gate
MAX_TOP10_PCT = 35.0            # excl. LP & burn wallets; above this = gate fail
MAX_SINGLE_HOLDER_PCT = 8.0     # excl. LP
MAX_BUNDLE_PCT = 25.0           # supply bought in launch-slot cluster

TIERS = [                       # (min_score, sol_size)
    (85, 1.00),
    (75, 0.75),
    (65, 0.50),
    (50, 0.25),
]

WEIGHTS = {
    "volume":    20,
    "holders":   20,
    "insiders":  20,
    "community": 15,
    "lore":      10,   # reduced: sentiment model takes 5 pts
    "sentiment": 5,    # pump.fun ML sentiment (Pumpdotstudio/pump-fun-sentiment-100k)
    "kol":       10,
}

# Features expected by the trained sentiment model (order matters)
_SENTIMENT_FEATURES = [
    "market_cap", "volume_24h", "liquidity", "holder_count",
    "top10_holder_pct", "buys_24h", "sells_24h", "bonding_progress",
]
_SENTIMENT_MODEL_PATH = os.path.join(os.path.dirname(__file__), "sentiment_model.pkl")

BURN_ADDRS = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}

# Token accounts whose *owner* is one of these programs are LP/AMM pools — exclude from holder calcs
DEX_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun bonding curve
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap AMM
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",   # Orca Whirlpool
    "5quBtoiQqxF9Jv6KYKctB59NT3gtFD2XqWNkwhroRufR",  # Orca v1
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",  # Orca v2
    "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX",   # Serum DEX v3
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",  # Moonshot
}

# ----------------------------- data types -----------------------------------

@dataclass
class Verdict:
    mint: str
    buy: bool
    size_sol: float
    score: float
    gates_passed: bool
    gate_failures: list = field(default_factory=list)
    breakdown: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def summary(self) -> str:
        if not self.gates_passed:
            return f"❌ SKIP {self.mint[:8]}… | gates failed: {', '.join(self.gate_failures)}"
        if not self.buy:
            return f"⚠️ SKIP {self.mint[:8]}… | score {self.score:.0f}/100 below tier floor"
        return (f"✅ BUY {self.size_sol} SOL | score {self.score:.0f}/100 | "
                + " ".join(f"{k}:{v:.0f}" for k, v in self.breakdown.items()))


# ----------------------------- KOL ledger ------------------------------------
# Tracks each KOL's historical call outcomes + deleted-tweet strikes.

class KolLedger:
    def __init__(self, path="kol_ledger.db"):
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS calls(
            kol TEXT, mint TEXT, tweet_id TEXT, ts INTEGER,
            outcome_1h REAL, deleted INTEGER DEFAULT 0,
            PRIMARY KEY (kol, mint))""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS blacklist(
            mint TEXT PRIMARY KEY, reason TEXT, ts INTEGER)""")
        self.db.commit()

    def record_call(self, kol, mint, tweet_id):
        self.db.execute("INSERT OR IGNORE INTO calls(kol,mint,tweet_id,ts) VALUES(?,?,?,?)",
                        (kol, mint, tweet_id, int(time.time())))
        self.db.commit()

    def record_deleted(self, kol, mint):
        self.db.execute("UPDATE calls SET deleted=1 WHERE kol=? AND mint=?", (kol, mint))
        self.db.execute("INSERT OR REPLACE INTO blacklist VALUES(?,?,?)",
                        (mint, f"deleted tweet by @{kol}", int(time.time())))
        self.db.commit()

    def record_outcome(self, kol, mint, pct_1h):
        self.db.execute("UPDATE calls SET outcome_1h=? WHERE kol=? AND mint=?",
                        (pct_1h, kol, mint))
        self.db.commit()

    def is_blacklisted(self, mint) -> bool:
        return self.db.execute("SELECT 1 FROM blacklist WHERE mint=?", (mint,)).fetchone() is not None

    def kol_score(self, kol) -> float:
        """0–10 pts. Deleted tweets are heavily punished; positive 1h outcomes rewarded."""
        rows = self.db.execute(
            "SELECT outcome_1h, deleted FROM calls WHERE kol=? ORDER BY ts DESC LIMIT 30",
            (kol,)).fetchall()
        if not rows:
            return 5.0  # unknown KOL = neutral
        strikes = sum(r[1] for r in rows)
        if strikes >= 2:
            return 0.0
        outcomes = [r[0] for r in rows if r[0] is not None]
        if not outcomes:
            return 5.0 - strikes * 3
        winrate = sum(1 for o in outcomes if o > 0) / len(outcomes)
        return max(0.0, min(10.0, winrate * 10 - strikes * 3))


# ----------------------------- analyzer --------------------------------------

class PowerAnalyzer:
    def __init__(self, helius_key: str, openrouter_key: Optional[str] = None,
                 x_bearer: Optional[str] = None, ledger: Optional[KolLedger] = None):
        self.helius = f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
        self.openrouter_key = openrouter_key
        self.x_bearer = x_bearer
        self.ledger = ledger or KolLedger()
        self._sentiment_model = self._load_sentiment_model()

    @staticmethod
    def _load_sentiment_model():
        try:
            with open(_SENTIMENT_MODEL_PATH, "rb") as f:
                bundle = pickle.load(f)
            return bundle["model"]
        except Exception:
            return None  # graceful fallback if model not trained yet

    # ---------- entry point ----------

    async def analyze(self, mint: str, tweet_id: str, kol_handle: str,
                      tweet_text: str = "") -> Verdict:
        v = Verdict(mint=mint, buy=False, size_sol=0.0, score=0.0, gates_passed=False)

        if self.ledger.is_blacklisted(mint):
            v.gate_failures.append("blacklisted CA")
            return v

        self.ledger.record_call(kol_handle, mint, tweet_id)

        async with aiohttp.ClientSession() as s:
            # ---- snapshot at detection (for delay-window delta checks) ----
            dex0 = await self._dexscreener(s, mint)
            rug0 = await self._rugcheck(s, mint)

            # ---- DELAY WINDOW: the anti drain-trap core ----
            elapsed = 0
            while elapsed < DELAY_WINDOW_SEC:
                await asyncio.sleep(RECHECK_INTERVAL_SEC)
                elapsed += RECHECK_INTERVAL_SEC

                if self.x_bearer and not await self._tweet_alive(s, tweet_id):
                    self.ledger.record_deleted(kol_handle, mint)
                    v.gate_failures.append("tweet deleted during delay window — trap")
                    return v

                dex_now = await self._dexscreener(s, mint)
                if dex_now and dex0:
                    liq0 = dex0.get("liquidity_usd", 0)
                    liq_now = dex_now.get("liquidity_usd", 0)
                    if liq0 > 0 and liq_now < liq0 * 0.6:
                        v.gate_failures.append("liquidity dropped >40% in delay window — rug in progress")
                        self.ledger.record_deleted(kol_handle, mint)
                        return v

            # ---- final data pulls ----
            dex = await self._dexscreener(s, mint)
            rug = await self._rugcheck(s, mint)
            # build LP exclusion set from RugCheck markets + DexScreener pair address
            lp_addrs = set((rug or {}).get("lp_addrs", set()))
            if dex and dex.get("pair_address"):
                lp_addrs.add(dex["pair_address"])
            holders = await self._top_holders(s, mint, lp_addrs=lp_addrs)
            bundle = await self._bundle_check(s, mint)
            deployer_flag = await self._deployer_history(s, mint, rug)

            # ---- HARD GATES ----
            gates = []
            if not dex:
                gates.append("no DexScreener pair")
            else:
                if dex["liquidity_usd"] < MIN_LIQUIDITY_USD:
                    gates.append(f"liquidity ${dex['liquidity_usd']:,.0f} < ${MIN_LIQUIDITY_USD:,}")
            if rug:
                if rug.get("mint_authority"):
                    gates.append("mint authority NOT revoked")
                if rug.get("freeze_authority"):
                    gates.append("freeze authority active")
                if not rug.get("lp_locked", False):
                    gates.append("LP not locked/burned")
            if holders and holders["top10_pct"] > MAX_TOP10_PCT:
                gates.append(f"top10 hold {holders['top10_pct']:.0f}% > {MAX_TOP10_PCT}%")
            if holders and holders["max_single_pct"] > MAX_SINGLE_HOLDER_PCT:
                gates.append(f"single wallet {holders['max_single_pct']:.0f}% > {MAX_SINGLE_HOLDER_PCT}%")
            if bundle and bundle["bundle_pct"] > MAX_BUNDLE_PCT:
                gates.append(f"bundle bought {bundle['bundle_pct']:.0f}% of supply")
            if deployer_flag:
                gates.append(deployer_flag)

            if gates:
                v.gate_failures = gates
                return v
            v.gates_passed = True

            # ---- SCORING ----
            v.breakdown["volume"]    = self._score_volume(dex)
            v.breakdown["holders"]   = self._score_holders(holders, dex)
            v.breakdown["insiders"]  = self._score_insiders(bundle, rug)
            v.breakdown["community"] = self._score_community(dex)
            v.breakdown["lore"]      = await self._score_lore(s, tweet_text, dex)
            v.breakdown["sentiment"] = self._score_sentiment_ml(dex, holders)
            v.breakdown["kol"]       = self.ledger.kol_score(kol_handle)

            v.score = sum(v.breakdown.values())

            for floor, size in TIERS:
                if v.score >= floor:
                    v.buy, v.size_sol = True, size
                    break
            return v

    # ---------- data sources ----------

    async def _dexscreener(self, s, mint):
        try:
            async with s.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json()
            pairs = d.get("pairs") or []
            if not pairs:
                return None
            p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd", 0))
            return {
                "liquidity_usd": (p.get("liquidity") or {}).get("usd", 0),
                "vol_5m":  (p.get("volume") or {}).get("m5", 0),
                "vol_1h":  (p.get("volume") or {}).get("h1", 0),
                "vol_24h": (p.get("volume") or {}).get("h24", 0),
                "buys_5m":  ((p.get("txns") or {}).get("m5") or {}).get("buys", 0),
                "sells_5m": ((p.get("txns") or {}).get("m5") or {}).get("sells", 0),
                "buys_1h":  ((p.get("txns") or {}).get("h1") or {}).get("buys", 0),
                "sells_1h": ((p.get("txns") or {}).get("h1") or {}).get("sells", 0),
                "buys_24h": ((p.get("txns") or {}).get("h24") or {}).get("buys", 0),
                "sells_24h":((p.get("txns") or {}).get("h24") or {}).get("sells", 0),
                "price_change_1h": (p.get("priceChange") or {}).get("h1", 0),
                "fdv": p.get("fdv", 0),
                "market_cap": p.get("marketCap", 0) or p.get("fdv", 0),
                "created_at": p.get("pairCreatedAt", 0),
                "socials": (p.get("info") or {}).get("socials", []),
                "pair_address": p.get("pairAddress", ""),
            }
        except Exception:
            return None

    async def _rugcheck(self, s, mint):
        try:
            async with s.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report",
                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                d = await r.json()
            markets = d.get("markets") or []
            lp_locked = any((m.get("lp") or {}).get("lpLockedPct", 0) > 90 for m in markets)
            # collect known LP/pool addresses to exclude from holder counts
            lp_addrs = set()
            for m in markets:
                if m.get("pubkey"):
                    lp_addrs.add(m["pubkey"])
                lp = m.get("lp") or {}
                for key in ("lpMint", "lpVault", "quoteVault", "baseVault"):
                    if lp.get(key):
                        lp_addrs.add(lp[key])
            return {
                "mint_authority": (d.get("token") or {}).get("mintAuthority"),
                "freeze_authority": (d.get("token") or {}).get("freezeAuthority"),
                "lp_locked": lp_locked,
                "lp_addrs": lp_addrs,
                "risks": [x.get("name") for x in (d.get("risks") or [])],
                "creator": d.get("creator"),
                "insider_pct": d.get("graphInsidersDetected", 0),
            }
        except Exception:
            return None

    async def _rpc(self, s, method, params):
        async with s.post(self.helius, json={"jsonrpc": "2.0", "id": 1,
                                             "method": method, "params": params},
                          timeout=aiohttp.ClientTimeout(total=15)) as r:
            return (await r.json()).get("result")

    async def _top_holders(self, s, mint, lp_addrs: set = None):
        try:
            res = await self._rpc(s, "getTokenLargestAccounts", [mint])
            supply = await self._rpc(s, "getTokenSupply", [mint])
            total = float(supply["value"]["uiAmount"]) if supply else 0
            if not res or not total:
                return None

            accts = [a for a in res["value"] if a["address"] not in BURN_ADDRS]
            if not accts:
                return None

            # Batch-fetch parsed account info to get the real wallet owner of each
            # token account — LP/AMM pool accounts will be owned by DEX programs
            addrs = [a["address"] for a in accts]
            multi = await self._rpc(s, "getMultipleAccounts",
                                    [addrs, {"encoding": "jsonParsed"}])

            exclude = set(BURN_ADDRS) | (lp_addrs or set())
            if multi and multi.get("value"):
                for addr, info in zip(addrs, multi["value"]):
                    if not info:
                        continue
                    data = info.get("data") or {}
                    if isinstance(data, dict):
                        owner = (data.get("parsed") or {}).get("info", {}).get("owner", "")
                        if owner in DEX_PROGRAMS or addr in exclude:
                            exclude.add(addr)

            pcts = sorted(
                (float(a["uiAmount"] or 0) / total * 100
                 for a in accts if a["address"] not in exclude),
                reverse=True
            )
            top10 = sum(pcts[:10])
            return {"top10_pct": top10, "max_single_pct": pcts[0] if pcts else 0,
                    "n_large": len(pcts)}
        except Exception:
            return None

    async def _bundle_check(self, s, mint):
        """Detect launch-slot sniper clusters: many buys landing in the token's
        first 1-2 slots = bundled/insider launch."""
        try:
            sigs = await self._rpc(s, "getSignaturesForAddress",
                                   [mint, {"limit": 1000}])
            if not sigs:
                return None
            slots = [x["slot"] for x in sigs]
            first_slot = min(slots)
            in_first = sum(1 for sl in slots if sl <= first_slot + 2)
            # heuristic: fraction of earliest 1000 txs in launch slots ≈ bundle weight
            bundle_pct = in_first / len(slots) * 100
            return {"bundle_pct": bundle_pct, "launch_slot_txs": in_first}
        except Exception:
            return None

    async def _deployer_history(self, s, mint, rug):
        """Serial rugger check via the creator's recent token activity."""
        try:
            creator = (rug or {}).get("creator")
            if not creator:
                return None
            sigs = await self._rpc(s, "getSignaturesForAddress",
                                   [creator, {"limit": 200}])
            if sigs and len(sigs) >= 190:
                return "deployer hyperactive (200+ recent txs) — likely token farm"
            return None
        except Exception:
            return None

    async def _tweet_alive(self, s, tweet_id) -> bool:
        try:
            async with s.get(f"https://api.twitter.com/2/tweets/{tweet_id}",
                             headers={"Authorization": f"Bearer {self.x_bearer}"},
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                d = await r.json()
            return "data" in d and not d.get("errors")
        except Exception:
            return True  # network error ≠ deleted; don't false-positive

    # ---------- scoring (each returns 0..weight) ----------

    def _score_volume(self, dex):
        w = WEIGHTS["volume"]
        if not dex:
            return 0
        pts = 0.0
        vol_liq = dex["vol_1h"] / dex["liquidity_usd"] if dex["liquidity_usd"] else 0
        pts += min(8, vol_liq * 4)                       # turnover vs liquidity
        total5 = dex["buys_5m"] + dex["sells_5m"]
        if total5:
            pts += (dex["buys_5m"] / total5) * 7         # buy pressure
        if dex["vol_5m"] > 0 and dex["vol_1h"] > 0:
            accel = dex["vol_5m"] * 12 / dex["vol_1h"]   # is 5m pace above 1h pace
            pts += min(5, accel * 2.5)
        return min(w, pts)

    def _score_holders(self, h, dex):
        w = WEIGHTS["holders"]
        if not h:
            return 0
        pts = 0.0
        pts += max(0, 10 * (1 - h["top10_pct"] / MAX_TOP10_PCT))   # flatter = better
        pts += max(0, 6 * (1 - h["max_single_pct"] / MAX_SINGLE_HOLDER_PCT))
        if dex and dex["buys_1h"] > 150:
            pts += 4                                      # broad participation
        elif dex and dex["buys_1h"] > 60:
            pts += 2
        return min(w, pts)

    def _score_insiders(self, bundle, rug):
        w = WEIGHTS["insiders"]
        pts = float(w)
        if bundle:
            pts -= min(12, bundle["bundle_pct"] * 0.5)    # launch-slot clustering
        if rug:
            pts -= min(8, rug.get("insider_pct", 0) * 0.4)
            pts -= 2 * len([r for r in rug.get("risks", []) if "insider" in (r or "").lower()])
        return max(0.0, min(w, pts))

    def _score_community(self, dex):
        w = WEIGHTS["community"]
        if not dex:
            return 0
        pts = 0.0
        socials = {(x.get("type") or "").lower() for x in dex.get("socials", [])}
        if "twitter" in socials:
            pts += 4
        if "telegram" in socials:
            pts += 4
        age_min = (time.time() * 1000 - dex["created_at"]) / 60000 if dex["created_at"] else 0
        if 10 <= age_min <= 720:
            pts += 4        # not a 30-second-old trap, not a dead revival
        if dex["buys_1h"] + dex["sells_1h"] > 400:
            pts += 3
        return min(w, pts)

    def _score_sentiment_ml(self, dex, holders) -> float:
        """On-chain sentiment score 0–5 from the Pumpdotstudio/pump-fun-sentiment-100k model.
        Maps bullish→5, neutral→2.5, bearish→0 weighted by model confidence."""
        w = WEIGHTS["sentiment"]
        if self._sentiment_model is None:
            return w * 0.5  # neutral fallback
        try:
            row = []
            for f in _SENTIMENT_FEATURES:
                if f == "market_cap":
                    v = (dex or {}).get("market_cap", 0) or 0
                elif f == "volume_24h":
                    v = (dex or {}).get("vol_24h", 0) or 0
                elif f == "liquidity":
                    v = (dex or {}).get("liquidity_usd", 0) or 0
                elif f == "holder_count":
                    v = (holders or {}).get("n_large", 0) or 0
                elif f == "top10_holder_pct":
                    v = (holders or {}).get("top10_pct", 50) or 50
                elif f == "buys_24h":
                    v = (dex or {}).get("buys_24h", 0) or 0
                elif f == "sells_24h":
                    v = (dex or {}).get("sells_24h", 0) or 0
                elif f == "bonding_progress":
                    v = 0  # not fetched; model handles 0 gracefully
                else:
                    v = 0
                # apply same log1p transform as training
                if f in ("market_cap", "volume_24h", "liquidity"):
                    v = math.log1p(max(v, 0))
                row.append(float(v))
            pred = self._sentiment_model.predict([row])[0]
            proba = self._sentiment_model.predict_proba([row])[0]
            confidence = max(proba)
            # 0=bearish,1=neutral,2=bullish
            if pred == 2:      # bullish
                return w * (0.5 + 0.5 * confidence)
            elif pred == 1:    # neutral
                return w * 0.4
            else:              # bearish
                return w * max(0, 0.3 - 0.3 * confidence)
        except Exception:
            return w * 0.5

    async def _score_lore(self, s, tweet_text, dex):
        """LLM lore/narrative score 0–10 via OpenRouter. Falls back to neutral."""
        w = WEIGHTS["lore"]
        if not self.openrouter_key or not tweet_text:
            return w * 0.45
        prompt = (
            "You are scoring a Solana memecoin's narrative power for a trader. "
            "Score 0-10 considering: originality of the meme/lore (vs derivative copycat), "
            "cultural timing/relevance, stickiness/virality potential, and whether the "
            "KOL tweet reads organic vs paid shill. Respond ONLY with JSON: "
            '{"score": <0-10>, "reason": "<10 words>"}\n\n'
            f"KOL tweet: {tweet_text[:500]}\nFDV: ${dex.get('fdv',0):,.0f}" if dex else ""
        )
        try:
            async with s.post("https://openrouter.ai/api/v1/chat/completions",
                              headers={"Authorization": f"Bearer {self.openrouter_key}"},
                              json={"model": "anthropic/claude-3.5-haiku",
                                    "messages": [{"role": "user", "content": prompt}],
                                    "max_tokens": 100},
                              timeout=aiohttp.ClientTimeout(total=20)) as r:
                d = await r.json()
            txt = d["choices"][0]["message"]["content"]
            obj = json.loads(txt.replace("```json", "").replace("```", "").strip())
            return max(0.0, min(w, float(obj["score"])))
        except Exception:
            return w * 0.45


# ----------------------------- example wiring --------------------------------

async def _demo():
    pa = PowerAnalyzer(helius_key="YOUR_HELIUS_KEY",
                       openrouter_key=None, x_bearer=None)
    v = await pa.analyze(
        mint="So11111111111111111111111111111111111111112",
        tweet_id="0", kol_handle="alonzocooks",
        tweet_text="new dog coin just dropped, this one is different trust")
    print(v.summary())

if __name__ == "__main__":
    asyncio.run(_demo())
