#!/usr/bin/env python3
"""Render a per-frame VMAF series as a scope-style timeline you can actually read.

The pooled score says a rendition is "82". This says WHEN it was 82 — and a
quality pulse is a shape in time, so it needs a time axis to be seen at all.

Three panels:
  overview   the whole clip, min/max enveloped per pixel column, with a
             draggable window
  scope      every frame in that window, with a graticule line at each IDR —
             this is where a 1s pulse becomes visible
  fold       mean VMAF at each position within one GOP, averaged over every
             cycle: the content averages out, a phase-locked pulse does not

Usage:
    python3 scripts/vmaf_timeline_html.py --scores scores.json --gop-frames 30 \\
        --fps 30 --out timeline.html [--title "h264 234p"] \\
        [--segment-frames 180] [--chunk-frames 360]

Get scores.json from analyze_vmaf_periodicity.py --save-scores.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze_vmaf_periodicity import fold  # noqa: E402

# Slots 1 and 2 of the validated categorical palette (light | dark). Only the
# trace uses a series hue; the graticule and the fold's mean line are neutrals,
# because they are reference furniture rather than a second series.
TRACE_LIGHT, TRACE_DARK = "#2a78d6", "#3987e5"
WARN_LIGHT, WARN_DARK = "#eb6834", "#d95926"

TEMPLATE = """<style>
  /* Light is the base palette; dark redefines ONLY tokens, under both the OS
     media query and the explicit stamp, so the un-stamped default resolves. */
  :root {
    --bg:        #f4f5f7;
    --panel:     #ffffff;
    --ink:       #14171c;
    --ink-2:     #4a515c;
    --ink-3:     #838b98;
    --rule:      #dfe3e9;
    --graticule: #c9d1dc;
    --trace:     %(trace_light)s;
    --warn:      %(warn_light)s;
    --window:    rgba(42,120,214,.14);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg:        #0e1114;
      --panel:     #161a1f;
      --ink:       #e9edf2;
      --ink-2:     #a3adba;
      --ink-3:     #6b7684;
      --rule:      #262c34;
      --graticule: #2f3742;
      --trace:     %(trace_dark)s;
      --warn:      %(warn_dark)s;
      --window:    rgba(57,135,229,.20);
    }
  }
  :root[data-theme="dark"] {
    --bg:        #0e1114;
    --panel:     #161a1f;
    --ink:       #e9edf2;
    --ink-2:     #a3adba;
    --ink-3:     #6b7684;
    --rule:      #262c34;
    --graticule: #2f3742;
    --trace:     %(trace_dark)s;
    --warn:      %(warn_dark)s;
    --window:    rgba(57,135,229,.20);
  }

  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 28px 20px 56px; }
  header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 12px 18px;
           border-bottom: 1px solid var(--rule); padding-bottom: 14px; }
  h1 { font-size: 19px; margin: 0; font-weight: 620; letter-spacing: -.01em; }
  .sub { color: var(--ink-2); font-size: 13px; }
  .mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
          font-variant-numeric: tabular-nums; }

  .stats { display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0 4px; }
  .stat { background: var(--panel); border: 1px solid var(--rule); border-radius: 6px;
          padding: 9px 13px; min-width: 104px; }
  .stat .k { font-size: 10.5px; text-transform: uppercase; letter-spacing: .07em;
             color: var(--ink-3); }
  .stat .v { font-size: 18px; margin-top: 2px; }

  .panel { background: var(--panel); border: 1px solid var(--rule); border-radius: 8px;
           padding: 14px 14px 10px; margin-top: 16px; }
  .panel h2 { font-size: 12px; text-transform: uppercase; letter-spacing: .08em;
              color: var(--ink-3); margin: 0 0 10px; font-weight: 600; }
  .panel .hint { color: var(--ink-3); font-size: 12px; margin: 8px 0 0; }
  canvas { display: block; width: 100%%; }
  #overview { cursor: grab; }
  #overview.dragging { cursor: grabbing; }
  .row { display: grid; grid-template-columns: 1fr; gap: 16px; }
  @media (min-width: 900px) { .row { grid-template-columns: 1.55fr 1fr; } }

  .readout { position: absolute; pointer-events: none; background: var(--panel);
             border: 1px solid var(--rule); border-radius: 5px; padding: 5px 8px;
             font-size: 12px; box-shadow: 0 2px 10px rgba(0,0,0,.16); opacity: 0;
             transition: opacity .08s; white-space: nowrap; }
  .scopewrap { position: relative; }
  .verdict { margin-top: 16px; padding: 12px 14px; border-left: 3px solid var(--warn);
             background: var(--panel); border-radius: 0 6px 6px 0; }
  table { border-collapse: collapse; font-size: 12.5px; width: 100%%; }
  th, td { text-align: right; padding: 3px 10px 3px 0; }
  th:first-child, td:first-child { text-align: left; }
  th { color: var(--ink-3); font-weight: 600; }
  .tablewrap { overflow-x: auto; margin-top: 10px; }
