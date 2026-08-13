#!/usr/bin/env python3
"""Render one or more per-frame VMAF series as a scope-style timeline.

The pooled score says a rendition is "82". This says WHEN it was 82 — and a
quality pulse is a shape in time, so it needs a time axis to be seen at all.

Built for the COMPARISON run: overlay a baseline against a looser-VBV variant,
a longer-GOP variant and a whole-variant encode, and toggle each off to isolate
it. Two consequences drive the design:

  * Variants deliberately change the PERIOD (the 2s-GOP variant folds over 60
    frames, the 1s baseline over 30), so the fold panel plots position as a
    FRACTION of each series' own period. On an absolute-frame axis the second
    series' mid-GOP would sit under the first's next IDR — an invented phase
    shift.
  * Rungs sit at wildly different quality levels (145 kbps might be VMAF 30,
    7800 kbps 95), so a shared absolute axis hides the low rung's pulse
    entirely. The Δ-from-mean view removes the level and compares SHAPE, which
    is what the question is about.

Usage:
    python3 scripts/vmaf_timeline_html.py --out timeline.html --fps 30 \\
        --series "baseline=base.json:30" \\
        --series "bufsize 0.5=loose.json:30" \\
        --series "gop 2s=gop2.json:60"

Each --series is "LABEL=path.json[:gop_frames]"; the gop defaults to
--gop-frames. Get the JSON from analyze_vmaf_periodicity.py --save-scores.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyze_vmaf_periodicity import diagnose, fold, fold_fraction  # noqa: E402

# The validated categorical palette, in its fixed order. Slots are assigned by
# SERIES INDEX and never re-packed when one is switched off — colour follows the
# entity, not its rank, or unchecking a box would repaint the survivors and
# every earlier screenshot would start lying.
PALETTE = [
    ("#2a78d6", "#3987e5"),   # blue
    ("#eb6834", "#d95926"),   # orange
    ("#1baf7a", "#199e70"),   # aqua
    ("#eda100", "#c98500"),   # yellow
    ("#e87ba4", "#d55181"),   # magenta
    ("#008300", "#008300"),   # green
    ("#4a3aa7", "#9085e9"),   # violet
    ("#e34948", "#e66767"),   # red
]

FOLD_BUCKETS = 60

TEMPLATE = """<style>
  :root {
    --bg:#f4f5f7; --panel:#ffffff; --ink:#14171c; --ink-2:#4a515c; --ink-3:#838b98;
    --rule:#dfe3e9; --graticule:#c9d1dc; --window:rgba(42,120,214,.14);
%(series_light)s  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg:#0e1114; --panel:#161a1f; --ink:#e9edf2; --ink-2:#a3adba; --ink-3:#6b7684;
      --rule:#262c34; --graticule:#2f3742; --window:rgba(57,135,229,.20);
%(series_dark)s    }
  }
  :root[data-theme="dark"] {
    --bg:#0e1114; --panel:#161a1f; --ink:#e9edf2; --ink-2:#a3adba; --ink-3:#6b7684;
    --rule:#262c34; --graticule:#2f3742; --window:rgba(57,135,229,.20);
%(series_dark)s  }

  body { margin:0; background:var(--bg); color:var(--ink);
         font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }
  .wrap { max-width:1180px; margin:0 auto; padding:28px 20px 56px; }
  header { display:flex; flex-wrap:wrap; align-items:baseline; gap:10px 18px;
           border-bottom:1px solid var(--rule); padding-bottom:14px; }
  h1 { font-size:19px; margin:0; font-weight:620; letter-spacing:-.01em; }
  .sub { color:var(--ink-2); font-size:13px; }
  .mono { font-family:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
          font-variant-numeric:tabular-nums; }

  /* Filters in one row above the charts. */
  .controls { display:flex; flex-wrap:wrap; align-items:center; gap:8px 18px;
              margin:16px 0 4px; padding:11px 13px; background:var(--panel);
              border:1px solid var(--rule); border-radius:8px; }
  .ser { display:inline-flex; align-items:center; gap:7px; cursor:pointer;
         font-size:13px; padding:3px 6px; border-radius:5px; }
  .ser:has(input:focus-visible) { outline:2px solid var(--ink-2); outline-offset:1px; }
  .ser input { accent-color:var(--swatch); width:15px; height:15px; margin:0; cursor:pointer; }
  .ser .dot { width:11px; height:11px; border-radius:3px; background:var(--swatch);
              flex:none; }
  .ser.off { opacity:.42; }
  .ser.off .dot { background:var(--ink-3); }
  .spacer { flex:1 1 auto; }
  .modes { display:inline-flex; border:1px solid var(--rule); border-radius:6px;
           overflow:hidden; }
  .modes button { font:inherit; font-size:12.5px; padding:4px 11px; border:0;
                  background:var(--panel); color:var(--ink-2); cursor:pointer; }
  .modes button[aria-pressed="true"] { background:var(--ink); color:var(--panel); }

  .panel { background:var(--panel); border:1px solid var(--rule); border-radius:8px;
           padding:14px 14px 10px; margin-top:16px; }
  .panel h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em;
              color:var(--ink-3); margin:0 0 10px; font-weight:600; }
  .panel .hint { color:var(--ink-3); font-size:12px; margin:8px 0 0; }
  canvas { display:block; width:100%%; }
  #overview { cursor:grab; } #overview.dragging { cursor:grabbing; }
  .row { display:grid; grid-template-columns:1fr; gap:16px; }
  @media (min-width:900px){ .row { grid-template-columns:1.55fr 1fr; } }
  .scopewrap { position:relative; }
  .readout { position:absolute; pointer-events:none; background:var(--panel);
             border:1px solid var(--rule); border-radius:5px; padding:6px 9px;
             font-size:12px; box-shadow:0 2px 10px rgba(0,0,0,.16); opacity:0;
             transition:opacity .08s; white-space:nowrap; z-index:5; }
  .readout .sw { display:inline-block; width:9px; height:9px; border-radius:2px;
                 margin-right:5px; vertical-align:middle; }
  .tablewrap { overflow-x:auto; margin-top:10px; }
  table { border-collapse:collapse; font-size:12.5px; width:100%%; }
  th,td { text-align:right; padding:3px 10px 3px 0; white-space:nowrap; }
  th:first-child,td:first-child { text-align:left; }
  th { color:var(--ink-3); font-weight:600; }
  .verdicts { margin-top:16px; display:grid; gap:8px; }
  .verdict { padding:10px 13px; background:var(--panel); border:1px solid var(--rule);
             border-left:3px solid var(--swatch); border-radius:0 6px 6px 0; font-size:13px; }
  .verdict b { font-weight:620; }
  @media (prefers-reduced-motion:reduce){ .readout{transition:none} }
