/*
 * Reads the EMA values straight off the chart legend and reports closed-bar
 * crosses to the background worker.
 *
 * Why scrape the legend rather than recompute from chain data: the numbers on
 * the chart are the ones the trader is looking at. Recomputing independently
 * means warmup, bar alignment and venue differences all have to match exactly,
 * and in practice they do not — a parallel implementation disagreed with this
 * chart's EMA21 by a third on a fast move. Reading the legend makes the bot
 * trade what you see, by construction.
 */

// Guard against double-injection: the manifest injects this, and the
// background re-injects on start as a fallback for frames the manifest misses.
if (window.__emaBotLoaded) { /* already running in this frame */ }
else {
window.__emaBotLoaded = true;

const STATE = {
  running: false,
  barSec: 5,
  emaIndex: 1,          // which legend number is the FAST (12) EMA
  // Pixels by default: this page carries several EMA studies and the legend
  // text cannot be attributed to the right one reliably.
  source: "pixels",     // "auto" | "legend" | "pixels"
  maxLegendSpread: 0.25,// reject legend pairs more than 25% apart
  fastColor: "#4CAF50", // green = EMA12 on this chart
  slowColor: "#EF5350", // red   = EMA21
  crossColor: "#FFEB3B",// yellow marker the indicator paints at a cross
  crossTol: 40,         // tight: Axiom's orange trade icons are close to yellow
  markerMinRun: 30,     // Crosses are plotted as COLUMNS: require a tall run
  minGapPx: 4,          // lines must be clearly apart, not touching
  requireMarker: true,  // gate ENTRIES on a fresh cross column (exits are never gated)
  freshWithinCss: 300,  // a column this many CSS px from the right edge still counts as recent
  bandFrac: 0.008,      // hysteresis: lines must be this far apart (fraction of pane height)
  target: "FLAT",       // the state the EMAs say we should be in, right now
  lastSentAt: 0,        // when the target was last reported to the background
  resendSec: 5,         // re-assert the target this often, so drift self-corrects
  lastFired: null,      // direction of the last signal sent, for de-duplication
  pendingCross: null,   // { seenAt, run } — a column seen but not yet acted on
  crossTtlSec: 45,      // how long a seen column stays actionable
  autoColor: true,      // re-derive the line colours if the configured ones miss
  swapLines: false,     // manual override if green/red get assigned the wrong way
  debugOverlay: false,  // draw the detected line positions onto the chart
  colorTol: 48,
  minEdgeBps: 0,
  lastBarStart: 0,
  lastClose: null,      // { fast, slow } at the previous bar close
  direction: null,      // "LONG" once 12 is above 21, "FLAT" once it is below
  ca: null,
  lastSample: null,
};

/* ---------- contract address of the open chart ----------
 * The chart itself is usually a TradingView iframe whose URL has no address in
 * it, so this reports candidates and lets the popup decide; the popup also
 * reads the TOP-LEVEL tab URL, which is the reliable source.
 */
const ADDR_RE = /0x[a-fA-F0-9]{40}/g;
const IGNORE = new Set([
  "0x0000000000000000000000000000000000000000",
  "0x000000000000000000000000000000000000dead",
  "0x0bd7d308f8e1639fab988df18a8011f41eacad73", // WETH
  "0x5fc5360d0400a0fd4f2af552add042d716f1d168", // USDG
  "0x000000000022d473030f116ddee9f6b43ac78ba3", // Permit2
  "0x8366a39cc670b4001a1121b8f6a443a643e40951", // V4 PoolManager
]);

function uniqAddrs(text) {
  const out = [];
  for (const m of String(text || "").matchAll(ADDR_RE)) {
    const a = m[0];
    if (IGNORE.has(a.toLowerCase())) continue;
    if (!out.includes(a)) out.push(a);
  }
  return out;
}

function detectCA() {
  // Axiom uses /meme/0x… and /token/0x…?chain=robinhood; take the path address
  // rather than any address that happens to be in the query string.
  const path = location.pathname.match(/\/(?:meme|token|t)\/(0x[a-fA-F0-9]{40})/);
  if (path) return path[1];
  const fromUrl = uniqAddrs(location.href);
  if (fromUrl.length) return fromUrl[0];
  // Links and copy-to-clipboard controls usually carry the real token address.
  const attrs = [];
  document.querySelectorAll("a[href],[data-address],[data-token],[title],[aria-label]")
    .forEach((el) => {
      attrs.push(el.getAttribute("href") || "", el.getAttribute("data-address") || "",
                 el.getAttribute("data-token") || "", el.getAttribute("title") || "",
                 el.getAttribute("aria-label") || "");
    });
  const fromAttrs = uniqAddrs(attrs.join(" "));
  if (fromAttrs.length) return fromAttrs[0];
  const fromText = uniqAddrs(document.body ? document.body.innerText : "");
  return fromText.length ? fromText[0] : null;
}

function diagnostics() {
  const attrs = [];
  document.querySelectorAll("a[href],[data-address],[data-token]").forEach((el) =>
    attrs.push(el.getAttribute("href") || "", el.getAttribute("data-address") || "",
               el.getAttribute("data-token") || ""));
  return {
    frameUrl: location.href,
    isTop: window.top === window.self,
    urlAddrs: uniqAddrs(location.href),
    attrAddrs: uniqAddrs(attrs.join(" ")).slice(0, 5),
    textAddrs: uniqAddrs(document.body ? document.body.innerText : "").slice(0, 5),
    legendFound: !!readLegend(),
  };
}

/* ---------- EMA values from the chart legend ---------- */
function parseNum(txt) {
  if (!txt) return null;
  const m = txt.trim().replace(/,/g, "").match(/^(-?\d*\.?\d+)\s*([KMB])?$/i);
  if (!m) return null;
  const mult = { K: 1e3, M: 1e6, B: 1e9 }[(m[2] || "").toUpperCase()] || 1;
  return parseFloat(m[1]) * mult;
}

function harvestNumbers(node, titleNums) {
  // Collect numbers from leaf elements only, so a container's concatenated text
  // does not produce phantom values.
  const out = [];
  const walk = document.createTreeWalker(node, NodeFilter.SHOW_ELEMENT);
  const seen = [];
  let el = node;
  do {
    if (el.children.length === 0) seen.push(el);
  } while ((el = walk.nextNode()));
  for (const leaf of seen) {
    const n = parseNum(leaf.textContent);
    if (n === null || !Number.isFinite(n)) continue;
    if (titleNums.includes(n)) continue;           // "(21, 12)" from the study title
    out.push(n);
  }
  return out;
}

function readLegend() {
  // TradingView's class names are hashed and Axiom may wrap them, so this looks
  // for any small element mentioning EMA and harvests the numbers around it,
  // climbing a few levels if the values sit in a sibling container.
  if (!document.body) return null;
  const candidates = [];
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
  let el;
  while ((el = walk.nextNode())) {
    const txt = (el.textContent || "").trim();
    if (!txt || txt.length > 240) continue;
    if (!/\bEMA\b/i.test(txt)) continue;
    candidates.push(el);
  }
  if (!candidates.length) return null;
  // Smallest first: the tightest element containing "EMA" is the legend row.
  candidates.sort((a, b) => (a.textContent || "").length - (b.textContent || "").length);

  for (const cand of candidates.slice(0, 12)) {
    const title = (cand.textContent || "").split("\n")[0].slice(0, 80);
    const titleNums = (title.match(/\d+(?:\.\d+)?/g) || []).map(Number);
    let node = cand;
    for (let depth = 0; node && depth < 5; node = node.parentElement, depth++) {
      const vals = harvestNumbers(node, titleNums);
      if (vals.length >= 2) return { title, values: vals.slice(0, 2), depth };
    }
  }
  return null;
}

/* ---------- cross detection on closed bars ---------- */
function tick() {
  const legend = readLegend();
  const ca = detectCA();
  STATE.ca = ca;

  let fast = null, slow = null, via = "pixels";

  // Legend text is no longer used at all. On this page it produced
  // "994.91 vs 20.000", "83.85 vs 20.000" and "16.430 vs 20.000" — numbers from
  // the chart's OTHER EMA studies — and each one fabricated a trade. Mixing it
  // with pixel readings also made consecutive signals disagree.
  if (false) {
    const a = legend.values[STATE.emaIndex];
    const b = legend.values[1 - STATE.emaIndex];
    // EMA12 and EMA21 of the SAME series are always close together — a few
    // percent at most. Anything wider means the parser latched onto numbers
    // from a different study (this chart also carries "EMA 100" and
    // "EMA 50 close 0"), which once produced "994.91 vs 20.000, 487455 bps"
    // and a completely fabricated LONG. Reject and fall through to pixels.
    const spread = Math.abs(a - b) / Math.max(Math.abs(b), 1e-30);
    if (a > 0 && b > 0 && spread <= STATE.maxLegendSpread) {
      fast = a; slow = b;
    } else {
      STATE.legendRejected = { a, b, spreadPct: +(spread * 100).toFixed(1) };
    }
  }

  if (fast == null || slow == null) {
    // No legend text — read the lines off the canvas instead. Positions are
    // pixels, not prices, so they answer "which is on top" but not "by how
    // much"; the edge filter therefore uses pixel gap rather than bps.
    let px = typeof readLines === "function"
      ? readLines(STATE.fastColor, STATE.slowColor, STATE.colorTol) : null;
    if (!px && STATE.autoColor && typeof autoColors === "function") {
      const auto = autoColors();
      if (auto) {
        STATE.fastColor = auto.fastColor;
        STATE.slowColor = auto.slowColor;
        px = readLines(auto.fastColor, auto.slowColor, STATE.colorTol);
      }
    }
    if (px) {
      via = "pixels";
      // Screen y grows downward, so negate to make "bigger = higher", matching
      // how prices behave.
      fast = -(STATE.swapLines ? px.slowY : px.fastY);
      slow = -(STATE.swapLines ? px.fastY : px.slowY);
      STATE.lastPx = px;
    }
  }

  if (fast == null || slow == null) {
    STATE.lastSample = {
      error: legend ? "EMA values unreadable" : "no legend text and no EMA lines found",
      hint: typeof sampleColors === "function" ? sampleColors().slice(0, 4) : null,
    };
    window.__emaBotSample = STATE.lastSample;
    return;
  }
  // Direction is a TARGET STATE, not a one-shot signal: 12 above 21 means the
  // bot should be holding, below means it should be flat. A missed or failed
  // order therefore stays wrong until it is corrected, rather than being lost.
  const live = fast > slow ? "LONG" : "FLAT";

  // Computed once per tick and reused below. The marker scan is the most
  // expensive read on the canvas and it was previously run twice per tick.
  const mark = typeof crossMarker === "function"
    ? crossMarker(STATE.crossColor, STATE.crossTol, STATE.markerMinRun)
    : { found: false, run: 0, runCss: 0, pixels: 0, fromRightCss: null };

  STATE.lastSample = {
    fast, slow, ca, via,
    title: legend ? legend.title : "canvas lines",
    direction: live,
    spreadBps: via === "legend"
      ? Math.abs(fast - slow) / Math.max(Math.abs(slow), 1e-30) * 10000
      : null,
    gapPx: via === "pixels" ? Math.abs(fast - slow) : null,
    colors: via === "pixels" ? { fast: STATE.fastColor, slow: STATE.slowColor } : null,
    fastY: STATE.lastPx ? STATE.lastPx.fastY : null,
    slowY: STATE.lastPx ? STATE.lastPx.slowY : null,
    fastTrace: STATE.lastPx ? STATE.lastPx.fastTrace : null,
    slowTrace: STATE.lastPx ? STATE.lastPx.slowTrace : null,
    fastCandidates: STATE.lastPx ? STATE.lastPx.fastCandidates : null,
    slowCandidates: STATE.lastPx ? STATE.lastPx.slowCandidates : null,
    columns: STATE.lastPx ? STATE.lastPx.columns : null,
    swapped: STATE.swapLines,
    legendRejected: STATE.legendRejected || null,
    marker: mark.found,
    markerRun: mark.runCss,
    markerFromRight: mark.fromRightCss,
    paneH: STATE.lastPx ? STATE.lastPx.paneH : null,
    gapFrac: STATE.lastPx ? STATE.lastPx.gapFrac : null,
    dpr: STATE.lastPx ? STATE.lastPx.dpr : null,
    target: STATE.target,
  };
  window.__emaBotSample = STATE.lastSample;   // readable by the popup across frames

  if (typeof debugOverlay === "function") {
    try {
      debugOverlay(STATE.debugOverlay,
                   STATE.lastPx ? STATE.lastPx.fastY : null,
                   STATE.lastPx ? STATE.lastPx.slowY : null,
                   mark.found ? mark.x : null);
    } catch (_) {}
  }

  const why = (reason, extra) => {
    STATE.lastEval = { t: Date.now(), reason, ...(extra || {}) };
    window.__emaBotSample = { ...(STATE.lastSample || {}), lastEval: STATE.lastEval };
  };

  if (!STATE.running) { why("bot not running"); return; }

  // Align to wall-clock bars so a "close" means the same thing as on the chart.
  const now = Date.now();
  const barStart = Math.floor(now / (STATE.barSec * 1000)) * STATE.barSec * 1000;
  const newBar = barStart !== STATE.lastBarStart;
  STATE.lastBarStart = barStart;

  /* ---------- target state: LEVEL-triggered, not edge-triggered ----------
   *
   * What should I be holding is answered by where the two lines are RIGHT NOW,
   * not by whether we happened to be looking at the instant they crossed. A
   * marker is one bar wide and then gone; "12 above 21" stays true for as long
   * as it is true. Deriving the target from the level means a missed tick costs
   * nothing — a throttled background tab, a re-rendered iframe, a restarted
   * worker — because the next tick re-derives the same answer and the
   * background reconciles the position to it.
   *
   * Hysteresis: a bare `fast > slow` flips on every touch while the lines are
   * tangled, which on 1s bars is most of the time. The target changes only once
   * the lines are clearly apart and HOLDS inside the dead zone. The band is a
   * fraction of pane height, not raw pixels, because a pixel gap means
   * different things at different zoom levels and after every autoscale.
   */
  const paneH = (STATE.lastPx && STATE.lastPx.paneH) || null;
  const gapSigned = fast - slow;                  // > 0 => 12 is above 21
  const gapFrac = paneH ? gapSigned / paneH : null;
  const band = STATE.bandFrac;

  let target = STATE.target;                      // inside the dead zone: hold
  if (gapFrac != null) {
    if (gapFrac > band) target = "LONG";
    else if (gapFrac < -band) target = "FLAT";
  }

  /* The cross column is a FRESHNESS GATE on entries, never the state itself.
   * "12 > 21" says nothing about whether the cross happened three seconds or
   * forty minutes ago, and entering a move that has already run is how you buy
   * the top. A column near the right edge says the cross is recent.
   *
   * Exits are never gated. Refusing to leave a position because a marker was
   * not found is the one failure mode here that can actually cost money.
   */
  const fresh = mark.found && mark.fromRightCss != null &&
                mark.fromRightCss <= STATE.freshWithinCss;
  if (fresh) {
    STATE.pendingCross = { seenAt: now, run: mark.runCss, fromRight: mark.fromRightCss };
  }
  const pending = STATE.pendingCross;
  const pendingAgeSec = pending ? (now - pending.seenAt) / 1000 : null;
  if (pending && pendingAgeSec > STATE.crossTtlSec) STATE.pendingCross = null;

  // Recorded rather than reported immediately: the branches below also call
  // why(), and a later one would overwrite this with a blander message. The
  // panel's "why" line is the thing you debug from, so the most specific
  // explanation has to be the one that survives.
  let entryHeld = null;
  if (target === "LONG" && STATE.target !== "LONG" &&
      STATE.requireMarker && !STATE.pendingCross) {
    entryHeld = "entry held: no fresh cross column (newest " +
      (mark.fromRightCss == null ? "none" : mark.fromRightCss + "px") +
      " from edge, need <=" + STATE.freshWithinCss + "px)";
    target = STATE.target;
  }

  const changed = target !== STATE.target;
  if (changed) {
    STATE.target = target;
    STATE.direction = target;
    STATE.lastFired = target;
    STATE.pendingCross = null;                    // consumed by this entry
  }

  // Re-assert the target periodically as well as on change, so a position that
  // drifted out of sync — a key that did not land, a manual trade, a worker
  // restart — is corrected on the next bar instead of staying wrong until the
  // next cross.
  const dueResend = now - STATE.lastSentAt > STATE.resendSec * 1000;
  const detail = { direction: target, marker: mark.found, markerRun: mark.runCss,
                   markerFromRight: mark.fromRightCss, armedAgoSec: pendingAgeSec,
                   entryHeld: !!entryHeld,
                   gapPct: gapFrac == null ? null : +(gapFrac * 100).toFixed(2) };

  if (!changed && !(newBar && dueResend)) {
    why(entryHeld ||
        ("holding " + target +
          (gapFrac == null ? "" :
            " (gap " + (gapFrac * 100).toFixed(2) + "% of pane, band +/-" +
            (band * 100).toFixed(2) + "%)")),
        detail);
    return;
  }

  STATE.lastSentAt = now;
  // A held entry still re-asserts the current target — a position that drifted
  // needs correcting whether or not we are also being kept out of a new one —
  // but the reason shown stays the specific one.
  why(entryHeld || ((changed ? "TARGET -> " : "re-asserting ") + target), detail);

  chrome.runtime.sendMessage({
    type: "TARGET",
    target, changed,
    direction: target,
    cross: target === "LONG" ? "UP" : "DOWN",
    ca, fast, slow, via,
    gapPx: gapSigned,
    gapPct: gapFrac == null ? null : +(gapFrac * 100).toFixed(2),
    marker: mark.found,
    markerRun: mark.runCss,
    markerFromRight: mark.fromRightCss,
    armedAgoSec: pendingAgeSec,
  });
}

/* Exposed for the dev harness and for inspecting a live tab: the content
 * script runs in the isolated world, so these are reachable from
 * chrome.scripting.executeScript (which shares that world) but are invisible to
 * the page itself. */
window.__emaBotTick = tick;
window.__emaBotSTATE = STATE;

setInterval(tick, 250);

/*
 * Keepalive. An open port resets the service worker's idle timer, so the
 * background stays alive for as long as the chart tab is open. Without this the
 * worker is killed ~30s after the popup closes and every signal is dropped.
 */
let keepPort = null;
let workerAliveAt = 0;

function connectKeepAlive() {
  try {
    keepPort = chrome.runtime.connect({ name: "emaBotKeepAlive" });
    keepPort.onMessage.addListener((m) => {
      if (!m || !m.alive) return;
      workerAliveAt = Date.now();
      window.__emaBotWorker = { at: workerAliveAt, running: m.running };

      /* Adopt the worker's view of whether the bot is running.
       *
       * This is the bug that made the panel read "bot not running" while the
       * log said STARTED. `running` used to arrive only as a one-shot
       * SET_RUNNING at start. Any frame that appeared afterwards — and Axiom
       * rebuilds the chart's blob: iframe routinely — began life with
       * running:false and was never told otherwise, so it ticked forever
       * without evaluating anything. The worker was already sending `running`
       * down this port every 15s; it was being displayed and thrown away.
       *
       * Pulling it means the content script cannot drift out of sync with the
       * worker regardless of injection order, worker restarts or reloads.
       */
      if (typeof m.running === "boolean" && m.running !== STATE.running) {
        STATE.running = m.running;
        if (m.running) {
          STATE.lastBarStart = 0;
          STATE.lastSentAt = 0;
          STATE.pendingCross = null;
          STATE.target = (m.position === "long") ? "LONG" : "FLAT";
        }
      }
      if (m.cfg) Object.assign(STATE, m.cfg);       // late frames get config too
      if (m.position) STATE.heldPosition = m.position;
    });
    keepPort.onDisconnect.addListener(() => {
      keepPort = null;
      setTimeout(connectKeepAlive, 1000);   // extension reloaded or worker cycled
    });
  } catch (_) {
    keepPort = null;
    setTimeout(connectKeepAlive, 2000);
  }
}
connectKeepAlive();

setInterval(() => {
  if (!keepPort) { connectKeepAlive(); return; }
  try { keepPort.postMessage({ ping: Date.now() }); }
  catch (_) { keepPort = null; connectKeepAlive(); }
}, 15000);

chrome.runtime.onMessage.addListener((msg, _s, reply) => {
  if (msg.type === "GET_STATE") {
    reply({ ca: STATE.ca, sample: STATE.lastSample, running: STATE.running,
            target: STATE.target, heldPosition: STATE.heldPosition || null,
            lastEval: STATE.lastEval || null,
            workerAliveAt, workerAgeSec: workerAliveAt ? (Date.now() - workerAliveAt) / 1000 : null,
            isTop: window.top === window.self, hasLegend: !!STATE.lastSample?.fast });
  } else if (msg.type === "DIAG") {
    reply(diagnostics());
  } else if (msg.type === "PIXEL_REPORT") {
    // Same code the bot uses, so the diagnostic cannot disagree with it.
    const cans = typeof chartCanvases === "function" ? chartCanvases() : [];
    reply({
      url: location.href.slice(0, 70),
      top: window.top === window.self,
      panes: cans.map((c) => `${c.width}x${c.height}`),
      colors: typeof sampleColors === "function" ? sampleColors() : [],
      lines: typeof readLines === "function"
        ? readLines(msg.fastColor || STATE.fastColor, msg.slowColor || STATE.slowColor,
                    msg.tol || STATE.colorTol) : null,
      auto: typeof autoColors === "function" ? autoColors() : null,
      marker: typeof crossMarker === "function" ? crossMarker(STATE.crossColor) : null,
    });
  } else if (msg.type === "SAMPLE_COLORS") {
    reply({ colors: typeof sampleColors === "function" ? sampleColors() : [],
            lines: typeof readLines === "function"
              ? readLines(msg.fastColor || STATE.fastColor, msg.slowColor || STATE.slowColor,
                          msg.tol || STATE.colorTol) : null });
  } else if (msg.type === "SET_CA") {
    STATE.ca = msg.ca;                       // manual override from the popup
    reply({ ok: true });
  } else if (msg.type === "SET_RUNNING") {
    STATE.running = msg.running;
    STATE.lastClose = null;                       // re-arm on every start
    STATE.lastBarStart = 0;
    STATE.lastFired = null;
    STATE.pendingCross = null;
    STATE.lastSentAt = 0;
    STATE.target = (msg.position === "long") ? "LONG" : "FLAT";
    reply({ ok: true });
  } else if (msg.type === "SET_CONFIG") {
    Object.assign(STATE, msg.config);
    reply({ ok: true });
  }
  return true;
});

} // end double-injection guard
