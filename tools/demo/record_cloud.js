// v2 of the demo drive.
//
// Changes over v1, all from watching the first take:
//   - The caption RESERVES a strip instead of overlaying: body gets a
//     padding-bottom so the app lays out above it and nothing is ever covered.
//   - The Outputs walkthrough spotlights the three areas of the detail panel
//     one at a time, with the cursor parked on each while it is described.
//   - The "across N machines" line is computed from the finished job's stages,
//     so it can never claim a fan-out that did not happen (v1 asserted three
//     machines on a run that used one).
const { chromium } = require('playwright');
// #356: the narration reads the app's own descriptions instead of carrying its
// own copy of them.
const { readControl, optionDesc, firstSentence } = require('./describe');

const BASE = process.env.BASE || 'http://localhost:8080';
// See record_local.js — artifacts go to the WORK dir, never next to the script.
const os = require('os'), path = require('path');
const WORK = process.env.DEMO_DIR || path.join(os.homedir(), 'Desktop', 'encoder-demo');
const OUT = path.join(WORK, process.env.OUTDIR || 'video4');
// Parameterised so the same drive works for a 20s fixture or a 5-minute 4K clip.
const SOURCE = process.env.SOURCE || 'smoke.mp4';
const CODEC = process.env.CODEC || 'h264';
const LADDER = process.env.LADDER || 'apple-uniq-live-xs';
const TAG = process.env.TAG || '';
// A second clip, used only to show the prediction scaling. Never encoded.
const SECOND = process.env.SECOND || '';
// How the narration should SAY the source. Derived from the filename when not
// given, because a hardcoded line here once described smoke.mp4 over footage of
// a different clip — the narration must never name a file the run did not use.
const SOURCE_SAY = process.env.SOURCE_SAY ||
  SOURCE.replace(/\.[^.]+$/, '').replace(/[_-]+/g, ' ');
const CAPTION_H = 130;
const ENCODE_TIMEOUT_MS = 45 * 60 * 1000;

