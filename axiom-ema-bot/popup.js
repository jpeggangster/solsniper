const $ = (id) => document.getElementById(id);

async function activeTab() {
  const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
  return t;
}

/*
 * Which tab the panel should report on.
 *
 * The side panel stays open across tab switches, so "the active tab" is the
 * wrong target once the bot is running — look at another tab and the panel
 * would show that page's (non-existent) chart. While running, follow the tab
 * the bot is locked to; otherwise follow whatever is in front.
 */
async function botTab() {
  try {
    const st = await chrome.runtime.sendMessage({ type: "STATUS" });
    if (st?.running && st.tabId != null) {
      try { return await chrome.tabs.get(st.tabId); } catch (_) {}
    }
  } catch (_) {}
  return activeTab();
}

function cfg() {
  return {
    dryRun: $("dry").value === "1",
    emaIndex: parseInt($("idx").value, 10),
    source: "pixels",
    markerMinRun: parseInt($("mmp").value, 10) || 30,
    swapLines: $("swp").checked,
    debugOverlay: $("dbg").checked,
    fastColor: $("fc").value,
    slowColor: $("sc").value,
    hotkeyMode: $("mode").value,
    buyKey: $("bk").value.trim().toLowerCase(),
    sellKey: $("sk").value.trim().toLowerCase(),
    minEdgeBps: 0,      // removed: every marker-confirmed cross acts
    cooldownSec: 0,
    maxTrades: 0,       // unlimited
    barSec: parseInt($("bar").value, 10) || 1,
    requireMarker: $("trg").value === "1",
    crossColor: $("cc").value,
    // Entered as a percent of pane height; stored as the fraction the content
    // script compares against.
    bandFrac: (parseFloat($("bnd").value) || 0.8) / 100,
    freshWithinCss: parseInt($("frw").value, 10) || 300,
  };
}

