"""Structured progress markers consumed by the Go server.

Each encode produces a stream of text log lines (ffmpeg stats, x265
init info, etc.). Interleaved with those, we emit a small set of
distinctive markers that the Go server's log scanner recognises and
parses into structured per-stage progress — the UI renders a table
of stages with live percentages.

Marker formats, each on its own line:

    [[ENCODER-PLAN <json-list-of-stage-descriptors>]]
    [[ENCODER-STAGE key=<id> status=<pending|running|done|failed> percent=<0-100>]]
    [[ENCODER-FLEET machine=<id> busy=<cores> perf=<cores>]]

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
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from typing import Iterable

from .telemetry import emit


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
    emit(f"[[ENCODER-PLAN {payload}]]")


def emit_stage(key: str, status: str, percent: float = 0.0) -> None:
    """Update one stage's status and (optionally) percent complete."""
    pct = max(0.0, min(100.0, float(percent)))
    emit(f"[[ENCODER-STAGE key={key} status={status} percent={pct:.1f}]]")
    # Piggyback the fleet CPU sample on the progress cadence (throttled inside)
    # rather than running a timer thread: these already fire ~20x per chunk,
    # which is a finer interval than the 15s throttle needs.
    emit_fleet_cpu()


# --- fleet CPU ---------------------------------------------------------------
# The SAME marker the local-dist path already emits, so the cloud reuses the
# whole existing pipeline: fleetMarkerRe in job.go, Manager.recordFleetCPU, and
# the UI's "N / M cores busy (P%)" bar. No new contract, no new rendering.
#
# Read from /proc/stat, which Docker does NOT namespace — so a container sees
# the HOST's counters. That is the point: the question is "am I using the CPU I
# am renting?", which is the whole instance, exactly what `top` shows and what
# the paid AWS/EC2 CPUUtilization metric reported. (The per-chunk question —
# "did this chunk use its reserved vCPU?" — is answered separately and better by
# cpu_s on ENCODER-TIMING, from getrusage.)
_FLEET_PREV = {"t": 0.0, "total": 0, "idle": 0, "emitted": 0.0}
# 5s, not 15: cloud chunks are SHORT. A 396p chunk measured on a real run encoded
# in 0.66s and produced 1.3s of log output in total, so a 15s throttle meant such
# a job emitted nothing at all. The sample itself is a single /proc/stat read, so
# emitting often costs nothing — and the control plane throttles INGESTION per
# machine anyway (fleetMinSampleGap), so more emissions cannot flood the history.
_FLEET_INTERVAL_S = 5.0
# Shortest /proc/stat delta that yields a meaningful ratio. Below this the idle
# delta rounds to zero on a loaded box and every sample reads exactly 100%.
_FLEET_MIN_WINDOW_S = 2.0
_INSTANCE_ID: str | None = None


def _host_busy_cores() -> float | None:
    """Logical cores busy on the HOST since the last call, or None if unknown.

    None on the first call — there is no interval to difference against, and a
    fabricated 0 would render as an idle box at the start of every chunk.
    """
    try:
        v = [int(x) for x in open("/proc/stat").readline().split()[1:]]
        total, idle = sum(v), v[3] + (v[4] if len(v) > 4 else 0)
    except (OSError, ValueError, IndexError):
        return None
    prev_t = _FLEET_PREV["t"]
    prev_total, prev_idle = _FLEET_PREV["total"], _FLEET_PREV["idle"]
    now = time.monotonic()
    # Reject a too-short interval WITHOUT consuming the baseline, so the window
    # keeps accumulating instead of restarting. Sub-second deltas are not merely
    # noisy: on a busy box d_idle is 0 over them, so the ratio below returns
    # exactly 1.0 and the box reports a flat, perfectly round 100%. That is what
    # a fleet of instances all reading 100% actually means. It bites hardest
    # right after prime_fleet_cpu() takes the baseline at phase start, and once
    # per chunk container, since each is its own process with its own baseline.
    if prev_t and now - prev_t < _FLEET_MIN_WINDOW_S:
        return None
    _FLEET_PREV["t"], _FLEET_PREV["total"], _FLEET_PREV["idle"] = (now, total, idle)
    if not prev_t or total <= prev_total:
        return None
    d_total, d_idle = total - prev_total, idle - prev_idle
    return max(0.0, (1.0 - d_idle / d_total) * (os.cpu_count() or 1))


def _instance_id() -> str:
    """EC2 instance-id via IMDS, cached. Empty off-instance (local encodes).

    Matches the identifier ENCODER-HOST reports, so the fleet row and the chunk
    cells group under the same machine instead of splitting into two.
    """
    global _INSTANCE_ID
    if _INSTANCE_ID is not None:
        return _INSTANCE_ID
    _INSTANCE_ID = ""
    import urllib.request
    try:
        tok_req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        tok = urllib.request.urlopen(tok_req, timeout=1).read().decode()
        req = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/instance-id",
            headers={"X-aws-ec2-metadata-token": tok})
        _INSTANCE_ID = urllib.request.urlopen(req, timeout=1).read().decode().strip()
    except Exception:  # noqa: BLE001 — off-instance, or IMDS disabled
        pass
    return _INSTANCE_ID


