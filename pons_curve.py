"""
Pons v2 bonding-curve integration for Robinhood Chain.
-------------------------------------------------------
Fresh pons launches are NOT tradeable through DexScreener or the KyberSwap
aggregator — both are blind to them until the launch graduates:

    fresh pons token -> DexScreener: 0 pairs
                     -> Kyber /routes: HTTP 400, no route
                     -> curve.buy(0.01 ETH): 4.73M tokens, 105k gas  ✅

So a token has to be routed by phase: on the curve before graduation, through
the aggregator afterwards.

Everything below was read off mainnet, not the docs (the docs list v1 factories
that went silent 8 days before this was written):

  v2 factory  0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e  (verified: PonsV2LaunchFactory)
  buy         0x59a87bc1  buy(uint256 quoteIn, uint256 minTokensOut, address recipient) payable
  sell        0xd04c6983  sell(uint256 tokensIn, uint256 minQuoteOut, address recipient)

Two things on this launchpad will quietly eat a position:

  1. SNIPE TAX — the factory reports snipeTaxStartBps = 9900 (99%) decaying over
     snipeTaxSeconds = 3. Buying inside that window burns almost the entire
     position. Nothing in the curve's return value warns you.
  2. CREATOR TAX — per-launch, observed at 0 / 500 / 1000 bps.

Rather than trusting either number, effective_tax_bps() simulates the actual buy
with eth_call and compares the real output against the constant-product ideal.
That catches the snipe tax, the creator tax, and anything else the curve does,
in one number.
"""

import logging
from typing import Optional

from web3 import Web3

log = logging.getLogger(__name__)

FACTORY = "0x7eD598BcEf8bd9Edd8C97A195C6d13f40801EC7e"
NATIVE_PAIR = "0x0000000000000000000000000000000000000000"

TOKEN_LAUNCHED_SIG = "TokenLaunched(address,address,address,address,uint256,uint256)"

# Phase enum on the factory's LaunchedToken struct.
PHASE_NOT_GRADUATED = 0
PHASE_POOL_CREATED = 2

FACTORY_ABI = [
    {"name": "getLaunchedToken", "stateMutability": "view", "type": "function",
     "inputs": [{"type": "address", "name": "token"}],
     "outputs": [{"type": "tuple", "name": "", "components": [
         {"type": "address", "name": "token"},
         {"type": "address", "name": "curve"},
         {"type": "address", "name": "deployer"},
         {"type": "address", "name": "creatorFeeRecipient"},
         {"type": "address", "name": "pairToken"},
         {"type": "uint256", "name": "graduationThreshold"},
         {"type": "uint24",  "name": "poolFee"},
         {"type": "int24",   "name": "tickSpacing"},
         {"type": "uint16",  "name": "creatorTaxBps"},
         {"type": "bool",    "name": "buybackEnabled"},
         {"type": "uint8",   "name": "phase"},
         {"type": "uint256", "name": "sweptQuote"},
         {"type": "uint256", "name": "sweptTokens"},
         {"type": "uint256", "name": "sweptAt"},
         {"type": "bool",    "name": "exists"},
     ]}]},
    {"name": "snipeTaxStartBps", "stateMutability": "view", "type": "function",
     "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "snipeTaxSeconds", "stateMutability": "view", "type": "function",
     "inputs": [], "outputs": [{"type": "uint256"}]},
    {"type": "event", "name": "TokenLaunched", "anonymous": False, "inputs": [
        {"type": "address", "name": "token", "indexed": True},
        {"type": "address", "name": "curve", "indexed": True},
        {"type": "address", "name": "deployer", "indexed": True},
        {"type": "address", "name": "pairToken", "indexed": False},
        {"type": "uint256", "name": "launchConfigId", "indexed": False},
        {"type": "uint256", "name": "graduationThreshold", "indexed": False},
    ]},
]

