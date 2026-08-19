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
const { readControl, describedControls, narrationFor, optionDesc,
        assertTourCovers } = require('./describe');

const BASE = process.env.BASE || 'http://localhost:8080';
// Every artifact goes to the WORK dir, never next to the script. These tools now
// live in the repo, and a take writes a webm, a cues.json, three text exports
// and (once narrated) ~190 wav clips — none of which belong in a git checkout.
// Defaults to the location the demo was first built in, so an existing setup
// keeps working unchanged.
const os = require('os'), path = require('path');
const WORK = process.env.DEMO_DIR || path.join(os.homedir(), 'Desktop', 'encoder-demo');
const OUT = path.join(WORK, process.env.OUTDIR || 'video4');
// Parameterised so the same drive works for a 20s fixture or a 5-minute 4K clip.
const SOURCE = process.env.SOURCE || 'smoke.mp4';
// CODEC is a LIST. The demo is now recorded as two halves that join like the
// local/cloud pair does: h264 alone, then h264+hevc. One recorder drives both,
// because the only differences are which boxes are ticked and whether the
// control tour plays — a third recorder would have been a third copy of the
// cursor/caption/spotlight scaffolding that the two existing ones already
// duplicate.
const CODECS = (process.env.CODEC || 'h264').split(',').map(c => c.trim()).filter(Boolean);
const CODEC = CODECS.join(',');
const FULL_TOUR = process.env.TOUR !== '0';
// Spoken names, not the flag values: "h264" is read aloud as a word by the TTS.
const CODEC_NAMES = { h264: 'H.264', hevc: 'HEVC', av1: 'AV1' };
const CODEC_SAY = CODECS.map(c => CODEC_NAMES[c] || c)
  .reduce((a, x, i, arr) => a + (i === 0 ? '' : i === arr.length - 1 ? ' and ' : ', ') + x, '');
