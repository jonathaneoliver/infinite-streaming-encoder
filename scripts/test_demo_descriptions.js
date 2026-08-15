// The demo narration does not carry its own copy of any control description
// (#356).
//
// #349 put each control's description in the DOM once. The recorders are the
// third reader of it, after the on-screen hint and aria-describedby — but that
// is a convention, and the thing it replaced was ELEVEN hardcoded strings that
// nobody noticed had drifted because nothing could notice.
//
// This is a STATIC check: it reads the two recorder sources and refuses a
// literal caption on any control-explaining call. It cannot run the recording —
// that needs a browser, a server and a real encode — so it checks the one
// property that a recording would only reveal by being watched.
//
// Run by `make check` as `js demodesc`, and by CI through the scripts/test_*.js
// glob.

const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const DEMO = path.join(ROOT, 'tools', 'demo');
const PAGE = path.join(ROOT, 'static', 'index.html');

const fail = [];

// ---------------------------------------------------------------------------
// 1. No control description may be a literal in a recorder.
//
// The calls that describe a control are explain / explainPair / explainSel. A
// second argument that STARTS a string literal is a hardcoded description.
// `explain('#encode-options', '…')` is exempt: it describes the PANEL, not a
// control, and the panel has no data-desc to read.
const EXEMPT_SELECTORS = new Set(['#encode-options']);

for (const file of ['record_local.js', 'record_cloud.js']) {
  const src = fs.readFileSync(path.join(DEMO, file), 'utf8');
  const re = /await\s+(explain|explainSel|explainPair)\(\s*([^)]*?)\)/gs;
  let m;
  while ((m = re.exec(src))) {
    const [, fn, args] = m;
    // The caption is the last argument. Find where it starts.
    const parts = args.split(',');
    const first = (parts[0] || '').trim().replace(/^['"]|['"]$/g, '');
    const caption = parts.slice(fn === 'explainPair' ? 2 : 1).join(',').trim();
    if (!caption) continue;
    if (EXEMPT_SELECTORS.has(first)) continue;
    // A template literal is fine — that is how the DOM-read text is composed.
    // A plain '…' or "…" string is a hardcoded description.
    if (/^['"]/.test(caption)) {
      const line = src.slice(0, m.index).split('\n').length;
      fail.push(`${file}:${line}  ${fn}(${first}, …) has a LITERAL description.\n` +
                `    Read it from the page: narrationFor(await readControl(page, '${first}'))`);
    }
  }
}

// ---------------------------------------------------------------------------
// 2. Every control the tour names must still exist and still be described.
//
// The tour holds ids, not descriptions — but an id that no longer matches a
// described control means the run throws at assertTourCovers, half an hour into
// a take. Catching it here costs nothing.
const html = fs.readFileSync(PAGE, 'utf8');
const controlsBlock = (() => {
  const i = html.indexOf('<div class="controls">');
  const j = html.indexOf('<div class="encode-actions">');
  return i >= 0 && j > i ? html.slice(i, j) : '';
})();
if (!controlsBlock) {
  fail.push('static/index.html: could not find the .controls block — this ' +
            'check is looking at the wrong thing, which is worse than failing.');
}

const describedIds = new Set();
for (const m of controlsBlock.matchAll(/id="([^"]+)"[^>]*data-desc=/g)) describedIds.add(m[1]);
for (const m of controlsBlock.matchAll(/data-desc="[^"]*"[^>]*id="([^"]+)"/g)) describedIds.add(m[1]);

const local = fs.readFileSync(path.join(DEMO, 'record_local.js'), 'utf8');
const tourMatch = local.match(/const TOUR = \[([\s\S]*?)\];/);
if (!tourMatch) {
  fail.push('record_local.js: no TOUR list found — the tour is supposed to be ' +
            'an explicit ORDER checked against the page, not an implicit walk.');
} else {
  const tour = [...tourMatch[1].matchAll(/'([^']+)'/g)].map(x => x[1]);
  const missing = tour.filter(id => !describedIds.has(id));
  if (missing.length) {
    fail.push('record_local.js TOUR names control(s) the page does not describe: ' +
              missing.join(', ') + '\n    Either the control was renamed, or it needs a data-desc.');
  }
  if (tour.length < 8) {
    fail.push(`record_local.js TOUR is down to ${tour.length} controls — that is ` +
              'a tour that stopped covering the form, not a shorter video.');
  }
}

// ---------------------------------------------------------------------------
// 3. describe.js is what makes any of this true.
const helpers = ['readControl', 'describedControls', 'narrationFor',
                 'optionDesc', 'firstSentence', 'assertTourCovers'];
const describeSrc = fs.readFileSync(path.join(DEMO, 'describe.js'), 'utf8');
for (const h of helpers) {
  if (!describeSrc.includes(h)) fail.push(`tools/demo/describe.js no longer exports ${h}`);
}
for (const file of ['record_local.js', 'record_cloud.js']) {
  const src = fs.readFileSync(path.join(DEMO, file), 'utf8');
  if (!src.includes("require('./describe')")) {
    fail.push(`${file} does not require ./describe — its captions cannot be ` +
              'reading the page.');
  }
}

if (fail.length) {
  console.log(`FAIL: ${fail.length} problem(s)`);
  for (const f of fail) console.log('  ' + f);
  process.exit(1);
}
console.log(`ok (${describedIds.size} described control(s); no literal descriptions in 2 recorders)`);