const INIT = `
window.__ui = () => {
  if (document.getElementById('__pwcursor')) return;
  // Reserve a strip so the caption never covers the app.
  document.body.style.paddingBottom = '${CAPTION_H}px';
  const mk = (id, css) => { const d = document.createElement('div'); d.id = id; d.style.cssText = css; document.body.appendChild(d); return d; };
  mk('__pwcursor', 'position:fixed;left:0;top:0;width:22px;height:22px;z-index:2147483647;pointer-events:none;transition:transform 420ms cubic-bezier(.4,.1,.2,1);transform:translate(60px,60px)')
    .innerHTML = '<svg width="22" height="22" viewBox="0 0 22 22"><path d="M2 2 L2 16 L6 12.5 L8.6 18.4 L11.4 17.2 L8.8 11.4 L14 11.2 Z" fill="#fff" stroke="#111" stroke-width="1.3" stroke-linejoin="round"/></svg>';
  mk('__pwring', 'position:fixed;left:0;top:0;width:34px;height:34px;margin:-6px 0 0 -6px;border:2px solid #4cc9f0;border-radius:50%;opacity:0;z-index:2147483646;pointer-events:none;transition:transform 420ms cubic-bezier(.4,.1,.2,1),opacity 300ms');
  mk('__pwbox', 'position:fixed;border:2px solid #4cc9f0;border-radius:6px;box-shadow:0 0 0 9999px rgba(3,7,15,.55),0 0 18px rgba(76,201,240,.6);opacity:0;z-index:2147483644;pointer-events:none;transition:all 420ms cubic-bezier(.4,.1,.2,1)');
  mk('__pwcap', 'position:fixed;left:0;right:0;bottom:0;height:${CAPTION_H}px;z-index:2147483645;display:flex;align-items:center;padding:0 32px;pointer-events:none;background:linear-gradient(180deg,rgba(8,14,26,.0),rgba(8,14,26,.97) 32%);color:#e6edf7;font:500 19px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;opacity:0;transition:opacity 250ms');
};
window.__moveCursor = (x, y) => { window.__ui();
  const t = 'translate(' + x + 'px,' + y + 'px)';
  document.getElementById('__pwcursor').style.transform = t;
  document.getElementById('__pwring').style.transform = t; };
window.__clickPulse = () => { const r = document.getElementById('__pwring'); if (!r) return;
  r.style.opacity = '1'; setTimeout(() => { r.style.opacity = '0'; }, 380); };
window.__say = (text) => { window.__ui(); const b = document.getElementById('__pwcap');
  b.textContent = text; b.style.opacity = text ? '1' : '0'; };
window.__spot = (rect) => { window.__ui(); const b = document.getElementById('__pwbox');
  if (!rect) { b.style.opacity = '0'; return; }
  b.style.left = (rect.x - 6) + 'px'; b.style.top = (rect.y - 6) + 'px';
  b.style.width = (rect.w + 12) + 'px'; b.style.height = (rect.h + 12) + 'px';
  b.style.opacity = '1'; };

// PRESENTATION OVERRIDE, disclosed in the notes: .file-list is capped at 400px
// with its own scrollbar, and the expanded output detail (3300px+) inherits
// that cap. At 400px the phase table alone (598px) cannot fit, so v2 spotlighted
// boxes that were clipped out of view. Lifting the cap lets the panel lay out at
// full height and the page scroll normally.
window.__expandLists = () => {
  if (document.getElementById('__pwcss')) return;
  const s = document.createElement('style');
  s.id = '__pwcss';
  s.textContent = '.file-list{max-height:none !important;overflow-y:visible !important}';
  document.head.appendChild(s);
};

// Resolve a spotlight target by key, page-side, so scrollIntoView can be called
// on the ELEMENT — that scrolls whichever ancestor actually scrolls, which
// window.scrollTo could not (the document itself does not scroll here).
window.__find = (key) => {
  const panels = [...document.querySelectorAll('.output-details .ladder-panel')];
  if (key === 'estimate') return document.getElementById('encode-estimate');
  if (key === 'fleet')    return document.getElementById('jobs-cloud-metrics');
  if (key === 'timeline-live') return document.getElementById('jobs-fleet-full');
  if (key === 'jobcard')  return (window.__jobId && document.getElementById('job-' + window.__jobId))
                                 || document.querySelector('#jobs-list .job');
  if (key === 'stages')   { const c = document.querySelector('#jobs-list .job'); 
                            return c ? c.querySelector('.job-stages, .stages, .details-toggle') : null; }
  if (key === 'ladder') return panels[0] || null;
  const run = panels[1];
  if (!run) return null;
  const kids = [...run.children];
  if (key === 'phase') return kids.find(c => (c.innerText || '').trim().startsWith('Phase')) || null;
  if (key === 'timeline') return kids.find(c => (c.innerText || '').trim().startsWith('Chunk timeline')) || null;
  return null;
};
window.__groupOf = (sel) => {
  const e = document.querySelector(sel);
  return e ? (e.closest('.control-group') || e) : null;
};
window.__scrollToSel = (sel) => {
  const el = window.__groupOf(sel);
  if (!el) return false;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  return true;
};
window.__rectOfSel = (sel) => {
  const el = window.__groupOf(sel);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: r.x, y: r.y, w: r.width, h: r.height };
};
window.__rectOfUnion = (a, b) => {
  const ga = window.__groupOf(a), gb = window.__groupOf(b);
  if (!ga || !gb) return null;
  const ra = ga.getBoundingClientRect(), rb = gb.getBoundingClientRect();
  const x = Math.min(ra.x, rb.x), y = Math.min(ra.y, rb.y);
  return { x, y, w: Math.max(ra.right, rb.right) - x, h: Math.max(ra.bottom, rb.bottom) - y };
};
window.__scrollTo = (key) => {
  const el = window.__find(key);
  if (!el) return false;
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  return true;
};
window.__rectOf = (key) => {
  const el = window.__find(key);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: r.x, y: r.y, w: r.width, h: r.height };
};
`;

const sleep = ms => new Promise(r => setTimeout(r, ms));

