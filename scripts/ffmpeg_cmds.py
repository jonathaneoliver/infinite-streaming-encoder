#!/usr/bin/env python3
"""`make ffmpeg-cmds` — collect the exact ffmpeg argv from every encode path and
diff the ENCODE SETTINGS across them.

progress.run_ffmpeg_with_progress logs one `[ffmpeg] <shlex-joined argv>` line
per invocation. That line is the ground truth for "what were these two runs
actually asked to do", but it lands in three different places, because each path
runs ffmpeg somewhere else and only [[ENCODER-]] markers are forwarded up to the
job log:

  local (monolithic)  $TMP_DIR/logs/<job>.log — the Go scanner appends non-marker
                      lines to the job log buffer, which is flushed to this file.
  local-dist          `docker logs encode-worker` on whichever box ran the chunk,
                      which may be a remote worker in $DIST_WORKERS.
  cloud-batch         CloudWatch /aws/batch/infinite-streaming-encoder, one
                      stream per Batch job. NOTE 7-day retention: past that the
                      evidence is gone.

Neither distributed path forwards the argv to the job log, deliberately: it is
~0.5-1.5 KB and every chunk emits one, so forwarding would flood the 1000-line
job log buffer (and, on local-dist, the Temporal workflow history) and push out
everything else. Container logs are the right home; this script is the thing
that makes them reachable from one place.

Comparison is on SETTINGS, not raw argv: the paths legitimately differ between
paths (`/tmp/act-<uuid>` work dirs, chunk-numbered outputs, MinIO vs S3 staging),
so a raw diff is all noise. `normalize()` drops the path-valued arguments and
keeps the rate control, preset, GOP and filter chain — the things that actually
determine the output. This is what #167 (local vs cloud bitrate drift) needed and
had to be reconstructed by hand.

Read-only. Every source degrades to "unavailable" rather than failing, so it runs
anywhere.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

PREFIX = "[ffmpeg] "
BATCH_LOG_GROUP = "/aws/batch/infinite-streaming-encoder"

# Arguments whose VALUE is a path/URL and therefore differs between paths by
# design. The flag is kept (dropping it would hide a missing -pass), the value is
# replaced, so `-passlogfile /tmp/act-ab12/x` and `-passlogfile /scratch/y` compare
# equal while still showing that both had a -passlogfile.
#
# `-f` is deliberately NOT here: it names a format, not a path, and two-pass
# differs by exactly that (`-f null` for the analysis pass vs `-f mp4`). Masking
# it would hide the single most likely two-pass divergence.
_PATH_VALUED = {"-i", "-passlogfile"}
# Per-CHUNK values, not settings. -ss and -frames:v differ for every chunk by
# design, so leaving them in makes each chunk its own "setting set" and every
# rung reads as divergent — noise that buries the real comparison. They are
# masked here and reported separately as the chunk PLAN, which is its own
# question (do local and cloud cut the clip in the same places?) and deserves
# its own answer rather than being mixed into the rate-control diff.
_PER_CHUNK = {"-ss", "-frames:v", "-t"}
_PATH_RE = re.compile(r"^(/|s3://|https?://|file:)")
# Output dir naming is the codec/height contract (see OutputStem in CLAUDE.md).
# `(?!\d)` rather than `\b` after the height: chunk outputs are named
# `h264_1080_c003.mp4`, and `\b` fails there because `_` is a word character —
# which silently bucketed every chunked encode (i.e. all of them) as "unknown".
_RUNG_RE = re.compile(r"\b(h264|hevc|av1)[_/-](\d{3,4})(?!\d)")


def normalize(argv: list[str]) -> str:
    """Settings-only rendering of an ffmpeg argv, stable across encode paths."""
    out: list[str] = []
    skip_next = False
    for i, tok in enumerate(argv):
        if skip_next:
            skip_next = False
            out.append("<PATH>")
            continue
        if tok in _PATH_VALUED:
            out.append(tok)
            skip_next = True
            continue
        if tok in _PER_CHUNK:
            out.append(tok)
            skip_next = True   # emitted as <PATH>; see chunk_plan() for the values
            continue
        # A bare trailing output path, or any other absolute path.
        out.append("<PATH>" if _PATH_RE.match(tok) else tok)
    # -progress/-stats_period are appended by run_ffmpeg_with_progress on every
    # invocation, so they carry no signal in a comparison.
    text = " ".join(out)
    for noise in (" -progress pipe:1", ):
        text = text.replace(noise, "")
    return re.sub(r" -stats_period \S+", "", text)


def chunk_span(argv: list[str]) -> tuple[str, str] | None:
    """This invocation's (-ss, -frames:v) — i.e. which slice of the clip it
    encoded. None for a whole-variant encode, which seeks nowhere."""
    def val(flag: str) -> str:
        return argv[argv.index(flag) + 1] if flag in argv else "-"
    if "-ss" not in argv:
        return None
    return (val("-ss"), val("-frames:v"))


def rung_key(argv: list[str]) -> str:
    """`<codec>_<height>` if the argv names one, else 'unknown'."""
    for tok in reversed(argv):
        m = _RUNG_RE.search(tok)
        if m:
            return f"{m.group(1)}_{m.group(2)}"
    return "unknown"


def _lines(text: str) -> list[str]:
    return [ln.split(PREFIX, 1)[1].strip()
            for ln in text.splitlines() if PREFIX in ln]


def _run(cmd: list[str], timeout: int = 30) -> str | None:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # docker logs writes to stderr; aws to stdout. Take both.
    return (p.stdout or "") + (p.stderr or "") if p.returncode == 0 else None


def from_job_logs(tmp_dir: str, limit: int) -> list[tuple[str, str]]:
    d = Path(tmp_dir) / "logs"
    if not d.is_dir():
        return []
    found = []
    for f in sorted(d.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        for ln in _lines(f.read_text(errors="replace")):
            found.append((f"local:{f.stem}", ln))
    return found


def from_docker(container: str, host: str | None, label: str) -> list[tuple[str, str]]:
    cmd = ["docker", "logs", "--tail", "20000", container]
    if host:
        cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host,
               " ".join(shlex.quote(c) for c in cmd)]
    text = _run(cmd, timeout=60)
    return [] if text is None else [(f"dist:{label}", ln) for ln in _lines(text)]


def from_cloudwatch(region: str, hours: int) -> list[tuple[str, str]] | None:
    # `"[ffmpeg] "` is a CloudWatch quoted-substring filter, applied server-side
    # so we transfer only matching events, not the whole (very large) group.
    text = _run(["aws", "logs", "filter-log-events", "--region", region,
                 "--log-group-name", BATCH_LOG_GROUP,
                 "--filter-pattern", '"[ffmpeg] "',
                 "--start-time", str(_start_ms(hours)),
                 "--output", "json"], timeout=120)
    if text is None:
        return None
    try:
        events = json.loads(text).get("events", [])
    except json.JSONDecodeError:
        return None
    return [(f"cloud:{e.get('logStreamName', '?').split('/')[-1][:12]}", ln)
            for e in events for ln in _lines(e.get("message", ""))]


def _start_ms(hours: int) -> int:
    # No time import at module scope on purpose — keep the pure functions above
    # trivially testable; this is the only clock read.
    import time
    return int((time.time() - hours * 3600) * 1000)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tmp-dir", default=os.environ.get("TMP_DIR", "/tmp"))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    ap.add_argument("--hours", type=int, default=24, help="CloudWatch lookback")
    ap.add_argument("--job-logs", type=int, default=10, help="recent job logs to scan")
    ap.add_argument("--raw", action="store_true", help="print full argv, no normalization")
    ap.add_argument("--rung", help="only this rung, e.g. h264_1080")
    args = ap.parse_args()

    rows: list[tuple[str, str]] = []
    rows += from_job_logs(args.tmp_dir, args.job_logs)

    container = os.environ.get("DIST_WORKER_CONTAINER", "encode-worker")
    rows += from_docker(container, None, "local")
    for spec in (os.environ.get("DIST_WORKERS") or "").split():
        label, _, host = spec.partition("=")
        if host:
            rows += from_docker(container, host, label)

    cloud = from_cloudwatch(args.region, args.hours)
    if cloud is None:
        print("cloud: CloudWatch unavailable (no creds/network) — skipped\n",
              file=sys.stderr)
    else:
        rows += cloud

    if not rows:
        print("No [ffmpeg] lines found. They are emitted by encodes run with this "
              "version of scripts/ — older runs predate the logging.", file=sys.stderr)
        return 1

    # Group by rung, then by normalized settings, so an identical setting set
    # across sources collapses to ONE entry listing every source that produced it.
    # Two entries under one rung IS the finding.
    by_rung: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for source, line in rows:
        try:
            argv = shlex.split(line)
        except ValueError:
            continue
        key = rung_key(argv)
        if args.rung and key != args.rung:
            continue
        by_rung[key][line if args.raw else normalize(argv)].add(source)

    # Chunk plans, per rung per source-kind. #176 made the orchestrator the sole
    # authority on boundaries, so local and cloud should now cut identically —
    # this is where that shows up or doesn't.
    plans: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for source, line in rows:
        try:
            argv = shlex.split(line)
        except ValueError:
            continue
        key = rung_key(argv)
        if args.rung and key != args.rung:
            continue
        span = chunk_span(argv)
        if span:
            plans[key][source.split(":")[0]].add(span)

    if plans:
        print("\n=== chunk plans (start_s, frames) — do the paths cut the clip alike?")
        for rung in sorted(plans):
            kinds = plans[rung]
            # dist:local and dist:ubuntu are the SAME plan split across boxes.
            merged: dict[str, set] = defaultdict(set)
            for k, v in kinds.items():
                merged["local" if k == "dist" else k] |= v
            sig = {k: sorted(v) for k, v in merged.items()}
            same = len({tuple(v) for v in sig.values()}) == 1 and len(sig) > 1
            mark = "identical" if same else ("only one path logged" if len(sig) < 2 else "DIFFER")
            print(f"  {rung}: " + ", ".join(f"{k}={len(v)} chunks" for k, v in sorted(sig.items()))
                  + f"  -> {mark}")
            if mark == "DIFFER":
                for k, v in sorted(sig.items()):
                    print(f"      {k}: " + ", ".join(a for a, _ in v[:8])
                          + (" ..." if len(v) > 8 else ""))

    print("\n=== encoder settings (per-chunk seek/frame-count masked)")
    divergent = 0
    for rung in sorted(by_rung):
        variants = by_rung[rung]
        flag = "  <-- DIVERGENT" if len(variants) > 1 else ""
        if len(variants) > 1:
            divergent += 1
        print(f"\n=== {rung}: {len(variants)} distinct setting set(s){flag}")
        for settings, sources in sorted(variants.items(), key=lambda kv: -len(kv[1])):
            print(f"  [{', '.join(sorted(sources))}]")
            print(f"    {settings}")

    if divergent:
        print(f"\n{divergent} rung(s) encoded with differing settings across paths. "
              f"Diff the lines above; both paths should agree (#172).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
