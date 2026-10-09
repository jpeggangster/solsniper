"""
Replay recorded ticks through the EMA-cross strategy and report what it would
actually have made — net of fees and slippage.

This exists because the question that decides whether the bot is worth running
is not "does the cross fire" but "does the average firing clear its cost". On a
5s chart, EMA12/21 fires often; every round trip pays pump.fun's swap fee,
PumpPortal's fee, the priority fee and slippage twice. The gross column below
shows the strategy's raw edge, the net column shows what reaches the wallet.

  # gather data first (paper mode records ticks as it runs)
  /opt/homebrew/bin/python3.14 ema_cross_bot.py --auto-discover

  # then replay the defaults
  /opt/homebrew/bin/python3.14 backtest_ema_cross.py

  # sweep the parameter grid to find what survives costs
  /opt/homebrew/bin/python3.14 backtest_ema_cross.py --sweep

Replay accuracy notes / known optimism:
  - Fills are taken at the tick price that triggered the signal; a real order
    lands a block or two later. Costs are charged on top, but latency is not
    modelled, so results are still a mild upper bound.
  - Price impact is a flat percentage, not a curve. Bigger sizes on thin pools
    will do worse than this shows.
"""

import argparse
import json
import os
import statistics
from collections import defaultdict

import ema_cross_strategy as strat

HERE = os.path.dirname(os.path.abspath(__file__))
TICKS_FILE = os.path.join(HERE, "ema_ticks.jsonl")


def load_ticks(path: str) -> dict[str, list]:
    by_mint: dict[str, list] = defaultdict(list)
    bad = 0
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
                by_mint[r["mint"]].append((r["ts"], r["price"]))
            except Exception:
                bad += 1
    for m in by_mint:
        by_mint[m].sort(key=lambda x: x[0])
    if bad:
        print(f"  ({bad} malformed lines skipped)")
    return dict(by_mint)


def replay_mint(ticks: list, cost_per_side: float) -> list[dict]:
    """Run one token's tick history through the strategy. Returns closed trades."""
    t = strat.new_tracker("bt", 1.0)
    trades, open_trade = [], None

    for ts, price in ticks:
        was_long = t["want_long"]
        strat.ingest_tick(t, price, ts)

        if t["want_long"] and not was_long:
            open_trade = {"entry_ts": ts, "entry": price * (1 + cost_per_side)}
            strat.set_entry_price(t, open_trade["entry"])
        elif not t["want_long"] and was_long and open_trade:
            exit_px = price * (1 - cost_per_side)
            open_trade.update(
                exit_ts=ts, exit=exit_px, reason=t.get("exit_reason") or "flat",
                pnl_pct=(exit_px - open_trade["entry"]) / open_trade["entry"] * 100,
                gross_pct=(price - open_trade["entry"] / (1 + cost_per_side))
                          / (open_trade["entry"] / (1 + cost_per_side)) * 100,
                held_sec=ts - open_trade["entry_ts"],
            )
            trades.append(open_trade)
            open_trade = None

    if open_trade and t["last_price"]:                 # mark the tail open position out
        price = t["last_price"]
        exit_px = price * (1 - cost_per_side)
        open_trade.update(
            exit_ts=t["last_tick_ts"], exit=exit_px, reason="end of data (marked out)",
            pnl_pct=(exit_px - open_trade["entry"]) / open_trade["entry"] * 100,
            gross_pct=(price - open_trade["entry"] / (1 + cost_per_side))
                      / (open_trade["entry"] / (1 + cost_per_side)) * 100,
            held_sec=(t["last_tick_ts"] or open_trade["entry_ts"]) - open_trade["entry_ts"],
        )
        trades.append(open_trade)
    return trades