// TOUR=0 skips the control tour and the estimate-scaling demo, exactly as the
// cloud half does — it opens on "Same ladder as before" rather than explaining
// twenty-two controls a viewer has just been shown. Only the SECOND half of a
// joined pair should set it.
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
// A spotlight is held for six to eight seconds while its caption plays, and on
// the Jobs tab the thing under it is ALIVE — chunk cells fill in, rows are
// added, the fleet panel repaints, and the page reflows around all of it. The
// rect was measured once, so the box sat still while its subject grew out from
// under it: the caption described one region and the frame drew another.
// Re-measure while the caption runs. __find is re-run each tick deliberately —
// a re-render replaces the element, so a cached node would go stale.
window.__spotTrack = (key) => {
  window.__spotUntrack();
  window.__spotTimer = setInterval(() => {
    const r = window.__rectOf(key);
    if (r) window.__spot(r);
  }, 400);
};
window.__spotUntrack = () => {
  if (window.__spotTimer) { clearInterval(window.__spotTimer); window.__spotTimer = null; }
};
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
  // The Ladders TAB card for window.__ladderName. Located by its <h3> text
  // because the cards are rendered from the ladder names and carry no ids.
  if (key.indexOf('ladder:') === 0) {
    const want = window.__ladderName;
    const card = [...document.querySelectorAll('#ladders-list > .panel')]
      .find(c => ((c.querySelector('h3') || {}).textContent || '').trim() === want);
    if (!card) return null;
    const part = key.slice(7);
    if (part === 'head') return card.querySelector('h3') ? card.querySelector('h3').parentElement : card;
    // The table and the charts sit side by side in one flex row; take the row
    // by the fact that it CONTAINS the table rather than by its inline style,
    // which is the kind of selector that breaks on a cosmetic edit.
    const row = [...card.querySelectorAll(':scope > div')].find(x => x.querySelector('table'));
    if (part === 'table')  return row ? row.children[0] : card;
    if (part === 'charts') return (row && row.children[1]) ? row.children[1] : card;
    return card;
  }
  if (key === 'estimate') return document.getElementById('encode-estimate');
  if (key === 'fleet')    return document.getElementById('jobs-local-metrics');
  if (key === 'fleet-live')    return document.getElementById('jobs-local-live')
                                     || document.getElementById('jobs-local-fleet');
  // The LANES, not the whole panel. Falls back to the panel so a recording made
  // against an older server still frames something rather than skipping the beat.
  if (key === 'timeline-live') return document.getElementById('jobs-local-lanes')
                                     || document.getElementById('jobs-local-fleet');
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
    await page.evaluate(k => window.__spotTrack(k), key);
    await say(caption, holdMs);
    await page.evaluate(() => { window.__spotUntrack(); window.__spot(null); });
  }

  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.evaluate(() => window.__ui());
  await sleep(1200);
  await say('Infinite Streaming Encoder. A Go control plane driving a Python encoder across a local farm.', 3400);

  await say('The Originals tab lists the source files on the master.', 1800);
  await tab('Originals');
  await page.waitForSelector(`#file-list input[value="${SOURCE}"]`, { timeout: 30000 });
  await say(`Selecting ${SOURCE_SAY}.`, 1200);
  await click(`#file-list input[value="${SOURCE}"]`, `select ${SOURCE}`);
  await sleep(800);

  // --- Tour the controls before touching any of them ----------------------
  //
  // Each line is grounded in the control's OWN tooltip, so the narration cannot
  // drift from what the app claims the control does. Boxes the whole labelled
  // .control-group rather than the bare input, so the viewer sees the label too.
  async function explain(sel, caption) {
    const ok = await page.evaluate(x => window.__scrollToSel(x), sel);
    if (!ok) { console.log(`  (control ${sel} not found)`); return; }
    await sleep(900);
    const r = await page.evaluate(x => window.__rectOfSel(x), sel);
    if (!r) return;
    await page.evaluate(rr => window.__spot(rr), r);
    await page.evaluate(([x, y]) => window.__moveCursor(x, y),
      [Math.round(r.x + r.w / 2), Math.round(r.y + r.h - 12)]);
    // Deliberately NOT page.hover(): the browser's own title tooltip pops up
    // at the pointer and lands on top of the caption strip. The caption already
    // says what the control does, so the native one is redundant noise.
    await page.mouse.move(2, 2).catch(() => {});
    console.log(`  [explain] ${sel}`);
    await say(caption);
    await page.evaluate(() => window.__spot(null));
    await sleep(250);
  }

  // Two adjacent controls that only make sense together get one box and one
  // line, rather than two boxes saying half a thing each.
  async function explainPair(selA, selB, caption) {
    await page.evaluate(x => window.__scrollToSel(x), selA);
    await sleep(900);
    const r = await page.evaluate(([a, b]) => window.__rectOfUnion(a, b), [selA, selB]);
    if (!r) { console.log(`  (pair ${selA}/${selB} not found)`); return; }
    await page.evaluate(rr => window.__spot(rr), r);
    await page.evaluate(([x, y]) => window.__moveCursor(x, y),
      [Math.round(r.x + r.w / 2), Math.round(r.y + r.h - 12)]);
    console.log(`  [explain] ${selA} + ${selB}`);
    await say(caption);
    await page.evaluate(() => window.__spot(null));
    await sleep(250);
  }

  const settle = async () => {
    let prev = null;
    for (let i = 0; i < 24; i++) {                  // multi-ladder = one fetch per ladder
      await sleep(700);
      const now = await page.evaluate(() => {
        const e = document.getElementById('encode-estimate');
        return e && e.style.display !== 'none' ? e.innerText.replace(/\s+/g, ' ').trim() : '';
      });
      if (now && now === prev) return now;
      prev = now;
    }
    return prev || '';
  };
  // The local estimate names its basis after the figure, and the phrase CHANGES
  // with the selection: one ladder reads "~45s on the local fleet", several read
  // "~90s 2 ladders priced". Matching only the first spelling returned null for
  // every multi-ladder reading — so the scaling segment, whose whole point is
  // watching the number grow, dropped its largest number and ended mid-sentence
  // on "…4 separate jobs, one output each." Silently, because the caption
  // degrades gracefully when there is no figure.
  const headline = (t) => {
    const m = t.match(/→\s*~?([^·]+?)\s+(?:on the local fleet|\d+ ladders priced)/)
           || t.match(/→\s*(\$[\d.]+)/);
    return m ? m[1].trim() : null;
  };
  // The note ends with "Resolution tiers above describe <ladder>." — guidance
  // for someone looking at the form, and a non-sequitur when spoken. Keep the
  // job/output count, drop the tail.
  const noteOf = async () => {
    const raw = await page.evaluate(() => {
      const n = document.getElementById('ladder-multi-note');
      return n && n.style.display !== 'none' ? n.textContent.replace(/\s+/g, ' ').trim() : '';
    });
    return raw.replace(/\s*Resolution tiers[^.]*\.\s*$/, '').trim();
  };
  const spotEstimate = async (caption) => {
    const r = await page.evaluate(() => {
      const e = document.getElementById('encode-estimate'); if (!e) return null;
      const n = document.getElementById('ladder-multi-note');
      const re = e.getBoundingClientRect();
      const rn = (n && n.style.display !== 'none') ? n.getBoundingClientRect() : re;
      const x = Math.min(re.x, rn.x), y = Math.min(re.y, rn.y);
      return { x, y, w: Math.max(re.right, rn.right) - x, h: Math.max(re.bottom, rn.bottom) - y };
    });
    if (r) await page.evaluate(rr => window.__spot(rr), r);
    await say(caption);
    await page.evaluate(() => { window.__spotUntrack(); window.__spot(null); });
  };

  if (FULL_TOUR) {
  // --- The control tour: the app's own words ------------------------------
  //
  // Nothing below writes a control description. Each line is read out of the
  // live DOM (`data-desc`, plus the name from whatever labels the control), so
  // the narration cannot claim something the UI does not. See describe.js.
  //
  // TOUR is the ORDER and the choreography — which controls to dwell on, and
  // where to show options by selecting them. It is NOT the list of what has a
  // description: that comes from the page, and assertTourCovers throws before
  // the take if the two disagree.
  const described = await describedControls(page);
  const TOUR = ['target', 'chunk-duration', 'codec', 'ladder-list', 'max-res',
                'min-res', 'time-limit', 'output-tag', 'group-rungs', 'burnin',
                'promote-after'];
  const COVERED_BY_GROUP = [
    // The three codec checkboxes are spoken for by the #codec group; narrating
    // each would say "each codec is its own job" four times.
    ...described.filter(c => /^ctl-desc-/.test(c.id)).map(c => c.id),
    // Cloud-only, and this is the local take. They are shown when Target
    // switches to cloud below, but the local tour does not dwell on them.
    'use-spot', 'skip-media', 'defer-packaging',
    // Hidden: retired legacy (one compute env means it can do nothing), and
    // developer-only behind ?developer=1.
    'cpu-arch', 'measure-vmaf', 'vmaf-prescale',
    // Selects whose options are read from the ladder, described by their own
    // controls above.
    'hls-format', 'padding',
  ];
  assertTourCovers(described, TOUR, COVERED_BY_GROUP);

  // Establish the panel before walking its controls — the tour previously cut
  // straight from picking a file to explaining Target, with nothing saying what
  // the viewer was now looking at.
  await explain('#encode-options',
    'These are the encoding options. Everything that decides what gets produced, and where it runs.');
  await say('Here is what each control does.');

  const target = await readControl(page, '#target');
  await explain('#target', narrationFor(target));
  // A native select popup is an OS widget and does not appear in a page
  // recording, so the options are shown by SELECTING them rather than by
  // opening the list — and each option speaks its OWN data-desc.
  await page.selectOption('#target', 'cloud');
  await sleep(1200);
  { const h = headline(await settle());
    await spotEstimate(`${optionDesc(target, 'cloud')} The estimate switches from time to money. ${h ? 'About ' + h + ' for this clip.' : ''}`); }
  await page.selectOption('#target', 'local');
  await sleep(1200);
  await say('Returning to a local encode. And the estimate is time again.', 2600);

  const chunking = await readControl(page, '#chunk-duration');
  await explain('#chunk-duration', narrationFor(chunking));
  // Same reason as Target: show the options by selecting them. Only the two
  // that matter — the ends of the range, not every rung of it.
  {
    const orig = await page.inputValue('#chunk-duration');
    for (const v of ['dynamic', '12']) {
      await page.selectOption('#chunk-duration', v);
      await sleep(1000);
      await say(optionDesc(chunking, v));
    }
    await page.selectOption('#chunk-duration', orig);
    await sleep(600);
  }

  for (const id of ['codec', 'ladder-list']) {
    await explain('#' + id, narrationFor(await readControl(page, '#' + id)));
  }
  // Max and min are one idea shown as two controls, so they are spotlit
  // together and described from the pair — the second description completes
  // the first ("…with Max Resolution it selects a contiguous band").
  {
    const mx = await readControl(page, '#max-res');
    const mn = await readControl(page, '#min-res');
    await explainPair('#max-res', '#min-res', `${narrationFor(mx)} ${narrationFor(mn)}`);
  }
  for (const id of ['time-limit', 'output-tag', 'group-rungs', 'burnin', 'promote-after']) {
    await explain('#' + id, narrationFor(await readControl(page, '#' + id)));
  }
  }  // end FULL_TOUR: the control tour

  // --- How the plan scales -------------------------------------------------
  //
  // Every figure spoken here is READ BACK from the page after the change
  // settles, never precomputed. If the estimate says something different from
  // what the narration claims, the narration is what changes.
  // The scaling demo MUST start from the run's own selection. That used to be
  // the trap here: the codec checkboxes defaulted to H.264 *and* HEVC, so
  // running this first made the baseline two codecs while the narration said
  // one — and the 'add HEVC' click then REMOVED it, halving the jobs under a
  // caption claiming they doubled.
  //
  // The form now defaults to H.264 alone, so the narration states that rather
  // than claiming a drop that no longer happens. The normalising clicks below
  // STAY: they are what makes the baseline true whatever state the form is in,
  // and they are the reason a changed default cannot silently re-open that gap.
  await say(FULL_TOUR ? 'First, the settings for this run.'
                     : 'Same clip and the same ladder as before. One thing changes.', 2400);
  // Says what is actually selected. A hardcoded sentence here is what made the
  // old version claim HEVC was being dropped after the form stopped selecting
  // it — the narration must follow the run, not the other way round.
  await point('#codec', 'codec');
  await say(CODECS.length > 1
    ? `Codec: ${CODEC_SAY}. Every codec is its own job, so this run is ${CODECS.length} jobs, one output directory each.`
    : `Codec: ${CODEC_SAY}, which is what the form starts with. Every codec is its own job, so this run is one job.`,
    2600);
  for (const c of ['h264', 'hevc', 'av1']) {
    const want = CODECS.includes(c);
    const on = await page.locator(`.codec-cb[value="${c}"]`).isChecked();
    if (want !== on) await click(`.codec-cb[value="${c}"]`, `${want ? 'check' : 'uncheck'} ${c}`);
  }

  await page.waitForSelector('.ladder-cb', { timeout: 30000 });
  // The ladder LIST, not the individual checkbox: the sentence is about the
  // ladder as a thing, and the list is what the viewer needs to find.
  await point('#ladder-list', 'ladder list');
  await say('The ladder is apple-uniq-live-xs. It carries the rungs and the delivery profile together.', 2600);
  for (const l of await page.locator('.ladder-cb').evaluateAll(e => e.map(x => ({ v: x.value, on: x.checked })))) {
    if (l.v !== LADDER && l.on) await click(`.ladder-cb[value="${l.v}"]`, `uncheck ${l.v}`);
  }
  if (!(await page.locator(`.ladder-cb[value="${LADDER}"]`).isChecked())) await click(`.ladder-cb[value="${LADDER}"]`, `check ${LADDER}`);
  await sleep(2200);

  // --- The ladder itself ---------------------------------------------------
  //
  // The narration has just claimed a ladder "carries the rungs and the delivery
  // profile together" and then never showed either. They live on the Ladders
  // tab, which already explains its own table and charts in a legend line — so
  // the captions here say what that legend says rather than inventing a second
  // account of the same colours. FIRST half only: the second one has just been
  // told this ladder is unchanged.
  if (FULL_TOUR) {
    await page.evaluate(n => { window.__ladderName = n; }, LADDER);
    await say('Before running it — this is the ladder we are about to encode with.', 2800);
    await tab('Ladders');
    await sleep(1800);

    // Counted from the page, not assumed. A ladder edited between takes would
    // otherwise be narrated with the previous take's rung count.
    const rungRows = await page.evaluate(() => {
      const t = window.__find('ladder:table');
      return t ? t.querySelectorAll('tbody tr').length : 0;
    });

    await spotlight('ladder:card',
      `${LADDER}. One object holding both halves: the rungs, and the delivery profile they are encoded to.`, 6000);
    // The profile's NUMBERS are on screen and read badly aloud — "1/2/6s" and
    // "0.25x" are for the eye. Name what the line contains instead.
    await spotlight('ladder:head',
      'Its delivery profile: the VBV shaping, the segment and partial lengths, the GOP, and the suffix. '
      + 'A ladder that pins its segment length is tagged with that length and served exactly as encoded. '
      + 'This one pins nothing and is tagged _xs — the flexible base, one encode that go-live re-chops into one, two and six second segments.', 14000);
    await spotlight('ladder:table',
      `The rungs${rungRows ? `, ${rungRows} of them` : ''}. Each one's resolution and target bitrate, and the step up from the rung below. Colour marks a step too small for the rung to earn its place, or a jump big enough to leave a hole.`, 8500);
    await spotlight('ladder:charts',
      'And the shaping. Each bar runs from average to peak bitrate over a one, two and six second window, against Apple\'s limits — peak within one and a quarter times average for live, twice that for video on demand. '
      + 'This profile sits right on the live limit even at a one second chop. A ladder pinned to six seconds sits on the same limit at six — but chop it to one and its peak doubles. '
      + 'That is the whole reason only this one may be re-chopped.', 15000);

    await tab('Originals');
    await sleep(1600);
  }

  if (FULL_TOUR) {
  await say('The panel predicts the whole plan before anything runs. Watch it as the selection grows.');
  const base = headline(await settle());
  await spotEstimate(`One clip, one codec, one ladder. ${base ? 'About ' + base + '.' : ''}`);

  if (SECOND) {
    await click(`#file-list input[value="${SECOND}"]`, `select ${SECOND}`);
    const t = await settle(); const h = headline(t); const n = await noteOf();
    await spotEstimate(`${n ? n + ' ' : ''}${h ? 'The prediction is now about ' + h + '.' : ''}`);
  }

  await click('.codec-cb[value="hevc"]', 'add hevc');
  { const t = await settle(); const h = headline(t); const n = await noteOf();
    await spotEstimate(`Adding HEVC doubles the jobs. ${n ? n + ' ' : ''}${h ? 'About ' + h + ' now, because HEVC is far slower than H.264.' : ''}`); }

  // A SIBLING of the chosen ladder — same rung table, different segment/GOP/VBV
  // — because the caption calls it "a different delivery profile". Picking the
  // first ladder that merely differs selected `apple`, a different rung table
  // entirely, which made that sentence untrue.
  const second = await page.locator('.ladder-cb').evaluateAll((els, keep) => {
    const stem = keep.replace(/-(xs|1s|2s|6s)$/, '');
    const sib = els.find(e => e.value !== keep && e.value.startsWith(stem + '-'));
    return (sib || els.find(e => e.value !== keep) || {}).value;
  }, LADDER);
  if (second) {
    await click(`.ladder-cb[value="${second}"]`, `add ladder ${second}`);
    const t = await settle(); const h = headline(t); const n = await noteOf();
    await spotEstimate(`A second ladder encodes the same sources again as a different delivery profile. ${n ? n + ' ' : ''}${h ? 'About ' + h + '.' : ''}`);
  }

  // Back to the run we actually want.
  await say('Back to one clip, one codec, one ladder for this run.');
  if (second) await click(`.ladder-cb[value="${second}"]`, `remove ladder ${second}`);
  await click('.codec-cb[value="hevc"]', 'remove hevc');
  if (SECOND) await click(`#file-list input[value="${SECOND}"]`, `deselect ${SECOND}`);
  { const t = await settle(); const h = headline(t);
    await spotEstimate(`${h ? 'Back to about ' + h + '.' : 'Back to the original plan.'}`); }
  }  // end FULL_TOUR: the estimate-scaling demo

  const est = await page.evaluate(() => {
    const e = document.getElementById('encode-estimate');
    return e && e.style.display !== 'none' ? e.innerText.replace(/\s+/g, ' ').trim() : null;
  });
  console.log('  [estimate]', est);
  global.__estimate = est;
  await spotlight('estimate', 'On a local target the estimate predicts time, not money.', 4000);

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
  await say('Re-encode replaces the existing output. The previous copy is archived, never deleted.', 2800);
  await click('#encode-caret', 'open encode menu');
  await sleep(700);
  await click('#encode-menu .split-menu-item', 'Re-encode (replace existing)');
  await sleep(900);
  await click('#dialog-actions button:has-text("Re-encode")', 'confirm Re-encode');
  await sleep(1500);

  await say('The job is now a Temporal workflow. Chunks go onto one queue that every worker polls.', 3200);
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

  // Mark the encode span so the boring middle can be cut or sped up in post
  // without anyone scrubbing for it.
  const encodeStartAt = (Date.now() - t0) / 1000;
  global.__marks = { encodeStart: encodeStartAt };

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
    'The fleet summary: which machines are up, and how many cores are busy.', 6000);
  await spotlight('fleet-live',
    'What each machine is running right now. Its cores, how much of them is in use, and the chunks it has in flight.', 7000);
  await spotlight('timeline-live',
    'And the machine timeline: what ran where, over time. One lane per box, filling in as chunks land, so you can see which machine sat idle while another was still the tail.', 8000);
  // "One row per chunk, per rung" was WRONG, and had been since v1 of this
  // driver — it survived two rewrites and being brought in-tree unread. The
  // page groups by codec:tier (groupStagesForDisplay) and renders each chunk as
  // a CELL inside its variant's row, so a full ladder is ~14 rows, not 336.
  // Anyone watching the screen could see the claim was false; nobody checked.
  await spotlight('jobcard',
    'And the job itself, expanded into its stages. One row per variant, one cell per chunk, filling in as each completes.', 7500);
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
    await openDetails();          // every SSE re-render can reset the fold
    if (['done', 'failed', 'move-failed', 'cancelled'].includes(st.status)) {
      global.__jobStatus = st.status; global.__hosts = st.hosts || []; global.__jobId = st.id; break;
    }
    await sleep(4000);
  }
  global.__marks.encodeEnd = (Date.now() - t0) / 1000;
  const hosts = global.__hosts || [];
  // Did it actually finish? Everything below describes a package that only
  // exists if it did. A cloud rehearsal that was stopped from the AWS tab still
  // reached here, said "the encode is complete", opened a directory left behind
  // by an EARLIER run, and toured it — a walkthrough of someone else's output,
  // narrated as this run's, and the process still exited 0.
  // Declared out here, not inside the branch: the console summary and the
  // cues.json write below both read them, on either path.
  let dirs = [];
  let dirName = null;
  const finished = global.__jobStatus === 'done';
  if (!finished) {
    await say(`The encode did not finish — it ended ${global.__jobStatus || 'unknown'}. `
      + 'There is no output to show for this run.', 4000);
    global.__failed = true;
  } else {
  // Truthful either way — this is the line v1 got wrong.
  await say(hosts.length > 1
    ? `The chunks were encoded in parallel across ${hosts.length} machines: ${hosts.join(', ')}.`
    : `Every chunk ran on one machine, ${hosts[0] || 'the master'}. The clip is short enough that no work was left to hand out.`,
    3600);

  await say('The encode is complete. The Outputs tab lists what landed in OUTPUT_DIR.', 2600);
  await tab('Outputs');
  await sleep(2200);
  const STEM = SOURCE.replace(/\.[^.]+$/, '');
  // ALL of them, not the first match. The second half of a joined pair produces
  // one directory per codec, and "how many landed" is the whole point of that
  // half — read off the page rather than asserted, so a codec that produced
  // nothing is reported instead of narrated as a success.
  dirs = await page.evaluate(([stem, tag]) =>
    [...document.querySelectorAll('.output-dir-name')].map(e => e.textContent.trim())
      .filter(n => n.toLowerCase().startsWith(stem.toLowerCase()))
      // The suffix is what makes a directory THIS run's. Matching the stem alone
      // picked up an earlier take's output and toured it as if it were ours.
      .filter(n => !tag || n.toLowerCase().endsWith('_' + tag.toLowerCase())), [STEM, TAG]);
  dirName = dirs[0] || null;
  console.log('  output dirs:', JSON.stringify(dirs));
  global.__dirs = dirs;

  // The three-area walkthrough belongs to the FIRST half only. Repeating it
  // verbatim after a fade-to-black is the same tour twice, and it buries what
  // the second half exists to show: that adding a codec added a package.
  if (!FULL_TOUR) {
    await say(dirs.length > 1
      ? `${dirs.length} directories, one per codec: ${dirs.join(', ')}.`
      : `One directory: ${dirName || 'none'}. The second codec produced no output.`,
      5000);
    // Open the one this half ADDED, not the one already toured. Derived from the
    // codecs actually selected, so it keeps working when the added codec is AV1.
    const added = dirs.find(d => CODECS.slice(1).some(c => d.includes(c)));
    if (added) {
      const escA = added.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      await click(`.output-dir:has(.output-dir-name:text-matches("^${escA}"))`, `open ${added}`);
      await page.evaluate(() => window.__expandLists());
      await sleep(2400);
      await say('The package the second job produced — same ladder, same rungs, its own encode.', 4000);
      await sleep(1200);
    }
  } else if (dirName) {
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
  }  // end: only describe outputs when the encode actually finished

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
    { name: 'setup',   from: 0,                  to: m.encodeStart ?? 0 },
    { name: 'encode',  from: m.encodeStart ?? 0, to: m.encodeEnd ?? 0 },
    { name: 'outputs', from: m.encodeEnd ?? 0,   to: total },
  ];
  fs.writeFileSync(path.join(WORK, 'cues.json'), JSON.stringify({
    video, source: SOURCE, codec: CODEC, ladder: LADDER,
    jobId: global.__jobId, jobStatus: global.__jobStatus, hosts: global.__hosts,
    estimate: global.__estimate, outputDir: dirName, outputDirs: global.__dirs,
    segments, cues,
  }, null, 2));
  console.log('SEGMENTS:', JSON.stringify(segments));
  console.log('VIDEO:', video);
  console.log('HOSTS:', (global.__hosts || []).join(', ') || 'none');
  console.log('JOB_STATUS:', global.__jobStatus, 'OUTPUT_DIR:', dirName);
  if (global.__failed) {
    console.log('RECORDING IS NOT USABLE: the encode ended', global.__jobStatus);
    process.exitCode = 1;   // after the artifacts are written, so they survive
  }
}

main().catch(e => { console.error('FAILED:', e.message); process.exit(1); });
