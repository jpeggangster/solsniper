"""Offline checks for crypto.py on synthetic bars: python3 -m unittest test_crypto (from this folder)."""
import json
import math
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crypto  # noqa: E402

T0 = 1791504000  # 2026-10-08 00:00 UTC


def walk(days=30, step=300, seed=1):
    r, p, out = random.Random(seed), 60000.0, []
    for i in range(days * 86400 // step):
        o = p
        c = o * math.exp(r.gauss(0.00002, 0.0015))
        out.append({"t": T0 + i * step, "o": o, "h": max(o, c) * 1.0005, "l": min(o, c) * 0.9995, "c": c, "v": 50.0})
        p = c
    return out


class CryptoTest(unittest.TestCase):
    def setUp(self):
        self.bars = walk()
        self.now = T0 + 29 * 86400 + 11 * 3600

    def test_symbols(self):
        for s in ("BTC", "btc-usd", "BTCUSDT", "BINANCE:BTCUSDT", "COINBASE:BTC-USD", "CRYPTO:BTCUSD"):
            self.assertEqual(crypto.base_of(s), "BTC")
        self.assertEqual(crypto.yahoo_ticker("SUI"), "SUI20947-USD")

    def test_parse_formats(self):
        tv = json.dumps({"bars": self.bars[:5]})
        wrapped = json.dumps([{"type": "text", "text": tv}])
        self.assertEqual(crypto.parse_bars(tv), crypto.parse_bars(wrapped))
        ms = json.dumps([dict(b, t=b["t"] * 1000) for b in self.bars[:5]])
        self.assertEqual(crypto.parse_bars(ms)[0]["t"], T0)

    def test_pipeline(self):
        res = crypto.evaluate(self.bars, self.now, 0, 60, 300)
        self.assertIn(res["status"], ("triggered", "waiting", "expired"))
        forming = crypto.evaluate(self.bars, T0 + 29 * 86400 + 1800, 0, 60, 300)
        self.assertEqual(forming["status"], "forming")
        doc = dict(res, symbol="BTC", interval="5m", status="triggered",
                   trigger=res.get("trigger") or {"price": self.bars[-1]["c"], "time_ts": self.now})
        plan = crypto.make_plan(doc, "pct:1", "2R")
        self.assertAlmostEqual(plan["target"] - plan["entry"], 2 * (plan["entry"] - plan["stop"]), delta=1)
        plan["entries_h"] = 12
        past = [b for b in self.bars if b["t"] + 300 <= plan["entry_ts"]]
        mc, hist = crypto.replay(plan, past, 300, 0, sims=500, seed=3)
        self.assertAlmostEqual(mc["target_first_pct"] + mc["stop_first_pct"] + mc["open_at_flat_pct"], 100, delta=0.2)
        self.assertEqual(mc, crypto.replay(plan, past, 300, 0, sims=500, seed=3)[0])  # same seed, same odds
        sz = crypto.size(plan, 2000, 20)
        self.assertLessEqual(sz["loss_at_stop"], 20)
        self.assertIn("not an order", sz["note"])

    def test_refusals(self):
        doc = {"status": "triggered", "trigger": {"price": 100.0, "time_ts": self.now}, "session_start_ts": T0}
        for stop in ("101", "50", "99.95"):
            with self.assertRaises(crypto.CryptoError):
                crypto.make_plan(doc, stop, "2R")

    def test_cli_with_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "BTC.json")
            with open(f, "w") as fh:
                json.dump({"bars": self.bars}, fh)
            self.assertEqual(crypto.main(["entry", "BTC", "--bars", f, "--now", "2026-11-05 12:00", "--json"]), 0)


if __name__ == "__main__":
    unittest.main()
