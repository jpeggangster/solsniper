/* Feeds readLines() a fake canvas with two coloured lines at known heights. */
const fs = require("fs");
const src = fs.readFileSync("../pixels.js", "utf8");

function makeCanvas(w, h, lines, opts = {}) {
  // lines: [{rgb:[r,g,b], y:Number}]
  const px = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < w * h; i++) { px[i*4]=13; px[i*4+1]=17; px[i*4+2]=23; px[i*4+3]=255; }
  const set = (x, y, c) => {
    if (x < 0 || y < 0 || x >= w || y >= h) return;
    const i = (y * w + x) * 4;
    px[i]=c[0]; px[i+1]=c[1]; px[i+2]=c[2]; px[i+3]=255;
  };
  for (const L of lines) for (let x = 0; x < w; x++) { set(x, L.y, L.rgb); set(x, L.y+1, L.rgb); }
  if (opts.legendText) for (const L of lines) for (let x = 4; x < 60; x++) set(x, 6, L.rgb);
  return {
    width: w, height: h,
    getContext: () => ({
      getImageData: (sx, sy, sw, sh) => {
        const out = new Uint8ClampedArray(sw * sh * 4);
        for (let y = 0; y < sh; y++) for (let x = 0; x < sw; x++) {
          const si = ((sy + y) * w + (sx + x)) * 4, di = (y * sw + x) * 4;
          out[di]=px[si]; out[di+1]=px[si+1]; out[di+2]=px[si+2]; out[di+3]=px[si+3];
        }
        return { data: out };
      },
    }),
  };
}

function load(canvases) {
  global.document = { querySelectorAll: (sel) => (sel === "canvas" ? canvases : []) };
  return new Function(src + "; return { readLines, sampleColors };")();
}

const GREEN = [76,175,80], RED = [239,83,80];
let pass = 0, total = 0;
function T(label, canvases, expect) {
  total++;
  const { readLines } = load(canvases);
  const r = readLines("#4CAF50", "#EF5350");
  const got = r ? r.direction : null;
  const ok = got === expect;
  console.log(`${ok ? "✅" : "❌"} ${label}`);
  console.log(`     ${r ? `fastY=${r.fastY} slowY=${r.slowY} gap=${r.gapPx}px -> ${r.direction}` : "NOT FOUND"}`
              + (ok ? "" : `   expected ${expect}`));
  if (ok) pass++;
}

// y grows downward: green ABOVE red means smaller y
T("green above red -> LONG", [makeCanvas(800, 400, [{rgb:GREEN,y:120},{rgb:RED,y:200}])], "LONG");
T("red above green -> FLAT", [makeCanvas(800, 400, [{rgb:GREEN,y:260},{rgb:RED,y:150}])], "FLAT");
T("lines 3px apart, green above", [makeCanvas(800, 400, [{rgb:GREEN,y:200},{rgb:RED,y:203}])], "LONG");
T("legend text in same colours is ignored (top 10%)",
  [makeCanvas(800, 400, [{rgb:GREEN,y:300},{rgb:RED,y:180}], {legendText:true})], "FLAT");
T("picks the largest canvas",
  [makeCanvas(200,120,[{rgb:GREEN,y:100},{rgb:RED,y:20}]),
   makeCanvas(900,500,[{rgb:GREEN,y:100},{rgb:RED,y:300}])], "LONG");

total++; {
  const { readLines } = load([makeCanvas(800,400,[{rgb:[80,80,90],y:100}])]);
  const r = readLines("#4CAF50", "#EF5350");
  const ok = r === null;
  console.log(`${ok ? "✅" : "❌"} no matching colours -> ${r === null ? "null (correct)" : JSON.stringify(r)}`);
  if (ok) pass++;
}

total++; {
  const { sampleColors } = load([makeCanvas(800,400,[{rgb:GREEN,y:120},{rgb:RED,y:200}])]);
  const c = sampleColors();
  const hasBoth = c.some(x=>x.rgb.startsWith("72,")||x.rgb.startsWith("76,")) &&
                  c.some(x=>x.rgb.startsWith("232,")||x.rgb.startsWith("239,"));
  console.log(`${hasBoth ? "✅" : "❌"} sampleColors finds both lines -> ${c.slice(0,3).map(x=>x.rgb).join("  ")}`);
  if (hasBoth) pass++;
}

console.log(`\n${pass}/${total} pass`);