async function refresh() {
  const st = await chrome.runtime.sendMessage({ type: "STATUS" });
  const tab = await botTab();

  $("st").textContent = st.running ? (st.cfg.dryRun ? "paper" : "LIVE") : "stopped";
  $("st").className = "pill " + (st.running ? (st.cfg.dryRun ? "paper" : "live") : "off");
  $("tr").textContent = st.cfg.maxTrades > 0 ? `${st.trades} / ${st.cfg.maxTrades}` : `${st.trades}`;
  const lastLog = (st.log || []).find((l) => l.kind === "buy" || l.kind === "sell");
  $("last").textContent = lastLog
    ? new Date(lastLog.t).toLocaleTimeString() + " " + (lastLog.msg.includes("UP") ? "LONG" : "FLAT")
    : "—";
  $("go").textContent = st.running ? "Stop bot" : "Start bot";
  $("go").className = st.running ? "stop" : "start";

  // The chart lives in an iframe whose URL has no address, so read the CA from
  // the TOP-LEVEL tab URL first, then the manual override, then the frames.
  const manual = ($("mca").value || "").trim();
  const fromTab = (tab.url || "").match(/0x[a-fA-F0-9]{40}/);
  let sample = null;
  try { sample = await chrome.tabs.sendMessage(tab.id, { type: "GET_STATE" }); } catch (_) {}
  // The legend usually lives in the chart iframe while the top frame answers
  // first, so poll every frame and keep whichever actually found it.
  if (!sample?.sample?.fast) {
    try {
      const res = await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true },
        func: () => (window.__emaBotSample || null),
      });
      const results = res.map((r) => r.result).filter(Boolean);
      const hit = results.find((r) => r && r.fast != null) || results.find((r) => r && r.lastEval);
      if (hit) sample = { ...(sample || {}), sample: hit, lastEval: hit.lastEval };
    } catch (_) {}
  }
  const ca = manual || (fromTab ? fromTab[0] : null) || sample?.ca || null;
  window.__ca = ca;
  const s = sample?.sample;
  $("ca").textContent = ca ? ca.slice(0, 10) + "…" + ca.slice(-4) : "not found";
  try {
    const front = await activeTab();
    $("ca").title = tab && front && tab.id !== front.id
      ? `watching a background tab: ${(tab.title || tab.url || "").slice(0, 70)}`
      : (tab?.url || "");
  } catch (_) {}
  $("ca").style.color = ca ? "#35d07f" : "#ff6b6b";
  if (s?.error) {
    $("ema").textContent = s.error;
    if (s.hint?.length) $("ema").title = "colours seen: " + s.hint.map((h) => h.rgb).join(" | ");
    $("dir").textContent = "—";
    $("dir").className = "pill off";
  } else if (s) {
    const up = s.fast > s.slow;
    $("ema").innerHTML = `<span class="${up ? "buy" : "sell"}">${up ? "12>21" : "12<21"}</span> ` +
      `<span class="muted">[${s.via}]</span>` +
      (s.via === "pixels"
        ? ` <span class="muted">${s.gapPx?.toFixed(0)}px apart (canvas)</span>`
        : ` ${Number(s.fast).toPrecision(5)} / ${Number(s.slow).toPrecision(5)}` +
          (s.spreadBps != null ? ` <span class="muted">${s.spreadBps.toFixed(0)}bps</span>` : ""));
    $("dir").textContent = (up ? "LONG" : "FLAT") + (s.marker ? "  ✦" : "");
    $("dir").className = "pill " + (up ? "on" : "off");
  }
  // Raw pixel positions, so a wrong colour assignment is visible rather than
  // inferred: on screen, SMALLER y means HIGHER on the chart.
  if (s && s.fastY != null) {
    const g = s.swapped ? s.slowY : s.fastY, r = s.swapped ? s.fastY : s.slowY;
    $("ly").innerHTML =
      `<span style="color:${s.colors?.fast}">12 y=${g}</span> ` +
      `<span style="color:${s.colors?.slow}">21 y=${r}</span> ` +
      `<span class="muted">${g < r ? "12 is higher" : "21 is higher"}</span>` +
      (s.swapped ? ' <span class="warn">swapped</span>' : "");
    $("ly").title =
      `12: traced ${s.fastTrace}/${s.columns} cols, candidates ${JSON.stringify(s.fastCandidates)}\n` +
      `21: traced ${s.slowTrace}/${s.columns} cols, candidates ${JSON.stringify(s.slowCandidates)}`;
  } else if (s) { $("ly").textContent = "—"; }

  // Shows whether the background service worker is alive — the thing that
  // silently dies and makes the bot stop acting.
  const age = sample?.workerAgeSec;
  if (age == null) { $("wk").textContent = "—"; $("wk").style.color = "#8b93a1"; }
  else if (age < 30) { $("wk").textContent = `alive (${age.toFixed(0)}s ago)`; $("wk").style.color = "#35d07f"; }
  else { $("wk").textContent = `stale (${age.toFixed(0)}s)`; $("wk").style.color = "#f0b429"; }

  const ev = sample?.lastEval || sample?.sample?.lastEval;
  $("why").textContent = ev ? ev.reason : "—";
  $("why").style.color = ev && /^TARGET ->/.test(ev.reason) ? "#35d07f"
                       : ev && /no EMA values|entry held|bot not running/.test(ev.reason) ? "#f0b429"
                       : "#8b93a1";
  const mfr = s?.markerFromRight ?? ev?.markerFromRight;
  $("mk").textContent = s && s.marker != null
    ? (s.marker ? "▮ column" : "none") +
      (s.markerRun ? ` (${s.markerRun}px tall` : "") +
      (s.marker && mfr != null ? `, ${mfr}px from edge)` : s.markerRun ? ")" : "")
    : "—";
  $("mk").style.color = s?.marker ? "#FFEB3B" : "#8b93a1";

  // The target is the hysteresis-filtered state the bot is steering towards —
  // not the raw line comparison, which flickers inside the dead zone.
  const tgt = sample?.target || s?.target || null;
  $("tgt").textContent = tgt
    ? tgt + (s?.gapFrac != null ? `  (${(s.gapFrac * 100).toFixed(2)}% apart)` : "")
    : "—";
  $("tgt").style.color = tgt === "LONG" ? "#35d07f" : tgt === "FLAT" ? "#8b93a1" : "#8b93a1";

  // Flag when the target and the held position disagree. Transient during a
  // fill; persistent means a key did not land.
  const want = tgt ? (tgt === "LONG" ? "long" : "flat") : null;
  const drift = want && want !== st.position;
  $("pos").textContent = st.position +
    (drift ? "  ⚠ should be " + want + (st.retries ? ` (${st.retries} tries)` : "") : "");
  $("pos").style.color = drift ? "#f0b429" : "#e6e6e6";

  $("log").innerHTML = (st.log || []).slice(0, 40).map((l) =>
    `<div class="l ${l.kind}">${new Date(l.t).toLocaleTimeString()} ${l.msg}</div>`).join("");
}

$("go").onclick = async () => {
  const st = await chrome.runtime.sendMessage({ type: "STATUS" });
  if (st.running) {
    await chrome.runtime.sendMessage({ type: "STOP" });
  } else {
    const tab = await activeTab();
    if (!/axiom\.trade/.test(tab.url || "")) {
      alert("Open the Axiom chart tab first.");
      return;
    }
    if (!window.__ca) {
      alert("No contract address detected.\n\nUse 'diagnose' to see what the page exposes, " +
            "or paste the CA into the override box.");
      return;
    }
    try { await chrome.tabs.sendMessage(tab.id, { type: "SET_CA", ca: window.__ca }); } catch (_) {}
    const r = await chrome.runtime.sendMessage({
      type: "START", tabId: tab.id, cfg: cfg(), ca: window.__ca });
    if (!r.ok) alert("Could not start: " + (r.error || "unknown"));
  }
  refresh();
};

