/*
 * Reads the EMA lines and the cross marker straight off the chart canvas.
 *
 * Axiom's chart draws its legend and series onto <canvas>, so there is no DOM
 * text to scrape. But the two EMA lines are still pixels of distinct colours,
 * and "is 12 above 21" is a question about their vertical positions. This finds
 * each line's y at the right-hand edge and compares them — which is exactly
 * what the eye does when reading the cross.
 *
 * Screen y grows downward, so the HIGHER line has the SMALLER y.
 *
 * ── DEVICE PIXELS vs CSS PIXELS ──────────────────────────────────────────────
 * A canvas has two sizes: `canvas.width` (the backing store, what getImageData
 * addresses) and its CSS box width (what you see). On a Retina display the
 * backing store is 2x the CSS size. Every constant below is expressed in CSS
 * pixels — what you would measure on screen — and scaled by `dprOf()` at the
 * point of use. Previously they were applied raw to the backing store, so on a
 * 2x display every search window was silently HALF its intended size on screen.
 * That is what hid the cross marker: see the note in crossMarker().
 */

const PX = {
  edgeCols: 26,      // CSS px sampled for the LINE positions (must stay near the edge)
  markerCols: 1200,  // CSS px searched for the cross COLUMN — see note in crossMarker()
  skipRight: 4,      // ignore the last few px (crosshair / price label overlap)
  topSkipFrac: 0.10, // ignore the top tenth, where the legend text is drawn
  tolerance: 48,     // per-channel colour distance
  maxLineRun: 5,     // CSS px vertical: longer than this is a candle, not a line
  traceStep: 7,      // CSS px the line may move per column while tracing
  bandMinRun: 6,     // CSS px vertical run that marks a column as "has content"
};

/*
 * Backing-store pixels per CSS pixel for this canvas.
 *
 * Falls back to 1 when the element has no layout box — which is the case for
 * the synthetic canvases in dev/, so the tests keep exercising the same code
 * path with a 1:1 mapping.
 */
function dprOf(cv) {
  try {
    if (typeof cv.getBoundingClientRect === "function") {
      const r = cv.getBoundingClientRect();
      if (r && r.width > 0) {
        const d = cv.width / r.width;
        if (d >= 0.5 && d <= 4) return d;   // sane range; ignore hidden/collapsed
      }
    }
  } catch (_) {}
  return 1;
}

const scale = (cssPx, dpr) => Math.max(1, Math.round(cssPx * dpr));