def summarize(trades: list[dict], size_sol: float) -> dict:
    if not trades:
        return {"n": 0}
    net = [x["pnl_pct"] for x in trades]
    gross = [x["gross_pct"] for x in trades]
    wins = [p for p in net if p > 0]
    losses = [p for p in net if p <= 0]
    gain = sum(wins)
    pain = abs(sum(losses))
    return {
        "n": len(trades),
        "win_rate": len(wins) / len(trades) * 100,
        "avg_net": statistics.mean(net),
        "med_net": statistics.median(net),
        "avg_gross": statistics.mean(gross),
        "total_net_pct": sum(net),
        "net_sol": sum(net) / 100 * size_sol,
        "profit_factor": (gain / pain) if pain else float("inf"),
        "best": max(net), "worst": min(net),
        "avg_hold": statistics.mean(x["held_sec"] for x in trades),
        "stop_slip": _stop_slippage(trades),
    }


def _stop_slippage(trades: list[dict]):
    """How far past the stop level the stop exits actually landed. A -12% stop
    that realises -55% is the single most expensive thing this strategy can do,
    and it does not show up in win rate at all."""
    stops = [x["pnl_pct"] for x in trades if "stop loss" in (x.get("reason") or "")]
    if not stops:
        return None
    return len(stops), min(stops), statistics.mean(stops)


def print_summary(label: str, s: dict, size_sol: float) -> None:
    if not s["n"]:
        print(f"{label}: no trades")
        return
    print(f"\n=== {label} ===")
    print(f"  trades            {s['n']}")
    print(f"  win rate          {s['win_rate']:.1f}%")
    print(f"  avg PnL / trade   {s['avg_net']:+.2f}% net   ({s['avg_gross']:+.2f}% gross, before costs)")
    print(f"  median PnL        {s['med_net']:+.2f}%")
    print(f"  best / worst      {s['best']:+.1f}% / {s['worst']:+.1f}%")
    print(f"  profit factor     {s['profit_factor']:.2f}")
    print(f"  avg hold          {s['avg_hold']:.0f}s")
    print(f"  TOTAL             {s['total_net_pct']:+.1f}%  =  {s['net_sol']:+.4f} SOL "
          f"at {size_sol} SOL/trade")
    if s["n"] < 30 or (s["avg_net"] > 0 > s["med_net"]):
        print()
        if s["n"] < 30:
            print(f"  ⚠ n={s['n']} is far too small to conclude anything. In testing, four extra")
            print(f"    trades moved the total from +1.6% to +121%. Aim for 100+ before trusting it.")
        if s["avg_net"] > 0 > s["med_net"]:
            print(f"  ⚠ mean {s['avg_net']:+.1f}% but median {s['med_net']:+.1f}% — the average is carried by")
            print(f"    outliers (best {s['best']:+.0f}%), while the typical trade loses. That profile can be")
            print(f"    real for memecoin momentum, but it means most trades bleed and a few rare")
            print(f"    winners pay for them. Size and patience matter more than win rate here.")

    if s.get("stop_slip"):
        n, worst, avg = s["stop_slip"]
        print(f"\n  ⚠ stop-loss slippage: {n} stop exits realised {avg:+.1f}% on average "
              f"(worst {worst:+.1f}%)\n"
              f"    against a -{strat.STOP_LOSS_PCT:g}% stop level. A stop fires on the first tick\n"
              f"    at or below the level — on a thin memecoin that tick can be far below it,\n"
              f"    so the stop bounds when you exit, not how much you lose.")


