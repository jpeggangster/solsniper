#!/usr/bin/env python3
"""Tiny live-log viewer for sniper.log"""
import os, time
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG_PATH = os.path.join(os.path.dirname(__file__), "sniper.log")

HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Sniper Live Logs</title>
  <style>
    body { background:#0d0d0d; color:#e0e0e0; font-family:monospace; font-size:13px; margin:0; }
    #toolbar { background:#1a1a1a; padding:10px 16px; display:flex; align-items:center; gap:16px;
               border-bottom:1px solid #333; position:sticky; top:0; z-index:10; }
    #toolbar h1 { margin:0; font-size:15px; color:#00e5ff; }
    #status { font-size:12px; color:#888; margin-left:auto; }
    #log { padding:12px 16px; white-space:pre-wrap; word-break:break-all; }
    .INFO  { color:#e0e0e0; }
    .ERROR { color:#ff5555; }
    .WARN  { color:#ffb86c; }
    .BUY   { color:#50fa7b; }
    .SELL  { color:#ff79c6; }
    .TX    { color:#8be9fd; }
  </style>
</head>
<body>
  <div id="toolbar">
    <h1>🎯 Sniper Live Logs</h1>
    <label style="color:#aaa;font-size:12px;">
      <input type="checkbox" id="autoScroll" checked> Auto-scroll
    </label>
    <span id="status">Loading…</span>
  </div>
  <div id="log"></div>
  <script>
    let lastLen = 0;
    const logEl = document.getElementById('log');
    const statusEl = document.getElementById('status');
    const autoScroll = document.getElementById('autoScroll');

    function colorLine(line) {
      if (line.includes('🟢') || line.includes('✅ Position')) return `<span class="BUY">${esc(line)}</span>`;
      if (line.includes('🔴') || line.includes('✅ Sold'))    return `<span class="SELL">${esc(line)}</span>`;
      if (line.includes('TX sent') || line.includes('solscan')) return `<span class="TX">${esc(line)}</span>`;
      if (line.includes('[ERROR]') || line.includes('❌'))    return `<span class="ERROR">${esc(line)}</span>`;
      if (line.includes('[WARNING]'))                          return `<span class="WARN">${esc(line)}</span>`;
      return `<span class="INFO">${esc(line)}</span>`;
    }
    function esc(s){ return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

    async function poll() {
      try {
        const r = await fetch('/logs?from=' + lastLen);
        if (!r.ok) throw new Error(r.status);
        const data = await r.json();
        if (data.text) {
          const lines = data.text.split('\\n');
          lines.forEach(l => { if(l) logEl.innerHTML += colorLine(l) + '\\n'; });
          lastLen = data.next;
          statusEl.textContent = 'Live · ' + new Date().toLocaleTimeString();
          if (autoScroll.checked) window.scrollTo(0, document.body.scrollHeight);
        }
      } catch(e) { statusEl.textContent = 'Error: ' + e; }
      setTimeout(poll, 1500);
    }
    poll();
  </script>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass  # silence access log

    def do_GET(self):
        if self.path == "/":
            self._send(200, "text/html", HTML.encode())
        elif self.path.startswith("/logs"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            offset = int(qs.get("from", ["0"])[0])
            try:
                with open(LOG_PATH, "rb") as f:
                    f.seek(offset)
                    chunk = f.read()
                    next_offset = offset + len(chunk)
                import json
                body = json.dumps({"text": chunk.decode("utf-8", errors="replace"), "next": next_offset}).encode()
                self._send(200, "application/json", body)
            except FileNotFoundError:
                import json
                self._send(200, "application/json", json.dumps({"text":"", "next":0}).encode())
        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 7070), Handler)
    print(f"Log viewer at http://127.0.0.1:7070", flush=True)
    server.serve_forever()
