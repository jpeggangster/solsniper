#!/usr/bin/env python3
"""
Build a daily BTC/USD OHLCV CSV (UTC days) from Bitstamp 1-minute data published at
github.com/ff137/bitstamp-btcusd-minute-data. Tracks TradingView INDEX:BTCUSD to ~0.03%.

Usage: python fetch_btc_daily.py [out.csv]   (default data/btc_bitstamp_1d.csv)
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd

REPO = "https://github.com/ff137/bitstamp-btcusd-minute-data"
FILES = ["data/historical/btcusd_bitstamp_1min_2012-2025.csv.gz",
         "data/updates/btcusd_bitstamp_1min_latest.csv"]


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "data/btc_bitstamp_1d.csv")
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "clone", "--depth", "1", "--filter=blob:none", "--no-checkout",
                        REPO, tmp], check=True)
        subprocess.run(["git", "-C", tmp, "checkout", "HEAD", "--", *FILES], check=True)
        m = pd.concat([pd.read_csv(Path(tmp) / f) for f in FILES])
    m = m.drop_duplicates("timestamp").sort_values("timestamp")
    m.index = pd.to_datetime(m["timestamp"], unit="s")
    d = m.resample("1D").agg({"open": "first", "high": "max", "low": "min",
                              "close": "last", "volume": "sum"}).dropna()
    d["time"] = (d.index - pd.Timestamp("1970-01-01")) // pd.Timedelta("1s")
    out.parent.mkdir(parents=True, exist_ok=True)
    d[["time", "open", "high", "low", "close", "volume"]].to_csv(out, index=False)
    print(f"{len(d)} daily bars {d.index[0].date()} -> {d.index[-1].date()} written to {out}")


if __name__ == "__main__":
    main()