def prime_fleet_cpu() -> None:
    """Take the baseline /proc/stat sample so the FIRST emit can produce a value.

    Without this, _host_busy_cores() returns None on its first call (nothing to
    difference against) and the first heartbeat emits nothing — which on a
    sub-second chunk means the job emits no CPU at all. Called once at phase
    start, so the first heartbeat already has an interval behind it.
    """
    _host_busy_cores()


def emit_fleet_cpu() -> None:
    """Emit ENCODER-FLEET for this box, throttled. No-op off-instance.

    Several chunks share an instance and each will call this; they all report
    the same host figure and the control plane keys by machine, so the last one
    per poll simply wins. Cheaper than electing a reporter.
    """
    now = time.monotonic()
    if now - _FLEET_PREV["emitted"] < _FLEET_INTERVAL_S:
        return
    machine = _instance_id()
    if not machine:
        return
    busy = _host_busy_cores()
    if busy is None:
        return
    _FLEET_PREV["emitted"] = now
    emit(f"[[ENCODER-FLEET machine={machine} busy={busy:.2f} "
         f"perf={os.cpu_count() or 0}]]")


_FFMPEG_EMITTED = False


def emit_ffmpeg_version() -> None:
    """Report the ffmpeg this process will actually encode with.

    The image tracks BtbN's rolling `latest` rather than a dated autobuild —
    dated pins get pruned upstream and break every build (see the Dockerfile).
    The reproducibility that a pin was buying is bought back here instead: the
    version is recorded per encode, so "which ffmpeg made this?" is answered by
    the artifact rather than inferred from a Dockerfile line that has since
    moved.

    Asks the BINARY, not /app/ffmpeg-version.txt: the file records what the
    image was built with, this records what is actually on PATH and running. A
    bind-mount, a PATH change or a hand-patched container makes those differ,
    and the one that produced the pixels is the true answer.

    Emitted from the worker that runs the encode, not the orchestrator — on
    cloud-batch those can be different images if tags have drifted. Once per
    process; the phase entry points call it on every chunk.
    """
    global _FFMPEG_EMITTED
    if _FFMPEG_EMITTED:
        return
    _FFMPEG_EMITTED = True
    import subprocess
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True,
                             text=True, timeout=10)
        line = (out.stdout or "").splitlines()[0].strip()
    except Exception:  # noqa: BLE001 — no ffmpeg on PATH, or it hung
        return
    if not line:
        return
    # "ffmpeg version N-125978-g95c43d7df7-20260806 Copyright (c) ..." — keep the
    # build id, drop the copyright tail. It is the same on every build, so it is
    # pure noise in a field that lands in every output's encode.json.
    m = re.match(r"ffmpeg version (\S+)", line)
    emit(f"[[ENCODER-FFMPEG version={m.group(1) if m else line}]]")


# Our codec name -> (ffmpeg encoder, regex capturing the library version from
# the encoder's own init banner).
#
# x264 is ABSENT ON PURPOSE and the gap is real: unlike x265 and SVT-AV1 it
# prints no version at init. Its core number is written only into the bitstream
# SEI, so reading it back needs a produced file and a parse — machinery out of
# proportion to the answer. For h264 the ffmpeg build (ENCODER-FFMPEG) is the
# proxy: a given BtbN build bundles a fixed x264, so the build ID pins it even
# though it does not name it. Recorded as such rather than left to look like an
# oversight; see encode.json's codec_libs.
_CODEC_LIBS = {
    "hevc": ("libx265", re.compile(r"HEVC encoder version (\S+)")),
    "av1": ("libsvtav1", re.compile(r"SVT-AV1 Encoder Lib v(\S+)")),
}
_CODEC_LIB_EMITTED: set[str] = set()


def emit_codec_lib(codec: str) -> None:
    """Report the ENCODER LIBRARY version for this codec, not just ffmpeg's.

    `ffmpeg -version` names libx264/libx265/libsvtav1 but carries no version for
    any of them, and the library is what actually determines the bitstream. An
    x265 bump under a static ffmpeg build changes HEVC output while every
    recorded version stays identical — precisely the confound the VMAF curve and
    ladder work cannot absorb.

    Probes with a one-frame nullsrc encode (~50ms, once per codec per process)
    rather than scraping the real encode's stderr, because that stderr is
    deliberately inherited so live `-stats` keeps flowing to the log viewer;
    capturing it to parse a banner would change that behaviour.
    """
    if codec in _CODEC_LIB_EMITTED:
        return
    _CODEC_LIB_EMITTED.add(codec)
    spec = _CODEC_LIBS.get(codec)
    if spec is None:
        return   # h264 — see _CODEC_LIBS
    encoder, pat = spec
    import subprocess
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "info",
             "-f", "lavfi", "-i", "nullsrc=s=64x64:d=0.04",
             "-c:v", encoder, "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001 — encoder missing, or the probe hung
        return
    m = pat.search((out.stderr or "") + (out.stdout or ""))
    if m:
        emit(f"[[ENCODER-CODECLIB codec={codec} lib={encoder} version={m.group(1)}]]")


