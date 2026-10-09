/* The legend sanity gate must reject cross-study number mixups like the
   real "994.91 vs 20.000" that fabricated a LONG. */
const MAX = 0.25;
function accept(a, b) {
  const spread = Math.abs(a - b) / Math.max(Math.abs(b), 1e-30);
  return a > 0 && b > 0 && spread <= MAX;
}
const cases = [
  ["real EMA12/21 (183K vs 180K)", 183000, 180100, true],
  ["real, 5% apart", 93400, 90100, true],
  ["the false signal (994.91 vs 20.000)", 994.91, 20.000, false],
  ["the earlier false signal (83.85 vs 20)", 83.850, 20.000, false],
  ["wildly apart", 1e6, 12, false],
  ["identical", 5000, 5000, true],
  ["24% apart — just inside", 124, 100, true],
  ["26% apart — just outside", 126, 100, false],
  ["zero value", 0, 100, false],
];
let pass = 0;
for (const [label, a, b, want] of cases) {
  const got = accept(a, b);
  const ok = got === want;
  const pct = (Math.abs(a - b) / Math.abs(b) * 100).toFixed(0);
  console.log(`${ok ? "✅" : "❌"} ${label.padEnd(38)} ${pct.padStart(7)}% -> ${got ? "accept" : "REJECT"}`);
  if (ok) pass++;
}
console.log(`\n${pass}/${cases.length} pass`);