</style>

<div class="wrap">
  <header>
    <h1>%(title)s</h1>
    <span class="sub mono">%(n)d frames &middot; %(fps).3f fps &middot; GOP %(gop)d frames (%(gop_s).3f s)</span>
  </header>

  <div class="stats">%(stats)s</div>

  <div class="panel">
    <h2>Whole clip &mdash; drag to move the window</h2>
    <canvas id="overview" height="90"></canvas>
    <p class="hint">Enveloped min/max per pixel column, never stride-sampled &mdash;
       a one-second pulse would alias away entirely under stride sampling.</p>
  </div>

  <div class="row">
    <div class="panel scopewrap">
      <h2>Scope &mdash; every frame in the window</h2>
      <canvas id="scope" height="230"></canvas>
      <div class="readout" id="readout"></div>
      <p class="hint">Vertical lines mark IDR frames. Look at the slope
         <em>immediately after</em> each one.</p>
    </div>
    <div class="panel">
      <h2>Folded over the GOP</h2>
      <canvas id="foldc" height="230"></canvas>
      <p class="hint">Mean at each position within a GOP, over %(cycles)d cycles.
         Content averages out; a phase-locked pulse does not.</p>
    </div>
  </div>

  <div class="verdict">%(verdict)s</div>

  <div class="panel">
    <h2>Folded values</h2>
    <div class="tablewrap"><table class="mono">
      <thead><tr><th>position in GOP</th>%(fold_head)s</tr></thead>
      <tbody><tr><td>mean VMAF</td>%(fold_body)s</tr></tbody>
    </table></div>
  </div>
</div>

<script>
const S = %(scores)s, GOP = %(gop)d, FPS = %(fps)f, FOLD = %(folded)s;
const css = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
let win = [0, Math.min(S.length, GOP * 10)];

function fit(c) {
  const d = window.devicePixelRatio || 1, w = c.clientWidth;
  c.width = w * d; c.height = c.getAttribute('height') * d;
  const x = c.getContext('2d'); x.setTransform(d, 0, 0, d, 0, 0);
  return [x, w, +c.getAttribute('height')];
}
const range = a => { let lo = Infinity, hi = -Infinity;
  for (const v of a) { if (v < lo) lo = v; if (v > hi) hi = v; }
  if (hi - lo < 1) { lo -= 1; hi += 1; } const pad = (hi - lo) * .08;
  return [lo - pad, hi + pad]; };

