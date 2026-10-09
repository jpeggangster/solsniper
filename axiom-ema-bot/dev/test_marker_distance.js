/*
 * Regression test for the bug that made `marker: none` appear while two yellow
 * columns were plainly on screen.
 *
 * test_marker.js only ever painted the column AT the newest bar, so it passed
 * with a search band of any width. The live failure was a column ~270 CSS px
 * left of the newest candle: outside the old 140-backing-store-pixel band,
 * which on a 2x display is only 70 CSS px.
 *
 * Also covers the second half of that bug — minRun being compared against
 * backing-store pixels, so a configured "30px tall" really asked for 15 on a
 * Retina display.
 */
const fs = require("fs");
const src = fs.readFileSync("../pixels.js", "utf8");

const BG = [13, 17, 23], TEAL = [8, 152, 128], YELLOW = [255, 235, 59];
const GREEN = [76, 175, 80], RED = [239, 83, 80];

/* dpr: backing store is `dpr` x the CSS box, as on a Retina display. */
function canvas(cssW, cssH, dpr, paint) {
  const w = cssW * dpr, h = cssH * dpr;
  const px = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < w * h; i++) {
    px[i*4] = BG[0]; px[i*4+1] = BG[1]; px[i*4+2] = BG[2]; px[i*4+3] = 255;
  }
  const set = (x, y, c) => {
    if (x < 0 || y < 0 || x >= w || y >= h) return;
    const i = (y * w + x) * 4;
    px[i] = c[0]; px[i+1] = c[1]; px[i+2] = c[2]; px[i+3] = 255;
  };
  paint(set, w, h, dpr);
  return {
    width: w, height: h,
    getBoundingClientRect: () => ({ width: cssW, height: cssH }),
    getContext: () => ({ getImageData: (sx, sy, sw, sh) => {
      const o = new Uint8ClampedArray(sw * sh * 4);
      for (let y = 0; y < sh; y++) for (let x = 0; x < sw; x++) {
        const si = ((sy + y) * w + (sx + x)) * 4, di = (y * sw + x) * 4;
        o[di] = px[si]; o[di+1] = px[si+1]; o[di+2] = px[si+2]; o[di+3] = px[si+3];
      }
      return { data: o };
    }})
  };
}

/* A pane that looks like the real thing: candles out to the newest bar, two EMA
 * lines, and cross columns at given CSS distances left of that newest bar. */
function pane(dpr, markers) {
  const cssW = 1321, cssH = 489, lastCss = 900;
  return canvas(cssW, cssH, dpr, (set, w, h) => {
    const last = lastCss * dpr;
    for (let x = 0; x <= last; x++) {                       // EMA lines
      const gy = Math.round((200 - x / (40 * dpr)) * dpr);
      const ry = Math.round((250 - x / (60 * dpr)) * dpr);
      for (let t = 0; t < dpr; t++) { set(x, gy + t, GREEN); set(x, ry + t, RED); }
    }
    // Spaced so the LAST candle lands exactly on `last`: contentBand measures
    // from the newest column with real content, and the thin EMA lines do not
    // qualify, so the newest candle is the origin every distance is relative to.
    for (let c = 0; c <= 40; c++) {                         // candle bodies
      const x = (100 + c * 20) * dpr;
      if (x > last) break;
      for (let y = 300 * dpr; y < 360 * dpr; y++)
        for (let t = 0; t < 3 * dpr; t++) set(x + t, y, TEAL);
    }
    for (const m of markers) {                              // cross columns
      const x0 = last - m.fromRightCss * dpr;
      const half = (m.heightCss * dpr) / 2;
      for (let y = Math.round(220 * dpr - half); y < Math.round(220 * dpr + half); y++)
        for (let t = 0; t < Math.max(1, 3 * dpr); t++) set(x0 + t, y, YELLOW);
    }
  });
}

function load(cv) {
  global.document = { querySelectorAll: () => [cv] };
  return new Function(src + "; return { crossMarker, readLines };")();
}

let pass = 0, total = 0;
const chk = (label, cond, extra = "") => {
  total++; console.log(`${cond ? "✅" : "❌"} ${label}${extra ? "  " + extra : ""}`);
  if (cond) pass++;
};

// ── 1. the live failure: a column 270 CSS px from the newest bar ────────────
let m = load(pane(1, [{ fromRightCss: 270, heightCss: 120 }]));
let r = m.crossMarker("#FFEB3B", 40, 30);
chk("column 270px from the edge is FOUND (dpr 1)", r.found,
    `fromRightCss=${r.fromRightCss} runCss=${r.runCss}`);
chk("...and its distance is reported accurately", r.found && Math.abs(r.fromRightCss - 270) <= 4,
    `got ${r.fromRightCss}`);

// ── 2. same chart on a Retina display ──────────────────────────────────────
m = load(pane(2, [{ fromRightCss: 270, heightCss: 120 }]));
r = m.crossMarker("#FFEB3B", 40, 30);
chk("column 270px from the edge is FOUND (dpr 2)", r.found,
    `fromRightCss=${r.fromRightCss} runCss=${r.runCss} dpr=${r.dpr}`);
chk("dpr is detected as 2", r.dpr === 2, `got ${r.dpr}`);
chk("distance is reported in CSS px, not backing-store px",
    r.found && Math.abs(r.fromRightCss - 270) <= 4, `got ${r.fromRightCss}`);
chk("height is reported in CSS px (120, not 240)",
    r.found && Math.abs(r.runCss - 120) <= 4, `got ${r.runCss}`);

// ── 3. min height is honoured in CSS px on a Retina display ────────────────
m = load(pane(2, [{ fromRightCss: 200, heightCss: 20 }]));
r = m.crossMarker("#FFEB3B", 40, 30);
chk("a 20px column is rejected when the minimum is 30px (dpr 2)", !r.found,
    `found=${r.found} runCss=${r.runCss}`);

// ── 4. of several columns, the NEWEST one wins ─────────────────────────────
m = load(pane(2, [{ fromRightCss: 600, heightCss: 150 },
                  { fromRightCss: 100, heightCss: 150 }]));
r = m.crossMarker("#FFEB3B", 40, 30);
chk("the newest column wins when several are on screen",
    r.found && Math.abs(r.fromRightCss - 100) <= 4, `fromRightCss=${r.fromRightCss}`);

// ── 5. the lines still read correctly on a Retina pane ─────────────────────
m = load(pane(2, [{ fromRightCss: 270, heightCss: 120 }]));
const lines = m.readLines("#4CAF50", "#EF5350");
chk("lines still resolve at dpr 2 and green is above red",
    !!lines && lines.direction === "LONG",
    lines ? `fastY=${lines.fastY} slowY=${lines.slowY} gapFrac=${lines.gapFrac?.toFixed(4)}` : "null");
chk("gapFrac is a sane fraction of pane height",
    !!lines && lines.gapFrac > 0 && lines.gapFrac < 1,
    lines ? String(lines.gapFrac?.toFixed(4)) : "null");

console.log(`\n${pass}/${total} pass`);
process.exit(pass === total ? 0 : 1);
