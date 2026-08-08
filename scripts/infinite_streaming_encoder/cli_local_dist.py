#!/usr/bin/env python3
"""Distributed local encode orchestrator — the local twin of cli_cloud.py.

Runs the full ABR DAG as a durable Temporal workflow, fanning phase activities
across the worker pool (`temporal_worker` on the master + every remote box) with
MinIO (S3-compatible) as the shared store, instead of AWS Batch + S3. The Go
server launches this as one detached worker exactly like it launches cli_local /
cli_cloud; the phases it dispatches are ordinary `cli_phase` workers (unchanged)
reading and writing MinIO.

Resilience (the whole point): every chunk is an idempotent unit keyed by its
output object in MinIO, and Temporal owns the routing and retry. An activity
whose worker exits or vanishes mid-encode is rescheduled onto another worker; a
chunk whose output already exists in MinIO is skipped. With no remote worker
reachable the pool collapses to {master} and the run still completes — so the
system works with and without the extra boxes and adapts if one goes away.

There was a second implementation of exactly this — a hand-rolled `pool`
backend that ssh'd to remote Docker daemons and did its own dispatch, retry and
failover. Temporal replaced it and it was not removed, so it survived as the
CLI's DEFAULT while the Go server always passed `--backend temporal` to opt
out. Two implementations of one thing means one of them stops getting fixes:
#173 gave packaging its SEGMENT_DURATION/PARTIAL_DURATION on the Temporal path
and left the pool path passing an empty env. It is gone; see --backend.

DAG:
  1. upload source -> MinIO
  2. mezzanine, audio
  3. per (codec, rung): plan chunks; dispatch each chunk as a `variant`
     activity across the workers, Temporal retrying; -> chunk mp4s in MinIO
  4. package-all + fragments + hls per codec -> output_<codec>/ in MinIO
  5. download output_<codec>/ -> <output-dir>/<stem>_<codec>/ for the Go server
     to move into OUTPUT_DIR.

Chunk-plan authority: THIS orchestrator plans the chunks once (from the
mezzanine it just built) and hands every worker its explicit
(index, start, duration); `cli_phase variant` no longer derives a plan of its
own. package-all still globs whatever chunk files land. Previously all three
re-derived the plan independently and were kept in step by a COALESCE_RUNT_TAIL
env flag — agreement by convention, which held only while every process probed
the same duration.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import signal
import sys
from pathlib import Path

from infinite_streaming_encoder.chunking import plan_chunks
from infinite_streaming_encoder.encode_variants import _coalesce_runt_tail, variant_stage_key
from infinite_streaming_encoder.ffprobe import ProbeError, probe
from infinite_streaming_encoder.ladder import (
    Rung, get_ladder, ladder_extra_args, ladder_passes,
    parse_bitrate_override, select_rungs,
    ladder_maxrate_percent, ladder_bufsize_multiplier,
    ladder_segment_duration, ladder_gop_duration, ladder_partial_duration,
)
from infinite_streaming_encoder.progress import Stage, emit_plan, emit_stage

_SEGMENT_DURATION_S = 6.0
# Fold a final chunk shorter than this into its predecessor (mirrors
# encode_variants._MIN_TAIL_CHUNK_S; the worker does the same via
# the plan it ships, so the dispatched spans are exactly what gets encoded).
_MAX_RETRIES_PER_CHUNK = 3












# ---------------------------------------------------------------------------
# MinIO (host-side boto3, endpoint-aware — mirrors cli_phase._s3)
# ---------------------------------------------------------------------------

def _s3():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=os.environ["S3_ENDPOINT_URL"],
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )


def _ensure_bucket(bucket: str) -> None:
    """Create the staging bucket if it doesn't exist. On a fresh MinIO — a first
    run on a new machine, or a wiped cluster — the bucket won't exist yet and the
    first put_object fails with NoSuchBucket. Idempotent: HEAD first, create only
    when missing, and tolerate a create race. (local-dist always targets MinIO via
    S3_ENDPOINT_URL, so a region-less create_bucket is correct here.)"""
    s3 = _s3()
    try:
        s3.head_bucket(Bucket=bucket)
        return
    except Exception:
        pass
    try:
        s3.create_bucket(Bucket=bucket)
        print(f"[dist] created staging bucket s3://{bucket}", flush=True)
    except Exception as exc:
        try:
            s3.head_bucket(Bucket=bucket)  # someone else won the race — fine
        except Exception:
            raise exc


def _progress_cb(stage_key: str, total_bytes: int):
    """A boto3 transfer Callback that emits throttled ENCODER-STAGE progress
    (every ~2%) for a byte transfer — so the UI shows a moving bar for the
    otherwise-dark source upload / output download."""
    total = max(1, total_bytes)
    sent = [0]
    last = [-5.0]

    def cb(n: int) -> None:
        sent[0] += n
        pct = min(99.0, sent[0] / total * 100.0)
        if pct - last[0] >= 2.0:
            last[0] = pct
            emit_stage(stage_key, "running", pct)

    return cb


def _upload_source(local: Path, bucket: str, key: str) -> None:
    _ensure_bucket(bucket)
    total = local.stat().st_size
    print(f"[dist] uploading source ({total / 1e6:.0f} MB) -> s3://{bucket}/{key}",
          flush=True)
    emit_stage("upload:source", "running", 0.0)
    _s3().upload_file(str(local), bucket, key,
                      Callback=_progress_cb("upload:source", total))
    emit_stage("upload:source", "done", 100.0)


def _object_exists(bucket: str, key: str) -> bool:
    try:
        _s3().head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


# Content-addressed staging for the mezzanine, SHARED across jobs. The
# mezzanine is a pure stream-copy of the source (no padding/burnin/partial —
# those apply later, per chunk), so its bytes depend only on the source file.
# Two jobs on the same source therefore want the SAME mezzanine, whatever their
# codec/ladder/overlay settings. Key = sha256(basename:size:mtime_ns): cheap
# (no full read) and it invalidates the moment a file is edited (size or mtime
# moves). Lives at `mezz-cache/<key>/` — OUTSIDE the per-job `jobs/<id>/`
# prefix, so job cleanup (delete_prefix) never reclaims it; dist_staging.gc
# sweeps mezz-cache separately on a longer idle window. The chunk phase's
# per-worker /tmp/mezz-cache is keyed off this URI too, so a box that already
# encoded this source skips the MinIO download entirely (symlinks its copy).
MEZZ_CACHE_PREFIX = "mezz-cache"


def _mezz_cache_rel(input_path: Path) -> str:
    """Bucket-relative prefix (`mezz-cache/<key>`) for this source's shared
    mezzanine. See MEZZ_CACHE_PREFIX."""
    st = input_path.stat()
    sig = f"{input_path.name}:{st.st_size}:{st.st_mtime_ns}"
    key = hashlib.sha256(sig.encode()).hexdigest()[:32]
    return f"{MEZZ_CACHE_PREFIX}/{key}"


def _touch_prefix(bucket: str, rel: str) -> None:
    """Refresh a shared-cache prefix's idle clock on a cache HIT. The staging
    GC (dist_staging.gc) evicts a mezzanine idle past MEZZ_CACHE_MAX_AGE_S, and
    keys idle off the NEWEST object — but reads don't move LastModified. So a
    mezzanine reused by job after job would look progressively staler and could
    be reclaimed out from under a running job. Dropping a tiny marker resets the
    prefix's clock, so anything actively reused is kept; only a genuinely unused
    mezzanine ages out. Best-effort — never fail a job over it."""
    try:
        _s3().put_object(Bucket=bucket, Key=f"{rel}/.touch", Body=b"")
    except Exception:  # noqa: BLE001
        pass


def _reclaim_staging(bucket: str, prefix: str, keep: bool) -> None:
    """Delete this job's MinIO staging now that the outputs are on disk (#93).

    Everything under `jobs/<id>-<base>/` — the source upload, the mezzanine,
    every per-chunk encode, the variants, the packaged HLS — is dead weight
    once `_download_prefix` has pulled the output down; nothing downstream
    reads it (a Retry submits a NEW job id, so it stages afresh). Left alone
    it accumulated ~2.3 GB per job forever.

    Best-effort by design: a reclaim failure must never turn a successful
    encode into a failed one. Whatever is left behind is picked up by the
    server's age-based GC and the bucket lifecycle rule (dist_staging.gc /
    ensure_lifecycle).
    """
    if keep:
        print(f"[dist] --keep-staging: leaving s3://{bucket}/{prefix}/", flush=True)
        return
    try:
        from infinite_streaming_encoder import dist_staging
        report = dist_staging.delete_prefix(f"{prefix}/", bucket=bucket)
    except Exception as e:  # noqa: BLE001
        print(f"[dist] staging cleanup skipped (non-fatal): {e}", flush=True)
        return
    for a in report.actions:
        print(f"[dist] staging {a.action}: {a.id}"
              f"{f' — {a.detail}' if a.detail else ''}", flush=True)


def _download_prefix(bucket: str, prefix: str, dest: Path) -> int:
    """Download every object under `prefix` into `dest`, preserving the tail
    path after `prefix`. Emits throttled download:outputs progress by bytes.
    Returns the file count."""
    s3 = _s3()
    dest.mkdir(parents=True, exist_ok=True)
    objs = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        objs.extend(page.get("Contents", []))
    total = sum(o.get("Size", 0) for o in objs)
    emit_stage("download:outputs", "running", 0.0)
    cb = _progress_cb("download:outputs", total)
    n = 0
    for obj in objs:
        rel = obj["Key"][len(prefix):].lstrip("/")
        if not rel:
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(bucket, obj["Key"], str(out), Callback=cb)
        n += 1
    emit_stage("download:outputs", "done", 100.0)
    return n


# ---------------------------------------------------------------------------
# Phase container execution
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Chunk fan-out with failover
# ---------------------------------------------------------------------------







def _parse_vmaf_estimates(items: list) -> dict:
    """Parse --vmaf-estimate 'CODEC/LABEL:VMAF:CLAMPED' items into a lookup keyed
    by (codec, label) -> (vmaf: float, clamped: bool). Malformed entries are
    skipped — this is cosmetic overlay data, never worth failing a run over."""
    out: dict = {}
    for it in items or []:
        try:
            key, vmaf, clamped = it.rsplit(":", 2)
            codec, label = key.split("/", 1)
            out[(codec, label)] = (float(vmaf), clamped == "1")
        except (ValueError, AttributeError):
            continue
    return out


def _rung_dict(codec: str, r, ests: dict) -> dict:
    """Plan entry for one rung, with the design-time VMAF estimate attached when
    Go supplied one for (codec, label). temporal_worker passes est_vmaf on to
    cli_phase --est-vmaf so the worker burns it into the overlay."""
    d = {"label": r.label, "width": r.width, "height": r.height, "bitrate": r.bitrate}
    e = ests.get((codec, r.label))
    if e:
        d["est_vmaf"], d["est_vmaf_clamped"] = e
    return d


# <codec>_<tier>_chunk<NNN>.mp4 — a staged chunk OUTPUT object (tier may carry an
# ordinal suffix like 540p_2, hence the greedy middle group). Excludes .mp4.done.
_CHUNK_OBJ_RE = re.compile(r"^([^_]+)_(.+)_chunk(\d+)\.mp4$")


def _emit_reused(key: str) -> None:
    print(f"[[ENCODER-REUSED key={key}]]", flush=True)


def _emit_reused_chunks(bucket: str, work_prefix: str) -> None:
    """Flag chunks a PRIOR run already staged under work_prefix as reused, so a
    resume shows them distinctly instead of as fresh encodes. This run hasn't
    produced any yet; cli_phase skips the encode when the output exists, so the
    cell should read 'reused'. Best-effort; never breaks the run."""
    try:
        paginator = _s3().get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=f"{work_prefix}/"):
            for obj in page.get("Contents", []):
                m = _CHUNK_OBJ_RE.match(obj["Key"].rsplit("/", 1)[-1])
                if m:
                    _emit_reused(f"encode:{m.group(1)}:{m.group(2)}:chunk{int(m.group(3))}")
    except Exception:  # noqa: BLE001 — cosmetic; must never fail the run
        return






# ---------------------------------------------------------------------------
# DAG
# ---------------------------------------------------------------------------

def _emit_plan(rungs_by_codec: dict[str, list[Rung]], chunks_by_variant: dict,
               has_audio: bool) -> None:
    """Announce upload + mezzanine + every chunk + audio + package/fragments/hls
    + download, so the UI lays out the full grid up front (like the cloud plan).
    The upload/download rows animate from the orchestrator's own S3 transfers."""
    stages: list[Stage] = [
        Stage(key="upload:source", label="upload source"),
        Stage(key="mezzanine", label="mezzanine"),
    ]
    for codec, rungs in rungs_by_codec.items():
        for rung in rungs:
            n = chunks_by_variant[(codec, rung.label)]
            for i in range(n):
                stages.append(Stage(
                    key=variant_stage_key(codec, rung.label, i),
                    label=f"encode {codec} {rung.label} chunk{i}",
                ))
    if has_audio:
        stages.append(Stage(key="audio", label="audio"))
    for codec in rungs_by_codec:
        stages.append(Stage(key=f"package:{codec}", label=f"package {codec}"))
        stages.append(Stage(key=f"fragments:{codec}", label=f"fragments {codec}"))
        stages.append(Stage(key=f"hls:{codec}", label=f"HLS {codec}"))
    stages.append(Stage(key="download:outputs", label="download outputs"))
    emit_plan(stages)


