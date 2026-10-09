/*
 * Owns the trading state and dispatches Axiom's hotkeys.
 *
 * Keys go through chrome.debugger / Input.dispatchKeyEvent rather than
 * dispatchEvent(new KeyboardEvent(...)). Synthetic events carry
 * isTrusted === false and most trading UIs ignore them; CDP input is
 * indistinguishable from real typing. The cost is the "… is debugging this
 * browser" banner while the bot is armed, which doubles as an honest
 * indicator that something can trade on your behalf.
 */

const KEYS = {
  space: { key: " ", code: "Space", vk: 32, text: " " },
  w:     { key: "w", code: "KeyW",  vk: 87, text: "w" },
  f:     { key: "f", code: "KeyF",  vk: 70, text: "f" },
};

const DEFAULTS = {
  buyKey: "w",
  sellKey: "f",
  hotkeyMode: "chord",      // "chord" = hold space and press the key
  barSec: 1,
  emaIndex: 1,
  source: "pixels",
  markerMinRun: 30,          // CSS px of vertical run, scaled for the display
  bandFrac: 0.008,           // hysteresis dead zone, fraction of pane height
  freshWithinCss: 300,       // how near the right edge a cross column must be
  resendSec: 5,              // target re-assertion interval
  maxRetries: 3,             // attempts to correct a position before giving up
  swapLines: false,
  minEdgeBps: 0,
  cooldownSec: 0,           // removed by request — every cross acts
  maxTrades: 0,             // 0 = unlimited
  dryRun: true,
  requireMarker: true,
  crossColor: "#FFEB3B",
};

let S = {
  running: false,
  tabId: null,
  attached: false,
  position: "flat",         // what we believe we hold
  direction: null,          // what the EMAs say we SHOULD hold
  trades: 0,
  lastTradeAt: 0,
  retries: 0,               // consecutive failed attempts to reach the target
  lockedCA: null,
  log: [],
  cfg: { ...DEFAULTS },
};

// MV3 kills idle service workers, taking S.log with them. Restore on wake so
// the popup does not appear to lose history Chrome merely unloaded.
/*
 * MV3 terminates an idle service worker after ~30s, taking S with it. The bot
 * then silently reverts to "not running" and drops every signal while the
 * content script keeps ticking — the chart looks live, the log goes quiet.
 * State is therefore mirrored to storage and restored on wake, and the content
 * script holds a port open as a keepalive.
 */
async function saveState() {
  try {
    await chrome.storage.local.set({
      botState: {
        running: S.running, tabId: S.tabId, lockedCA: S.lockedCA,
        position: S.position, direction: S.direction, trades: S.trades,
        lastTradeAt: S.lastTradeAt, cfg: S.cfg,
      },
    });
  } catch (_) {}
}

let restored = null;
async function restoreState() {
  if (restored) return restored;
  restored = (async () => {
    try {
      const { botState, log } = await chrome.storage.local.get(["botState", "log"]);
      if (Array.isArray(log) && !S.log.length) S.log = log;
      if (!botState) return;
      Object.assign(S, {
        running: botState.running, tabId: botState.tabId,
        lockedCA: botState.lockedCA, position: botState.position,
        direction: botState.direction, trades: botState.trades,
        lastTradeAt: botState.lastTradeAt || 0,
        cfg: { ...DEFAULTS, ...(botState.cfg || {}) },
      });
      if (S.running && S.tabId != null) {
        // The debugger attachment does not survive the worker, so re-attach.
        const ok = await attach(S.tabId);
        if (!ok) {
          note("could not re-attach after worker restart — stopping", "error");
          S.running = false;
          await saveState();
        } else {
          note("service worker restarted — state restored, still running");
          // Re-push rather than waiting up to 15s for a keepalive ping, and so
          // frames created while the worker was dead are configured at once.
          try {
            await chrome.tabs.sendMessage(S.tabId, { type: "SET_CONFIG", config: contentCfg() });
            await chrome.tabs.sendMessage(S.tabId, {
              type: "SET_RUNNING", running: true, position: S.position });
          } catch (_) { /* the port will sync them */ }
        }
      }
    } catch (e) {
      note(`state restore failed: ${e.message}`, "warn");
    }
  })();
  return restored;
}