function drawOverview() {
  const c = document.getElementById('overview'), [x, w, h] = fit(c);
  x.clearRect(0, 0, w, h);
  const [lo, hi] = range(S), y = v => h - 6 - (v - lo) / (hi - lo) * (h - 12);
  // min/max envelope per column: a stride sample would hide the very thing
  // this page exists to show.
  x.strokeStyle = css('--trace'); x.lineWidth = 1; x.beginPath();
  for (let px = 0; px < w; px++) {
    const a = Math.floor(px / w * S.length), b = Math.max(a + 1, Math.floor((px + 1) / w * S.length));
    let mn = Infinity, mx = -Infinity;
    for (let i = a; i < b && i < S.length; i++) { if (S[i] < mn) mn = S[i]; if (S[i] > mx) mx = S[i]; }
    if (mn === Infinity) continue;
    x.moveTo(px + .5, y(mx)); x.lineTo(px + .5, y(mn));
  }
  x.stroke();
  const wx = f => f / S.length * w;
  x.fillStyle = css('--window'); x.fillRect(wx(win[0]), 0, Math.max(2, wx(win[1] - win[0])), h);
  x.strokeStyle = css('--trace'); x.lineWidth = 1.5;
  x.strokeRect(wx(win[0]) + .5, .5, Math.max(2, wx(win[1] - win[0])) - 1, h - 1);
}

function drawScope() {
  const c = document.getElementById('scope'), [x, w, h] = fit(c);
  x.clearRect(0, 0, w, h);
  const seg = S.slice(win[0], win[1]); if (!seg.length) return;
  const [lo, hi] = range(seg);
  const px = i => (i / (seg.length - 1 || 1)) * (w - 44) + 38;
  const py = v => h - 22 - (v - lo) / (hi - lo) * (h - 40);
  x.strokeStyle = css('--graticule'); x.lineWidth = 1;
  for (let f = Math.ceil(win[0] / GOP) * GOP; f < win[1]; f += GOP) {
    const gx = px(f - win[0]); x.beginPath(); x.moveTo(gx, 6); x.lineTo(gx, h - 22); x.stroke();
  }
  x.fillStyle = css('--ink-3'); x.font = '11px ui-monospace, monospace';
  [hi, lo].forEach(v => x.fillText(v.toFixed(1), 2, py(v) + 4));
  x.fillText(((win[0]) / FPS).toFixed(2) + 's', 38, h - 6);
  x.fillText(((win[1]) / FPS).toFixed(2) + 's', w - 46, h - 6);
  x.strokeStyle = css('--trace'); x.lineWidth = 2; x.lineJoin = 'round'; x.beginPath();
  seg.forEach((v, i) => i ? x.lineTo(px(i), py(v)) : x.moveTo(px(i), py(v)));
  x.stroke();
  c._m = { seg, px, py, lo, hi, w, h };
}

function drawFold() {
  const c = document.getElementById('foldc'), [x, w, h] = fit(c);
  x.clearRect(0, 0, w, h);
  const [lo, hi] = range(FOLD);
  const px = i => (i / (FOLD.length - 1 || 1)) * (w - 50) + 42;
  const py = v => h - 22 - (v - lo) / (hi - lo) * (h - 40);
  const mean = FOLD.reduce((a, b) => a + b, 0) / FOLD.length;
  x.strokeStyle = css('--graticule'); x.setLineDash([4, 4]); x.lineWidth = 1;
  x.beginPath(); x.moveTo(42, py(mean)); x.lineTo(w - 8, py(mean)); x.stroke();
  x.setLineDash([]);
  x.fillStyle = css('--ink-3'); x.font = '11px ui-monospace, monospace';
  x.fillText(hi.toFixed(1), 2, py(hi) + 4); x.fillText(lo.toFixed(1), 2, py(lo) + 4);
  x.fillText('IDR', 38, h - 6); x.fillText('+' + (FOLD.length - 1), w - 40, h - 6);
  x.strokeStyle = css('--trace'); x.lineWidth = 2; x.lineJoin = 'round'; x.beginPath();
  FOLD.forEach((v, i) => i ? x.lineTo(px(i), py(v)) : x.moveTo(px(i), py(v)));
  x.stroke();
  x.fillStyle = css('--trace');
  x.beginPath(); x.arc(px(0), py(FOLD[0]), 4, 0, 7); x.fill();
}

