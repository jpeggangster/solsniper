# Axiom EMA Cross Bot (Chrome extension)

Watches the EMA12/21 cross on the Axiom chart you have open and fires Axiom's
own hotkeys — `space+W` to buy, `space+F` to sell 100%.

## Why it reads the chart instead of the chain

The EMA values come from the chart legend, not from a parallel calculation.
A separate on-chain implementation has to match bar alignment, warmup depth and
venue exactly to agree with what you see; in testing it did not — an
independently computed EMA21 differed from this chart's by a third during a fast
move, and a 9 bps "cross" that existed only in the calculation triggered a real
losing trade. Scraping the legend makes the bot trade what you are looking at,
by construction.

## Install

1. `chrome://extensions` → enable **Developer mode**
2. **Load unpacked** → select this folder
3. Open your Axiom chart, click the extension icon — the UI opens in Chrome's
   **side panel** and stays open while you use the chart. (A popup would close
   the moment focus moved to the page, which is exactly when the bot matters.)

While the bot is running the panel follows the tab it locked onto, so the
readouts stay correct even if you switch tabs.

## Use

1. Put the **EMA Cross (21, 12)** study on the chart — the bot reads its legend,
   so it must be visible.
2. Open the popup. It shows the detected contract address and the live EMA
   reading. **If either is blank, stop — it cannot trade what it cannot read.**
3. Leave **mode = paper** for the first session. Paper logs every decision and
   sends no keys.
4. When the log looks right, switch to **LIVE**.

## How it decides

The bot steers towards a **target state**, it does not react to cross events.

Every tick it asks "where are the two lines *now*" and derives the target from
that: 12 above 21 means the target is LONG, below means FLAT. The background
then compares the target with what it believes it holds and presses a key only
if they differ.

This is deliberate. A cross marker exists for one bar and is then gone, so a bot
that waits to *catch* one is wrong forever if it blinks at the wrong moment — a
throttled background tab, a re-rendered chart iframe, a restarted service
worker. A level is true for as long as it is true, so a missed tick costs
nothing: the next one re-derives the same answer. The target is also re-asserted
every few bars, so a position that drifted out of sync (a key that did not land,
a manual trade) is corrected rather than staying wrong until the next cross.

**Hysteresis.** A bare `fast > slow` flips on every touch while the lines are
tangled, which on 1s bars is most of the time. The target changes only once the
lines are clearly apart and holds inside the dead zone. The band is a percent of
pane height, not raw pixels, because a pixel gap means different things at
different zoom levels and after every autoscale.

**The cross column gates entries, never exits.** "12 > 21" says nothing about
whether the cross happened three seconds or forty minutes ago, and entering a
move that already ran is how you buy the top — so a new LONG requires a recent
column near the right edge. Exits are never gated: refusing to leave a position
because a marker was not found is the one failure here that can cost real money.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| mode | paper | paper logs only; LIVE sends real hotkeys |
| entry gate | need fresh cross column | or trade on line positions alone |
| cross must be within | 300 px | how near the newest bar a column still counts as recent |
| hysteresis band | 0.8 % of pane | dead zone; below this the target holds |
| min column height | 30 px | screen pixels, scaled for Retina automatically |
| bar interval | 1 s | evaluation cadence, match it to the chart |
| fast EMA is | 2nd legend value | which legend number is the 12 |
| hotkey style | hold space + key | vs press space then the key |
| buy / sell key | w / f | Axiom's hotkeys |

Every pixel setting is in **CSS pixels** — what you would measure on screen —
and scaled by the display's device-pixel ratio internally. They used to be
applied raw to the canvas backing store, so on a Retina display every search
window was silently half its intended size. That is what hid the cross marker:
the search band was 140 backing-store pixels, i.e. 70 on screen, while the
columns sat ~270 px from the newest bar.

**`fast EMA is`** matters. Your indicator is labelled `Short: 21 / Long: 12`,
which is inverted naming, so the legend prints `21` first and `12` second — hence
the default. Check the popup: on a rising chart it should read `12>21`. If it
reads backwards, flip this setting.

## Safety

- Crosses are only acted on at **5-second bar closes**, matching the chart.
- Buys only when flat, sells only when long — no stacking.
- The contract address is **locked at start**. Switch charts and it stops acting.
- Closing the tab, detaching the debugger, or hitting max trades stops the bot.
- Chrome shows a **"… is debugging this browser"** banner the whole time the bot
  is armed. That banner is deliberate: it means something can trade for you.

## Reading the chart

Two sources, tried in order (`EMA source` = auto):

1. **Legend text** — if the study's values exist in the DOM.
2. **Canvas pixels** — Axiom draws the chart on `<canvas>`, so the legend is
   often pixels rather than text. The bot then finds the **green (EMA12)** and
   **red (EMA21)** lines near the right edge and compares their heights: green
   above red is LONG. The **yellow cross marker** is read too and logged as
   `✦marker` when it confirms a cross on the same bar.

Colours are auto-detected — of the saturated colours at the right edge it takes
the most green-dominant and most red-dominant. `sample chart colours` shows what
it found and whether both lines resolve. Override with the two swatches if your
theme confuses it.

Pixel mode knows *which line is on top*, not by how much in price, so the
15 bps noise filter does not apply there; it uses a **2-pixel minimum gap**
instead.

## Known limits — read before going LIVE

- **The thresholds are guesses, not measurements.** The 0.8 % band and the 300 px
  freshness window are starting points chosen to be plausible, not values tuned
  against your fills. Watch the `why` line in paper mode for a session and move
  them: too many flips means widen the band, missed entries mean widen it less
  or loosen the freshness window.
- **Legend scraping is disabled.** This chart carries several EMA studies and
  the legend text could not be attributed to the right one — it produced
  "994.91 vs 20.000" from a different study and fabricated a trade. Everything
  now comes from the canvas, which is why separations are reported as a percent
  of pane height rather than in bps.
- **Hotkey style is a guess.** "space + W" could be a chord or a sequence. Both
  are implemented; try paper mode first and confirm Axiom reacts.
- **Sizing is Axiom's, not the bot's.** It presses your buy key; whatever size
  that key is bound to is what trades.
- **No stop-loss.** Exits happen when the target turns FLAT and nothing else.
- **Retries are bounded.** If a position cannot be corrected after `maxRetries`
  attempts the bot logs an error and stops pressing that key, rather than
  hammering the hotkey every bar for the rest of the session. Fix it by hand.

## Tests

`cd dev && node test_marker_distance.js` — the regression test for the marker
bug: a column away from the right edge, on both a 1x and a 2x display.
`node test_state.js` drives the real `tick()` over synthetic panes to check the
dead zone, the entry gate and that exits are never gated. `test_pixels.js` and
`test_legend.js` are stale (they predate `contentBand` and the legend removal)
and fail on purpose; `test_pixels2.js` supersedes the former.