// Single definition of what the content script needs, so the start path and the
// keepalive path cannot drift apart.
function contentCfg() {
  return {
    barSec: S.cfg.barSec, emaIndex: S.cfg.emaIndex, minEdgeBps: S.cfg.minEdgeBps,
    source: S.cfg.source, fastColor: S.cfg.fastColor, slowColor: S.cfg.slowColor,
    crossColor: S.cfg.crossColor, requireMarker: S.cfg.requireMarker,
    markerMinRun: S.cfg.markerMinRun, swapLines: S.cfg.swapLines,
    bandFrac: S.cfg.bandFrac, freshWithinCss: S.cfg.freshWithinCss,
    resendSec: S.cfg.resendSec,
  };
}

function note(msg, kind = "info") {
  S.log.unshift({ t: Date.now(), kind, msg });
  S.log = S.log.slice(0, 200);
  chrome.storage.local.set({ log: S.log });
}

async function attach(tabId) {
  if (S.attached && S.tabId === tabId) return true;
  try {
    await chrome.debugger.attach({ tabId }, "1.3");
    S.attached = true;
    S.tabId = tabId;
    note(`debugger attached to tab ${tabId}`);
    return true;
  } catch (e) {
    note(`attach failed: ${e.message}`, "error");
    return false;
  }
}

async function detach() {
  if (!S.attached || S.tabId == null) return;
  try { await chrome.debugger.detach({ tabId: S.tabId }); } catch (_) {}
  S.attached = false;
  note("debugger detached");
}

function send(tabId, type, k, modifiers = 0) {
  return chrome.debugger.sendCommand({ tabId }, "Input.dispatchKeyEvent", {
    type,
    key: k.key,
    code: k.code,
    windowsVirtualKeyCode: k.vk,
    nativeVirtualKeyCode: k.vk,
    text: type === "keyDown" ? k.text : undefined,
    modifiers,
  });
}

async function fireHotkey(which) {
  const k = KEYS[which];
  if (!k) throw new Error(`unknown key ${which}`);
  const tabId = S.tabId;
  if (S.cfg.hotkeyMode === "chord") {
    // hold space, tap the key, release space
    await send(tabId, "keyDown", KEYS.space);
    await new Promise((r) => setTimeout(r, 30));
    await send(tabId, "keyDown", k);
    await new Promise((r) => setTimeout(r, 30));
    await send(tabId, "keyUp", k);
    await send(tabId, "keyUp", KEYS.space);
  } else {
    // press and release space, then the key
    await send(tabId, "keyDown", KEYS.space);
    await send(tabId, "keyUp", KEYS.space);
    await new Promise((r) => setTimeout(r, 60));
    await send(tabId, "keyDown", k);
    await send(tabId, "keyUp", k);
  }
}

