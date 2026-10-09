/* Reproduces the real Axiom layout: a wide price pane with an empty right
   margin, candles, two EMA lines, plus a narrow price-axis canvas carrying
   coloured badges that previously hijacked colour detection. */
const fs = require("fs");
const src = fs.readFileSync("../pixels.js", "utf8");

function canvas(w, h, paint) {
  const px = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < w * h; i++) { px[i*4]=13; px[i*4+1]=17; px[i*4+2]=23; px[i*4+3]=255; }
  const set = (x, y, c) => { if (x<0||y<0||x>=w||y>=h) return;
    const i=(y*w+x)*4; px[i]=c[0]; px[i+1]=c[1]; px[i+2]=c[2]; px[i+3]=255; };
  paint(set, w, h);
  return { width:w, height:h, getContext:()=>({ getImageData:(sx,sy,sw,sh)=>{
    const o=new Uint8ClampedArray(sw*sh*4);
    for(let y=0;y<sh;y++)for(let x=0;x<sw;x++){
      const si=((sy+y)*w+(sx+x))*4, di=(y*sw+x)*4;
      o[di]=px[si];o[di+1]=px[si+1];o[di+2]=px[si+2];o[di+3]=px[si+3];}
    return {data:o};}})};
}

const GREEN=[76,175,80], RED=[239,83,80], TEAL=[8,152,128], DRED=[176,40,48], YEL=[248,232,56];
const LAST_X = 900;                 // newest candle; 1321-900 = 421px of empty margin

const pane = canvas(1321, 489, (set) => {
  for (let x = 0; x <= LAST_X; x++) {            // the two EMA lines
    const gy = 200 - Math.round(x/40), ry = 250 - Math.round(x/60);
    set(x, gy, GREEN); set(x, gy+1, GREEN);
    set(x, ry, RED);   set(x, ry+1, RED);
  }
  for (let c = 0; c < 40; c++) {                 // teal candle bodies
    const x = 100 + c*20;
    if (x > LAST_X) break;
    for (let y = 300; y < 360; y++) { set(x, y, TEAL); set(x+1, y, TEAL); set(x+2, y, TEAL); }
  }
  for (let y = 195; y < 205; y++) for (let x = LAST_X-8; x <= LAST_X; x++) set(x, y, YEL);
  // last-price line: 2px tall, spans the FULL width including the empty margin.
  // This is what your chart shows as rgb(240,48,64) x14 out in the right margin.
  for (let x = 0; x < 1321; x++) { set(x, 180, [240,48,64]); set(x, 181, [240,48,64]); }
});

// price axis: narrow, full of coloured badges — the thing that fooled it before
const axis = canvas(56, 489, (set) => {
  const badge = (y, c) => { for (let yy=y; yy<y+18; yy++) for (let x=2; x<54; x++) set(x, yy, c); };
  badge(150, YEL); badge(220, DRED); badge(260, GREEN);
});

global.document = { querySelectorAll: () => [pane, axis, canvas(1321,28,()=>{}), canvas(56,28,()=>{})] };
const m = new Function(src + "; return { readLines, autoColors, sampleColors, crossMarker, chartCanvases };")();

let pass=0, total=0;
const chk=(label,cond,extra="")=>{total++;console.log(`${cond?"✅":"❌"} ${label}${extra?"  "+extra:""}`);if(cond)pass++;};

const cans = m.chartCanvases();
chk("picks only the wide price pane (ignores 56px axis)", cans.length===1 && cans[0].width===1321,
    `-> ${cans.map(c=>c.width+"x"+c.height).join(", ")}`);

const cols = m.sampleColors();
const top = cols.slice(0,4).map(c=>`${c.rgb}(perCol ${c.perCol})`).join("  ");
chk("colour sample comes from the pane, not badges", cols.length>0 && cols[0].canvas==="1321x489", `-> ${top}`);

const auto = m.autoColors();
const okAuto = auto && Math.abs(parseInt(auto.fastColor.slice(1,3),16)-76)<40
            && Math.abs(parseInt(auto.slowColor.slice(1,3),16)-239)<40;
chk("auto-colour picks the EMA green/red, not badge colours", okAuto, `-> ${JSON.stringify(auto)}`);

const r = m.readLines("#4CAF50", "#EF5350");
chk("finds both lines despite 421px empty right margin", !!r,
    r ? `-> fastY=${r.fastY} slowY=${r.slowY} rightX=${r.rightX} ${r.direction}` : "-> NOT FOUND");
chk("direction is LONG (green above red)", r && r.direction==="LONG");

const cm = m.crossMarker("#FFEB3B");
chk("finds the yellow cross marker", cm.found, `-> ${JSON.stringify(cm)}`);

console.log(`\n${pass}/${total} pass`);