</style>

<div class="wrap">
  <header>
    <h1>%(title)s</h1>
    <span class="sub mono">%(nseries)d series &middot; %(fps).3f fps</span>
  </header>

  <div class="controls" id="controls">%(checkboxes)s
    <span class="spacer"></span>
    <span class="modes" role="group" aria-label="Value mode">
      <button id="m-abs" aria-pressed="true">Absolute</button>
      <button id="m-rel" aria-pressed="false">&Delta; from mean</button>
    </span>
  </div>

  <div class="panel">
    <h2>Whole clip &mdash; drag to move the window</h2>
    <canvas id="overview" height="94"></canvas>
    <p class="hint">Min/max envelope per pixel column, never stride-sampled &mdash;
       stride-sampling a one-second pulse is exactly how you hide it.</p>
  </div>

  <div class="row">
    <div class="panel scopewrap">
      <h2>Scope &mdash; every frame in the window</h2>
      <canvas id="scope" height="250"></canvas>
      <div class="readout" id="readout"></div>
      <p class="hint">Graticule lines mark the first series' IDRs. Watch the slope
         <em>immediately after</em> each one.</p>
    </div>
    <div class="panel">
      <h2>Folded over the GOP</h2>
      <canvas id="foldc" height="250"></canvas>
      <p class="hint">Position as a <em>fraction</em> of each series' own period, so
         a 2s-GOP variant overlays a 1s one honestly.</p>
    </div>
  </div>

  <div class="verdicts">%(verdicts)s</div>

  <div class="panel">
    <h2>Summary</h2>
    <div class="tablewrap"><table class="mono">
      <thead><tr><th>series</th><th>frames</th><th>GOP</th><th>mean</th><th>min</th>
      <th>std</th><th>fold amplitude</th></tr></thead>
      <tbody>%(rows)s</tbody>
    </table></div>
  </div>