function rgbOf(hex) {
  const h = String(hex).replace("#", "");
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

function near(r, g, b, target, tol) {
  return Math.abs(r - target[0]) <= tol &&
         Math.abs(g - target[1]) <= tol &&
         Math.abs(b - target[2]) <= tol;
}

function chartCanvases() {
  // Only the main price pane. The narrow 56px-wide canvases beside it are the
  // PRICE AXIS: its coloured badges (yellow/red/green last-price labels) look
  // exactly like line colours and will fool both colour picking and line
  // finding. Require a wide canvas, not merely a large one.
  return [...document.querySelectorAll("canvas")]
    .map((c) => ({ c, w: c.width || 0, h: c.height || 0 }))
    .filter((x) => x.w >= 400 && x.h >= 200)      // excludes axes and toolbars
    .sort((a, b) => b.w * b.h - a.w * a.h)
    .map((x) => x.c);
}

/**
 * TradingView leaves empty space to the right of the newest candle, so the far
 * right columns contain nothing. Walk leftwards to the newest column that
 * actually has plotted content, and sample a band ending there.
 */
function contentBand(ctx, w, h, yTop, dpr = 1) {
  // The newest column with real plotted content, found by walking leftwards.
  //
  // A simple "any saturated pixel" test fails here: TradingView draws the
  // last-price line and order lines horizontally across the ENTIRE pane,
  // including the empty margin right of the newest candle. Those are 1-2px
  // tall. A candle body or wick is a tall vertical run, so requiring a run of
  // MIN_RUN contiguous saturated pixels finds the real edge of the data.
  const MIN_RUN = scale(PX.bandMinRun, dpr);
  const maxScan = Math.min(Math.floor(w * 0.6), scale(800, dpr));
  let right = null;
  for (let x = w - 1; x > w - maxScan && x > 0; x--) {
    let data;
    try { data = ctx.getImageData(x, yTop, 1, h - yTop).data; } catch (_) { return null; }
    let run = 0, best = 0;
    for (let i = 0; i < data.length; i += 4) {
      const r = data[i], g = data[i+1], b = data[i+2], a = data[i+3];
      const mx = Math.max(r,g,b), mn = Math.min(r,g,b);
      if (a >= 200 && mx >= 60 && mx - mn >= 40) { run++; if (run > best) best = run; }
      else run = 0;
    }
    if (best >= MIN_RUN) { right = x; break; }
  }
  if (right == null) return null;
  const width = Math.min(scale(PX.edgeCols, dpr), right + 1);
  return { x0: Math.max(0, right - width + 1), w: width, right };
}

/**
 * Median y of the LINE pixels matching `target` within the content band.
 *
 * Candle bodies are drawn in greens and reds very close to the EMA line
 * colours, so a naive colour match reads the candles as well as the line and
 * drags the median far away — on a live chart this reported the two EMAs as
 * 264px apart on a 489px pane and pinned the direction to LONG permanently.
 *
 * A line is thin: a few pixels of vertical run per column. A candle body or
 * wick is a long run. Keeping only short runs isolates the line.
 */
function lineY(ctx, w, h, target, tol, band, maxRun, detail, dpr = 1) {
  const yTop = Math.floor(h * PX.topSkipFrac);
  band = band || contentBand(ctx, w, h, yTop, dpr);
  if (!band) return null;
  const x0 = band.x0, cw = band.w;
  maxRun = maxRun || scale(PX.maxLineRun, dpr);
  let data;
  try {
    data = ctx.getImageData(x0, yTop, cw, h - yTop).data;
  } catch (_) {
    return null;                       // tainted or WebGL canvas
  }
  const rows = h - yTop;

  // Candidate y positions per column: thin runs only, so candle bodies are out.
  const perCol = [];
  for (let x = 0; x < cw; x++) {
    const ys = [];
    let run = 0, start = 0;
    for (let y = 0; y <= rows; y++) {
      let hit = false;
      if (y < rows) {
        const i = (y * cw + x) * 4;
        hit = data[i + 3] >= 128 &&
              near(data[i], data[i + 1], data[i + 2], target, tol);
      }
      if (hit) { if (run === 0) start = y; run++; }
      else {
        if (run > 0 && run <= maxRun) ys.push(start + run / 2 + yTop);
        run = 0;
      }
    }
    perCol.push(ys);
  }

  // Trace lines from the newest column leftwards, following the nearest
  // candidate in each previous column.
  //
  // Grouping by y instead (the previous approach) breaks on a sloping line: a
  // steep EMA spreads its points over many y values so no band is populated,
  // while a horizontal dashed position line concentrates every point at ONE y
  // and wins. That misread a green dashed marker line as the EMA12. Tracing
  // follows slope, and a dashed line breaks its own trace at every gap.
  const STEP = scale(PX.traceStep, dpr);   // max px the line may move per column
  let best = null;

  // Start from the newest column that actually HAS a candidate. The band's
  // right edge is set by the newest candle, and a candle body or wick can
  // extend past the last plotted EMA point — starting rigidly at the last
  // column then finds nothing and the read fails entirely.
  let lastCol = -1;
  for (let x = cw - 1; x >= 0; x--) {
    if (perCol[x] && perCol[x].length) { lastCol = x; break; }
  }
  if (lastCol < 0) return null;

  for (const startY of perCol[lastCol] || []) {
    let y = startY, len = 1, gaps = 0;
    for (let x = lastCol - 1; x >= 0; x--) {
      let nearest = null, dist = Infinity;
      for (const cy of perCol[x]) {
        const d = Math.abs(cy - y);
        if (d < dist) { dist = d; nearest = cy; }
      }
      if (nearest != null && dist <= STEP) { y = nearest; len++; }
      else if (++gaps > 3) break;      // a few dash gaps tolerated, not many
    }
    if (!best || len > best.len) best = { y: startY, len, gaps };
  }
  if (!best) return null;

  if (detail) {
    detail.traceLen = best.len;
    detail.columns = cw;
    detail.startCol = lastCol;
    detail.candidates = (perCol[lastCol] || []).map((v) => Math.round(v));
  }
  return best.y;
}

/**
 * Pick the EMA line colours automatically: of the saturated colours near the
 * right edge, take the most green-dominant and the most red-dominant. Saves
 * hand-matching hex values and survives a theme change.
 */
function autoColors() {
  // Only consider colours that appear in most columns of the band — a line is
  // continuous, a candle body is not.
  const cols = sampleColors().filter((c) => c.perCol == null || c.perCol >= 0.6);
  let best = { fast: null, slow: null, fastScore: -1, slowScore: -1 };
  for (const c of cols) {
    const [r, g, b] = c.rgb.split(",").map(Number);
    const greenness = g - Math.max(r, b);
    const redness = r - Math.max(g, b);
    if (greenness > best.fastScore && greenness > 20) {
      best.fastScore = greenness; best.fast = [r, g, b];
    }
    if (redness > best.slowScore && redness > 20) {
      best.slowScore = redness; best.slow = [r, g, b];
    }
  }
  if (!best.fast || !best.slow) return null;
  const hex = (c) => "#" + c.map((v) => v.toString(16).padStart(2, "0")).join("");
  return { fastColor: hex(best.fast), slowColor: hex(best.slow) };
}

/**
 * The indicator paints a column (yellow by default) where the EMAs cross.
 * Returns the NEWEST such column and how far left of the newest data it sits,
 * which is what tells us how old the cross is.
 *
 * ── why this used to find nothing ───────────────────────────────────────────
 * Two compounding bugs, both about units:
 *
 *  1. The search band was 140 pixels of BACKING STORE. On a 2x display that is
 *     70 CSS px — roughly three bars on a 1s chart. A cross column scrolls out
 *     of that within seconds and is then invisible forever. Observed live: the
 *     columns sat ~270 CSS px left of the newest candle, four times outside the
 *     window, so `marker: none` was reported while two columns were plainly on
 *     screen.
 *  2. `minRun` was likewise compared against backing-store pixels, so a
 *     configured "30px tall" was really asking for 15 CSS px.
 *
 * Both are now scaled through dprOf(). The band is also far wider, because
 * limiting the search is the wrong way to decide a cross is stale: scan wide,
 * report `fromRight`, and let the caller apply a freshness rule it can state in
 * bars. Scanning stops at the first (newest) qualifying column, so a wide band
 * costs little — the common case exits within a few columns of the edge.
 */
function crossMarker(hex = "#FFEB3B", tol = 40, minRun = 1) {
  const target = rgbOf(hex);
  let best = { run: 0, pixels: 0, x: null, y: null, fromRight: null, dpr: 1 };
  for (const cv of chartCanvases()) {
    const ctx = cv.getContext("2d", { willReadFrequently: true });
    if (!ctx) continue;
    const dpr = dprOf(cv);
    const w = cv.width, h = cv.height;
    const yTop = Math.floor(h * PX.topSkipFrac);
    const band = contentBand(ctx, w, h, yTop, dpr);
    if (!band) continue;
    const need = scale(minRun, dpr);
    const wide = Math.min(scale(PX.markerCols, dpr), band.right + 1);
    const x0 = Math.max(0, band.right - wide + 1);
    const cols = wide;
    let data;
    try { data = ctx.getImageData(x0, yTop, cols, h - yTop).data; }
    catch (_) { continue; }

    const rows = h - yTop;
    // Right to left: the FIRST qualifying column is the most recent cross,
    // which is the one a freshness rule cares about. (The old code kept the
    // TALLEST column anywhere in the band, which on a wide band would prefer an
    // old, fully-drawn column over the newest one.)
    let found = null;
    for (let x = cols - 1; x >= 0 && !found; x--) {
      let run = 0, colBest = 0, total = 0, topY = null, bestTop = null;
      for (let y = 0; y < rows; y++) {
        const i = (y * cols + x) * 4;
        const hit = data[i + 3] >= 128 &&
                    near(data[i], data[i + 1], data[i + 2], target, tol);
        if (hit) {
          total++;
          if (run === 0) topY = y;
          run++;
          if (run > colBest) { colBest = run; bestTop = topY; }
        } else run = 0;
      }
      if (colBest >= need) {
        found = { run: colBest, pixels: total, x: x0 + x,
                  fromRight: band.right - (x0 + x),
                  y: bestTop == null ? null : bestTop + yTop };
      }
      if (colBest > best.run) best = { ...best, run: colBest, pixels: total, dpr };
    }
    if (found) {
      best = { ...found, dpr };
      break;
    }
  }
  const dpr = best.dpr || 1;
  return {
    found: best.fromRight != null,
    run: best.run,                                   // backing-store px
    runCss: Math.round(best.run / dpr),              // what you would measure on screen
    pixels: best.pixels,
    x: best.x, y: best.y,
    fromRight: best.fromRight,
    fromRightCss: best.fromRight == null ? null : Math.round(best.fromRight / dpr),
    dpr,
  };
}

/** Distinct saturated colours near the right edge — used to pick line colours. */
function sampleColors() {
  const out = [];
  for (const cv of chartCanvases()) {
    const ctx = cv.getContext("2d", { willReadFrequently: true });
    if (!ctx) continue;
    const dpr = dprOf(cv);
    const w = cv.width, h = cv.height;
    const yTop = Math.floor(h * PX.topSkipFrac);
    const band = contentBand(ctx, w, h, yTop, dpr);
    if (!band) continue;
    let data;
    try { data = ctx.getImageData(band.x0, yTop, band.w, h - yTop).data; }
    catch (_) { continue; }
    const hist = new Map();
    for (let i = 0; i < data.length; i += 4) {
      const r = data[i], g = data[i + 1], b = data[i + 2], a = data[i + 3];
      if (a < 200) continue;
      const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
      if (mx < 60 || mx - mn < 40) continue;          // skip greys, grid, background
      const key = `${r >> 3 << 3},${g >> 3 << 3},${b >> 3 << 3}`;
      hist.set(key, (hist.get(key) || 0) + 1);
    }
    // A line spans many columns; a candle body spans few. Lines therefore show
    // up as colours present in most sampled columns.
    [...hist.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8)
      .forEach(([k, n]) => out.push({ rgb: k, n, cols: band.w,
                                      perCol: +(n / band.w).toFixed(1),
                                      canvas: `${w}x${h}`, rightX: band.right }));
  }
  return out;
}

/**
 * Returns { fastY, slowY, direction, gapPx, gapFrac, paneH, dpr } or null.
 * direction is "LONG" when the fast line sits above the slow line.
 *
 * `gapFrac` is the separation as a fraction of pane height. Raw pixel gaps are
 * meaningless as a threshold because they change with zoom and with every
 * autoscale, so the hysteresis band upstream is expressed against this instead.
 */
function readLines(fastHex, slowHex, tol = PX.tolerance) {
  const fast = rgbOf(fastHex), slow = rgbOf(slowHex);
  for (const cv of chartCanvases()) {
    const ctx = cv.getContext("2d", { willReadFrequently: true });
    if (!ctx) continue;
    const dpr = dprOf(cv);
    const w = cv.width, h = cv.height;
    const yTop = Math.floor(h * PX.topSkipFrac);
    const band = contentBand(ctx, w, h, yTop, dpr);
    if (!band) continue;
    const fd = {}, sd = {};
    const fy = lineY(ctx, w, h, fast, tol, band, null, fd, dpr);
    const sy = lineY(ctx, w, h, slow, tol, band, null, sd, dpr);
    if (fy == null || sy == null) continue;
    const paneH = h - yTop;
    return {
      fastY: fy, slowY: sy,
      gapPx: sy - fy,                    // positive => fast is above
      gapFrac: (sy - fy) / paneH,        // zoom-invariant
      paneH, dpr,
      direction: fy < sy ? "LONG" : "FLAT",
      canvas: `${w}x${h}`, rightX: band.right,
      fastTrace: fd.traceLen, slowTrace: sd.traceLen, columns: fd.columns,
      fastCandidates: fd.candidates, slowCandidates: sd.candidates,
    };
  }
  return null;
}


/*
 * Visual debug: draw where the bot thinks each line is, over the real chart.
 *
 * Every direction bug so far has come from the reader locking onto the wrong
 * thing — a price axis badge, a candle body, a dashed position line. Arguing
 * about it from screenshots is slow; overlaying the detected positions on the
 * chart answers it in one glance.
 */
function debugOverlay(show, fastY, slowY, markerX) {
  const id = "__emaBotOverlay";
  let host = document.getElementById(id);
  if (!show) { if (host) host.remove(); return; }

  const cv = chartCanvases()[0];
  if (!cv) return;
  const rect = cv.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const sy = rect.height / cv.height;          // canvas px -> CSS px
  const sx = rect.width / cv.width;

  if (!host) {
    host = document.createElement("div");
    host.id = id;
    host.style.cssText = "position:fixed;pointer-events:none;z-index:2147483647";
    document.body.appendChild(host);
  }
  host.style.left = rect.left + "px";
  host.style.top = rect.top + "px";
  host.style.width = rect.width + "px";
  host.style.height = rect.height + "px";

  const line = (key, yCanvas, color, label) => {
    let el = host.querySelector("[data-k=" + key + "]");
    if (yCanvas == null) { if (el) el.remove(); return; }
    if (!el) {
      el = document.createElement("div");
      el.dataset.k = key;
      el.innerHTML = '<span style="position:absolute;right:2px;top:-14px;font:10px monospace;' +
                     'padding:1px 3px;border-radius:2px;color:#000"></span>';
      host.appendChild(el);
    }
    el.style.cssText = `position:absolute;left:0;width:100%;height:0;border-top:2px dashed ${color};` +
                       `top:${yCanvas * sy}px`;
    const tag = el.firstChild;
    tag.style.background = color;
    tag.textContent = label;
  };

  line("f", fastY, "#00e5ff", "bot: EMA12");
  line("s", slowY, "#ff00e5", "bot: EMA21");

  let m = host.querySelector("[data-k=m]");
  if (markerX == null) { if (m) m.remove(); }
  else {
    if (!m) { m = document.createElement("div"); m.dataset.k = "m"; host.appendChild(m); }
    m.style.cssText = "position:absolute;top:0;height:100%;width:0;" +
                      "border-left:2px dotted #ffffff;left:" + (markerX * sx) + "px";
  }
}