// On-screen text and spoken text are NOT the same string.
//
// A caption is read by a human, so it should say "4K FPV" and "VBV". A TTS
// engine handed those either spells them unpredictably or mangles them, so the
// narration file wants "four K F P V" and "V B V". Writing the captions in the
// spoken form put "four K F P V clip" on screen, which reads as nonsense —
// so the captions stay natural and this expands them on the way out.
const SPEECH = [
  [/\bAWS\b/g, 'A W S'], [/\bVBV\b/g, 'V B V'], [/\bCPU\b/g, 'C P U'],
  [/\bHLS\b/g, 'H L S'], [/\bAV1\b/g, 'A V one'], [/\bfMP4\b/g, 'fragmented M P four'],
  [/\bMP4\b/g, 'M P four'], [/\bS3\b/g, 'S three'], [/\bFPV\b/g, 'F P V'],
  [/\b4K\b/g, 'four K'], [/\bGOP\b/g, 'gop'], [/\bH\.264\b/g, 'H 264'],
  [/\bHEVC\b/g, 'H E V C'], [/\bOUTPUT_DIR\b/g, 'the output directory'],
  [/apple-uniq-live-xs/g, 'apple uniq live x s'],
  [/\b(\d+)p\b/g, '$1 p'],
];
const forSpeech = (t) => SPEECH.reduce((acc, [re, to]) => acc.replace(re, to), t);