$("dbg").addEventListener("change", async () => {
  const tab = await botTab();
  try {
    await chrome.tabs.sendMessage(tab.id,
      { type: "SET_CONFIG", config: { debugOverlay: $("dbg").checked } });
  } catch (_) {}
});

$("diag").onclick = async (e) => {
  e.preventDefault();
  const tab = await botTab();
  const lines = [`TAB URL: ${tab.url}`, ""];

  // Isolated world: what the DOM looks like.
  let dom = [];
  try {
    dom = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: () => {
        const txt = document.body ? document.body.innerText : "";
        return {
          url: location.href, top: window.top === window.self,
          canvases: document.querySelectorAll("canvas").length,
          hasEmaText: /\bEMA\b/i.test(txt),
          bodyTextLen: txt.length,
          emaSnippet: (txt.match(/.{0,40}EMA.{0,60}/i) || [""])[0].replace(/\n/g, " "),
        };
      },
    });
  } catch (err) { lines.push("DOM probe failed: " + err.message); }

  dom.forEach((f, i) => {
    const r = f.result || {};
    lines.push(`frame ${i}${r.top ? " (top)" : ""}  ${String(r.url).slice(0, 62)}`);
    lines.push(`   canvas:${r.canvases}  EMA-in-text:${r.hasEmaText}  textLen:${r.bodyTextLen}`);
    if (r.emaSnippet) lines.push(`   "${r.emaSnippet}"`);
  });

  // MAIN world: chart globals and the websocket feed.
  lines.push("", "PAGE WORLD:");
  try {
    const main = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true }, world: "MAIN",
      func: () => {
        const s = window.__emaBotWS;
        return {
          url: location.href,
          hooked: !!window.__emaBotWSHooked,
          probe: s && s.probe ? s.probe() : null,
          sockets: s ? s.sockets.map((x) => x.url).slice(0, 6) : [],
          counts: s ? s.counts : {},
          sample: s && s.samples.length ? s.samples[s.samples.length - 1].data.slice(0, 260) : null,
        };
      },
    });
    main.forEach((f, i) => {
      const r = f.result || {};
      if (!r.hooked && !r.sockets?.length) return;
      lines.push(`frame ${i}: hooked:${r.hooked} globals:${JSON.stringify(r.probe?.globals || [])}`);
      (r.sockets || []).forEach((u) => lines.push(`   ws ${String(u).slice(0, 70)}  msgs:${r.counts[u] || 0}`));
      if (r.sample) lines.push(`   sample: ${r.sample}`);
    });
  } catch (err) { lines.push("page probe failed: " + err.message); }

  // Verdict FIRST so a partial copy still carries the answer.
  const anyEmaText = dom.some((f) => f.result?.hasEmaText);
  const anyCanvas = dom.reduce((n, f) => n + (f.result?.canvases || 0), 0);
  const wsList = [];
  lines.forEach((l) => { const m = l.match(/ws (\S+)\s+msgs:(\d+)/); if (m) wsList.push(m[1] + " x" + m[2]); });
  const verdict = anyEmaText
    ? "DOM — EMA text found, scraping can work"
    : anyCanvas ? `CANVAS — no EMA text, ${anyCanvas} canvas element(s)`
                : "NEITHER — no EMA text, no canvas (is the chart open?)";
  const head = [
    "=== VERDICT: " + verdict + " ===",
    `frames:${dom.length}  canvases:${anyCanvas}  emaText:${anyEmaText}  websockets:${wsList.length}`,
    wsList.length ? "ws: " + wsList.slice(0, 4).join(" | ") : "ws: none captured",
    "",
  ];
  const out = head.concat(lines).join("\n");
  await navigator.clipboard.writeText(out);
  alert(out);
};

$("copy").onclick = async (e) => {
  e.preventDefault();
  const st = await chrome.runtime.sendMessage({ type: "STATUS" });
  const text = (st.log || []).slice().reverse()
    .map((l) => `${new Date(l.t).toISOString()} [${l.kind}] ${l.msg}`).join("\n");
  await navigator.clipboard.writeText(text);
  $("copy").textContent = "copied";
  setTimeout(() => ($("copy").textContent = "copy"), 1200);
};

