"""Structured progress markers consumed by the Go server.

Each encode produces a stream of text log lines (ffmpeg stats, x265
init info, etc.). Interleaved with those, we emit a small set of
distinctive markers that the Go server's log scanner recognises and
parses into structured per-stage progress — the UI renders a table
of stages with live percentages.

Marker formats, each on its own line:

    [[ENCODER-PLAN <json-list-of-stage-descriptors>]]
    [[ENCODER-STAGE key=<id> status=<pending|running|done|failed> percent=<0-100>]]

A stage descriptor is a JSON object with at least `key` and `label`.
The double brackets are deliberate — they're not produced by ffmpeg,
x265, or Shaka Packager, so the scanner can detect them without
false positives on normal encoder chatter.

The helper `run_ffmpeg_with_progress(cmd, duration_s, key)` runs
ffmpeg with `-progress pipe:1`, parses the `out_time_us=...` key/value
stream, and emits STAGE markers at a bounded rate.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from typing import Iterable


# ---------------------------------------------------------------------------
# Plan + stage markers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Stage:
    """Stable identifier + human-readable label for one row of the UI table."""
    key: str
    label: str


def emit_plan(stages: Iterable[Stage]) -> None:
    """Announce the full ordered list of stages up front.

    The Go server uses this to seed the Job.Stages slice with rows in
    the correct display order; subsequent STAGE markers then update
    matching rows by key.
    """
    payload = json.dumps([asdict(s) for s in stages], separators=(",", ":"))
    print(f"[[ENCODER-PLAN {payload}]]", flush=True)


def emit_stage(key: str, status: str, percent: float = 0.0) -> None:
    """Update one stage's status and (optionally) percent complete."""
    pct = max(0.0, min(100.0, float(percent)))
    print(
        f"[[ENCODER-STAGE key={key} status={status} percent={pct:.1f}]]",
        flush=True,
    )


# ---------------------------------------------------------------------------
# ffmpeg progress parser
# ---------------------------------------------------------------------------

# Throttle how often we emit STAGE markers. The driving constraint is
# ffmpeg's own `-stats_period` (see _FFMPEG_STATS_PERIOD below); setting
# this any tighter than that just means we process ticks as they arrive.
# We intentionally keep it below the stats period so no tick gets
# dropped — 0.2s against a 0.25s ffmpeg period leaves headroom.
_MIN_EMIT_INTERVAL_S = 0.2

# How often to ask ffmpeg to emit -progress output. Default is 0.5s,
# which on fast encodes (several × realtime on short clips) means
# each tick represents a big chunk of content and the UI progress
# bar jumps in visible steps. 0.25s doubles the rate without
# meaningfully increasing CPU or log volume.
_FFMPEG_STATS_PERIOD = "0.25"


def run_ffmpeg_with_progress(
    cmd: list[str],
    duration_s: float,
    stage_key: str,
) -> None:
    """Run ffmpeg with `-progress pipe:1` appended and emit live STAGE updates.

    `duration_s` is the expected output duration (used to convert
    ffmpeg's `out_time_us` to percent). If it's zero or negative,
    only status transitions (running → done) are emitted.

    ffmpeg's stderr is inherited so normal stats/log lines still flow
    to the container's stdout and the Go scanner. stdout is captured
    so the Go scanner never sees the raw key=value `-progress` output.

    We keep `-stats` enabled (i.e. do NOT pass `-nostats`) — the log
    viewer relies on those \r-separated frame= lines to show live
    encode detail. The structured STAGE markers flow through a
    different channel (stdout → Python → emit_stage), so enabling
    -stats doesn't double-report anything.

    Raises `subprocess.CalledProcessError` if ffmpeg exits non-zero.
    """
    full_cmd = [*cmd, "-progress", "pipe:1", "-stats_period", _FFMPEG_STATS_PERIOD]

    emit_stage(stage_key, "running", 0.0)

    proc = subprocess.Popen(
        full_cmd,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
    )
    assert proc.stdout is not None

    last_emit = 0.0
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key == "out_time_us" and duration_s > 0:
                try:
                    out_us = int(value)
                except ValueError:
                    continue
                percent = (out_us / (duration_s * 1_000_000.0)) * 100.0
                now = time.monotonic()
                if now - last_emit >= _MIN_EMIT_INTERVAL_S:
                    emit_stage(stage_key, "running", percent)
                    last_emit = now
            elif key == "progress" and value == "end":
                break
    finally:
        rc = proc.wait()

    if rc != 0:
        emit_stage(stage_key, "failed", 0.0)
        raise subprocess.CalledProcessError(rc, full_cmd)

    emit_stage(stage_key, "done", 100.0)