async function act(side, info) {
  const now = Date.now();

  if (!S.running) return;
  if (S.lockedCA && info.ca && info.ca.toLowerCase() !== S.lockedCA.toLowerCase()) {
    note(`ignored ${side}: chart changed to ${info.ca?.slice(0, 10)}…`, "warn");
    return;
  }
  if (side === "BUY" && S.position !== "flat") return;
  if (side === "SELL" && S.position !== "long") return;
  if (S.cfg.cooldownSec > 0 && now - S.lastTradeAt < S.cfg.cooldownSec * 1000) {
    note(`ignored ${side}: cooldown`, "warn");
    return;
  }
  if (S.cfg.maxTrades > 0 && S.trades >= S.cfg.maxTrades) {
    note(`stopped: hit max ${S.cfg.maxTrades} trades`, "warn");
    await stop();
    return;
  }

  const arrow = info.cross === "UP" ? "↑" : "↓";
  const mark = info.markerRun
    ? ` ▮column(${info.markerRun}px tall` +
      (info.markerFromRight != null ? `, ${info.markerFromRight}px from edge` : "") +
      (info.armedAgoSec != null ? `, armed ${info.armedAgoSec.toFixed(0)}s ago` : "") + ")"
    : " (no column)";
  const sep = info.gapPct != null
    ? `${Math.abs(info.gapPx).toFixed(0)}px apart, ${Math.abs(info.gapPct).toFixed(2)}% of pane`
    : info.via === "pixels"
      ? `${Math.abs(info.fast - info.slow).toFixed(0)}px apart`
      : `${info.edgeBps?.toFixed(0)} bps`;
  const label = `${arrow} ${info.changed === false ? "RE-ASSERT" : "TARGET"} ` +
                `${info.direction}${mark} | green vs red on canvas (${sep})`;

  if (S.cfg.dryRun) {
    note(`DRY RUN — ${label}`, info.cross === "UP" ? "buy" : "sell");
    S.position = side === "BUY" ? "long" : "flat";
    S.direction = info.direction;
    S.trades += 1;
    S.lastTradeAt = now;
    await saveState();
    return;
  }

  try {
    await fireHotkey(side === "BUY" ? S.cfg.buyKey : S.cfg.sellKey);
    S.position = side === "BUY" ? "long" : "flat";
    S.direction = info.direction;
    S.trades += 1;
    S.lastTradeAt = now;
    note(`${label} — ${side === "BUY" ? S.cfg.buyKey : S.cfg.sellKey} key sent`,
         side === "BUY" ? "buy" : "sell");
    await saveState();
  } catch (e) {
    note(`hotkey failed: ${e.message}`, "error");
  }
}

/*
 * Drive the held position towards the target the content script reports.
 *
 * This replaces one-shot BUY/SELL signals. The content script says what state
 * the chart implies; this decides whether a key is needed to get there. Because
 * the target is re-asserted on a heartbeat, a position that drifted — a key
 * that did not land, a manual trade, a worker restart mid-move — is corrected
 * on the next bar rather than staying wrong until the next cross.
 *
 * Retries are bounded: an order that will not fill should stop pressing keys
 * and say so, not hammer the hotkey every bar for the rest of the session.
 */
async function reconcile(msg) {
  if (!S.running) return;
  const want = msg.target === "LONG" ? "long" : "flat";

  if (msg.changed) S.retries = 0;              // a new target deserves a fresh try
  if (S.position === want) { S.retries = 0; return; }

  if (S.retries >= (S.cfg.maxRetries || 3)) {
    if (S.retries === (S.cfg.maxRetries || 3)) {
      S.retries++;                             // log once, then stay quiet
      note(`giving up on ${want.toUpperCase()} after ${S.cfg.maxRetries} attempts ` +
           `— position still ${S.position}; fix manually or restart`, "error");
    }
    return;
  }

  const before = S.position;
  await act(want === "long" ? "BUY" : "SELL", msg);
  if (S.position === before) {
    S.retries++;
    note(`attempt ${S.retries} to reach ${want.toUpperCase()} did not take`, "warn");
  } else {
    S.retries = 0;
  }
}

async function ensureInjected(tabId) {
  // Axiom draws its chart inside blob: iframes. Manifest content scripts need
  // match_origin_as_fallback to reach those; this re-injection covers any frame
  // that still slipped through, so the bot cannot end up silently blind.
  try {
    const res = await chrome.scripting.executeScript({
      target: { tabId, allFrames: true },
      files: ["pixels.js", "content.js"],
    });
    note(`injected into ${res.length} frame(s)`);
  } catch (e) {
    note(`injection fallback failed: ${e.message}`, "warn");
  }
}