async function main() {
  const browser = await chromium.launch({ headless: false, slowMo: 60 });
  const ctx = await browser.newContext({
    viewport: { width: 1680, height: 1080 },
    recordVideo: { dir: OUT, size: { width: 1680, height: 1080 } },
  });
  await ctx.addInitScript(INIT);
  const page = await ctx.newPage();

  const t0 = Date.now();
  const cues = [];

  // How long a caption must stay up: long enough to READ, and long enough for a
  // voice track to speak it. Narration is ~2.3 words/second at an unhurried
  // pace, plus a beat at each end to land and clear. 2.15 rather than 2.3
  // because the measured narration left six cues under a second of slack,
  // and a regenerated take that lands half a second longer would then
  // start the next caption mid-sentence. Hand-picked holds were
  // wrong in both directions — 1.2s for a sentence nobody could finish reading.
  // The caller's value is a FLOOR, never a cap.
  const MIN_HOLD_MS = 2800;
  const readMs = (text) => {
    const words = text.trim().split(/\s+/).filter(Boolean).length;
    return Math.max(MIN_HOLD_MS, Math.round(1100 + (words / 2.15) * 1000));
  };

  async function say(text, holdMs = 0) {
    const at = (Date.now() - t0) / 1000;
    const hold = text ? Math.max(holdMs, readMs(text)) : holdMs;
    if (text) {
      cues.push({ at, text, spoken: forSpeech(text), holdMs: hold, words: text.trim().split(/\s+/).length });
      console.log(`  [cue ${at.toFixed(1)}s  hold ${(hold / 1000).toFixed(1)}s] ${text}`);
    }
    await page.evaluate(t => window.__say(t), text).catch(() => {});
    if (hold) await sleep(hold);
  }
  async function point(sel, label) {
    const el = page.locator(sel).first();
    await el.waitFor({ state: 'visible', timeout: 30000 });
    await el.scrollIntoViewIfNeeded();
    const b = await el.boundingBox();
    await page.evaluate(([x, y]) => window.__moveCursor(x, y),
      [Math.round(b.x + b.width / 2), Math.round(b.y + b.height / 2)]);
    await sleep(600); console.log('  ->', label || sel); return el;
  }
  async function click(sel, label) {
    const el = await point(sel, label);
    await page.evaluate(() => window.__clickPulse());
    await sleep(180); await el.click(); await sleep(250);
    await page.mouse.move(2, 2).catch(() => {});   // no lingering title tooltip
    await sleep(200);
  }
  async function tab(name) { await click(`.tab:text-is("${name}")`, `tab: ${name}`); await sleep(900); }

  // Spotlight a target by key: scroll it into view (scrollIntoView walks every
  // scrolling ancestor, unlike window.scrollTo), re-measure AFTER the scroll
  // settles, box it, park the cursor on it, then describe it.
  async function spotlight(key, caption, holdMs) {
    const ok = await page.evaluate(k => window.__scrollTo(k), key);
    if (!ok) { console.log(`  (spotlight target '${key}' not found)`); return; }
    await sleep(1300);                       // let smooth scrolling finish
    const r = await page.evaluate(k => window.__rectOf(k), key);
    if (!r) { console.log(`  (no rect for '${key}')`); return; }
    // Verify it is actually on screen before claiming to point at it.
    const vh = page.viewportSize().height;
    const visible = r.y + r.h > 0 && r.y < vh - CAPTION_H;
    console.log(`  [spot] ${key} y=${Math.round(r.y)} h=${Math.round(r.h)} visible=${visible}`);
    await page.evaluate(rr => window.__spot(rr), r);
    await page.evaluate(([x, y]) => window.__moveCursor(x, y),
      [Math.round(r.x + Math.min(r.w / 2, 420)), Math.round(Math.max(r.y + 26, 40))]);
    await say(caption, holdMs);
    await page.evaluate(() => window.__spot(null));
  }

  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => window.__ui());
  await sleep(1200);

  await tab('Originals');
  await page.waitForSelector(`#file-list input[value="${SOURCE}"]`, { timeout: 30000 });
  await say(`Picking the same clip, this time for a cloud encode.`);
  await click(`#file-list input[value="${SOURCE}"]`, `select ${SOURCE}`);
  await sleep(800);

  async function explainSel(sel, caption) {
    await page.evaluate(x => window.__scrollToSel(x), sel);
    await sleep(700);
    const r = await page.evaluate(x => window.__rectOfSel(x), sel);
    if (r) {
      await page.evaluate(rr => window.__spot(rr), r);
      await page.evaluate(([x, y]) => window.__moveCursor(x, y),
        [Math.round(r.x + r.w / 2), Math.round(r.y + r.h - 12)]);
    }
    await say(caption);
    await page.evaluate(() => window.__spot(null));
  }

  // Read from the DOM (#356), and take only the FIRST SENTENCE of the cloud
  // option's description. Its second sentence lists the controls choosing cloud
  // reveals — spot, leave-media-in-S3, defer packaging — and this run uses none
  // of them: it downloads the media as part of the encode. Narrating controls
  // the viewer never sees used would be describing the form rather than the run.
  {
    const target = await readControl(page, '#target');
    const first = firstSentence(optionDesc(target, 'cloud')).replace(/\.$/, '');
    await explainSel('#target',
      `Target: cloud. ${first}, instead of the machines on this network.`);
  }
  await page.selectOption('#target', 'cloud');
  await sleep(1600);
  // Bring the media home rather than leaving it in S3, so the output is
  // playable the moment the run finishes.
  const skip = page.locator('#skip-media');
  if (await skip.count() && await skip.isChecked()) {
    await click('#skip-media', 'uncheck leave-media-in-S3');
  }
  await say('Codec: H.264 only. Every codec becomes its own job, so dropping HEVC halves the work.', 2200);
  for (const c of ['hevc', 'av1']) {
    if (await page.locator(`.codec-cb[value="${c}"]`).isChecked()) await click(`.codec-cb[value="${c}"]`, `uncheck ${c}`);
  }
  if (!(await page.locator(`.codec-cb[value="${CODEC}"]`).isChecked())) await click(`.codec-cb[value="${CODEC}"]`, `check ${CODEC}`);

  await say('Same ladder as before: apple-uniq-live-xs.', 2000);
  await page.waitForSelector('.ladder-cb', { timeout: 30000 });
  for (const l of await page.locator('.ladder-cb').evaluateAll(e => e.map(x => ({ v: x.value, on: x.checked })))) {
    if (l.v !== LADDER && l.on) await click(`.ladder-cb[value="${l.v}"]`, `uncheck ${l.v}`);
  }
  if (!(await page.locator(`.ladder-cb[value="${LADDER}"]`).isChecked())) await click(`.ladder-cb[value="${LADDER}"]`, `check ${LADDER}`);
  await sleep(2200);

  const est = await page.evaluate(() => {
    const e = document.getElementById('encode-estimate');
    return e && e.style.display !== 'none' ? e.innerText.replace(/\s+/g, ' ').trim() : null;
  });
  console.log('  [estimate]', est);
  global.__estimate = est;
  await spotlight('estimate', 'On a cloud target the estimate is money: spot compute plus the egress to bring the media home.', 4000);

  if (TAG) {
    // An output suffix gives this run its own directory, so a source that
    // already has comparison encodes keeps every one of them.
    await say('An output suffix gives this run its own directory, so the existing encodes of this source are untouched.', 2400);
    await point('#output-tag', 'output suffix');
    await page.fill('#output-tag', TAG);
    await page.dispatchEvent('#output-tag', 'change');
    await sleep(2200);
  }
  // Re-encode, NOT Encode. resolveCodec() skips a file whose <stem>_<codec>
  // output already exists, so plain Encode produced a 0-stage job that finished
  // in 35 seconds the moment a previous take had created this directory. The
  // output suffix already protects the 37 comparison encodes of this source;
  // the only thing Re-encode archives is our own previous demo output.
  await say('Re-encode produces every variant fresh.', 2200);
  await click('#encode-caret', 'open encode menu');
  await sleep(700);
  await click('#encode-menu .split-menu-item', 'Re-encode (replace existing)');
  await sleep(900);
  await click('#dialog-actions button:has-text("Re-encode")', 'confirm Re-encode');
  await sleep(1500);

  await say('The job is now a Step Functions execution. Every chunk becomes an AWS Batch job, launched on spot capacity.', 3200);
  // startEncode() calls switchTab('jobs') itself on a successful submit, so the
  // view is already here — clicking the tab again just looks wrong on video.
  for (let i = 0; i < 20; i++) {
    const onJobs = await page.evaluate(() => {
      const t = document.getElementById('tab-jobs');
      return !!t && getComputedStyle(t).display !== 'none';
    }).catch(() => false);
    if (onJobs) break;
    await sleep(500);
  }
  await sleep(600);

  // Fold the LOCAL fleet panel away for a cloud run. The farm workers are
  // still connected so the panel renders, but it is not what this run uses and
  // it pushes the cloud fleet down the page. Uses the app's own fold control
  // (persisted in localStorage) rather than hiding the element.
  await page.evaluate(() => {
    try {
      localStorage.setItem('fleet-collapsed-local', '1');
      localStorage.setItem('fleet-collapsed-cloud', '0');
      if (typeof applyFleetCollapse === 'function') {
        applyFleetCollapse('local'); applyFleetCollapse('cloud');
      }
    } catch (e) {}
  }).catch(() => {});
  await sleep(600);

  // Mark the encode span so the boring middle can be cut or sped up in post
  // without anyone scrubbing for it.
  const encodeStartAt = (Date.now() - t0) / 1000;
  global.__marks = { encodeStart: encodeStartAt };
  await say('Waiting for compute resources. Batch is launching spot instances and pulling the image before a single frame is encoded.');

  // Wait for OUR job to appear before touching anything, then make sure its
  // details are open so every chunk row is on screen.
  //
  // Not a click on `.details-toggle`: _detailsCollapsed() already returns
  // !_jobRunning(j), so a RUNNING job is expanded by default — clicking it
  // collapsed the very rows this is meant to show. Pinning jobDetailsPref
  // instead also survives the re-render that every SSE update triggers.
  let liveJobId = null;
  for (let i = 0; i < 40 && !liveJobId; i++) {
    liveJobId = await page.evaluate(async (SRC) => {
      const r = await fetch('/api/jobs'); const j = await r.json();
      const list = Array.isArray(j) ? j : (j.jobs || []);
      const mine = list.filter(x => ((x.config || {}).files || []).includes(SRC));
      return mine.length ? String(mine[mine.length - 1].id) : null;
    }, SOURCE);
    if (!liveJobId) await sleep(1000);
  }
  console.log('  live job:', liveJobId);
  await page.evaluate(id => { window.__jobId = id; }, liveJobId).catch(() => {});
  const openDetails = async () => {
    if (!liveJobId) return;
    await page.evaluate((id) => {
      if (typeof jobDetailsPref === 'object') jobDetailsPref[id] = 'open';
      const row = document.getElementById('job-' + id);
      if (row) row.classList.remove('details-collapsed');
    }, liveJobId).catch(() => {});
  };
  await sleep(1500);
  await openDetails();
  const shown = await page.evaluate((id) => {
    const row = document.getElementById('job-' + id);
    if (!row) return null;
    const d = row.querySelector('.job-details');
    return { collapsed: row.classList.contains('details-collapsed'),
             detailHeight: d ? Math.round(d.getBoundingClientRect().height) : 0,
             rows: row.querySelectorAll('.job-details tr, .job-details .stage-row').length };
  }, liveJobId).catch(() => null);
  console.log('  details:', JSON.stringify(shown));
  await sleep(800);

  await spotlight('fleet',
    'The cloud fleet: how many spot instances Batch has launched, and what they are costing.', 7000);
  await spotlight('timeline-live',
    'Per-instance packing and lifecycle, read from the AWS inventory.', 7500);
  await spotlight('jobcard',
    'And the job, expanded. One row per chunk, per rung, as Batch works through the fan-out.', 7500);
  await say('', 0);

  const started = Date.now();
  let last = '';
  while (Date.now() - started < ENCODE_TIMEOUT_MS) {
    const st = await page.evaluate(async (SRC) => {
      const r = await fetch('/api/jobs'); const j = await r.json();
      const list = Array.isArray(j) ? j : (j.jobs || []);
      const mine = list.filter(x => ((x.config || {}).files || []).includes(SRC));
      if (!mine.length) return { none: true };
      const j0 = mine[mine.length - 1];
      const hosts = [...new Set((j0.stages || []).map(s => s.instance).filter(Boolean))];
      return { id: j0.id, status: j0.status, pct: j0.overall_progress, hosts };
    }, SOURCE);
    if (st.none) { await sleep(3000); continue; }
    const line = `${st.status} ${st.pct != null ? Math.round(st.pct) + '%' : ''} hosts=${(st.hosts || []).join(',')}`;
    if (line !== last) { console.log('  [job]', line); last = line; }
      // FFWD must cover ONLY the encoding: the capacity wait in front and the
      // idle tail behind are real cloud costs and the reason to show this.
      if (!global.__marks.chunksStart && (st.hosts || []).length) {
        global.__marks.chunksStart = (Date.now() - t0) / 1000;
        await say('Machines are up. Every chunk is now an independent Batch job, spread across '
                + 'as many instances as Batch will give us.');
      }
    await openDetails();
      // Re-assert the local-fleet fold: the panel starts display:none and only
      // appears once fleet data arrives, so a one-shot call at tab-open runs
      // before the element exists and does nothing.
      await page.evaluate(() => {
        try {
          localStorage.setItem('fleet-collapsed-local', '1');
          if (typeof applyFleetCollapse === 'function') applyFleetCollapse('local');
        } catch (e) {}
      }).catch(() => {});          // every SSE re-render can reset the fold
    if (['done', 'failed', 'move-failed', 'cancelled'].includes(st.status)) {
      global.__jobStatus = st.status; global.__hosts = st.hosts || []; global.__jobId = st.id; break;
    }
    await sleep(4000);
  }
  global.__marks.chunksEnd = (Date.now() - t0) / 1000;
    // Linger deliberately. The instances are still up and still charged after
    // the last chunk lands; cutting here would hide what a cloud run costs.
    await spotlight('fleet',
      'The encode has finished, but the instances are still up — idle, and still billing, '
      + 'until Batch scales them down.', 9000);
    await sleep(5000);
    await spotlight('jobcard',
      'The run reports what it actually cost, against what the same ladder would cost elsewhere '
      + '— and how much of the machine time nobody was using.', 9000);
    await sleep(3000);
    global.__marks.encodeEnd = (Date.now() - t0) / 1000;
  const hosts = global.__hosts || [];
  // Truthful either way — this is the line v1 got wrong.
  await say(hosts.length > 1
    ? `The chunks were encoded across ${hosts.length} Batch instances.`
    : `Every chunk ran on one machine, ${hosts[0] || 'the master'}. The clip is short enough that no work was left to hand out.`,
    3600);

  await say('The encode is complete. The Outputs tab lists what landed in OUTPUT_DIR.', 2600);
  await tab('Outputs');
  await sleep(2200);
  const STEM = SOURCE.replace(/\.[^.]+$/, '');
  const dirName = await page.evaluate((stem) =>
    [...document.querySelectorAll('.output-dir-name')].map(e => e.textContent.trim())
      .find(n => n.toLowerCase().startsWith(stem.toLowerCase())) || null, STEM);
  console.log('  output dir:', dirName);
  if (dirName) {
    const esc = dirName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    await click(`.output-dir:has(.output-dir-name:text-matches("^${esc}"))`, `open ${dirName}`);
    await page.evaluate(() => window.__expandLists());
    await sleep(2600);
    await say('The package opens into three areas.', 2200);

    // 1 — the ladder
    await spotlight('ladder', 'First, the ladder that was encoded. Its profile and VBV settings, then every rung with its resolution and bitrate.', 6000);

    // 2 — the phase table
    await spotlight('phase', 'Second, the phase table. Every phase with its span, the CPU it consumed, and its peak memory.', 6500);

    // 3 — the chunk timeline
    await spotlight('timeline', 'Third, the chunk timeline. One lane per machine, coloured by rung, so you can see which box encoded what.', 6500);

    await say('', 0);
    await sleep(1200);
  }
  await sleep(1500);

  await ctx.close();
  await browser.close();

  const fs = require('fs');
  const files = fs.readdirSync(OUT).filter(f => f.endsWith('.webm'));
  const video = files.length ? OUT + '/' + files[0] : null;
  fs.writeFileSync(path.join(WORK, 'narration.txt'),
    cues.map(c => forSpeech(c.text)).join('\n') + '\n');
  fs.writeFileSync(path.join(WORK, 'captions.txt'), cues.map(c => c.text).join('\n') + '\n');
  const ts = s => {
    const p = (n, w = 2) => String(n).padStart(w, '0');
    return `${p(Math.floor(s / 3600))}:${p(Math.floor(s % 3600 / 60))}:${p(Math.floor(s % 60))},${p(Math.round((s % 1) * 1000), 3)}`;
  };
  fs.writeFileSync(path.join(WORK, 'narration.srt'), cues.map((c, i) =>
    `${i + 1}\n${ts(c.at)} --> ${ts(i + 1 < cues.length ? cues[i + 1].at : c.at + 5)}\n${c.text}\n`).join('\n'));
  fs.writeFileSync(path.join(WORK, 'narration-spoken.srt'), cues.map((c, i) =>
    `${i + 1}\n${ts(c.at)} --> ${ts(i + 1 < cues.length ? cues[i + 1].at : c.at + 5)}\n${forSpeech(c.text)}\n`).join('\n'));
  // Segments, so the encode middle can be sped up mechanically. `encode` runs
  // from the moment the Jobs tab opens to the moment the job goes terminal —
  // the part that is minutes of a progress bar.
  const m = global.__marks || {};
  const total = (Date.now() - t0) / 1000;
  const segments = [
    { name: 'setup',   from: 0,               to: m.encodeStart ?? 0 },
    { name: 'encode',  from: m.encodeStart ?? 0, to: m.encodeEnd ?? 0 },
    { name: 'outputs', from: m.encodeEnd ?? 0,   to: total },
  ];
  // Fast-forward the ENCODING only. Startup and the idle tail stay real-time.
  const ffwd = (m.chunksStart && m.chunksEnd && m.chunksEnd - m.chunksStart > 60)
    ? [{ from: m.chunksStart, to: m.chunksEnd, factor: 30, label: 'FFWD x30',
         text: 'The encoding itself, fast-forwarded thirty times.' }]
    : [];
  fs.writeFileSync(path.join(WORK, 'cues.json'), JSON.stringify({
    ffwd,
    video, source: SOURCE, codec: CODEC, ladder: LADDER,
    jobId: global.__jobId, jobStatus: global.__jobStatus, hosts: global.__hosts,
    estimate: global.__estimate, outputDir: dirName, segments, cues,
  }, null, 2));
  console.log('SEGMENTS:', JSON.stringify(segments));
  console.log('VIDEO:', video);
  console.log('HOSTS:', (global.__hosts || []).join(', ') || 'none');
  console.log('JOB_STATUS:', global.__jobStatus, 'OUTPUT_DIR:', dirName);
}

main().catch(e => { console.error('FAILED:', e.message); process.exit(1); });
