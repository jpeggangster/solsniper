/* Validates the legend finder against TradingView-shaped DOM, including the
   hashed class names and the wrapper markup Axiom is likely to add. */
const { JSDOM } = require("jsdom");
const fs = require("fs");

const src = fs.readFileSync("content.js", "utf8");
const body = src.slice(src.indexOf("function parseNum"), src.indexOf("/* ---------- cross detection"));

function run(html, label, expect) {
  const dom = new JSDOM(`<body>${html}</body>`);
  global.document = dom.window.document;
  global.NodeFilter = dom.window.NodeFilter;
  const fn = new Function(body + "; return { readLegend, parseNum };")();
  const r = fn.readLegend();
  const got = r ? r.values : null;
  const ok = got && expect && got[0] === expect[0] && got[1] === expect[1];
  console.log(`${ok ? "✅" : "❌"} ${label}`);
  console.log(`     -> ${r ? JSON.stringify(r.values) + `  title="${r.title}" depth=${r.depth}` : "NOT FOUND"}`);
  if (!ok && expect) console.log(`     expected ${JSON.stringify(expect)}`);
  return ok;
}

let pass = 0, total = 0;
const T = (h, l, e) => { total++; if (run(h, l, e)) pass++; };

// 1. Classic TradingView legend with hashed classes
T(`<div class="legend-3sxgd4pP"><div data-name="legend-source-item" class="item-l31H9iuA">
   <div class="titleWrapper-l31H9iuA"><div class="title-l31H9iuA">EMA Cross (21, 12)</div></div>
   <div class="valuesWrapper-l31H9iuA"><div class="valueItem-l31H9iuA"><div class="valueValue-l31H9iuA">163K</div></div>
   <div class="valueItem-l31H9iuA"><div class="valueValue-l31H9iuA">191K</div></div></div></div></div>`,
  "TradingView hashed classes", [163000, 191000]);

// 2. Axiom-style wrapper with extra chrome around it
T(`<div class="chart"><section><header>X Link/USD on Pons V2, 5s, axiom.trade</header>
   <div class="row"><span>Volume (20)</span><span>9.726K</span><span>4.489K</span></div>
   <div class="row"><span>EMA Cross (21, 12)</span><span>163K</span><span>191K</span></div>
   </section></div>`,
  "Axiom-style wrapper, Volume row above", [163000, 191000]);

// 3. Values in a sibling container (needs the parent climb)
T(`<div class="item"><div class="t">EMA Cross (21, 12)</div></div>
   <div class="v"><span>10.3K</span><span>10.1K</span></div>`,
  "values in sibling container", null);

// 4. Decimal values, no K suffix
T(`<div><div>EMA Cross (21, 12)</div><div><span>25.6338</span><span>24.4012</span></div></div>`,
  "plain decimals", [25.6338, 24.4012]);

// 5. Must not be fooled by the title numbers 21 and 12
T(`<div><span>EMA Cross (21, 12)</span><span>21</span><span>12</span><span>163K</span><span>191K</span></div>`,
  "title numbers repeated as values", [163000, 191000]);

// 6. No EMA study on the chart at all
total++; {
  const dom = new JSDOM(`<body><div>Volume (20) 9.726K</div></body>`);
  global.document = dom.window.document; global.NodeFilter = dom.window.NodeFilter;
  const fn = new Function(body + "; return { readLegend };")();
  const r = fn.readLegend();
  const ok = r === null;
  console.log(`${ok ? "✅" : "❌"} no EMA study -> correctly returns ${r === null ? "null" : JSON.stringify(r)}`);
  if (ok) pass++;
}

console.log(`\n${pass}/${total} pass`);
