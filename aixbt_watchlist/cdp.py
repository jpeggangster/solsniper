#!/usr/bin/env python3.14
"""Minimal CDP evaluator for the TradingView desktop app (port 9222)."""
import asyncio, json, sys, urllib.request
import websockets

CDP = "http://127.0.0.1:9222"

def pick_target():
    d = json.load(urllib.request.urlopen(CDP + "/json/list", timeout=5))
    for t in d:
        if t.get("type") == "page" and "tradingview.com/chart" in (t.get("url") or ""):
            return t
    raise SystemExit("no tradingview chart tab found")

async def run(expr, timeout=40.0):
    t = pick_target()
    async with websockets.connect(t["webSocketDebuggerUrl"], max_size=64*1024*1024) as ws:
        await ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expr, "returnByValue": True,
                       "awaitPromise": True, "userGesture": True},
        }))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))
            if msg.get("id") == 1:
                return msg

if __name__ == "__main__":
    expr = sys.stdin.read()
    res = asyncio.run(run(expr))
    r = res.get("result", {})
    if "exceptionDetails" in r:
        print("EXCEPTION:", json.dumps(r["exceptionDetails"])[:2000], file=sys.stderr)
        sys.exit(1)
    val = r.get("result", {}).get("value")
    print(val if isinstance(val, str) else json.dumps(val))