$("clear").onclick = async (e) => {
  e.preventDefault();
  await chrome.runtime.sendMessage({ type: "CLEAR_LOG" });
  refresh();
};

$("pick").onclick = async (e) => {
  e.preventDefault();
  const tab = await botTab();
  // Ask every frame to run the BOT'S OWN reader, so this diagnostic and the
  // trading logic can never disagree.
  let reports = [];
  try {
    const frames = await chrome.webNavigation?.getAllFrames?.({ tabId: tab.id });
    const ids = frames ? frames.map((f) => f.frameId) : [0];
    for (const frameId of ids) {
      try {
        const r = await chrome.tabs.sendMessage(tab.id,
          { type: "PIXEL_REPORT", fastColor: $("fc").value, slowColor: $("sc").value },
          { frameId });
        if (r) reports.push({ frameId, ...r });
      } catch (_) {}
    }
  } catch (_) {}
  if (!reports.length) {
    // Nothing answered — inject on demand and retry. This is the blob: iframe
    // case: manifest patterns do not match blob URLs.
    try {
      await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true }, files: ["pixels.js", "content.js"] });
      await new Promise((r) => setTimeout(r, 400));
      const frames = await chrome.webNavigation?.getAllFrames?.({ tabId: tab.id });
      for (const f of (frames || [{ frameId: 0 }])) {
        try {
          const r = await chrome.tabs.sendMessage(tab.id,
            { type: "PIXEL_REPORT", fastColor: $("fc").value, slowColor: $("sc").value },
            { frameId: f.frameId });
          if (r) reports.push({ frameId: f.frameId, ...r });
        } catch (_) {}
      }
    } catch (_) {}
  }
  if (!reports.length) { alert("No frame responded — reload the extension AND the tab."); return; }

  const withLines = reports.find((r) => r.lines);
  const withPane = reports.find((r) => (r.panes || []).length);
  const verdict = withLines
    ? `WORKING — ${withLines.lines.direction} (${withLines.lines.gapPx}px apart)`
    : withPane
      ? "PANE FOUND but the two line colours were not — set the swatches from the list"
      : "NO CHART PANE — no canvas ≥400x200 in any frame";

  const out = [`=== ${verdict} ===`, ""];
  for (const r of reports) {
    if (!(r.panes || []).length && !(r.colors || []).length) continue;
    out.push(`frame ${r.frameId}${r.top ? " (top)" : ""} panes:[${(r.panes||[]).join(", ")}]  ${r.url}`);
    if (r.auto) out.push(`   auto-detected colours: fast ${r.auto.fastColor}  slow ${r.auto.slowColor}`);
    if (r.lines) out.push(`   lines: fastY=${r.lines.fastY} slowY=${r.lines.slowY} ` +
                          `gap=${r.lines.gapPx}px newestX=${r.lines.rightX} -> ${r.lines.direction}`);
    if (r.marker) out.push(`   cross marker: ${r.marker.found ? "YES (" + r.marker.pixels + "px)" : "no"}`);
    (r.colors || []).slice(0, 6).forEach((c) =>
      out.push(`   rgb(${c.rgb})  x${c.n}  perCol ${c.perCol}  [${c.canvas}]`));
  }
  const text = out.join("\n");
  await navigator.clipboard.writeText(text);
  alert(text);
};

for (const id of ["dry","idx","mode","bk","sk","src","fc","sc","trg","cc","bar","mmp","swp","bnd","frw"]) {
  $(id).addEventListener("change", () => chrome.storage.local.set({ cfg: cfg() }));
}
chrome.storage.local.get("cfg").then(({ cfg: c }) => {
  if (!c) return;
  $("dry").value = c.dryRun ? "1" : "0";
  $("idx").value = String(c.emaIndex);
  $("mode").value = c.hotkeyMode;
  $("bk").value = c.buyKey; $("sk").value = c.sellKey;

  if (c.source) $("src").value = c.source;
  if (c.fastColor) $("fc").value = c.fastColor;
  if (c.slowColor) $("sc").value = c.slowColor;
  if (c.crossColor) $("cc").value = c.crossColor;
  if (c.markerMinRun) $("mmp").value = c.markerMinRun;
  if (c.swapLines != null) $("swp").checked = !!c.swapLines;
  if (c.debugOverlay != null) $("dbg").checked = !!c.debugOverlay;
  if (c.barSec) $("bar").value = String(c.barSec);
  if (c.requireMarker != null) $("trg").value = c.requireMarker ? "1" : "0";
  if (c.bandFrac != null) $("bnd").value = String(+(c.bandFrac * 100).toFixed(3));
  if (c.freshWithinCss != null) $("frw").value = String(c.freshWithinCss);
});

refresh();
setInterval(refresh, 1000);