CURVE_ABI = [
    {"name": "buy", "stateMutability": "payable", "type": "function",
     "inputs": [{"type": "uint256", "name": "quoteIn"},
                {"type": "uint256", "name": "minTokensOut"},
                {"type": "address", "name": "recipient"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "sell", "stateMutability": "nonpayable", "type": "function",
     "inputs": [{"type": "uint256", "name": "tokensIn"},
                {"type": "uint256", "name": "minQuoteOut"},
                {"type": "address", "name": "recipient"}],
     "outputs": [{"type": "uint256"}]},
    {"name": "getReserves", "stateMutability": "view", "type": "function",
     "inputs": [], "outputs": [{"type": "uint256"}, {"type": "uint256"}]},
    {"name": "realQuoteReserve", "stateMutability": "view", "type": "function",
     "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "graduationThreshold", "stateMutability": "view", "type": "function",
     "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "sellableTokens", "stateMutability": "view", "type": "function",
     "inputs": [], "outputs": [{"type": "uint256"}]},
    {"name": "graduated", "stateMutability": "view", "type": "function",
     "inputs": [], "outputs": [{"type": "bool"}]},
    {"name": "creatorTaxBps", "stateMutability": "view", "type": "function",
     "inputs": [], "outputs": [{"type": "uint16"}]},
]


def factory(w3: Web3):
    return w3.eth.contract(address=w3.to_checksum_address(FACTORY), abi=FACTORY_ABI)


def curve(w3: Web3, addr: str):
    return w3.eth.contract(address=w3.to_checksum_address(addr), abi=CURVE_ABI)


def lookup(w3: Web3, token: str) -> Optional[dict]:
    """Launch record for a token, or None if pons never launched it."""
    try:
        t = factory(w3).functions.getLaunchedToken(w3.to_checksum_address(token)).call()
    except Exception:
        return None
    if not t[14]:                      # exists
        return None
    return {
        "token": t[0],
        "curve": t[1],
        "deployer": t[2],
        "pair_token": t[4],
        "graduation_threshold": t[5],
        "creator_tax_bps": t[8],
        "phase": t[10],
        "graduated": t[10] == PHASE_POOL_CREATED,
        "native_eth": t[4].lower() == NATIVE_PAIR,
    }


def state(w3: Web3, curve_addr: str) -> Optional[dict]:
    """Live curve reserves and progress toward graduation."""
    try:
        c = curve(w3, curve_addr)
        quote_res, token_res = c.functions.getReserves().call()
        real = c.functions.realQuoteReserve().call()
        thr = c.functions.graduationThreshold().call()
    except Exception:
        return None
    return {
        "quote_reserve": quote_res,
        "token_reserve": token_res,
        "real_quote_reserve": real,
        "graduation_threshold": thr,
        "progress": (real / thr) if thr else 0.0,
        # marginal price of one whole token, in whole units of the quote asset
        "price_quote": (quote_res / token_res) if token_res else 0.0,
    }


def simulate_buy(w3: Web3, curve_addr: str, quote_wei: int, recipient: str) -> Optional[int]:
    """eth_call the real buy() and return tokensOut. Balance is overridden so this
    works from an unfunded wallet. None means the call reverts."""
    c = curve(w3, curve_addr)
    data = c.encode_abi("buy", args=[quote_wei, 0, w3.to_checksum_address(recipient)])
    call = {"from": w3.to_checksum_address(recipient),
            "to": w3.to_checksum_address(curve_addr),
            "value": hex(quote_wei), "data": data}
    override = {w3.to_checksum_address(recipient): {"balance": hex(quote_wei + 10 ** 18)}}
    try:
        r = w3.provider.make_request("eth_call", [call, "latest", override])
        if "result" not in r:
            return None
        return int(r["result"], 16)
    except Exception:
        return None


def effective_tax_bps(w3: Web3, curve_addr: str, quote_wei: int, recipient: str) -> Optional[int]:
    """How much worse the real buy is than the constant-product ideal, in bps.

    Rolls the snipe tax, the creator tax and any curve fee into one measured
    number — so a 99% snipe-tax window shows up as ~9900 without having to know
    the launch timestamp.
    """
    st = state(w3, curve_addr)
    if not st or not st["quote_reserve"] or not st["token_reserve"]:
        return None
    actual = simulate_buy(w3, curve_addr, quote_wei, recipient)
    if actual is None:
        return None

    q, t = st["quote_reserve"], st["token_reserve"]
    ideal = t - (q * t) // (q + quote_wei)      # x*y=k
    if ideal <= 0:
        return None
    loss = (ideal - actual) / ideal
    return max(0, int(loss * 10_000))


def build_buy_tx(w3: Web3, curve_addr: str, quote_wei: int, min_tokens_out: int,
                 recipient: str, sender: str, native_eth: bool = True) -> dict:
    c = curve(w3, curve_addr)
    return {
        "from": w3.to_checksum_address(sender),
        "to": w3.to_checksum_address(curve_addr),
        "value": quote_wei if native_eth else 0,
        "data": c.encode_abi("buy", args=[quote_wei, min_tokens_out,
                                          w3.to_checksum_address(recipient)]),
    }


def build_sell_tx(w3: Web3, curve_addr: str, tokens_in: int, min_quote_out: int,
                  recipient: str, sender: str) -> dict:
    c = curve(w3, curve_addr)
    return {
        "from": w3.to_checksum_address(sender),
        "to": w3.to_checksum_address(curve_addr),
        "value": 0,
        "data": c.encode_abi("sell", args=[tokens_in, min_quote_out,
                                           w3.to_checksum_address(recipient)]),
    }


def snipe_tax_params(w3: Web3) -> tuple[int, int]:
    """(startBps, seconds) — observed 9900 bps decaying over 3s."""
    try:
        f = factory(w3)
        return f.functions.snipeTaxStartBps().call(), f.functions.snipeTaxSeconds().call()
    except Exception:
        return 9900, 3


def recent_launches(w3: Web3, from_block: int, to_block: Optional[int] = None) -> list[dict]:
    """TokenLaunched events in a block range, oldest first."""
    to_block = to_block if to_block is not None else w3.eth.block_number
    topic = w3.keccak(text=TOKEN_LAUNCHED_SIG)
    try:
        logs = w3.eth.get_logs({"address": w3.to_checksum_address(FACTORY),
                                "fromBlock": from_block, "toBlock": to_block,
                                "topics": [topic]})
    except Exception as e:
        log.warning(f"TokenLaunched get_logs failed ({from_block}-{to_block}): {e}")
        return []
    ev = factory(w3).events.TokenLaunched()
    out = []
    failures = 0
    for lg in logs:
        try:
            a = ev.process_log(lg)["args"]
        except Exception as e:
            failures += 1
            if failures == 1:
                log.warning(f"TokenLaunched decode failed: {type(e).__name__}: {e}")
            continue
        out.append({
            "token": a["token"], "curve": a["curve"], "deployer": a["deployer"],
            "pair_token": a["pairToken"],
            "graduation_threshold": a["graduationThreshold"],
            "block": lg["blockNumber"],
            "native_eth": a["pairToken"].lower() == NATIVE_PAIR,
        })
    return out


def quote_out_estimate(w3: Web3, curve_addr: str, tokens_in: int) -> Optional[int]:
    """Constant-product estimate of the quote returned for selling tokens_in.

    Used to set a sane minQuoteOut. The curve takes its cut on top of this, so
    callers must still subtract their slippage tolerance.
    """
    st = state(w3, curve_addr)
    if not st or not st["quote_reserve"] or not st["token_reserve"]:
        return None
    q, t = st["quote_reserve"], st["token_reserve"]
    return q - (q * t) // (t + tokens_in)