async function start(tabId, cfg, cfgCA) {
  S.cfg = { ...DEFAULTS, ...cfg };
  await ensureInjected(tabId);
  const ok = await attach(tabId);
  if (!ok) return { ok: false, error: "could not attach debugger" };

  let ca = cfgCA || null;
  if (!ca) {
    try {
      const tab = await chrome.tabs.get(tabId);
      const m = (tab.url || "").match(/0x[a-fA-F0-9]{40}/);
      if (m) ca = m[0];
    } catch (_) {}
  }
  if (!ca) {
    try {
      const st = await chrome.tabs.sendMessage(tabId, { type: "GET_STATE" });
      ca = st?.ca || null;
    } catch (_) {}
  }

  S.running = true;
  S.tabId = tabId;
  S.position = "flat";
  S.trades = 0;
  S.lastTradeAt = 0;
  S.retries = 0;
  S.lockedCA = ca;

  try {
    await chrome.tabs.sendMessage(tabId, { type: "SET_CONFIG", config: contentCfg() });
    await chrome.tabs.sendMessage(tabId, {
      type: "SET_RUNNING", running: true, position: S.position });
  } catch (e) {
    // Not fatal: frames also pull running+config off the keepalive port, so a
    // frame that missed this (or did not exist yet) syncs within one ping.
    note(`initial push to content scripts failed: ${e.message} — frames will pull`, "warn");
  }

  note(`STARTED on ${ca ? ca.slice(0, 10) + "…" : "unknown CA"} ` +
       `(${S.cfg.dryRun ? "DRY RUN" : "LIVE"}, ${S.cfg.hotkeyMode})`, "start");
  await saveState();
  return { ok: true, ca };
}

async function stop() {
  S.running = false;
  if (S.tabId != null) {
    try { await chrome.tabs.sendMessage(S.tabId, { type: "SET_RUNNING", running: false }); } catch (_) {}
  }
  await detach();
  note("STOPPED", "stop");
  await saveState();
  return { ok: true };
}

// Clicking the toolbar icon opens the side panel. Unlike a popup it stays open
// while you interact with the chart, so status and the log remain visible while
// the bot is actually doing something.
chrome.runtime.onInstalled.addListener(() => {
  chrome.sidePanel?.setPanelBehavior?.({ openPanelOnActionClick: true })
    .catch((e) => note(`side panel setup: ${e.message}`, "warn"));
});
chrome.sidePanel?.setPanelBehavior?.({ openPanelOnActionClick: true }).catch(() => {});

chrome.action.onClicked.addListener(async (tab) => {
  try { await chrome.sidePanel.open({ tabId: tab.id }); } catch (_) {}
});

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "emaBotKeepAlive") return;
  restoreState();
  port.onMessage.addListener(() => {
    // Any message resets the worker's idle timer; reply so the page knows the
    // worker is alive and can show it.
    // Carry the authoritative state on every ping. This is what lets a content
    // script that appeared after start (or after a worker restart) discover
    // that it should be running, without any push reaching it.
    try {
      port.postMessage({ alive: true, running: S.running, position: S.position,
                         cfg: contentCfg(), at: Date.now() });
    } catch (_) {}
  });
  port.onDisconnect.addListener(() => { /* content script will reconnect */ });
});

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    await restoreState();
    if (msg.type === "TARGET") {
      await reconcile(msg);
      reply({ ok: true });
    } else if (msg.type === "SIGNAL") {
      await act(msg.side, msg);            // legacy one-shot path
      reply({ ok: true });
    } else if (msg.type === "START") {
      reply(await start(msg.tabId, msg.cfg, msg.ca));
    } else if (msg.type === "STOP") {
      reply(await stop());
    } else if (msg.type === "CLEAR_LOG") {
      S.log = [];
      await chrome.storage.local.set({ log: [] });
      reply({ ok: true });
    } else if (msg.type === "STATUS") {
      let log = S.log;
      if (!log.length) {
        const stored = await chrome.storage.local.get("log");
        log = stored.log || [];
      }
      reply({
        running: S.running, position: S.position, direction: S.direction,
        trades: S.trades, tabId: S.tabId, retries: S.retries,
        lockedCA: S.lockedCA, attached: S.attached, cfg: S.cfg, log,
      });
    }
  })();
  return true;
});

// If the tab goes away or the user detaches the debugger, stop trading.
chrome.debugger.onDetach.addListener((src) => {
  if (src.tabId === S.tabId) { S.attached = false; if (S.running) stop(); }
});
chrome.tabs.onRemoved.addListener((tabId) => { if (tabId === S.tabId && S.running) stop(); });