def emit_boot_ami() -> None:
    """Report the AMI this worker's instance actually booted from, via IMDS
    (free, no IAM). The Go server compares it to the pre-baked worker AMI to
    decide whether the cache was hit — a definitive signal, not a timing guess.
    No-op off-instance (local encodes)."""
    import urllib.request
    try:
        tok = urllib.request.Request(
            "http://169.254.169.254/latest/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        token = urllib.request.urlopen(tok, timeout=2).read().decode()
        req = urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/ami-id",
            headers={"X-aws-ec2-metadata-token": token})
        ami = urllib.request.urlopen(req, timeout=2).read().decode().strip()
    except Exception:  # noqa: BLE001 — not on EC2, or IMDS unavailable
        return
    if ami:
        emit(f"[[ENCODER-BOOT ami={ami}]]")


# ---------------------------------------------------------------------------
# ffmpeg progress parser
# ---------------------------------------------------------------------------

# How often a running stage reports its percent. This is a TRANSPORT rate, and
# it is deliberately decoupled from ffmpeg's tick rate (_FFMPEG_STATS_PERIOD).
#
# It used to be 0.2s, chosen to sit just under the 0.25s stats period so that no
# ffmpeg tick was ever dropped. That is the right instinct for a local pipe and
# the wrong one for a message: at 0.2s a marker is superseded 250ms later, and
# measured against real runs (~8,800 encode-seconds per 4K ladder) it produced
# ~35,000 progress markers per run — every one of them written to CloudWatch,
# re-read by the orchestrator, re-printed, and re-scanned by the Go server.
#
# Nothing downstream can use that resolution. The UI renders a bar and polls on
# a multi-second cadence, so ~8 of every 9 markers were paid for and discarded.
#
# 5s, not the 2s first tried. A heartbeat is the ONE thing here that may lag:
# chunk start and chunk end are events the grid is really showing and they now
# bypass the publisher's buffer entirely (telemetry.is_heartbeat), so slowing
# the ticks between them costs nothing anyone can see. It halves the dominant
# term in message volume, and volume is what the consumer is limited by — SQS
# caps a receive at 10 messages, so throughput is round trips, not bytes.
#
# Dropping ticks is the intended behaviour, not a tolerated side effect: percent
# is a GAUGE, so the newest value is the only one that matters and a missed one
# costs nothing. Records (TIMING/SPEED/VMAF) are emitted unthrottled elsewhere.
_MIN_EMIT_INTERVAL_S = float(os.environ.get("ENCODER_PROGRESS_INTERVAL_S", "5.0"))

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
    pct_lo: float = 0.0,
    pct_hi: float = 100.0,
    terminal: bool = True,
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

    # Log the EXACT argv, shell-quoted so it can be pasted and re-run verbatim.
    #
    # Written because a local and a cloud encode of the same rung delivered 25%
    # different bitrates (#167) and there was no record of what either had
    # actually been asked to do — the settings had to be reconstructed by
    # reading three files and inferring which env vars each path passed. The
    # command line is the ground truth that ends that class of argument in one
    # diff, and it cannot be recovered after the fact.
    #
    # `full_cmd`, not `cmd`: -progress and -stats_period are appended here, so
    # logging `cmd` would print something that is not what ran. Emitted for
    # every invocation, so a two-pass encode logs both passes separately.
    #
    # One line per encode against ffmpeg's own per-second stats output, so the
    # volume is negligible. Deliberately NOT gated behind a debug flag: it is
    # only useful if it was already on when the run happened.
    print(f"[ffmpeg] {shlex.join(full_cmd)}", flush=True)

    # Map ffmpeg's own 0-100% onto the caller's [pct_lo, pct_hi] band, so a
    # phase whose ffmpeg step is only the middle of the stage (mezzanine/audio:
    # download → copy → upload) fills one continuous bar instead of jumping.
    # Defaults (0..100, terminal) leave variant encodes unchanged.
    def _scale(p: float) -> float:
        return pct_lo + max(0.0, min(100.0, p)) / 100.0 * (pct_hi - pct_lo)

    emit_stage(stage_key, "running", pct_lo)

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
                    emit_stage(stage_key, "running", _scale(percent))
                    last_emit = now
            elif key == "progress" and value == "end":
                break
    finally:
        rc = proc.wait()

    if rc != 0:
        emit_stage(stage_key, "failed", pct_lo)
        raise subprocess.CalledProcessError(rc, full_cmd)

    # terminal → the stage is complete (done at pct_hi, normally 100); otherwise
    # this ffmpeg step is a mid-stage segment, so just advance to pct_hi and let
    # the caller emit the final "done" after its remaining work (e.g. upload).
    emit_stage(stage_key, "done" if terminal else "running", pct_hi)
