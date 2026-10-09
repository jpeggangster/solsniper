/*
 * The state machine: level-triggered target with a hysteresis dead zone, and a
 * cross column that gates ENTRIES only.
 *
 * Drives the real tick() over synthetic panes, so the thing under test is the
 * code that ships, not a restatement of it.
 */
const fs = require("fs");
const pixels = fs.readFileSync("../pixels.js", "utf8");
const content = fs.readFileSync("../content.js", "utf8");

const BG=[13,17,23], TEAL=[8,152,128], YELLOW=[255,235,59], GREEN=[76,175,80], RED=[239,83,80];

/* gapCss > 0 => green (EMA12) above red (EMA21). markerFromRightCss: null for none. */
function pane({ gapCss, markerFromRightCss, dpr = 1 }) {
  const cssW=1321, cssH=489, lastCss=900, w=cssW*dpr, h=cssH*dpr;
  const px = new Uint8ClampedArray(w*h*4);
  for (let i=0;i<w*h;i++){px[i*4]=BG[0];px[i*4+1]=BG[1];px[i*4+2]=BG[2];px[i*4+3]=255;}
  const set=(x,y,c)=>{if(x<0||y<0||x>=w||y>=h)return;const i=((y|0)*w+(x|0))*4;
    px[i]=c[0];px[i+1]=c[1];px[i+2]=c[2];px[i+3]=255;};
  const last = lastCss*dpr;
  const midCss = 240;
  const gy = (midCss - gapCss/2)*dpr, ry = (midCss + gapCss/2)*dpr;
  for (let x=0;x<=last;x++) for(let t=0;t<dpr;t++){ set(x,gy+t,GREEN); set(x,ry+t,RED); }
  for (let c=0;c<=40;c++){ const x=(100+c*20)*dpr; if(x>last)break;
    for(let y=330*dpr;y<390*dpr;y++) for(let t=0;t<3*dpr;t++) set(x+t,y,TEAL); }
  if (markerFromRightCss != null) {
    const x0 = last - markerFromRightCss*dpr;
    for (let y=150*dpr;y<300*dpr;y++) for(let t=0;t<3*dpr;t++) set(x0+t,y,YELLOW);
  }
  return { width:w, height:h,
    getBoundingClientRect:()=>({width:cssW,height:cssH}),
    getContext:()=>({getImageData:(sx,sy,sw,sh)=>{
      const o=new Uint8ClampedArray(sw*sh*4);
      for(let y=0;y<sh;y++)for(let x=0;x<sw;x++){const si=((sy+y)*w+(sx+x))*4,di=(y*sw+x)*4;
        o[di]=px[si];o[di+1]=px[si+1];o[di+2]=px[si+2];o[di+3]=px[si+3];}
      return {data:o};}})};
}

function harness() {
  const sent = [];
  let current = pane({ gapCss: 0, markerFromRightCss: null });
  let now = 1_700_000_000_000;
  const listeners = [];
  const win = {};
  const g = {
    window: win,
    document: {
      querySelectorAll: () => [current],
      body: { innerText: "" },
      createTreeWalker: () => ({ nextNode: () => null }),
    },
    location: { pathname: "/meme/0x" + "a".repeat(40), href: "https://axiom.trade/meme/0x" + "a".repeat(40) },
    chrome: {
      runtime: {
        sendMessage: (m) => { sent.push(m); },
        connect: () => ({ onMessage:{addListener:(f)=>listeners.push(f)},
                          onDisconnect:{addListener(){}}, postMessage(){} }),
        onMessage: { addListener(){} },
      },
    },
    setInterval: () => 0,
    setTimeout: () => 0,
    Date: { now: () => now },
    NodeFilter: { SHOW_ELEMENT: 1 },
  };
  const fn = new Function(...Object.keys(g), pixels + "\n" + content + "\n;return window;");
  fn(...Object.values(g));
  return {
    sent, listeners,
    STATE: win.__emaBotSTATE,
    tick: win.__emaBotTick,
    setChart: (o) => { current = pane(o); },
    advanceBar: (n = 1) => { now += 1000 * n; },
    nowRef: () => now,
  };
}