def _emit_commercial_cost(rungs_by_codec, info, input_path,
                          hevc_two_pass=True) -> None:
    """Print an ENCODER-COMMERCIAL marker with three cost baselines for this
    ladder: a commercial cloud encoder + AWS MediaConvert (both per output-minute
    of SOURCE duration) and our own AWS Batch spot fleet (per ENCODING hour —
    compute-based, so it weights each variant's real work). Best-effort."""
    try:
        from infinite_streaming_encoder.commercial_cloud import (
            estimate_usd, mediaconvert_usd, aws_spot_usd, aws_ondemand_usd)
        try:
            src_mbps = input_path.stat().st_size * 8 / (info.duration_s or 1) / 1e6
        except OSError:
            src_mbps = 0.0
        usd = estimate_usd(rungs_by_codec, info.duration_s, fps=float(info.fps),
                           has_audio=info.has_audio, src_mbps=src_mbps)
        mc = mediaconvert_usd(rungs_by_codec, info.duration_s, fps=float(info.fps))
        aws = aws_spot_usd(rungs_by_codec, info.duration_s,
                           hevc_two_pass=hevc_two_pass)
        aws_od = aws_ondemand_usd(rungs_by_codec, info.duration_s,
                                  hevc_two_pass=hevc_two_pass)
        print(f"[[ENCODER-COMMERCIAL commercial={usd:.4f} mediaconvert={mc:.4f} "
              f"aws={aws:.4f} aws_od={aws_od:.4f}]]", flush=True)
    except Exception:  # noqa: BLE001 — cost estimate is cosmetic, never fail a run
        pass




