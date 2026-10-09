"""
Render recorded ticks as the 5s candles the bot actually trades, with EMA12/21
and the entry/exit points marked — so the strategy can be eyeballed against a
TradingView chart of the same token.

  /opt/homebrew/bin/python3.14 plot_ema_cross.py              # busiest token
  /opt/homebrew/bin/python3.14 plot_ema_cross.py --mint <MINT>
  /opt/homebrew/bin/python3.14 plot_ema_cross.py --top 5      # 5 busiest

Writes a self-contained HTML file (no CDN, no network) and prints the path.
Gap-filled bars — 5s windows where nothing traded — are drawn hollow, so it is
obvious when the EMAs are tracking silence rather than price.
"""

import argparse
import json
import os
from collections import defaultdict

import ema_cross_strategy as strat

HERE = os.path.dirname(os.path.abspath(__file__))


def build_series(ticks: list) -> dict:
    """Replay ticks and capture per-bar OHLC, EMAs and signal points."""
    t = strat.new_tracker("plot", 1.0)
    bars, emas, marks = [], [], []
    last_bar, last_seen = None, -1

    def snapshot(traded: bool):
        bars.append({"o": t["bar_o"], "h": t["bar_h"], "l": t["bar_l"], "c": t["bar_c"],
                     "t": t["bar_start"], "traded": traded})
        emas.append({"f": t["fast"], "s": t["slow"]})

    for ts, price in ticks:
        was = t["want_long"]
        prev_bars = t["bars_seen"]
        strat.ingest_tick(t, price, ts)
        # a bar (or several, gap-filled) closed on this tick
        if t["bars_seen"] > prev_bars:
            for _ in range(t["bars_seen"] - prev_bars):
                snapshot(traded=False)
            if bars:
                bars[-1]["traded"] = True
        if t["want_long"] != was:
            marks.append({"i": max(len(bars) - 1, 0), "p": price,
                          "side": "buy" if t["want_long"] else "sell",
                          "reason": t.get("exit_reason") or "ema cross up"})
    return {"bars": bars, "emas": emas, "marks": marks,
            "ticks": t["ticks"], "ready": strat.is_ready(t)}