let pass=0,total=0;
const chk=(l,c,e="")=>{total++;console.log(`${c?"✅":"❌"} ${l}${e?"  "+e:""}`);if(c)pass++;};

// ── 1. not running: nothing is evaluated ───────────────────────────────────
let H = harness();
H.setChart({ gapCss: 60, markerFromRightCss: 10 });
H.tick();
chk("stopped bot evaluates nothing", H.sent.length === 0 && H.STATE.lastEval.reason === "bot not running",
    H.STATE.lastEval?.reason);

// ── 2. the keepalive port turns it on, with no SET_RUNNING push ────────────
chk("a frame learns it is running from the keepalive port alone",
    (() => { H.listeners.forEach((f) => f({ alive: true, running: true, position: "flat" }));
             return H.STATE.running === true; })(),
    `running=${H.STATE.running}`);

// ── 3. hysteresis: inside the dead zone the target holds ───────────────────
H.STATE.requireMarker = false;
H.setChart({ gapCss: 1, markerFromRightCss: null });   // ~0.2% of pane: inside 0.8%
H.advanceBar(); H.tick();
chk("a 1px gap does not flip the target (dead zone)", H.STATE.target === "FLAT",
    `target=${H.STATE.target} why="${H.STATE.lastEval.reason}"`);

// ── 4. clearly apart: the target flips ─────────────────────────────────────
H.setChart({ gapCss: 40, markerFromRightCss: null });  // ~9% of pane
H.advanceBar(); H.tick();
chk("a 40px gap flips the target to LONG", H.STATE.target === "LONG",
    `target=${H.STATE.target} why="${H.STATE.lastEval.reason}"`);
chk("...and a TARGET message is sent", H.sent.some((m) => m.type==="TARGET" && m.target==="LONG"),
    JSON.stringify(H.sent.filter(m=>m.type==="TARGET").map(m=>m.target)));

// ── 5. chop does not whipsaw it back out ───────────────────────────────────
const before = H.sent.length;
for (const g of [30, 6, 20, 3, 25]) { H.setChart({ gapCss: g, markerFromRightCss: null });
                                      H.advanceBar(); H.tick(); }
chk("staying above the band through chop sends no new target",
    !H.sent.slice(before).some((m) => m.type==="TARGET" && m.changed),
    `${H.sent.slice(before).filter(m=>m.type==="TARGET").length} re-asserts, 0 changes`);

// ── 6. exits are never gated by the marker ─────────────────────────────────
H.STATE.requireMarker = true;
H.setChart({ gapCss: -40, markerFromRightCss: null });  // red above green, no column
H.advanceBar(); H.tick();
chk("an exit fires with NO cross column present", H.STATE.target === "FLAT",
    `target=${H.STATE.target} why="${H.STATE.lastEval.reason}"`);

// ── 7. entries ARE gated when the column is stale ──────────────────────────
H.setChart({ gapCss: 40, markerFromRightCss: 800 });    // column far off to the left
H.advanceBar(); H.tick();
chk("an entry is held when the newest column is beyond the freshness window",
    H.STATE.target === "FLAT" && /entry held/.test(H.STATE.lastEval.reason),
    `target=${H.STATE.target} why="${H.STATE.lastEval.reason}"`);

// ── 8. ...and allowed once a fresh column is there ─────────────────────────
H.setChart({ gapCss: 40, markerFromRightCss: 120 });    // within 300px of the edge
H.advanceBar(); H.tick();
chk("the same entry fires once a fresh column appears", H.STATE.target === "LONG",
    `target=${H.STATE.target} why="${H.STATE.lastEval.reason}"`);

// ── 9. the target is re-asserted so a drifted position self-corrects ───────
const n9 = H.sent.length;
for (let i = 0; i < 12; i++) { H.advanceBar(); H.tick(); }
const re = H.sent.slice(n9).filter((m) => m.type === "TARGET");
chk("the target is re-asserted on a heartbeat", re.length >= 1 && re.every((m) => !m.changed),
    `${re.length} re-asserts over 12 bars`);

console.log(`\n${pass}/${total} pass`);
process.exit(pass === total ? 0 : 1);
