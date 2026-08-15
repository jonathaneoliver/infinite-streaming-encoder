'use strict';
//
// The narration reads the app's own words (#356).
//
// Until now every control description in the recorders was a hardcoded string.
// #349's issue text claimed otherwise — that `record_local.js` already read
// each control's `title` from the live DOM — and it was simply wrong: there
// were ten literals in the local tour and one in the cloud tour. So the
// narration and the UI had never shared a word. They agreed because they were
// written the same afternoon, which is not a mechanism.
//
// #349/#352/#353 put the description in the DOM once, as `data-desc`, with
// `aria-describedby` and the on-screen hint reading it. This module makes the
// recorder the third reader. A control whose description changes now changes
// the video the next time it is recorded, and a control with no description
// FAILS THE RUN instead of being silently skipped.
//
// Used by both record_local.js and record_cloud.js. Keep it dependency-free —
// it is required from a Playwright driver, not bundled.

// Every described control in the encode form. `:not(option)` because options
// carry a data-desc too and belong to their parent select, not beside it.
const CONTROL_SEL = '.controls [data-desc]:not(option)';

// Read one control's NAME and DESCRIPTION out of the live page.
//
// The name resolution mirrors what the platform does for an accessible name,
// in the same priority order, because #349 made all four routes real: a
// `<label for>`, an `aria-labelledby`, a wrapping `<label>`, or an `aria-label`.
// Before #349 most controls had none of them and this would have returned
// nothing for two thirds of the form.
async function readControl(page, sel) {
  return page.evaluate((s) => {
    const el = document.querySelector(s);
    if (!el) return null;

    const clean = (node) => {
      // A wrapping <label> may contain the sr-only description element. It is
      // supposed to sit outside the label — that was a real bug, where the
      // description was absorbed into the accessible NAME — so this is
      // belt-and-braces against it coming back.
      const c = node.cloneNode(true);
      c.querySelectorAll('.sr-only, .control-hint').forEach((x) => x.remove());
      return c.textContent.replace(/\s+/g, ' ').trim();
    };

    let name = '';
    const lb = el.getAttribute('aria-labelledby');
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) name = clean(l);
    }
    if (!name && lb) {
      const n = document.getElementById(lb);
      if (n) name = clean(n);
    }
    if (!name && el.closest('label')) name = clean(el.closest('label'));
    if (!name) name = (el.getAttribute('aria-label') || '').trim();

    const options = [...(el.options || [])].map((o) => ({
      value: o.value,
      label: o.textContent.trim(),
      desc: (o.dataset.desc || '').trim(),
    }));

    return { sel: s, id: el.id, name, desc: (el.dataset.desc || '').trim(), options };
  }, sel);
}

// Every described control, in DOM order. This is what replaces the recorder's
// hardcoded list of selectors: the tour is checked against what the page
// actually offers, so a control added tomorrow cannot be silently skipped.
async function describedControls(page) {
  return page.evaluate((s) => {
    return [...document.querySelectorAll(s)].map((el) => ({
      id: el.id,
      desc: (el.dataset.desc || '').trim(),
      hidden: !el.offsetParent && getComputedStyle(el).display === 'none',
    }));
  }, CONTROL_SEL);
}

// "Target. Where the encode runs." — the name, then the description.
//
// The name is spoken because the descriptions are written to sit UNDER a
// labelled control and do not repeat it: `data-desc` on Target is "Where the
// encode runs", which on its own names nothing. That is correct for the UI and
// incomplete for narration, and joining them here is what lets one string serve
// both.
function narrationFor(c, { name = true } = {}) {
  const d = c.desc || '';
  if (!d) return '';
  if (!name || !c.name) return d;
  // "Duration (s)" is a fine label to LOOK at and a bad one to hear — the
  // parenthetical is a unit for the eye, and read aloud it becomes "Duration s"
  // or worse. Same for "CPU (cloud)". The description that follows always says
  // the unit in words anyway ("the first N seconds").
  const spoken = c.name.replace(/\s*\([^)]*\)\s*$/, '').trim();
  // The description is written to sit under a label, so it may start lower-case
  // where the first word is a command name — promote-after's begins "rsyncs".
  // Joined after the name it is starting a sentence.
  const body = d.charAt(0).toUpperCase() + d.slice(1);
  return `${spoken}. ${body}`;
}

// The first sentence only.
//
// The cloud recording deliberately does not describe the spot / leave-media /
// defer controls — it downloads the media as part of the run, so describing
// controls it does not use would be narrating a feature the viewer never sees.
// Target's cloud option describes them in its second sentence, so the cloud
// tour takes the first and stops. Not a truncation for length: a description
// written for someone looking at the form is allowed to mention what the form
// reveals, and the video is not the form.
function firstSentence(s) {
  const m = (s || '').match(/^[^.]*\./);
  return m ? m[0].trim() : (s || '').trim();
}

function optionDesc(control, value) {
  const o = (control.options || []).find((x) => x.value === value);
  return o ? o.desc : '';
}

// Fail the RUN, not the video.
//
// A control with no description used to be invisible: the tour walked a fixed
// list, so anything not on it was skipped in silence and the omission only
// showed up as a gap nobody noticed in a 14-minute recording. Recording is
// expensive — a real encode, and for the cloud take, real money — so this
// throws BEFORE the take rather than reporting afterwards.
//
// `covered` names controls that a preceding entry already speaks for: the three
// codec checkboxes are described by the `#codec` group, so narrating each would
// say the same thing four times.
function assertTourCovers(controls, visitedIds, covered) {
  const seen = new Set([...visitedIds, ...covered]);
  const missed = controls.filter((c) => c.id && !seen.has(c.id));
  if (missed.length) {
    throw new Error(
      'The tour does not cover ' + missed.length + ' described control(s): ' +
      missed.map((c) => '#' + c.id).join(', ') +
      '\nAdd them to TOUR, or to COVERED_BY_GROUP with a reason. A control the ' +
      'app describes and the demo skips is exactly the drift this reads the DOM to prevent.');
  }
  const unknown = [...visitedIds].filter((id) => !controls.some((c) => c.id === id));
  if (unknown.length) {
    throw new Error(
      'The tour visits ' + unknown.join(', ') + ', which the page does not ' +
      'describe. Either the control was removed, or it needs a data-desc.');
  }
}

module.exports = {
  CONTROL_SEL, readControl, describedControls, narrationFor, firstSentence,
  optionDesc, assertTourCovers,
};