const ro = document.getElementById('readout');
document.getElementById('scope').addEventListener('mousemove', e => {
  const c = e.currentTarget, m = c._m; if (!m) return;
  const r = c.getBoundingClientRect(), rel = e.clientX - r.left;
  const i = Math.round((rel - 38) / (m.w - 44) * (m.seg.length - 1));
  if (i < 0 || i >= m.seg.length) { ro.style.opacity = 0; return; }
  const f = win[0] + i;
  ro.style.opacity = 1;
  ro.style.left = Math.min(r.width - 150, m.px(i) + 10) + 'px';
  ro.style.top = (m.py(m.seg[i]) - 6) + 'px';
  ro.innerHTML = '<b class="mono">' + m.seg[i].toFixed(2) + '</b> VMAF<br>' +
    '<span class="mono">frame ' + f + ' &middot; +' + (f %% GOP) + ' in GOP</span>';
});
document.getElementById('scope').addEventListener('mouseleave', () => ro.style.opacity = 0);

const ov = document.getElementById('overview');
function moveWin(e) {
  const r = ov.getBoundingClientRect();
  const span = win[1] - win[0];
  let start = Math.round(((e.clientX - r.left) / r.width) * S.length - span / 2);
  start = Math.max(0, Math.min(S.length - span, start));
  win = [start, start + span]; drawOverview(); drawScope();
}
ov.addEventListener('mousedown', e => { ov.classList.add('dragging'); moveWin(e); });
window.addEventListener('mousemove', e => { if (ov.classList.contains('dragging')) moveWin(e); });
window.addEventListener('mouseup', () => ov.classList.remove('dragging'));

function redraw() { drawOverview(); drawScope(); drawFold(); }
window.addEventListener('resize', redraw);
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', redraw);
redraw();
</script>
"""


def stat(k, v):
    return f'<div class="stat"><div class="k">{k}</div><div class="v mono">{v}</div></div>'


def render(scores, gop, fps, title, verdict, extra_stats):
    folded = fold(scores, gop)
    amp = (max(folded) - min(folded)) if folded else 0.0
    stats = "".join([
        stat("mean", f"{statistics.fmean(scores):.2f}"),
        stat("min", f"{min(scores):.2f}"),
        stat("std", f"{statistics.pstdev(scores):.2f}"),
        stat("fold amplitude", f"{amp:.2f}"),
    ] + [stat(k, v) for k, v in extra_stats])
    step = max(1, len(folded) // 12)
    idx = list(range(0, len(folded), step))
    return TEMPLATE % {
        "title": title, "n": len(scores), "fps": fps, "gop": gop,
        "gop_s": gop / fps if fps else 0.0, "cycles": len(scores) // gop,
        "scores": json.dumps([round(s, 3) for s in scores]),
        "folded": json.dumps([round(s, 4) for s in folded]),
        "stats": stats, "verdict": verdict,
        "fold_head": "".join(f"<th>+{i}</th>" for i in idx),
        "fold_body": "".join(f"<td>{folded[i]:.2f}</td>" for i in idx),
        "trace_light": TRACE_LIGHT, "trace_dark": TRACE_DARK,
        "warn_light": WARN_LIGHT, "warn_dark": WARN_DARK,
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="vmaf_timeline_html")
    ap.add_argument("--scores", type=Path, required=True)
    ap.add_argument("--gop-frames", type=int, required=True)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--title", default="Per-frame VMAF")
    ap.add_argument("--verdict", default="")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    scores = json.loads(args.scores.read_text())
    verdict = args.verdict
    if not verdict:
        from analyze_vmaf_periodicity import diagnose
        verdict = diagnose(fold(scores, args.gop_frames))
    args.out.write_text(render(scores, args.gop_frames, args.fps,
                               args.title, verdict, []))
    print(f"wrote {args.out}  ({len(scores)} frames)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