def run(by_mint: dict, cost_per_side: float, size_sol: float) -> tuple[list, dict]:
    all_trades = []
    for ticks in by_mint.values():
        if len(ticks) < strat.SLOW_PERIOD:
            continue
        all_trades += replay_mint(ticks, cost_per_side)
    return all_trades, summarize(all_trades, size_sol)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", default=TICKS_FILE)
    ap.add_argument("--cost-per-side", type=float, default=2.5,
                    help="fees + slippage per side, percent (default 2.5)")
    ap.add_argument("--size-sol", type=float, default=0.18)
    ap.add_argument("--sweep", action="store_true", help="grid search over parameters")
    ap.add_argument("--bar-sec", type=int, default=None)
    ap.add_argument("--fast", type=int, default=None)
    ap.add_argument("--slow", type=int, default=None)
    args = ap.parse_args()

    if not os.path.exists(args.ticks):
        raise SystemExit(
            f"No tick data at {args.ticks}\n"
            "Run the bot in paper mode first to record some:\n"
            "  /opt/homebrew/bin/python3.14 ema_cross_bot.py --auto-discover"
        )

    print(f"loading {args.ticks} …")
    by_mint = load_ticks(args.ticks)
    n_ticks = sum(len(v) for v in by_mint.values())
    spans = [v[-1][0] - v[0][0] for v in by_mint.values() if len(v) > 1]
    print(f"  {n_ticks} ticks across {len(by_mint)} mints"
          + (f", median span {statistics.median(spans):.0f}s" if spans else ""))

    cost = args.cost_per_side / 100

    if not args.sweep:
        strat.configure(bar_sec=args.bar_sec, fast_period=args.fast, slow_period=args.slow)
        p = strat.params()
        print(f"\nparams: EMA{p['fast']}/{p['slow']} on {p['bar_sec']}s bars | "
              f"edge>={p['min_edge_bps']:.0f}bps hold>={p['min_hold_bars']} "
              f"cooldown={p['cooldown_bars']} stop=-{p['stop_loss_pct']:g}% trail=-{p['trail_pct']:g}%")
        print(f"cost: {args.cost_per_side:.1f}%/side = {args.cost_per_side * 2:.1f}% round trip")
        trades, s = run(by_mint, cost, args.size_sol)
        print_summary(f"EMA{p['fast']}/{p['slow']} @ {p['bar_sec']}s", s, args.size_sol)
        if trades:
            print("\n  last 10 trades:")
            for x in trades[-10:]:
                print(f"    {x['pnl_pct']:+7.2f}% net  ({x['gross_pct']:+6.2f}% gross)  "
                      f"{x['held_sec']:5.0f}s  {x['reason']}")
        return

    # ── sweep ────────────────────────────────────────────────────────────────
    grid = []
    for bar in (5, 10, 15, 30):
        for fast, slow in ((12, 21), (9, 21), (8, 34), (5, 15), (21, 55)):
            grid.append((bar, fast, slow))

    print(f"\nsweeping {len(grid)} configs at {args.cost_per_side * 2:.1f}% round-trip cost…\n")
    header = f"{'bars':>5} {'fast/slow':>10} {'n':>5} {'win%':>6} {'avg net':>9} {'avg gross':>10} {'PF':>6} {'total':>9}"
    print(header)
    print("-" * len(header))

    rows = []
    for bar, fast, slow in grid:
        strat.configure(bar_sec=bar, fast_period=fast, slow_period=slow)
        _, s = run(by_mint, cost, args.size_sol)
        if not s["n"]:
            print(f"{bar:>5} {f'{fast}/{slow}':>10} {'—':>5}")
            continue
        rows.append((s["total_net_pct"], bar, fast, slow, s))
        print(f"{bar:>5} {f'{fast}/{slow}':>10} {s['n']:>5} {s['win_rate']:>5.1f}% "
              f"{s['avg_net']:>+8.2f}% {s['avg_gross']:>+9.2f}% {s['profit_factor']:>6.2f} "
              f"{s['total_net_pct']:>+8.1f}%")

    if rows:
        rows.sort(reverse=True)
        best = rows[0]
        print(f"\nbest by total net: EMA{best[2]}/{best[3]} on {best[1]}s bars "
              f"— {best[4]['total_net_pct']:+.1f}% over {best[4]['n']} trades")
        print("\nA config only means something if `n` is large (100+) and it holds up on\n"
              "data the grid wasn't picked on. With a small sample the top row is noise.")


if __name__ == "__main__":
    main()