</div>

<script>
const SERIES = %(data)s;
const FPS = %(fps)f;
let mode = 'abs';
const on = new Set(SERIES.map((_, i) => i));
const css = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
const colour = i => css('--s' + i);
const maxLen = Math.max(...SERIES.map(s => s.v.length));
let win = [0, Math.min(maxLen, SERIES[0].gop * 10)];

// Δ-from-mean strips the level so SHAPE is comparable across rungs that sit
// 60 VMAF points apart. Absolute keeps the real values for a same-rung compare.
const val = (s, i) => mode === 'abs' ? s.v[i] : s.v[i] - s.mean;
const active = () => SERIES.filter((_, i) => on.has(i));

function fit(c) {
  const d = window.devicePixelRatio || 1, w = c.clientWidth;
  c.width = w * d; c.height = c.getAttribute('height') * d;
  const x = c.getContext('2d'); x.setTransform(d, 0, 0, d, 0, 0);
  return [x, w, +c.getAttribute('height')];
}
function bounds(vals) {
  let lo = Infinity, hi = -Infinity;
  for (const v of vals) { if (v < lo) lo = v; if (v > hi) hi = v; }
  if (!isFinite(lo)) return [0, 1];
  if (hi - lo < .5) { lo -= .5; hi += .5; }
  const pad = (hi - lo) * .08; return [lo - pad, hi + pad];
}

function drawOverview() {
  const c = document.getElementById('overview'), [x, w, h] = fit(c);
  x.clearRect(0, 0, w, h);
  const act = active(); if (!act.length) return;
  const all = []; act.forEach(s => s.v.forEach((_, i) => all.push(val(s, i))));
  const [lo, hi] = bounds(all), y = v => h - 6 - (v - lo) / (hi - lo) * (h - 12);
  SERIES.forEach((s, si) => {
    if (!on.has(si)) return;
    x.strokeStyle = colour(si); x.globalAlpha = act.length > 1 ? .8 : 1;
    x.lineWidth = 1; x.beginPath();
    for (let px = 0; px < w; px++) {
      const a = Math.floor(px / w * s.v.length);
      const b = Math.max(a + 1, Math.floor((px + 1) / w * s.v.length));
      let mn = Infinity, mx = -Infinity;
      for (let i = a; i < b && i < s.v.length; i++) {
        const v = val(s, i); if (v < mn) mn = v; if (v > mx) mx = v;
      }
      if (mn === Infinity) continue;
      x.moveTo(px + .5, y(mx)); x.lineTo(px + .5, y(mn));
    }
    x.stroke(); x.globalAlpha = 1;
  });
  const wx = f => f / maxLen * w;
  x.fillStyle = css('--window'); x.fillRect(wx(win[0]), 0, Math.max(2, wx(win[1] - win[0])), h);
  x.strokeStyle = css('--ink-2'); x.lineWidth = 1.5;
  x.strokeRect(wx(win[0]) + .5, .5, Math.max(2, wx(win[1] - win[0])) - 1, h - 1);
}