def _resolve_chunk_duration_s(requested_s: float, content_duration_s: float) -> float:
    """Resolve the requested --chunk-duration. A value <= 0 means "whole variant"
    (the UI's "whole" option arrives as 0 from the Go control plane): return the
    smallest whole-segment multiple that spans the clip, so plan_chunks yields
    exactly ONE chunk per variant — no chunk boundaries, the variant is encoded
    in a single continuous pass. A positive value is used verbatim."""
    if requested_s > 0:
        return requested_s
    seg = _SEGMENT_DURATION_S
    n_segments = int(content_duration_s // seg) + (1 if content_duration_s % seg else 0)
    return max(1, n_segments) * seg


def _resolve_plan(args, info):
    """Shared prep: resolve rungs_by_codec + the coalesced chunk PLAN from the
    source probe. Used by both the pool DAG and the Temporal plan.

    Returns the chunks themselves, not just how many: this orchestrator is the
    sole authority on the boundaries and hands each worker its explicit span.
    Workers no longer run plan_chunks, so a count alone is not enough."""
    ladder_def = get_ladder(args.ladder)
    overrides = {"hevc": parse_bitrate_override(args.bitrate_override_hevc),
                 "h264": parse_bitrate_override(args.bitrate_override_h264), "av1": {}}
    codecs = {"both": ["hevc", "h264"], "all": ["hevc", "h264", "av1"]}.get(
        args.codec, [args.codec])
    rungs_by_codec: dict[str, list[Rung]] = {}
    for codec in codecs:
        rr = select_rungs(ladder_def, codec, args.max_res, info.width, overrides[codec],
                          min_res=args.min_res)
        if rr:
            rungs_by_codec[codec] = rr
    chunks = _coalesce_runt_tail(
        plan_chunks(info.duration_s, args.chunk_duration_s, _SEGMENT_DURATION_S))
    return rungs_by_codec, chunks


# Map a Temporal activity_id (set in EncodeWorkflow._phase) back to the UI stage
# key, so activity progress drives the same per-chunk grid the pool backend does.
_ENC_ACT_RE = re.compile(r"^enc-(hevc|h264|av1)-(.+)-c(\d+)$")


def _stage_key_for(activity_id: str) -> str | None:
    if activity_id in ("mezzanine", "audio"):
        return activity_id
    m = _ENC_ACT_RE.match(activity_id)
    if m:
        return f"encode:{m.group(1)}:{m.group(2)}:chunk{m.group(3)}"
    if activity_id.startswith("pkg-"):
        return f"package:{activity_id[4:]}"
    if activity_id.startswith("byteranges-"):
        return f"fragments:{activity_id[len('byteranges-'):]}"
    if activity_id.startswith("hls-"):
        return f"hls:{activity_id[4:]}"
    return None


async def _emit_temporal_progress(handle, EventType, emitted: dict, client=None) -> None:
    """Read the workflow history and emit an ENCODER-STAGE marker for each
    activity that changed state (scheduled→running, completed→done). Cheap
    enough at our scale (tens of activities); the Go server scans these off the
    orchestrator's stdout exactly like the single-container path."""
    try:
        hist = await handle.fetch_history()
    except Exception:  # noqa: BLE001 — progress is best-effort, never fail the run
        return
    sched: dict[int, str] = {}
    states: dict[str, str] = {}
    hosts: dict[str, str] = {}   # activity_id -> worker identity (which box ran it)
    for e in hist.events:
        et = e.event_type
        if et == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            aid = e.activity_task_scheduled_event_attributes.activity_id
            sched[e.event_id] = aid
            # SCHEDULED means QUEUED, not running. This used to say "running",
            # so every chunk lit up the moment the workflow fanned out and the
            # grid claimed work was underway while nothing was executing —
            # 30 chunks at once on a 3-machine farm. It also made the machine
            # timeline look broken rather than honest: a box legitimately had no
            # lane yet, because it genuinely had not started anything, while the
            # grid beside it showed its rungs "running".
            #
            # Promotion to running happens in _emit_fleet_cpu, off the pending
            # activity actually reaching STARTED on a worker.
            states.setdefault(aid, "queued")
        elif et == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            a = e.activity_task_started_event_attributes
            aid = sched.get(a.scheduled_event_id)
            if aid and a.identity:
                hosts[aid] = a.identity
        elif et == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            a = e.activity_task_completed_event_attributes
            aid = sched.get(a.scheduled_event_id)
            if aid:
                states[aid] = "done"
                # Relay the markers the worker collected (#141). Without this
                # the VMAF audit runs on every chunk, costs real time, and is
                # discarded — the Go scanner tails the ORCHESTRATOR, and workers
                # print to their own stdout which nothing forwards.
                if client is not None and emitted.get(f"relay:{aid}") is None:
                    emitted[f"relay:{aid}"] = True
                    for marker in _activity_markers(a, client):
                        print(marker, flush=True)
    for aid, st in states.items():
        key = _stage_key_for(aid)
        if not key:
            continue
        # Which machine ran this stage → colours the UI chunk/phase plot by box.
        host = hosts.get(aid)
        if host and emitted.get(f"host:{aid}") != host:
            emitted[f"host:{aid}"] = host
            print(f"[[ENCODER-HOST key={key} instance={host}]]", flush=True)
        if emitted.get(aid) != st:
            emitted[aid] = st
            emit_stage(key, st, 100.0 if st == "done" else 0.0)


def _activity_markers(attrs, client) -> list[str]:
    """Decode an ActivityTaskCompleted result into the markers to relay.

    Best-effort and defensive: an activity from an older worker returns None,
    and a decode failure must not stop progress reporting for the whole run.
    Anything that isn't a plain [[ENCODER-…]] string is dropped rather than
    printed, so a malformed result can't inject arbitrary lines into the job log
    that the Go scanner would then try to parse.
    """
    try:
        payloads = getattr(attrs, "result", None)
        if not payloads or not payloads.payloads:
            return []
        vals = client.data_converter.payload_converter.from_payloads(
            list(payloads.payloads))
    except Exception:  # noqa: BLE001 — relaying is best-effort, never fail a run
        return []
    out: list[str] = []
    for v in vals:
        if isinstance(v, list):
            out += [m for m in v
                    if isinstance(m, str) and m.startswith("[[ENCODER-")]
    return out


# Temporal PendingActivityState.STARTED — an activity actually executing on a
# worker right now (vs SCHEDULED = still queued). Stable enum value.
_PA_STARTED = 2
_HOST_SEEN: dict = {}   # chunk activity_id -> (machine, version), dedups ENCODER-HOST
_MACHINE_VERSION: dict = {}  # machine -> build (image content hash), once it says
_RUN_SEEN: dict = {}    # activity_id -> 1 once promoted queued -> running


def fleet_marker(machine: str, busy, perf, version: str, chunks: list) -> str:
    """Build one ENCODER-FLEET line. Pure, so the Go-side contract is testable.

    `version` goes BEFORE chunks deliberately: the Go pattern reads chunks as
    `[^\\]]*` (it has to, for the `|` separators), so any field after it would be
    swallowed into the chunk list rather than parsed. Omitted entirely when
    unknown — an absent field means "did not say", which the reader must not
    confuse with agreement.
    """
    ver = f"version={version} " if version else ""
    return (f"[[ENCODER-FLEET machine={machine} busy={busy} perf={perf} "
            f"{ver}chunks={'|'.join(chunks[:16])}]]")


def should_emit_host(aid: str, machine: str, version: str, seen: dict) -> bool:
    """Is (chunk on machine, running version) NEW information worth a marker?

    Deduped on the PAIR, not on machine alone. Machine alone looks right and is
    the version that shipped first: it re-tags on failover, which is the case
    everyone thinks about. But a chunk is usually first seen via
    last_worker_identity BEFORE its worker's first heartbeat arrives, so the
    first emission carries no version — and keying on machine then suppressed
    every later emission, permanently. Measured on a real run: 2 of 14 chunks
    got a version.

    Keying on the pair means learning the build re-emits exactly once. Mutates
    `seen` when it returns True, so callers cannot record and test out of step.
    """
    if not machine:
        return False
    if seen.get(aid) == (machine, version):
        return False
    seen[aid] = (machine, version)
    return True


def host_marker(key: str, machine: str, version: str = "") -> str:
    """Build one ENCODER-HOST line — which box, and which build, ran a chunk.

    The version is what makes the PER-CHUNK record in run.json self-describing
    later; `instance` alone answers "where" but never "running what" (#248).
    """
    ver = f" version={version}" if version else ""
    return f"[[ENCODER-HOST key={key} instance={machine}{ver}]]"


async def _emit_fleet_cpu(handle, client) -> None:
    """Emit one ENCODER-FLEET marker per machine with its live CPU (busy/perf,
    from the workers' heartbeat details) AND the chunks currently RUNNING on it
    (STARTED pending activities, keyed by last_worker_identity). Both come from a
    single describe_workflow — the Temporal-native backchannel, no ssh, no extra
    channel. Best-effort — never fails the run."""
    try:
        desc = await handle.describe()
    except Exception:  # noqa: BLE001
        return
    raw = getattr(desc, "raw_description", None)
    if raw is None:
        return
    pcv = client.data_converter.payload_converter

    # PASS 1 — decode every pending activity once, and harvest each box's build
    # into _MACHINE_VERSION before anything is emitted.
    #
    # A box's build is a property of the BOX, not of the chunk whose heartbeat
    # happened to carry it. Reading it off `cpu` per chunk (the first cut of
    # this) tagged 2 of 14 chunks in a real run: a chunk is usually first seen
    # via last_worker_identity BEFORE its first heartbeat lands, so its marker
    # went out with no version — and the dedup below then suppressed the re-emit
    # forever. Harvesting first means one chunk's heartbeat teaches every other
    # chunk on that box, in the same poll.
    seen: list = []
    for pa in getattr(raw, "pending_activities", []):
        machine = getattr(pa, "last_worker_identity", "") or ""
        cpu = None
        hb = getattr(pa, "heartbeat_details", None)
        if hb and hb.payloads:
            try:
                vals = pcv.from_payloads(list(hb.payloads))
                cpu = next((v for v in vals if isinstance(v, dict) and "machine" in v), None)
            except Exception:  # noqa: BLE001
                cpu = None
        if cpu and cpu.get("machine"):
            machine = cpu["machine"]
        if machine and cpu and cpu.get("version"):
            _MACHINE_VERSION[machine] = cpu["version"]
        seen.append((pa, machine, cpu))

    # PASS 2 — stage promotion, per-machine aggregation, and the markers.
    agg: dict = {}   # machine -> {"busy", "perf", "chunks": [activity_id, ...]}
    for pa, machine, cpu in seen:
        # A STARTED pending activity is one a worker is executing RIGHT NOW.
        # Promote its stage here — for every activity kind, and before the
        # machine checks below, which `continue` when identity is unknown. An
        # activity with no reported identity is still running.
        _aid = getattr(pa, "activity_id", "") or ""
        if getattr(pa, "state", 0) == _PA_STARTED and _aid:
            _k = _stage_key_for(_aid)
            if _k and _RUN_SEEN.get(_aid) != 1:
                _RUN_SEEN[_aid] = 1
                emit_stage(_k, "running", 0.0)
        if not machine:
            continue
        a = agg.setdefault(machine, {"busy": 0, "perf": 0, "chunks": [],
                                     "version": ""})
        if cpu:
            a["busy"], a["perf"] = cpu.get("busy", 0), cpu.get("perf", 0)
        # Which BUILD this box is running (#248), from pass 1 rather than from
        # this one chunk's heartbeat — so the fleet line reports it even on a
        # poll where only the chunks WITHOUT fresh heartbeat details were seen.
        # Absent from workers older than the heartbeat field, which is itself
        # the answer: a box that cannot say is a box running something old.
        a["version"] = _MACHINE_VERSION.get(machine, "") or a["version"]
        aid = getattr(pa, "activity_id", "") or ""
        if getattr(pa, "state", 0) == _PA_STARTED and aid.startswith("enc-"):
            a["chunks"].append(aid)
            key = _stage_key_for(aid)
            if key:
                # Colour the grid cell by the machine running it, from the START —
                # authoritative here (last_worker_identity) so the cell matches the
                # fleet swatch + chip for the whole encode, not just at completion.
                #
                # version rides along so the PER-CHUNK record in run.json says
                # which build encoded it, not just which box (#248) — the half
                # that answers the question months later, about a run nobody was
                # watching. It comes from _MACHINE_VERSION (the box), not from
                # this chunk's own heartbeat, which is usually not there yet the
                # first time the chunk is seen.
                #
                # Deduped on (machine, version), NOT machine alone: failover
                # re-tags, AND learning the build re-emits exactly once so a
                # chunk first seen before any heartbeat still gets its version.
                # Keying on machine alone tagged 2 of 14 chunks in a real run —
                # the first, version-less emission suppressed every retry.
                ver = _MACHINE_VERSION.get(machine, "")
                if should_emit_host(aid, machine, ver, _HOST_SEEN):
                    print(host_marker(key, machine, ver), flush=True)
                # Real per-chunk progress: the worker rides ffmpeg's out_time/
                # duration % on its heartbeat, so a chunk fills 0→100 as it actually
                # encodes instead of snapping 0→100 on completion.
                p = (cpu or {}).get("progress")
                if p is not None and 0 < p < 100:
                    emit_stage(key, "running", float(p))
    for m, a in agg.items():
        print(fleet_marker(m, a["busy"], a["perf"], a["version"], a["chunks"]),
              flush=True)


def run_temporal(args: argparse.Namespace) -> int:
    """Temporal backend: prep locally, then hand the DAG to a durable
    EncodeWorkflow that fans activities across the worker pool. The heavy
    lifting + failover live in Temporal; this just plans, uploads, starts the
    workflow, waits, and pulls the output down."""
    import asyncio
    from temporalio.client import Client

    input_path = Path(args.input)
    if not os.access(input_path, os.R_OK):
        print(f"error: cannot read input: {input_path}", file=sys.stderr)
        return 1
    try:
        info = probe(input_path)
    except ProbeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    # Resolve "whole variant" (0) to a clip-spanning single chunk before the
    # value fans out to _resolve_plan / the workflow plan / worker env.
    args.chunk_duration_s = _resolve_chunk_duration_s(
        args.chunk_duration_s, info.duration_s)

    bucket = args.s3_bucket
    prefix = args.job_prefix.strip("/")
    src_key = f"{prefix}/input{input_path.suffix}"
    rungs_by_codec, chunks = _resolve_plan(args, info)
    n_chunks = len(chunks)
    if not rungs_by_codec:
        print("error: no ladder rungs fit this source", file=sys.stderr)
        return 1

    _emit_plan(rungs_by_codec,
               {(c, r.label): n_chunks for c, rr in rungs_by_codec.items() for r in rr},
               info.has_audio)
    _emit_commercial_cost(rungs_by_codec, info, input_path,
                          hevc_two_pass=not args.hevc_single_pass)
    # Flag chunks left over from a prior (cancelled/failed) run as reused, so a
    # resume shows them distinctly instead of as fresh encodes.
    _emit_reused_chunks(bucket, f"{prefix}/work")

    # Cross-job mezzanine cache. If this exact source already has a completed
    # mezzanine in MinIO, the mezzanine phase will reuse it — and since the
    # source is ONLY consumed by that phase, we can skip uploading it entirely
    # (the biggest single I/O in the prep). On a miss, upload as usual and the
    # mezzanine phase populates the shared cache for the next job.
    mezz_rel = _mezz_cache_rel(input_path)
    mezz_prefix = f"s3://{bucket}/{mezz_rel}"
    if _object_exists(bucket, f"{mezz_rel}/mezzanine.mp4.done"):
        print(f"[dist] mezzanine cache HIT {mezz_prefix} — skipping source "
              f"upload + mezzanine (same source encoded before)", flush=True)
        _touch_prefix(bucket, mezz_rel)  # keep it fresh while this job reuses it
        emit_stage("upload:source", "done", 100.0)
    else:
        _upload_source(input_path, bucket, src_key)

    # Pass count + extra_args come from the ladder profile now; hevc_single_pass
    # remains a per-encode override forcing HEVC single-pass.
    ladder_def = get_ladder(args.ladder)
    two_pass = {c: ladder_passes(ladder_def, c) == 2 for c in ("h264", "hevc", "av1")}
    if args.hevc_single_pass:
        two_pass["hevc"] = False
    _vmaf_ests = _parse_vmaf_estimates(getattr(args, "vmaf_estimate", []))
    plan = {
        "bucket": bucket, "job_prefix": prefix, "src_key": src_key,
        "mezz_prefix": mezz_prefix, "job_rank": args.job_rank,
        "has_audio": info.has_audio, "chunk_duration_s": args.chunk_duration_s,
        "n_chunks": n_chunks,
        # The boundaries themselves, so temporal_worker can hand each activity
        # its explicit span instead of every worker re-deriving the plan from its
        # own probe (agreement by contract, not by convention).
        "chunks": [{"index": c.index, "start_s": c.start_s,
                    "duration_s": c.duration_s} for c in chunks],
        # What those boundaries were planned against, for the worker's check.
        "content_duration_s": info.duration_s,
        "measure_vmaf": args.measure_vmaf, "burnin": args.burnin,
        "vmaf_prescale": getattr(args, "vmaf_prescale", False),
        # Ladder-level VBV. The workers read these as MAXRATE_PERCENT /
        # BUFSIZE_MULT; without them cli_phase falls back to the module defaults
        # (124% / 0.25x) and the ladder's shaping is silently discarded — which
        # is what local-dist did until #167, encoding apple-uniq-live with a 2.5x
        # looser buffer than it specifies and delivering ~25% more bits than the
        # same rung on cloud.
        "maxrate_percent": ladder_maxrate_percent(ladder_def),
        "bufsize_multiplier": ladder_bufsize_multiplier(ladder_def),
        # Profile timing, same reasoning as the VBV knobs above. GOP sets keyint
        # on the variant encode; PARTIAL drives LL-HLS parts in packaging and 0
        # turns them off entirely (VOD); SEGMENT is read by BOTH. A job-level
        # override wins over the ladder when the control plane sent one.
        "segment_duration": (float(args.segment_duration)
                             if args.segment_duration not in (None, "")
                             else ladder_segment_duration(ladder_def)),
        "gop_duration": (float(args.gop_duration)
                         if args.gop_duration not in (None, "")
                         else ladder_gop_duration(ladder_def)),
        "partial_duration": (float(args.partial_duration)
                             if args.partial_duration not in (None, "")
                             else ladder_partial_duration(ladder_def)),
        "codecs": {c: {"two_pass": two_pass[c],
                       "extra_args": ladder_extra_args(ladder_def, c),
                       "rungs": [_rung_dict(c, r, _vmaf_ests) for r in rr]}
                   for c, rr in rungs_by_codec.items()},
    }

    async def go():
        from temporalio.api.enums.v1 import EventType
        client = await Client.connect(args.temporal_address)
        wid = f"encode-{prefix.replace('/', '-')}"
        print(f"[dist] starting workflow {wid} on {args.temporal_address}", flush=True)
        handle = await client.start_workflow(
            "EncodeWorkflow", plan, id=wid, task_queue=args.temporal_task_queue)

        # Cancel plumbing: the Go control plane cancels a job by `docker stop`-ing
        # THIS orchestrator container (SIGTERM + 30s grace). The encode DAG is a
        # durable Temporal workflow that outlives us — if we just exit, it keeps
        # dispatching chunks to the remote workers. So on SIGTERM we cancel the
        # workflow; the Temporal server then delivers cancellation to every running
        # activity (each kills its ffmpeg via activity.is_cancelled()) and stops
        # scheduling new ones — even after we're gone. That's what actually stops
        # the remote chunk encodes.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass

        # Await the result while streaming per-activity progress to stdout as
        # ENCODER-STAGE markers (drives the UI chunk grid live).
        result_task = asyncio.ensure_future(handle.result())
        emitted: dict = {}
        try:
            while not result_task.done():
                if stop.is_set():
                    print("[dist] cancel requested — cancelling workflow "
                          "(stops remote chunk encodes)", flush=True)
                    try:
                        await handle.cancel()
                    except Exception as e:  # noqa: BLE001
                        print(f"[dist] workflow cancel failed: {e}", flush=True)
                    result_task.cancel()
                    return "cancelled"
                await _emit_temporal_progress(handle, EventType, emitted, client)
                await _emit_fleet_cpu(handle, client)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)  # 1s cadence
                except asyncio.TimeoutError:
                    pass
            await _emit_temporal_progress(handle, EventType, emitted, client)
            return await result_task
        finally:
            # ORPHAN GUARD. The only path above that cancels the workflow is the
            # explicit SIGTERM one. But the orchestrator can also leave with the
            # workflow still RUNNING — an exception in the progress loop, or
            # handle.result() raising when a chunk fails terminally while siblings
            # are still encoding. If we just exit, Temporal (durable) keeps
            # dispatching the remaining chunks to the workers forever, pinning the
            # CPU (the orphaned-workflow bug). So on ANY exit, if the workflow is
            # still un-terminal, cancel it. Cancellation is server-side and
            # durable, so it takes effect (and kills each activity's ffmpeg via
            # activity.is_cancelled()) even though we're about to exit.
            try:
                desc = await handle.describe()
                status = str(getattr(desc.status, "name", desc.status) or "")
                if "RUNNING" in status:
                    print("[dist] workflow still RUNNING on orchestrator exit — "
                          "cancelling to prevent an orphan", flush=True)
                    await handle.cancel()
            except Exception as e:  # noqa: BLE001
                print(f"[dist] orphan-guard cancel failed: {e}", flush=True)

    try:
        result = asyncio.run(go())
    except Exception as e:  # noqa: BLE001
        # Surface the ROOT cause, not just the generic top-level "Workflow
        # execution failed" (#116). Temporal wraps the real error:
        # WorkflowFailureError -> ActivityError -> ApplicationError(the cli_phase
        # output tail the worker relayed). Walk the chain (`.cause` on Temporal
        # failures, `__cause__` otherwise) to the deepest message.
        root, seen = e, set()
        while id(root) not in seen:
            seen.add(id(root))
            nxt = getattr(root, "cause", None) or getattr(root, "__cause__", None)
            if nxt is None:
                break
            root = nxt
        print(f"[dist] workflow failed: {e}", file=sys.stderr)
        if root is not e:
            print(f"[dist] root cause: {type(root).__name__}: {root}",
                  file=sys.stderr)
        return 1
    if result == "cancelled":
        print("[dist] cancelled — skipping output download", flush=True)
        return 130
    print(f"[dist] workflow result: {result}", flush=True)

    out_dir = Path(args.output_dir)
    downloaded = []
    for codec in rungs_by_codec:
        dest = out_dir / (f"{args.output}_{codec}"
                          + (f"_{args.output_tag}" if args.output_tag else ""))
        n = _download_prefix(bucket, f"{prefix}/out/output_{codec}/", dest)
        print(f"[dist] downloaded {n} file(s) -> {dest}", flush=True)
        downloaded.append(n)
    # Same reclaim + empty-download guard as the pool backend above.
    if downloaded and all(n > 0 for n in downloaded):
        _reclaim_staging(bucket, prefix, args.keep_staging)
    print("[dist] done", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli_local_dist")
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True, help="output stem")
    p.add_argument("--output-tag", default="", dest="output_tag",
                   help="suffix appended AFTER the codec (e.g. 'xs' -> "
                        "<stem>_<codec>_xs); blank = none")
    p.add_argument("--output-dir", required=True, dest="output_dir")
    p.add_argument("--codec", default="hevc")
    p.add_argument("--ladder", default="apple-uniq-live")
    # Job-level overrides of the ladder's profile timing. Absent -> the ladder's
    # value. Strings, not floats, so "" and "0" stay distinguishable ("0" is a
    # real setting: PARTIAL_DURATION=0 disables LL-HLS parts).
    p.add_argument("--segment-duration", default=None, dest="segment_duration")
    p.add_argument("--gop-duration", default=None, dest="gop_duration")
    p.add_argument("--partial-duration", default=None, dest="partial_duration")
    # No `choices=`: tier options are derived from the selected ladder's real
    # rung heights, which include non-standard ones (954p, 1800p...).
    p.add_argument("--max-res", default=None, dest="max_res")
    p.add_argument("--min-res", default=None, dest="min_res")
    p.add_argument("--hevc-single-pass", action="store_true", dest="hevc_single_pass")
    p.add_argument("--measure-vmaf", action="store_true", dest="measure_vmaf",
                   help="per-rendition VMAF audit after each chunk encode (slow)")
    p.add_argument("--vmaf-prescale", action="store_true", dest="vmaf_prescale",
                   help="#109: build a near-lossless pre-scaled VMAF reference "
                        "once per box (speeds the audit on slow-to-decode sources; "
                        "gated + falls back to the mezzanine)")
    p.add_argument("--no-burnin", action="store_false", dest="burnin", default=True,
                   help="disable the burnt-in text overlay on every variant; "
                        "on by default")
    p.add_argument("--vmaf-estimate", action="append", default=[], dest="vmaf_estimate",
                   metavar="CODEC/LABEL:VMAF:CLAMPED",
                   help="design-time VMAF estimate for a rung (from the Go quality "
                        "curves) to burn into the overlay, e.g. hevc/2160p:82.4:1; "
                        "repeatable, one per rung")
    p.add_argument("--bitrate-override-hevc", default=None, dest="bitrate_override_hevc")
    p.add_argument("--bitrate-override-h264", default=None, dest="bitrate_override_h264")
    p.add_argument("--chunk-duration", type=float, default=12.0,
                   dest="chunk_duration_s", help="seconds; multiple of segment (6)")
    p.add_argument("--encode-threads", type=int, default=0, dest="encode_threads",
                   help="threads/encode → slot sizing (0=2)")
    # MinIO
    p.add_argument("--s3-bucket", required=True, dest="s3_bucket")
    p.add_argument("--job-prefix", required=True, dest="job_prefix",
                   help="MinIO key prefix for this job's objects")
    p.add_argument("--job-rank", type=int, default=0, dest="job_rank",
                   help="arrival rank among active local-dist jobs (0=oldest); "
                        "folded into each chunk's Temporal priority_key so an "
                        "older job's chunks outrank a younger job's (see #99)")
    p.add_argument("--keep-staging", action="store_true", dest="keep_staging",
                   help="don't delete this job's MinIO staging after the outputs "
                        "download (default: reclaim it — see dist_staging)")
    # Pool
    p.add_argument("--worker", action="append", default=None,
                   help="repeatable: 'local' or 'ssh://user@host'")
    p.add_argument("--image-local", default="encoder:latest", dest="image_local")
    p.add_argument("--image-remote", default="ghcr.io/jonathaneoliver/infinite-streaming-encoder:latest",
                   dest="image_remote")
    p.add_argument("--remote-code-mount", default=None, dest="remote_code_mount",
                   help="host path to mount over /app/scripts/infinite_streaming_encoder on remote "
                        "workers (for a stale remote image)")
    # Temporal is the only backend. The flag survives because the Go control
    # plane passes `--backend temporal` explicitly (internal/encode/job.go), and
    # an image whose Python drops the argument while an older server still sends
    # it would fail to start every local-dist encode. Keeping it also makes an
    # old `--backend pool` invocation fail LOUDLY with argparse's "invalid
    # choice" rather than silently running something else.
    #
    # There used to be a hand-rolled `pool` backend here: a worker pool over
    # ssh'd Docker daemons with its own chunk retry and failover. Temporal
    # replaced it and it was never removed, so it survived as the CLI DEFAULT
    # while the server always opted out — the documented system (CLAUDE.md,
    # infra/local-cluster/README.md) described only Temporal. It then drifted:
    # #173 gave the packaging phases their SEGMENT_DURATION/PARTIAL_DURATION on
    # the Temporal path and left the pool path passing an empty env, so a
    # hand-run comparison silently used the wrong VBV and timing — the same
    # 25% bitrate error #168 measured, from a path nobody thought they were on.
    p.add_argument("--backend", choices=("temporal",), default="temporal")
    p.add_argument("--temporal-address", default="127.0.0.1:7233",
                   dest="temporal_address")
    p.add_argument("--temporal-task-queue", default="encode",
                   dest="temporal_task_queue")
    return p


def main() -> int:
    args = build_parser().parse_args()
    return run_temporal(args)


if __name__ == "__main__":
    sys.exit(main())
