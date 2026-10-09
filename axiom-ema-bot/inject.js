/*
 * Runs in the PAGE's own JS world (content scripts are isolated and cannot see
 * the page's WebSocket). Wraps WebSocket so we can tell whether the chart is
 * driven by a feed we could read directly — which matters because if Axiom
 * draws its legend on <canvas>, there is no DOM text to scrape and the feed is
 * the only way to compute the EMA.
 *
 * Records metadata and small samples only. It does not alter or send anything.
 */
(() => {
  if (window.__emaBotWSHooked) return;
  window.__emaBotWSHooked = true;
  const store = (window.__emaBotWS = { sockets: [], samples: [], counts: {} });

  const Native = window.WebSocket;
  window.WebSocket = function (url, protocols) {
    const ws = protocols ? new Native(url, protocols) : new Native(url);
    try {
      store.sockets.push({ url: String(url), at: Date.now() });
      store.counts[String(url)] = 0;
      ws.addEventListener("message", (ev) => {
        store.counts[String(url)] = (store.counts[String(url)] || 0) + 1;
        if (store.samples.length < 40 && typeof ev.data === "string") {
          store.samples.push({ url: String(url), data: ev.data.slice(0, 400) });
        }
      });
    } catch (_) {}
    return ws;
  };
  window.WebSocket.prototype = Native.prototype;
  Object.assign(window.WebSocket, Native);

  // Also note how the chart is drawn.
  store.probe = () => ({
    canvases: document.querySelectorAll("canvas").length,
    hasEmaText: /\bEMA\b/i.test(document.body ? document.body.innerText : ""),
    bodyTextLen: (document.body ? document.body.innerText : "").length,
    globals: ["TradingView", "tvWidget", "LightweightCharts", "klinecharts"]
      .filter((g) => g in window),
  });
})();