function drawScope() {
  const c = document.getElementById('scope'), [x, w, h] = fit(c);
  x.clearRect(0, 0, w, h);
  const act = active(); if (!act.length) return;
  const L = win[1] - win[0];
  const all = [];
  SERIES.forEach((s, si) => { if (!on.has(si)) return;
    for (let i = win[0]; i < win[1] && i < s.v.length; i++) all.push(val(s, i)); });
  const [lo, hi] = bounds(all);
  const px = i => (i / (L - 1 || 1)) * (w - 48) + 40;
  const py = v => h - 24 - (v - lo) / (hi - lo) * (h - 44);
  const gop = SERIES[[...on][0] ?? 0].gop;
  x.strokeStyle = css('--graticule'); x.lineWidth = 1;
  for (let f = Math.ceil(win[0] / gop) * gop; f < win[1]; f += gop) {
    const gx = px(f - win[0]); x.beginPath(); x.moveTo(gx, 6); x.lineTo(gx, h - 24); x.stroke();
  }
  x.fillStyle = css('--ink-3'); x.font = '11px ui-monospace, monospace';
  x.fillText(hi.toFixed(1), 2, py(hi) + 4); x.fillText(lo.toFixed(1), 2, py(lo) + 4);
  x.fillText((win[0] / FPS).toFixed(2) + 's', 40, h - 7);
  x.fillText((win[1] / FPS).toFixed(2) + 's', w - 48, h - 7);
  SERIES.forEach((s, si) => {
    if (!on.has(si)) return;
    x.strokeStyle = colour(si); x.lineWidth = 2; x.lineJoin = 'round'; x.beginPath();
    let started = false;
    for (let i = win[0]; i < win[1] && i < s.v.length; i++) {
      const X = px(i - win[0]), Y = py(val(s, i));
      started ? x.lineTo(X, Y) : (x.moveTo(X, Y), started = true);
    }
    x.stroke();
  });
  c._m = { px, py, lo, hi, w, h, L };
}

function drawFold() {
  const c = document.getElementById('foldc'), [x, w, h] = fit(c);
  x.clearRect(0, 0, w, h);
  const act = active().filter(s => s.fold.length); if (!act.length) return;
  const adj = s => mode === 'abs' ? s.fold : s.fold.map(v => v - s.mean);
  const all = []; act.forEach(s => adj(s).forEach(v => all.push(v)));
  const [lo, hi] = bounds(all);
  const n = act[0].fold.length;
  const px = i => (i / (n - 1 || 1)) * (w - 56) + 44;
  const py = v => h - 24 - (v - lo) / (hi - lo) * (h - 44);
  x.fillStyle = css('--ink-3'); x.font = '11px ui-monospace, monospace';
  x.fillText(hi.toFixed(1), 2, py(hi) + 4); x.fillText(lo.toFixed(1), 2, py(lo) + 4);
  x.fillText('IDR', 40, h - 7); x.fillText('next IDR', w - 62, h - 7);
  SERIES.forEach((s, si) => {
    if (!on.has(si) || !s.fold.length) return;
    const f = adj(s);
    x.strokeStyle = colour(si); x.lineWidth = 2; x.lineJoin = 'round'; x.beginPath();
    f.forEach((v, i) => i ? x.lineTo(px(i), py(v)) : x.moveTo(px(i), py(v)));
    x.stroke();
    x.fillStyle = colour(si);
    x.beginPath(); x.arc(px(0), py(f[0]), 4, 0, 7); x.fill();
  });
}

const ro = document.getElementById('readout');
document.getElementById('scope').addEventListener('mousemove', e => {
  const c = e.currentTarget, m = c._m; if (!m || !on.size) { ro.style.opacity = 0; return; }
  const r = c.getBoundingClientRect();
  const i = Math.round((e.clientX - r.left - 40) / (m.w - 48) * (m.L - 1));
  if (i < 0 || i >= m.L) { ro.style.opacity = 0; return; }
  const f = win[0] + i;
  let html = '<div class="mono" style="color:var(--ink-3)">frame ' + f +
             ' &middot; ' + (f / FPS).toFixed(3) + 's</div>';
  SERIES.forEach((s, si) => {
    if (!on.has(si) || f >= s.v.length) return;
    html += '<div><span class="sw" style="background:' + colour(si) + '"></span>' +
            s.label + ' <b class="mono">' + val(s, f).toFixed(2) + '</b>' +
            ' <span class="mono" style="color:var(--ink-3)">+' + (f %% s.gop) + '</span></div>';
  });
  ro.innerHTML = html; ro.style.opacity = 1;
  ro.style.left = Math.min(r.width - 190, m.px(i) + 12) + 'px';
  ro.style.top = '14px';
});
document.getElementById('scope').addEventListener('mouseleave', () => ro.style.opacity = 0);

