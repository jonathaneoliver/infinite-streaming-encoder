// One job per codec (#324) — the submit-side codec helpers, exercised for real.
//
// The form fans out over ladders AND codecs now, so the number of jobs the
// Encode button starts is the product. Two helpers decide it, and they differ in
// exactly the way that matters:
//
//   selectedCodecs()  falls back to EVERY codec when none are ticked, so the
//                     res-tier list is never empty. That fallback is for DISPLAY.
//   submitCodecs()    falls back to h264 alone, which is what the form has
//                     always submitted.
//
// Using the display helper on the submit path would turn an empty selection into
// three jobs — including AV1, the slowest thing here. `make check`'s page-JS step
// is a syntax check and would not notice, so this runs the functions.
//
// No test framework and no DOM: the functions are extracted from index.html by
// brace matching and given a stubbed document. Skipped when node is absent, the
// same way the staticcheck/tofu/ruff steps skip.
const fs = require('fs');
const path = require('path');

const INDEX = path.join(__dirname, '..', 'static', 'index.html');
const src = fs.readFileSync(INDEX, 'utf8');

// Extract a top-level `function name(...) { ... }` by matching braces. Naive on
// purpose — braces inside strings or regexes in the extracted body would break
// it, and if that ever happens the test fails loudly rather than silently
// testing the wrong text.
function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`function ${name} not found in ${INDEX}`);
  let depth = 0;
  let i = src.indexOf('{', start);
  for (; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) break; }
  }
  if (depth !== 0) throw new Error(`unbalanced braces extracting ${name}`);
  return src.slice(start, i + 1);
}

let ticked = [];
global.document = {
  querySelector(sel) {
    const m = sel.match(/\.codec-cb\[value="(\w+)"\]/);
    return m ? { checked: ticked.includes(m[1]) } : null;
  },
};

eval(extract('submitCodecs'));
eval(extract('codecValue'));

let fails = 0;
function eq(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) console.log(`  ok   ${name}`);
  else { console.log(`  FAIL ${name}: got ${g}, want ${w}`); fails++; }
}

console.log('submitCodecs — one entry per ticked codec, canonical order');
ticked = ['h264']; eq('h264 alone', submitCodecs(), ['h264']);
ticked = ['hevc']; eq('hevc alone', submitCodecs(), ['hevc']);
ticked = ['h264', 'hevc']; eq('h264 + hevc', submitCodecs(), ['h264', 'hevc']);
ticked = ['h264', 'hevc', 'av1']; eq('all three', submitCodecs(), ['h264', 'hevc', 'av1']);
// Order is the canonical list's, not the order boxes were clicked — the first
// entry seeds nothing, but a stable order keeps job submission deterministic.
ticked = ['av1', 'h264']; eq('canonical order, not click order', submitCodecs(), ['h264', 'av1']);

console.log('\nthe fallback — this is the one that matters');
ticked = [];
eq('nothing ticked submits h264 ALONE', submitCodecs(), ['h264']);
eq('and specifically not every codec', submitCodecs().length, 1);

console.log('\ncodecValue still yields the backend selector for its other callers');
// The estimate and the AWS panel still send a combined selector; parseCodecSel
// on the Go side keeps accepting both/all, so nothing there had to change.
ticked = []; eq('empty', codecValue(), 'h264');
ticked = ['h264', 'hevc']; eq('h264+hevc is "both"', codecValue(), 'both');
ticked = ['h264', 'hevc', 'av1']; eq('three is "all"', codecValue(), 'all');
ticked = ['h264', 'av1']; eq('other subsets are a comma list', codecValue(), 'h264,av1');
ticked = ['hevc']; eq('single codec', codecValue(), 'hevc');

// codecValue is now built ON submitCodecs, so the two cannot drift on the
// fallback — which is what let them disagree in the first place.
console.log('\ncodecValue and submitCodecs agree on the fallback');
ticked = [];
eq('both fall back to h264', codecValue(), submitCodecs().join(','));

console.log('\nthe submit body carries no combined codec');
// The loop sets `codec` per job. A combined value left in the body would be
// silently overridden and would read, to anyone opening the file, as though one
// job encoded several codecs.
const bodyBlock = src.slice(src.indexOf('const body = {'), src.indexOf('const body = {') + 900);
if (/^\s*codec:/m.test(bodyBlock)) {
  console.log('  FAIL submit body still sets a combined `codec` — the loop overrides it');
  fails++;
} else {
  console.log('  ok   body sets no codec; the per-job loop supplies it');
}
if (!/body:\s*JSON\.stringify\(\{\s*\.\.\.body,\s*ladder:\s*name,\s*codec:\s*codec\s*\}\)/.test(src)) {
  console.log('  FAIL the submit loop no longer sends a per-job ladder+codec');
  fails++;
} else {
  console.log('  ok   the submit loop sends one ladder + one codec per job');
}

console.log();
if (fails) { console.log(`FAILED (${fails})`); process.exit(1); }
console.log('all codec-split checks passed');