HTML = """<title>EMA%(fast)d/%(slow)d @ %(bar)ds — %(mint)s</title>
<style>
 :root{--bg:#fbfbfa;--fg:#1a1a19;--grid:#e4e4e1;--up:#0f9d58;--dn:#d93025;
       --fast:#1a73e8;--slow:#f9ab00;--muted:#77776f;}
 :root:not([data-theme=light]){}
 @media (prefers-color-scheme:dark){:root:not([data-theme=light]){
   --bg:#161614;--fg:#f0efec;--grid:#2f2d2a;--muted:#9a978f;}}
 :root[data-theme=dark]{--bg:#161614;--fg:#f0efec;--grid:#2f2d2a;--muted:#9a978f;}
 body{background:var(--bg);color:var(--fg);font:14px system-ui,-apple-system,sans-serif;padding:24px;}
 h1{font-size:17px;margin:0 0 2px;font-weight:600;}
 .sub{color:var(--muted);font-size:12.5px;margin-bottom:16px;}
 .wrap{overflow-x:auto;border:1px solid var(--grid);border-radius:8px;background:var(--bg);}
 canvas{display:block;}
 .key{margin-top:12px;color:var(--muted);font-size:12.5px;line-height:1.7;}
 .sw{display:inline-block;width:11px;height:3px;vertical-align:middle;margin-right:5px;}
 code{background:var(--grid);padding:1px 5px;border-radius:4px;font-size:12px;}
</style>
<h1>EMA%(fast)d / EMA%(slow)d on %(bar)ds bars</h1>
<div class="sub"><code>%(mint)s</code> — %(nbars)d bars from %(nticks)d real swaps, %(nmarks)d signals</div>
<div class="wrap"><canvas id="c"></canvas></div>
<div class="key">
 <span class="sw" style="background:var(--fast)"></span>EMA%(fast)d &nbsp;
 <span class="sw" style="background:var(--slow)"></span>EMA%(slow)d &nbsp;
 &nbsp;▲ entry &nbsp; ▼ exit &nbsp;&nbsp; hollow candle = no trades in that %(bar)ds window (gap-filled at last close)
</div>
<script>
const D=%(data)s, BW=9, GAP=2, PAD=54, H=420;
const c=document.getElementById('c'), x=c.getContext('2d');
const n=D.bars.length, W=Math.max(760, PAD*2+n*(BW+GAP));
const dpr=window.devicePixelRatio||1;
c.width=W*dpr; c.height=H*dpr; c.style.width=W+'px'; c.style.height=H+'px'; x.scale(dpr,dpr);
const cs=getComputedStyle(document.documentElement), C=k=>cs.getPropertyValue(k).trim();
let lo=Infinity,hi=-Infinity;
for(const b of D.bars){if(b.l<lo)lo=b.l;if(b.h>hi)hi=b.h;}
for(const e of D.emas){for(const v of [e.f,e.s]) if(v!=null){if(v<lo)lo=v;if(v>hi)hi=v;}}
const pad=(hi-lo)*0.10||hi*0.02||1; lo-=pad; hi+=pad;
const Y=v=>H-36-(v-lo)/(hi-lo)*(H-70);
const X=i=>PAD+i*(BW+GAP)+BW/2;
x.strokeStyle=C('--grid'); x.fillStyle=C('--muted'); x.font='11px system-ui'; x.lineWidth=1;
for(let g=0;g<=4;g++){const v=lo+(hi-lo)*g/4, y=Y(v);
  x.beginPath();x.moveTo(PAD-8,y);x.lineTo(W-10,y);x.stroke();
  x.fillText(v.toExponential(2), 4, y+3.5);}
D.bars.forEach((b,i)=>{const up=b.c>=b.o, col=up?C('--up'):C('--dn');
  x.strokeStyle=col; x.fillStyle=col; x.lineWidth=1;
  x.beginPath(); x.moveTo(X(i),Y(b.h)); x.lineTo(X(i),Y(b.l)); x.stroke();
  const y0=Y(Math.max(b.o,b.c)), hgt=Math.max(Math.abs(Y(b.o)-Y(b.c)),1);
  if(b.traded) x.fillRect(X(i)-BW/2,y0,BW,hgt); else x.strokeRect(X(i)-BW/2+.5,y0+.5,BW-1,Math.max(hgt-1,1));});
function line(key,col){x.strokeStyle=col;x.lineWidth=1.8;x.beginPath();let started=false;
  D.emas.forEach((e,i)=>{const v=e[key]; if(v==null)return;
    if(!started){x.moveTo(X(i),Y(v));started=true;}else x.lineTo(X(i),Y(v));});
  x.stroke();}
line('s',C('--slow')); line('f',C('--fast'));
D.marks.forEach(m=>{const buy=m.side==='buy', y=Y(m.p);
  x.fillStyle=buy?C('--up'):C('--dn'); x.beginPath();
  if(buy){x.moveTo(X(m.i),y+12);x.lineTo(X(m.i)-6,y+23);x.lineTo(X(m.i)+6,y+23);}
  else{x.moveTo(X(m.i),y-12);x.lineTo(X(m.i)-6,y-23);x.lineTo(X(m.i)+6,y-23);}
  x.closePath();x.fill();});
</script>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", default=os.path.join(HERE, "ema_ticks.jsonl"))
    ap.add_argument("--mint", default=None)
    ap.add_argument("--top", type=int, default=1, help="render the N busiest tokens")
    ap.add_argument("--out-dir", default=HERE)
    args = ap.parse_args()

    by = defaultdict(list)
    with open(args.ticks) as f:
        for line in f:
            try:
                r = json.loads(line)
                by[r["mint"]].append((r["ts"], r["price"]))
            except Exception:
                pass
    for m in by:
        by[m].sort()

    if args.mint:
        chosen = [args.mint] if args.mint in by else []
        if not chosen:
            raise SystemExit(f"{args.mint} not in {args.ticks}")
    else:
        chosen = [m for m, _ in sorted(by.items(), key=lambda kv: -len(kv[1]))[:args.top]]

    for mint in chosen:
        s = build_series(by[mint])
        html = HTML % {
            "fast": strat.FAST_PERIOD, "slow": strat.SLOW_PERIOD, "bar": strat.BAR_SEC,
            "mint": mint, "nbars": len(s["bars"]), "nticks": s["ticks"],
            "nmarks": len(s["marks"]),
            "data": json.dumps({"bars": s["bars"], "emas": s["emas"], "marks": s["marks"]}),
        }
        out = os.path.join(args.out_dir, f"ema_chart_{mint[:8]}.html")
        with open(out, "w") as f:
            f.write(html)
        traded = sum(1 for b in s["bars"] if b["traded"])
        print(f"{out}\n   {len(s['bars'])} bars ({traded} traded, "
              f"{len(s['bars']) - traded} gap-filled), {len(s['marks'])} signals")


if __name__ == "__main__":
    main()