const ov = document.getElementById('overview');
function moveWin(e) {
  const r = ov.getBoundingClientRect(), span = win[1] - win[0];
  let s = Math.round(((e.clientX - r.left) / r.width) * maxLen - span / 2);
  s = Math.max(0, Math.min(maxLen - span, s));
  win = [s, s + span]; drawOverview(); drawScope();
}
ov.addEventListener('mousedown', e => { ov.classList.add('dragging'); moveWin(e); });
window.addEventListener('mousemove', e => { if (ov.classList.contains('dragging')) moveWin(e); });
window.addEventListener('mouseup', () => ov.classList.remove('dragging'));

document.querySelectorAll('.ser input').forEach(cb => {
  cb.addEventListener('change', () => {
    const i = +cb.dataset.i;
    cb.checked ? on.add(i) : on.delete(i);
    cb.closest('.ser').classList.toggle('off', !cb.checked);
    redraw();
  });
});
function setMode(m) {
  mode = m;
  document.getElementById('m-abs').setAttribute('aria-pressed', m === 'abs');
  document.getElementById('m-rel').setAttribute('aria-pressed', m === 'rel');
  redraw();
}
document.getElementById('m-abs').addEventListener('click', () => setMode('abs'));
document.getElementById('m-rel').addEventListener('click', () => setMode('rel'));

function redraw() { drawOverview(); drawScope(); drawFold(); }
window.addEventListener('resize', redraw);
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', redraw);
redraw();
</script>
"""


def build(series, fps, title):
    """series: list of (label, scores, gop)."""
    if len(series) > len(PALETTE):
        raise SystemExit(
            f"{len(series)} series but the validated palette has {len(PALETTE)} "
            f"slots. A 9th hue would be generated rather than chosen — drop one "
            f"or render two pages.")
    data, rows, checks, verdicts = [], "", "", ""
    for i, (label, scores, gop) in enumerate(series):
        folded_abs = fold(scores, gop)
        amp = (max(folded_abs) - min(folded_abs)) if folded_abs else 0.0
        data.append({
            "label": label, "gop": gop,
            "v": [round(s, 3) for s in scores],
            "mean": round(statistics.fmean(scores), 4),
            # Fraction-folded so differing periods overlay honestly.
            "fold": [round(v, 4) for v in fold_fraction(scores, gop, FOLD_BUCKETS)],
        })
        rows += (f"<tr><td>{label}</td><td>{len(scores)}</td><td>{gop}</td>"
                 f"<td>{statistics.fmean(scores):.2f}</td><td>{min(scores):.2f}</td>"
                 f"<td>{statistics.pstdev(scores):.2f}</td><td>{amp:.2f}</td></tr>")
        checks += (f'\n    <label class="ser" style="--swatch:var(--s{i})">'
                   f'<input type="checkbox" data-i="{i}" checked>'
                   f'<span class="dot"></span>{label}</label>')
        verdicts += (f'<div class="verdict" style="--swatch:var(--s{i})">'
                     f'<b>{label}</b> &mdash; {diagnose(folded_abs)}</div>')
    return TEMPLATE % {
        "title": title, "fps": fps, "nseries": len(series),
        "data": json.dumps(data), "rows": rows,
        "checkboxes": checks, "verdicts": verdicts,
        "series_light": "".join(f"    --s{i}:{lt};\n" for i, (lt, _) in enumerate(PALETTE[:len(series)])),
        "series_dark": "".join(f"      --s{i}:{dk};\n" for i, (_, dk) in enumerate(PALETTE[:len(series)])),
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="vmaf_timeline_html")
    ap.add_argument("--series", action="append", required=True, metavar="LABEL=PATH[:GOP]",
                    help="repeatable; overlay one per variant or per rung")
    ap.add_argument("--gop-frames", type=int, default=30, help="default GOP per series")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--title", default="Per-frame VMAF")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    series = []
    for spec in args.series:
        if "=" not in spec:
            raise SystemExit(f"--series must be LABEL=PATH[:GOP], got {spec!r}")
        label, _, rest = spec.partition("=")
        path, _, gop = rest.rpartition(":")
        if not path:                       # no ":GOP" given
            path, gop = rest, ""
        scores = json.loads(Path(path).read_text())
        series.append((label, scores, int(gop) if gop else args.gop_frames))

    args.out.write_text(build(series, args.fps, args.title))
    print(f"wrote {args.out}  ({len(series)} series, "
          f"{sum(len(s[1]) for s in series)} frames total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
