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
  1. mezzanine, built HERE from the mounted source (the source is never staged;
     see _build_mezzanine_on_host) -> the shared mezz-cache/ in MinIO
  2. audio
  3. per (codec, rung): plan chunks; dispatch each chunk as a `variant`
     activity across the workers, Temporal retrying; -> chunk mp4s in MinIO
  4. one `package-all` per codec — DASH packaging, the fragment-granularity
     manifest and the LL-HLS playlists, from one local copy of the ladder.
     Run HERE by default (_package_on_host), straight into
     <output-dir>/<stem>_<codec>/ for the Go server to move into OUTPUT_DIR;
     with --no-host-package it is dispatched as an activity, writes
     output_<codec>/ to MinIO, and step 5 fetches it back.
  5. download output_<codec>/ -> <output-dir>/<stem>_<codec>/ (only for codecs
     step 4 did not package here).

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
import contextlib
import fcntl
import hashlib
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace as dc_replace
from pathlib import Path

from infinite_streaming_encoder.chunking import plan_chunks, variant_object_name
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
    otherwise-dark output download."""
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


# Activity ids whose stage rows THIS process already drove to completion, so the
# workflow-history reader must not re-announce them.
#
# The mezzanine is built here now (_build_mezzanine_on_host), and the workflow
# still schedules its mezzanine activity — which finds the .done and returns
# immediately. Without this the history reader would see that activity SCHEDULED
# and emit `mezzanine queued`, walking a row that is already done at 100% back to
# an empty cell. Nothing downstream guards against that: emit_stage is a plain
# print and the Go server's upsertStage is last-writer-wins, which is exactly why
# cli_batch grew its own _emit_stage chokepoint for the same class of race.
#
# Keyed by ACTIVITY id, not stage key, so it suppresses one specific announcer
# rather than muting a row.
#
# Not named _HOST_*: in this file `host` already means "which BOX ran a chunk"
# (_HOST_SEEN, host_marker, ENCODER-HOST), which is a different question from
# "which PROCESS ran this phase".
_SELF_RUN_STAGES: set = set()


# The running prefetcher, or None. Module-level for the same reason
# _SELF_RUN_STAGES is: the completion signal is read deep inside the progress
# poll, which has no business taking a downloader as a parameter.
_PREFETCH: "_ChunkPrefetcher | None" = None


class _ChunkPrefetcher:
    """Download each chunk as it finishes encoding, so packaging doesn't (#311).

    Packaging pulls the whole ladder out of staging in one burst AFTER the last
    chunk lands — 3.4-3.5 GB, 41-64s, half to two thirds of the package phase,
    all of it serial with nothing. The bytes are ready long before that: the
    first chunk of a 40-minute run is complete about a minute in.

    So this is not a new signal, it is the existing one used earlier. The
    progress poll already learns of every completion from Temporal history; each
    one is handed here and fetched in the background while the rest of the
    ladder is still encoding.

    THE STREAM IS AN ACCELERATOR, NEVER AN INVENTORY. phase_package_all still
    builds its fetch list by LISTING the staging prefix, and still fails if an
    expected chunk is absent. A completion this never hears about, a download
    that fails, a prefetcher that dies with the first exception — each costs one
    download later and nothing else. That is why every error here is swallowed:
    there is no failure mode worth failing a run for, and the alternative is a
    best-effort optimisation that can take down an encode.

    Only the codecs THIS process will package are prefetched. A codec packaged
    in a worker is fetched by that worker, from inside the cluster, and pulling
    it here would be pure waste.
    """

    def __init__(self, bucket: str, work_prefix: str, dest_root: Path,
                 codecs: "set[str]", workers: int = 8) -> None:
        from concurrent.futures import ThreadPoolExecutor
        self._bucket = bucket
        self._prefix = work_prefix.strip("/")
        self._root = dest_root
        self._codecs = codecs
        self._seen: set = set()
        self._lock = threading.Lock()
        self.fetched = 0
        self.bytes = 0
        self.failed = 0
        # One client for the pool. botocore's default connection pool is 10, so
        # a wider pool than that would queue on sockets rather than run.
        import boto3
        from botocore.config import Config
        self._s3 = boto3.client(
            "s3",
            endpoint_url=os.environ["S3_ENDPOINT_URL"],
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            config=Config(s3={"addressing_style": "path"},
                          max_pool_connections=workers + 4),
        )
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="prefetch")

    def dir_for(self, codec: str) -> Path:
        """Sibling of the packaging work dir, never inside it: cli_phase's
        _prepare_work_dir rmtree's ENCODER_WORK_DIR on entry, so a prefetch
        staged there would be deleted by the phase meant to consume it."""
        return self._root / f".prefetch-{codec}"

    def note_activity_done(self, activity_id: str) -> None:
        """Called for every activity Temporal reports COMPLETED. Non-chunk ids
        are ignored here — the mezzanine and audio are single objects packaging
        fetches once, and there is nothing to overlap them with."""
        m = _ENC_ACT_RE.match(activity_id)
        if not m:
            return
        codec, label, index = m.group(1), m.group(2), int(m.group(3))
        if codec not in self._codecs:
            return
        name = variant_object_name(codec, label, index)
        with self._lock:
            if name in self._seen:
                return          # at-least-once delivery, or a re-read of history
            self._seen.add(name)
        self._pool.submit(self._fetch, codec, name)

    def _fetch(self, codec: str, name: str) -> None:
        dest = self.dir_for(codec)
        try:
            dest.mkdir(parents=True, exist_ok=True)
            # The .done sidecar FIRST, and the object second: the consumer
            # accepts a prefetched object only when the sidecar records its
            # exact size, so a half-written object simply reads as a miss.
            self._s3.download_file(self._bucket, f"{self._prefix}/{name}.done",
                                   str(dest / f"{name}.done"))
            self._s3.download_file(self._bucket, f"{self._prefix}/{name}",
                                   str(dest / name))
            size = (dest / name).stat().st_size
        except Exception:  # noqa: BLE001 — see the class docstring
            with self._lock:
                self.failed += 1
            return
        with self._lock:
            self.fetched += 1
            self.bytes += size

    def close(self) -> None:
        """Stop fetching. Called before packaging starts — anything still in
        flight would be racing the consumer for the same file."""
        self._pool.shutdown(wait=True)
        if self.fetched or self.failed:
            print(f"[dist] prefetched {self.fetched} chunk(s), "
                  f"{self.bytes / 1e6:.0f} MB during the encode"
                  + (f" ({self.failed} deferred to packaging)"
                     if self.failed else ""), flush=True)


def _emit_self_run_host(activity_id: str) -> None:
    """Colour the lane of a phase THIS process runs, with the box it runs on.

    A stage's machine only ever comes from an ENCODER-HOST marker, and the two
    emitters in this file both read it off a Temporal worker — so a phase that
    never reaches a worker has no machine at all, and the machine timeline drops
    it (`_laneStages` filters on `s.instance`). Since the mezzanine and the
    packaging moved onto this box that is the head and the tail of every run:
    the master draws IDLE for both while it is the only machine doing anything,
    and those minutes land in the lane's idle arithmetic (#293).

    WORKER_LABEL is the same name the master's own worker connects to Temporal
    under, so these phases land in that box's existing lane instead of opening a
    second one for the same machine. Unset — an older server that does not pass
    it through — emits nothing: a missing lane is a gap, while a guessed one
    (the container's hostname, say) is a machine that does not exist.

    Both callers run after `_emit_plan`, which is load-bearing rather than
    incidental: the Go side updates a stage row in place here and, unlike the
    REUSED marker, does not seed one — a HOST marker for a key with no row yet
    is dropped in silence.
    """
    machine = os.environ.get("WORKER_LABEL", "")
    if not machine:
        return
    for key in _stage_keys_for(activity_id):
        # No version: this process's build is not what _MACHINE_VERSION holds
        # (that is what each WORKER reported), and an absent field means "did
        # not say", which the Go reader keeps distinct from agreement.
        print(host_marker(key, machine), flush=True)


def _build_mezzanine_on_host(input_path: Path, mezz_prefix: str, work_dir: Path,
                             time_limit_s: float | None) -> bool:
    """Build the mezzanine HERE, in the orchestrator container, and upload it
    straight to the shared cache — so the source is never staged at all.

    The local twin of the cloud path's #266, and it removes strictly more: this
    orchestrator runs on the master with SOURCE_DIR mounted, so the source is
    already on the disk it would have been uploaded from. Staging it moved a
    source-sized file into MinIO, a worker pulled the same bytes back out, and
    the mezzanine it produced went in again — three transfers of a file that had
    not left the box. Building it here is one.

    Shells out to the SAME `cli_phase mezzanine` the activity runs, with a LOCAL
    path for --s3-in (the flag is named for the deployment that came first, not
    for the only thing it accepts). Deliberately not a host-side reimplementation:
    the chunk plan is built from this mezzanine's exact duration and cli_phase
    already refuses when the two drift.

    NO workflow change is needed. phase_mezzanine short-circuits on the .done
    sidecar, so the activity the workflow still schedules finds this build and
    returns without reading anything — the same shape as the cloud path's
    MezzCheck, reusing a guard that was already there.

    work_dir sits INSIDE the job's own $TMP_DIR/<jobID>/, because that is the
    only scratch here that anything cleans: Manager.run removes it on every
    terminal outcome and tmpstage sweeps it if the server dies first. It must be
    gone before the run finishes — moveTmpToOutput renames EVERY top-level entry
    of that directory into OUTPUT_DIR, dot-prefixed or not — which is what the
    finally below is for. The one path that skips it (this process killed
    mid-mezzanine) also fails the job, and a failed job moves nothing.

    Returns False on failure, having already said why.
    """
    cmd = [sys.executable, "-m", "infinite_streaming_encoder.cli_phase",
           "mezzanine", "--s3-in", str(input_path), "--s3-out", mezz_prefix]
    env = dict(os.environ)
    # cli_phase rmtree's ENCODER_WORK_DIR on entry, so it must not be shared.
    env["ENCODER_WORK_DIR"] = str(work_dir)
    # The duration limit (#184) is applied by the mezzanine and nowhere else, so
    # skipping the activity takes its env with it unless it is supplied here. The
    # limit is already folded into the cache key, so a limited mezzanine can
    # never be filed under a full run's key.
    if time_limit_s and time_limit_s > 0:
        env["TIME_LIMIT_S"] = f"{time_limit_s:g}"
    print(f"[dist] building mezzanine on the host -> {mezz_prefix} "
          f"(no source upload)", flush=True)
    # Before the build, not after it: the point is the lane being coloured WHILE
    # this runs, which on a cache miss is the first minutes of the run.
    _emit_self_run_host("mezzanine")
    try:
        proc = subprocess.Popen(cmd, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert proc.stdout is not None
        # Relay verbatim. cli_phase emits its own ENCODER-STAGE markers and this
        # process's stdout IS the channel to the Go server, so the mezzanine bar
        # runs live through the normal marker path with no bespoke parser.
        for line in proc.stdout:
            print(line.rstrip("\n"), flush=True)
        rc = proc.wait()
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    if rc != 0:
        print(f"error: host mezzanine failed (exit {rc})", file=sys.stderr)
        return False
    # The source is never staged now, so the activity CANNOT fall back to
    # building from S3 — its --s3-in points at an object nothing uploaded. Assert
    # the handoff here, where the message can say what actually went wrong,
    # rather than letting it surface four retries later as a NoSuchKey on a key
    # no one expected to be read.
    bucket, _, rel = mezz_prefix[len("s3://"):].partition("/")
    if not _object_exists(bucket, f"{rel}/mezzanine.mp4.done"):
        print(f"error: host mezzanine reported success but {mezz_prefix}/"
              f"mezzanine.mp4.done is missing", file=sys.stderr)
        return False
    _SELF_RUN_STAGES.add("mezzanine")
    return True


def _codec_dir(output_stem: str, codec: str, output_tag: str) -> str:
    """`<stem>_<codec>[_<tag>]` — this run's local output dir for one codec.

    The profile tag goes AFTER the codec so the `_p200_<codec>` shape that
    OutputStem / resolveCodec / the watcher all key off stays intact. Two callers
    need this name — the sync-back that downloads a MinIO-packaged codec, and
    host packaging, which produces the directory directly — and they must not
    spell it differently or the same clip lands in two places.

    cli_batch._local_codec_dir is the cloud twin, deliberately not imported: the
    local orchestrator pulling in the cloud one would couple the two paths for a
    string.
    """
    return f"{output_stem}_{codec}" + (f"_{output_tag}" if output_tag else "")


def _package_on_host(codec: str, s3_variants: str, out_dir: Path, dest: Path,
                     segment_duration: str, partial_duration: str,
                     prefetch_dir: Path | None = None) -> int:
    """Package one codec HERE, straight into the run's output dir.

    The tail-side twin of _build_mezzanine_on_host, and the local twin of
    cli_batch._package_on_host (#197). Same shape as both: shell out to the SAME
    `cli_phase package-all` the activity runs, differing only in `--s3-out` being
    a local directory (see cli_phase._deliver_dir).

    What it removes is transfer, not queue latency — there is no cold start here.
    A packaged codec used to be uploaded to MinIO by whichever worker drew the
    activity and downloaded straight back by this process; if that worker was a
    remote box, the whole ladder crossed the LAN twice to reach a disk on the
    master. Packaging here reads the chunks once and writes the result where it
    is already wanted.

    It also restores LIVE package/fragments/hls rows. cli_phase emits its own
    per-step markers, but they are CLASS_LIVE and so deliberately not relayed
    through the activity result — on the worker path they never reach this
    process, and the three rows can only move together on activity completion.
    Run here, this process's stdout IS the channel to the Go server.

    NO RETRY. Temporal owned that, and this gives it up: a packaging failure
    fails the run instead of being rescheduled onto another worker. The trade is
    the same one #197 made and it is softer here — minutes of local CPU on a box
    that is already up, against chunk encodes that are hours — but the recovery
    is worse than cloud's, because a Retry submits a NEW job id and therefore a
    new staging prefix, which re-encodes everything. Hence the error below: the
    chunks are still staged, and re-running against the SAME --job-prefix reuses
    them (cli_phase skips a chunk whose output exists; _emit_reused_chunks
    colours them as reused).

    Returns the number of files delivered, so the caller can refuse to reclaim
    staging behind an empty result.
    """
    work = out_dir / f".pkg-work-{codec}"
    env = dict(os.environ)
    # Each codec gets its own scratch: cli_phase._prepare_work_dir rmtree's
    # ENCODER_WORK_DIR on entry, so a shared one would delete a sibling codec's
    # inputs — and only ever on a multi-codec run.
    env["ENCODER_WORK_DIR"] = str(work)
    # Where the prefetch put this codec's chunks (#311). A sibling of the work
    # dir, so the handoff is a hardlink rather than a copy — and so cli_phase's
    # rmtree of ENCODER_WORK_DIR on entry cannot delete it. Unset when nothing
    # prefetched, which is exactly the pre-#311 behaviour.
    if prefetch_dir is not None:
        env["ENCODER_PREFETCH_DIR"] = str(prefetch_dir)
    # Read from the workflow plan, not this process's environment, so the
    # packaging cannot disagree with the timing the chunks were encoded to. A
    # segment mismatch produces playlists whose boundaries do not land on the
    # media's keyframes. "0" is a real setting for PARTIAL (LL-HLS parts off),
    # so only "" means "unset — take cli_phase's default".
    if segment_duration:
        env["SEGMENT_DURATION"] = segment_duration
    if partial_duration:
        env["PARTIAL_DURATION"] = partial_duration
    print(f"[dist] packaging {codec} on the host (no worker, no sync-back)",
          flush=True)
    # All three rows this activity drives, before the work: they run LIVE from
    # here (see above), so the lane fills as they do rather than at the end.
    _emit_self_run_host(f"pkg-{codec}")
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "infinite_streaming_encoder.cli_phase",
             "package-all", "--codec", codec,
             "--s3-variants", s3_variants, "--s3-audio", s3_variants,
             "--s3-out", str(out_dir)],
            env=env, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line.rstrip("\n"), flush=True)
        rc = proc.wait()
    finally:
        # Before the rc check, and before anything is moved: these sit inside
        # $TMP_DIR/<jobID>/, every top-level entry of which moveTmpToOutput
        # renames into OUTPUT_DIR. The prefetch dir goes the same way and for
        # the same reason — its contents are hardlinks, so removing it here
        # frees nothing until the work dir goes too, and both go together.
        shutil.rmtree(work, ignore_errors=True)
        if prefetch_dir is not None:
            shutil.rmtree(prefetch_dir, ignore_errors=True)
    if rc != 0:
        raise RuntimeError(
            f"host packaging failed for {codec} (exit {rc}). The encoded chunks "
            f"are still staged under {s3_variants} — re-running with the SAME "
            f"--job-prefix packages them without re-encoding anything.")
    # cli_phase delivers output_<codec>/; the local contract is
    # <stem>_<codec>[_<tag>]/ so several codecs of one clip coexist in OUTPUT_DIR.
    src = out_dir / f"output_{codec}"
    # `dest != src` is not paranoia: a source named output.mp4 with no duration
    # suffix gives the stem "output", so dest IS src — and the rmtree below would
    # then delete the ladder that was just packaged, leaving the rename to fail
    # on a directory it had removed itself.
    if dest != src:
        if dest.exists():
            shutil.rmtree(dest)
        src.rename(dest)   # same filesystem — a rename, not a copy
    n = sum(1 for p in dest.rglob("*") if p.is_file())
    print(f"[dist] packaged {codec} in {time.monotonic() - t0:.0f}s -> "
          f"{dest} ({n} file(s))", flush=True)
    return n


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


@contextlib.contextmanager
def _mezz_build_lock(rel: str):
    """Serialise the mezzanine check-then-build across ORCHESTRATOR PROCESSES.

    The Go half of #291 is an in-process mutex, which is right for cloud-batch —
    one server builds every cloud mezzanine. It cannot reach here: on local-dist
    each job runs in its own orchestrator container, so two jobs on one source
    are two processes and a mutex in either is invisible to the other.

    What they DO share is $TMP_DIR, mounted into every orchestrator from the same
    host directory. So an flock on a file there is the cheapest thing that spans
    them. Advisory and process-scoped: the kernel drops it when the container
    dies, so a killed orchestrator cannot wedge the key — which a claim OBJECT in
    MinIO would, and is why this is not that.

    The key is deliberately ladder-independent (a mezzanine is a stream copy, so
    the ladder cannot change it), which means all four apple-uniq-live-* ladders
    of one source map to ONE lock — and #286 made submitting those four a single
    click. Best-effort: any failure to lock proceeds unserialised, because a
    duplicate build wastes work while a hard failure here would lose the encode.
    """
    lock_dir = Path(os.environ.get("TMP_DIR") or tempfile.gettempdir())
    fh = None
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        fh = open(lock_dir / f".mezz-{rel.replace('/', '_')}.lock", "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
    except OSError as e:
        print(f"[dist] mezzanine lock unavailable ({e}) — proceeding unserialised; "
              f"a concurrent job on this source may build it twice", flush=True)
        if fh:
            fh.close()
        fh = None
    try:
        yield
    finally:
        if fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()


def _mezz_cache_rel(input_path: Path, time_limit_s: float | None = None) -> str:
    """Bucket-relative prefix (`mezz-cache/<key>`) for this source's shared
    mezzanine. See MEZZ_CACHE_PREFIX.

    The duration limit (#184) is part of the key, not just the content. A
    limited run truncates the mezzanine, and this cache is shared across jobs
    and outlives them — so keying on the source alone would let one 30s encode
    serve a 30s mezzanine to every later full encode of the same file, silently
    producing 30s outputs until the staging GC evicted it. The failure is
    invisible until someone plays the result back."""
    st = input_path.stat()
    sig = f"{input_path.name}:{st.st_size}:{st.st_mtime_ns}"
    if time_limit_s and time_limit_s > 0:
        sig += f":t{time_limit_s:g}"
    key = hashlib.sha256(sig.encode()).hexdigest()[:32]
    return f"{MEZZ_CACHE_PREFIX}/{key}"


def _apply_time_limit(info, time_limit_s: float | None):
    """Clamp the probed duration to the requested limit (#184), returning the
    info the rest of the run should plan against.

    Applied once, immediately after the probe, because `info.duration_s` is the
    single input to everything that has to agree about how long the content is:
    the chunk plan, the ladder's chunk sizing, the cost estimate and the
    progress totals. Clamping here means none of them has to know a limit
    exists — they simply plan a shorter clip, which is exactly what the
    truncated mezzanine will contain.

    A limit at or above the clip is dropped rather than applied, so it can't
    produce a plan longer than the media."""
    if not time_limit_s or time_limit_s <= 0:
        return info
    if time_limit_s >= info.duration_s:
        print(f"[dist] --time {time_limit_s:g}s >= clip {info.duration_s:g}s — "
              f"encoding the whole clip", flush=True)
        return info
    print(f"[dist] --time {time_limit_s:g}s of {info.duration_s:g}s — planning "
          f"against the truncated length", flush=True)
    return dc_replace(info, duration_s=float(time_limit_s))


def _effective_time_limit(info, time_limit_s: float | None) -> float | None:
    """The limit that will actually be applied, or None. Mirrors
    _apply_time_limit's "at or above the clip is not a limit" rule so the cache
    key and the mezzanine env agree with the plan.

    SNAPPED to the nearest whole segment first, because a limit that isn't a
    segment multiple ends the clip mid-segment: chunk boundaries must land on
    segments, so the plan could not end where the media does. Snapped against
    _SEGMENT_DURATION_S specifically — the value plan_chunks below is given — so
    this path stays self-consistent whatever the caller sent. The Go control
    plane snaps too, against the job's ladder-resolved segment duration, and this
    is idempotent on an already-snapped value; running cli_local_dist by hand
    gets the same treatment.

    Snap BEFORE the clip-length test, so a request that rounds up past the clip
    correctly becomes "no limit" rather than a limit longer than the media."""
    if not time_limit_s or time_limit_s <= 0:
        return None
    snapped = _snap_to_segment(float(time_limit_s), _SEGMENT_DURATION_S)
    if snapped >= info.duration_s:
        return None
    return snapped


def _snap_to_segment(secs: float, seg_s: float) -> float:
    """Round secs to the nearest positive multiple of seg_s (min one segment).

    floor(x + 0.5), not round(): Python's round() is banker's rounding, so
    round(2.5) == 2 while Go's math.Round(2.5) == 3. The Go control plane snaps
    first and this is idempotent on its output, so the two only meet at a
    half-segment request typed directly at this CLI — but a rule that disagrees
    with itself across the two languages is exactly the kind of thing that gets
    found months later, in a comparison run that quietly wasn't comparing equal
    spans."""
    if seg_s <= 0:
        return secs
    return max(1, math.floor(secs / seg_s + 0.5)) * seg_s


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
               has_audio: bool, sync_back: bool = True) -> None:
    """Announce mezzanine + every chunk + audio + package/fragments/hls (+
    download), so the UI lays out the full grid up front (like the cloud plan).

    Two rows are declared only when something will actually drive them, because
    a declared stage that never fires is a row that sits pending forever at the
    end of every run — the same rule cli_batch._emit_plan applies:

      upload:source   never. The mezzanine is built here from the mounted
                      source, so nothing stages it on any run.
      download:outputs only when a codec is packaged in MinIO and therefore has
                      to come back from it. Host packaging writes straight to the
                      output dir.
    """
    stages: list[Stage] = [Stage(key="mezzanine", label="mezzanine")]
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
    if sync_back:
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


def _stage_keys_for(activity_id: str) -> list[str]:
    """The UI rows one activity drives. Usually one; `pkg-<codec>` drives three.

    package-all does the packaging, the fragment expansion and the HLS playlists
    in a single phase — that is what it is for, and the cloud state machine has
    named one PackageAll task per codec since. The workflow here kept running the
    old `byteranges` and `hls` phases after it, so each re-downloaded the whole
    packaged ladder out of MinIO, redid work package-all had already done, and
    pushed it back: two full-ladder round trips per codec, for no change to the
    output. They are gone, and their three rows are driven from the one activity
    that actually produces them — the same mapping cli_batch._host_stage_keys
    makes for a pkgall job.

    The three then move in lockstep, which is honest at this granularity:
    package-all's own per-step markers are CLASS_LIVE, so they are deliberately
    not relayed through the activity result and never reach this process.

    `byteranges-`/`hls-` are NOT mapped. A farm mid-rolling-update can still have
    a box running the old workflow, and those activities would then re-announce
    rows this run had already completed — walking a finished cell backwards,
    which nothing between here and the UI guards against. Dropping them costs
    only that the two extra phases run unreported on such a box.
    """
    if activity_id in ("mezzanine", "audio"):
        return [activity_id]
    m = _ENC_ACT_RE.match(activity_id)
    if m:
        return [f"encode:{m.group(1)}:{m.group(2)}:chunk{m.group(3)}"]
    if activity_id.startswith("pkg-"):
        c = activity_id[4:]
        return [f"package:{c}", f"fragments:{c}", f"hls:{c}"]
    return []


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
                # Start pulling this chunk NOW, while the rest of the ladder is
                # still encoding, instead of leaving all 336 to one burst after
                # the last one lands (#311). Best-effort by construction: see
                # _ChunkPrefetcher. Guarded on `emitted` so re-reading history
                # each poll does not re-announce a chunk to it every second —
                # the prefetcher dedupes too, but not paying for the call is
                # cheaper than deduping it.
                if _PREFETCH is not None and emitted.get(f"pf:{aid}") is None:
                    emitted[f"pf:{aid}"] = True
                    _PREFETCH.note_activity_done(aid)
                # Relay the markers the worker collected (#141). Without this
                # the VMAF audit runs on every chunk, costs real time, and is
                # discarded — the Go scanner tails the ORCHESTRATOR, and workers
                # print to their own stdout which nothing forwards.
                if client is not None and emitted.get(f"relay:{aid}") is None:
                    emitted[f"relay:{aid}"] = True
                    for marker in _activity_markers(a, client):
                        print(marker, flush=True)
    for aid, st in states.items():
        # A stage this process ran itself is already reported, and at a finer
        # grain than history can offer. See _SELF_RUN_STAGES.
        if aid in _SELF_RUN_STAGES:
            continue
        keys = _stage_keys_for(aid)
        if not keys:
            continue
        # Which machine ran this stage → colours the UI chunk/phase plot by box.
        host = hosts.get(aid)
        if host and emitted.get(f"host:{aid}") != host:
            emitted[f"host:{aid}"] = host
            for key in keys:
                print(f"[[ENCODER-HOST key={key} instance={host}]]", flush=True)
        if emitted.get(aid) != st:
            emitted[aid] = st
            for key in keys:
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
_HOST_SEEN: dict = {}   # activity_id -> (machine, version), dedups ENCODER-HOST
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
    """Is (activity on machine, running version) NEW information worth a marker?

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
    """Build one ENCODER-HOST line — which box, and which build, ran an activity.

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
        # Activities this process ran itself are excluded throughout: their rows
        # are already driven, at a finer grain, by the phase running here — and
        # the workflow still schedules a mezzanine activity that finds the .done
        # and returns, so a worker briefly holds it and would otherwise re-tag
        # the row with ITS box.
        aid = getattr(pa, "activity_id", "") or ""
        started = bool(aid) and (getattr(pa, "state", 0) == _PA_STARTED
                                 and aid not in _SELF_RUN_STAGES)
        keys = _stage_keys_for(aid) if started else []
        # Promote its stage here — for every activity kind, and before the
        # machine checks below, which `continue` when identity is unknown. An
        # activity with no reported identity is still running.
        if keys and _RUN_SEEN.get(aid) != 1:
            _RUN_SEEN[aid] = 1
            for key in keys:
                emit_stage(key, "running", 0.0)
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
        # Colour the stage by the machine running it, from the START —
        # authoritative here (last_worker_identity) so the cell matches the
        # fleet swatch + chip for the whole encode, not just at completion.
        #
        # For EVERY activity, not just the chunks. Temporal does not write an
        # ActivityTaskStarted event into history until the activity completes,
        # so the history reader below cannot learn where a running phase is —
        # and a phase gated to `enc-` here meant a long non-chunk one (audio, or
        # packaging on a run that asked for it in a worker) drew its box as IDLE
        # for its whole duration, on the exact chart someone consults to ask
        # what the run is waiting on (#293).
        #
        # version rides along so the PER-CHUNK record in run.json says which
        # build encoded it, not just which box (#248) — the half that answers
        # the question months later, about a run nobody was watching. It comes
        # from _MACHINE_VERSION (the box), not from this chunk's own heartbeat,
        # which is usually not there yet the first time the chunk is seen.
        #
        # Deduped on (machine, version), NOT machine alone: failover re-tags,
        # AND learning the build re-emits exactly once so a chunk first seen
        # before any heartbeat still gets its version. Keying on machine alone
        # tagged 2 of 14 chunks in a real run — the first, version-less emission
        # suppressed every retry. Tested ONCE per activity rather than per key,
        # because `pkg-<codec>` drives three rows and a per-key test would
        # record on the first and then suppress the other two.
        ver = _MACHINE_VERSION.get(machine, "")
        if keys and should_emit_host(aid, machine, ver, _HOST_SEEN):
            for key in keys:
                print(host_marker(key, machine, ver), flush=True)
        if started and aid.startswith("enc-"):
            a["chunks"].append(aid)
            # Real per-chunk progress: the worker rides ffmpeg's out_time/
            # duration % on its heartbeat, so a chunk fills 0→100 as it actually
            # encodes instead of snapping 0→100 on completion. Chunks only — a
            # phase's heartbeat carries no such fraction.
            p = (cpu or {}).get("progress")
            if p is not None and 0 < p < 100:
                for key in keys:
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
    # Duration limit BEFORE _resolve_plan reads info.duration_s — the chunk plan,
    # the ladder sizing and the cost estimate must all describe the truncated clip.
    time_limit_s = _effective_time_limit(info, getattr(args, "time_limit_s", None))
    info = _apply_time_limit(info, time_limit_s)
    # Resolve "whole variant" (0) to a clip-spanning single chunk before the
    # value fans out to _resolve_plan / the workflow plan / worker env.
    args.chunk_duration_s = _resolve_chunk_duration_s(
        args.chunk_duration_s, info.duration_s)

    bucket = args.s3_bucket
    prefix = args.job_prefix.strip("/")
    # Where the source WOULD be staged. Nothing puts it there any more — the
    # mezzanine is built on this box — but the workflow's mezzanine activity
    # still takes an --s3-in, and a plan without one would not start. It is never
    # read: that activity short-circuits on the .done this run just wrote, and
    # _build_mezzanine_on_host refuses to hand off until it has confirmed it.
    src_key = f"{prefix}/input{input_path.suffix}"
    rungs_by_codec, chunks = _resolve_plan(args, info)
    n_chunks = len(chunks)
    if not rungs_by_codec:
        print("error: no ladder rungs fit this source", file=sys.stderr)
        return 1

    # Which codecs THIS process packages. All of them by default: packaging in a
    # worker uploads the finished ladder to MinIO for this process to download
    # straight back, and if that worker is a remote box the whole thing crosses
    # the LAN twice to reach a disk on the master. Kept a per-codec list rather
    # than a flag so it matches the plan key the workflow reads, and so a future
    # reason to package one codec remotely does not need a new shape.
    host_package = [] if args.no_host_package else list(rungs_by_codec)
    # A worker running an OLDER workflow packages regardless of the plan key, so
    # its pkg activities would announce over rows this process is driving live.
    # Suppress them the same way the host-built mezzanine is suppressed.
    _SELF_RUN_STAGES.update(f"pkg-{c}" for c in host_package)

    # Pull each chunk down as it finishes rather than all of them after the last
    # one does (#311). Only for codecs THIS process packages — a worker-packaged
    # codec is fetched from inside the cluster by that worker.
    global _PREFETCH
    if host_package and not args.no_prefetch:
        _PREFETCH = _ChunkPrefetcher(bucket, f"{prefix}/work",
                                     Path(args.output_dir), set(host_package))

    _emit_plan(rungs_by_codec,
               {(c, r.label): n_chunks for c, rr in rungs_by_codec.items() for r in rr},
               info.has_audio,
               sync_back=any(c not in host_package for c in rungs_by_codec))
    _emit_commercial_cost(rungs_by_codec, info, input_path,
                          hevc_two_pass=not args.hevc_single_pass)
    # Flag chunks left over from a prior (cancelled/failed) run as reused, so a
    # resume shows them distinctly instead of as fresh encodes.
    _emit_reused_chunks(bucket, f"{prefix}/work")

    # Cross-job mezzanine cache. If this exact source already has a completed
    # mezzanine in MinIO, every later job reuses it — the source is ONLY consumed
    # by the mezzanine phase, so a hit skips the whole of the prep. On a miss we
    # build it HERE rather than staging the source for a worker to build it from,
    # and the shared cache is populated for the next job either way.
    mezz_rel = _mezz_cache_rel(input_path, time_limit_s)
    mezz_prefix = f"s3://{bucket}/{mezz_rel}"
    # The lock spans the CHECK and the BUILD (#291). Guarding only the build
    # would leave both jobs having already missed, so both would still build.
    # A job that waits here re-checks inside and finds it warm.
    with _mezz_build_lock(mezz_rel):
        if _object_exists(bucket, f"{mezz_rel}/mezzanine.mp4.done"):
            print(f"[dist] mezzanine cache HIT {mezz_prefix} — skipping the "
                  f"mezzanine entirely (same source encoded before)", flush=True)
            _touch_prefix(bucket, mezz_rel)  # keep it fresh while this job reuses it
        else:
            _ensure_bucket(bucket)
            if not _build_mezzanine_on_host(
                    input_path, mezz_prefix,
                    Path(args.output_dir) / ".mezz-work", time_limit_s):
                return 1

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
        # Codecs this process packages itself, so the workflow must not. Absent
        # in plans from an older orchestrator, which correctly reads as "package
        # everything in a worker" — the old behaviour.
        "host_package": host_package,
        # Duration limit (#184) — consumed by the mezzanine activity ONLY. The
        # chunk boundaries below were already planned against the truncated
        # length, so nothing else needs to know.
        "time_limit_s": time_limit_s or 0,
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
        # is what local-dist did until #167, encoding apple-uniq-live-xs with a 2.5x
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

    # Stop fetching before anything consumes what was fetched: an in-flight
    # download would be racing the packaging phase for the same file.
    if _PREFETCH is not None:
        _PREFETCH.close()

    out_dir = Path(args.output_dir)
    delivered = []
    for codec in rungs_by_codec:
        dest = out_dir / _codec_dir(args.output, codec, args.output_tag)
        if codec in host_package:
            try:
                n = _package_on_host(codec, f"s3://{bucket}/{prefix}/work",
                                     out_dir, dest,
                                     str(plan.get("segment_duration", "")),
                                     str(plan.get("partial_duration", "")),
                                     _PREFETCH.dir_for(codec) if _PREFETCH else None)
            except RuntimeError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
        else:
            n = _download_prefix(bucket, f"{prefix}/out/output_{codec}/", dest)
            print(f"[dist] downloaded {n} file(s) -> {dest}", flush=True)
        delivered.append(n)
    # Reclaim only behind a non-empty result for EVERY codec. An empty one means
    # the staging is the only copy of the encode, whichever way it was delivered.
    if delivered and all(n > 0 for n in delivered):
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
    p.add_argument("--ladder", default="apple-uniq-live-xs")
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
    p.add_argument("--time", type=float, default=None, dest="time_limit_s",
                   help="encode only the first N seconds (default: whole clip); "
                        "SNAPPED to the nearest whole segment, since chunk "
                        "boundaries must land on segments. Applied by truncating "
                        "the mezzanine, and folded into the mezzanine cache key so "
                        "a limited run can never serve a truncated mezzanine to a "
                        "later full encode.")
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
    p.add_argument("--no-host-package", action="store_true", dest="no_host_package",
                   help="package each codec in a WORKER and download the result, "
                        "instead of packaging here (default: package here — see "
                        "_package_on_host). The escape hatch for the one thing "
                        "host packaging gives up, Temporal's retry: a run whose "
                        "packaging keeps failing can be re-run against the same "
                        "--job-prefix with this set, and the chunks are reused.")
    p.add_argument("--no-prefetch", action="store_true", dest="no_prefetch",
                   help="don't download chunks as they finish encoding; leave "
                        "every object to the one burst at the start of "
                        "packaging (the pre-#311 behaviour). An escape hatch "
                        "for a box where the extra concurrent transfers would "
                        "compete with the encodes themselves.")
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
