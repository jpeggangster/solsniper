#!/usr/bin/env python3
"""
Compare gaussian_channel_strategy.py against a TradingView export.

Usage: python compare_gaussian_tv.py data/btc_bitstamp_1d.csv TV_trades.xlsx [TV_chart.csv]
  TV_trades.xlsx : Strategy Tester "Export" workbook (Trades sheet)
  TV_chart.csv   : optional chart export with Filter / Upper Band / Lower Band columns
"""
import sys

import pandas as pd

from gaussian_channel_strategy import backtest, gaussian_channel


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    df = pd.read_csv(sys.argv[1]).sort_values("time").reset_index(drop=True)
    gc = gaussian_channel(df)

    if len(sys.argv) > 3:
        tv = pd.read_csv(sys.argv[3]).set_index("time")
        j = tv.join(gc.set_index("time")[["filt", "hband", "lband"]], how="inner")
        for a, b in [("Filter", "filt"), ("Upper Band", "hband"), ("Lower Band", "lband")]:
            err = (j[b] / j[a] - 1).abs() * 100
            print(f"{a:11s} mean err {err.mean():.4f}%  max {err.max():.4f}%  ({len(j)} bars)")

    x = pd.read_excel(sys.argv[2], sheet_name="Trades")
    en = x[x["Type"].str.startswith("Entry")].set_index("Trade number")
    ex = x[x["Type"].str.startswith("Exit")].set_index("Trade number")
    tv = pd.DataFrame({"entry": en["Date and time"].dt.date, "tv_exit": ex["Date and time"].dt.date})

    trades, _ = backtest(gc)
    my = pd.DataFrame({"entry": [t.entry_time.date() for t in trades],
                       "my_exit": [t.exit_time.date() for t in trades]})
    both = tv.merge(my, on="entry")
    print(f"TV trades {len(tv)} | replica {len(my)} | same entry day {len(both)} | "
          f"same entry+exit {(both.tv_exit == both.my_exit).sum()}")
    print("TV only   :", sorted(set(tv.entry) - set(my.entry)))
    print("replica only:", sorted(set(my.entry) - set(tv.entry)))


if __name__ == "__main__":
    main()
