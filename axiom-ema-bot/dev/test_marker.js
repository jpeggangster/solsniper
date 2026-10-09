/* The yellow cross marker must be distinguishable from Axiom's own orange
   "SS" trade icons, which sit on the same chart and are close in hue. */
const fs = require("fs");
const src = fs.readFileSync("../pixels.js", "utf8");

function canvas(w, h, paint) {
  const px = new Uint8ClampedArray(w*h*4);
  for (let i=0;i<w*h;i++){px[i*4]=13;px[i*4+1]=17;px[i*4+2]=23;px[i*4+3]=255;}
  const set=(x,y,c)=>{if(x<0||y<0||x>=w||y>=h)return;const i=(y*w+x)*4;px[i]=c[0];px[i+1]=c[1];px[i+2]=c[2];px[i+3]=255;};
  paint(set,w,h);
  return {width:w,height:h,getContext:()=>({getImageData:(sx,sy,sw,sh)=>{
    const o=new Uint8ClampedArray(sw*sh*4);
    for(let y=0;y<sh;y++)for(let x=0;x<sw;x++){const si=((sy+y)*w+(sx+x))*4,di=(y*sw+x)*4;
      o[di]=px[si];o[di+1]=px[si+1];o[di+2]=px[si+2];o[di+3]=px[si+3];}
    return {data:o};}})};
}
const GREEN=[76,175,80], RED=[239,83,80], YELLOW=[255,235,59], ORANGE=[255,171,0];
const LAST=900;

function build({marker, ssIcon}) {
  return canvas(1321,489,(set)=>{
    for(let x=0;x<=LAST;x++){ const gy=200-((x/40)|0), ry=250-((x/60)|0);
      set(x,gy,GREEN);set(x,gy+1,GREEN);set(x,ry,RED);set(x,ry+1,RED); }
    for(let c=0;c<40;c++){const x=100+c*20; if(x>LAST)break;
      for(let y=300;y<360;y++){set(x,y,[8,152,128]);set(x+1,y,[8,152,128]);set(x+2,y,[8,152,128]);}}
    if (marker) for(let y=195;y<205;y++) for(let x=LAST-6;x<=LAST;x++) set(x,y,YELLOW);
    if (ssIcon) { // orange circle ~20px, in the newest columns
      for(let y=120;y<142;y++) for(let x=LAST-18;x<LAST+2;x++) {
        const dx=x-(LAST-8), dy=y-131; if (dx*dx+dy*dy<=110) set(x,y,ORANGE); } }
  });
}
function load(cv){ global.document={querySelectorAll:()=>[cv]};
  return new Function(src+"; return {crossMarker,readLines};")(); }

let pass=0,total=0;
const chk=(l,c,e="")=>{total++;console.log(`${c?"✅":"❌"} ${l}${e?"  "+e:""}`);if(c)pass++;};

let m=load(build({marker:true,ssIcon:false}));
let r=m.crossMarker("#FFEB3B",44);
chk("yellow cross marker detected", r.found, JSON.stringify(r));

m=load(build({marker:false,ssIcon:false}));
r=m.crossMarker("#FFEB3B",44);
chk("no marker -> not found", !r.found, JSON.stringify(r));

m=load(build({marker:false,ssIcon:true}));
r=m.crossMarker("#FFEB3B",44);
chk("orange SS icon does NOT count as a cross (tol 44)", !r.found, JSON.stringify(r));

m=load(build({marker:false,ssIcon:true}));
r=m.crossMarker("#FFEB3B",70);
chk("...but a loose tolerance DOES false-positive (why tol is 44)", r.found, JSON.stringify(r));

m=load(build({marker:true,ssIcon:true}));
r=m.crossMarker("#FFEB3B",44);
const lines=m.readLines("#4CAF50","#EF5350");
chk("marker + icon together: marker found, direction still LONG",
    r.found && lines && lines.direction==="LONG", `${JSON.stringify(r)} ${lines&&lines.direction}`);

console.log(`\n${pass}/${total} pass`);
