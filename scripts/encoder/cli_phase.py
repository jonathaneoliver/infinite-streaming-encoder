"""Phase sub-command dispatcher for Batch jobs.

Each Batch job definition in infra/terraform/modules/jobs/main.tf
invokes this module with a specific phase name + the S3 URIs of its
inputs and outputs:

  python -m encoder.cli_local phase mezzanine  --s3-in URI --s3-out URI
  python -m encoder.cli_local phase variant    --codec C --tier T --s3-mezz URI --s3-out URI
  python -m encoder.cli_local phase audio                  --s3-mezz URI --s3-out URI
  python -m encoder.cli_local phase package    --codec C --s3-variants URI --s3-audio URI --s3-out URI
  python -m encoder.cli_local phase hls        --codec C --s3-package URI --s3-out URI
  python -m encoder.cli_local phase byteranges --codec C --s3-package URI --s3-out URI

Each phase:
  1. Downloads whatever prereqs it needs into /tmp/work/
  2. Invokes the existing in-process Python pipeline function for that
     phase (nothing new about how the encoding itself runs)
  3. Uploads its output artifacts back to S3 with matching .done
     sidecars so downstream phases can validate completeness

S3 conventions (all URIs below are relative to the job's prefix,
typically s3://<bucket>/jobs/<id>/):

  input/<clip.mp4>             source file(s)
  mezzanine.mp4 + .done        phase-2 output
  {codec}_{tier}.mp4 + .done   variant outputs (12 total for h264+hevc)
  audio.mp4 + .done            phase-4 output
  <stem>_<codec>/              packaged DASH + HLS per codec
"""
from __future__ import annotations

import argparse
import os
import re
import resource
import shutil
import sys
import time
from pathlib import Path

from encoder.audio import AudioSpec, create_audio
from encoder.chunking import DEFAULT_CHUNK_DURATION_S, plan_chunks
from encoder.encode_variants import EncodeContext, concat_chunks, encode_variant
from encoder.ffprobe import probe
from encoder.hls import (
    TsHlsSpec, generate_byteranges_sidecars, generate_fmp4_hls,
)
from encoder.manifests import write_fragmented_mpd
from encoder.ladder import (
    BUFSIZE_MULTIPLIER, DEFAULT_MAXRATE_PERCENT, Rung, burnin_for_height,
    label_res_name,
)
from encoder.mezzanine import MezzanineSpec, create_mezzanine
from encoder.packager import PackageSpec, package
from encoder.padding import multi_duration_lcm, plan_padding
from encoder.progress import emit_boot_ami, emit_stage

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover — boto3 is in requirements.txt for the image
    boto3 = None  # type: ignore
    ClientError = Exception  # type: ignore


# Local scratch dir inside the Batch container. Batch containers get
# their own ephemeral filesystem; no need for per-phase isolation.
_WORK_DIR = Path(os.environ.get("ENCODER_WORK_DIR", "/tmp/work"))

_SEGMENT_DURATION_S = 6.0
_PARTIAL_DURATION_S = 0.2
_GOP_DURATION_S = 1.0


# ---------------------------------------------------------------------------
# S3 helpers — thin wrappers so the phase fns don't each reimplement
# the parse + download + upload pattern.
# ---------------------------------------------------------------------------

def _s3():
    if boto3 is None:
        raise RuntimeError("boto3 not installed — required for phase commands")
    # Local testing hook: when S3_ENDPOINT_URL is set (e.g. LocalStack), point
    # the client at it and force path-style addressing — virtual-host style
    # would rewrite the host to <bucket>.<endpoint>, which doesn't resolve on
    # a docker network. Unset in production, so this is a no-op there.
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    endpoint = os.environ.get("S3_ENDPOINT_URL")
    if endpoint:
        from botocore.config import Config
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            region_name=region,
            config=Config(s3={"addressing_style": "path"}),
        )
    return boto3.client("s3", region_name=region)


def _parse(uri: str) -> tuple[str, str]:
    """Split s3://bucket/key/... into (bucket, key). Missing scheme = error."""
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3 URI: {uri}")
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    return bucket, key.rstrip("/")


class _Xfer:
    """boto3 transfer Callback that prints a throttled `[progress]` line so
    the poll loop's live-log forwarder can render a download/upload bar
    without drowning the app log. Emits at most every 2s, plus on completion."""

    def __init__(self, label: str, total: int,
                 stage: tuple[str, float, float] | None = None):
        self.label = label
        self.total = max(total, 1)
        self.done = 0
        self._last = 0.0
        # (stage_key, pct_lo, pct_hi): also drive that stage's bar into the
        # given sub-band, so a slow download/upload moves the mezzanine/audio
        # stage instead of it sitting at 0 (or 100) the whole transfer.
        self.stage = stage

    def __call__(self, n: int) -> None:
        self.done += n
        now = time.monotonic()
        if now - self._last < 2.0 and self.done < self.total:
            return
        self._last = now
        pct = min(100, self.done * 100 // self.total)
        print(f"[progress] {self.label}: {pct}% "
              f"({self.done / 1048576:.1f}/{self.total / 1048576:.1f} MB)",
              flush=True)
        if self.stage:
            key, lo, hi = self.stage
            emit_stage(key, "running", lo + pct / 100.0 * (hi - lo))


def _download(uri: str, dst: Path, progress: bool = True,
              stage: tuple[str, float, float] | None = None) -> None:
    bucket, key = _parse(uri)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cb = None
    if progress:
        try:
            total = _s3().head_object(Bucket=bucket, Key=key)["ContentLength"]
            cb = _Xfer(f"downloading {Path(key).name}", total, stage=stage)
        except Exception:  # noqa: BLE001 — progress is best-effort
            cb = None
    _s3().download_file(bucket, key, str(dst), Callback=cb)


def _upload(src: Path, uri: str, progress: bool = True,
            stage: tuple[str, float, float] | None = None) -> None:
    bucket, key = _parse(uri)
    cb = _Xfer(f"uploading {Path(key).name}", src.stat().st_size,
               stage=stage) if progress else None
    _s3().upload_file(str(src), bucket, key, Callback=cb)


def _s3_exists(uri: str) -> bool:
    """True if the object already exists in S3. Used for skip-if-already-done:
    S3 only exposes fully-uploaded objects, so existence == a complete output
    from a prior run or a spot-reclaim retry that got far enough to upload."""
    bucket, key = _parse(uri)
    try:
        _s3().head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NotFound", "NoSuchKey"):
            return False
        raise


def _write_done(path: Path) -> None:
    """Write <path>.done containing the file's size; atomic rename."""
    size = path.stat().st_size
    marker = path.with_suffix(path.suffix + ".done")
    tmp = marker.with_suffix(marker.suffix + ".tmp")
    tmp.write_text(f"{size}\n")
    tmp.rename(marker)


def _upload_with_done(local: Path, s3_uri: str,
                      stage: tuple[str, float, float] | None = None) -> None:
    """Upload a file + its .done sidecar so consumers can verify."""
    _write_done(local)
    _upload(local, s3_uri, stage=stage)
    _upload(local.with_suffix(local.suffix + ".done"), s3_uri + ".done",
            progress=False)


def _download_if_complete(s3_uri: str, dst: Path,
                          stage: tuple[str, float, float] | None = None) -> bool:
    """Download `s3_uri` + its .done sidecar into dst. Returns True iff
    both landed and the .done recorded size matches the file size.

    The remote phase that produced `dst` calls `_upload_with_done`, so
    the .done-matches-size invariant is a correctness check — if it
    fails, the object was overwritten mid-upload or partial and the
    downstream phase should abort rather than continue.
    """
    _download(s3_uri, dst, stage=stage)
    marker_s3 = s3_uri + ".done"
    marker_local = dst.with_suffix(dst.suffix + ".done")
    try:
        _download(marker_s3, marker_local, progress=False)
    except ClientError:
        return False
    try:
        recorded = int(marker_local.read_text().strip())
    except (OSError, ValueError):
        return False
    return recorded == dst.stat().st_size


def _prepare_work_dir() -> Path:
    """Wipe + re-create the scratch dir so successive phase invocations
    inside the same container (rare, but happens during local testing)
    start clean."""
    if _WORK_DIR.exists():
        shutil.rmtree(_WORK_DIR)
    _WORK_DIR.mkdir(parents=True, exist_ok=True)
    return _WORK_DIR


def _env_flag(name: str) -> bool:
    """Truthy-string env var → bool. Accepts 1/true/yes (any case)."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes")


def _env_num(name: str, default: float) -> float:
    """Numeric env var → float, or `default` if unset/unparseable. Used for the
    ladder-level VBV knobs (MAXRATE_PERCENT / BUFSIZE_MULT) the SFN injects."""
    try:
        v = os.environ.get(name, "").strip()
        return float(v) if v else default
    except ValueError:
        return default


def _cgroup_mem_limit_mib() -> float | None:
    """The container's hard memory limit in MiB (the MEMORY resourceRequirement,
    enforced as a cgroup limit), or None if unlimited/unreadable. Lets the mem
    report show peak vs limit. Handles cgroup v2 (memory.max) and v1."""
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(path).read().strip()
        except OSError:
            continue
        if v == "max":
            return None
        try:
            n = int(v)
        except ValueError:
            continue
        if n <= 0 or n > (1 << 50):  # unset / effectively unlimited
            return None
        return n / (1024 * 1024)
    return None


def _chunk_duration_s() -> float:
    """Chunk size (seconds) from CHUNK_DURATION_S, injected by the state
    machine. Must match what the Go control plane used for chunk_indices so
    the two agree on the chunk count. Falls back to the default."""
    v = os.environ.get("CHUNK_DURATION_S", "").strip()
    try:
        return float(v) if v else DEFAULT_CHUNK_DURATION_S
    except ValueError:
        return DEFAULT_CHUNK_DURATION_S


# Per-chunk encode files ({codec}_{label}_chunkNNN.mp4); excluded when
# discovering whole-variant labels for packaging.
_CHUNK_RE = re.compile(r"_chunk\d+$")


def _rung_from_args(args: argparse.Namespace) -> Rung:
    """Reconstruct the concrete rung the Go control plane resolved for this
    variant job. Geometry + bitrate come straight from the SFN input (Go owns
    the ladder store and resolves each rung, so the worker needs no ladder
    knowledge — this is what lets user-defined ladders work in the cloud);
    burn-in is derived from height.
    """
    ftc, flbl, x, ytc, ylbl = burnin_for_height(args.height)
    return Rung(
        label=args.label, res_name=label_res_name(args.label),
        width=args.width, height=args.height, bitrate=args.bitrate,
        preset=args.preset, fontsize_tc=ftc, fontsize_label=flbl,
        burnin_x=x, burnin_y_tc=ytc, burnin_y_label=ylbl,
    )


# ---------------------------------------------------------------------------
# mezzanine — stream-copy the source clip into a fragmented MP4 so
# subsequent encode/audio phases share one normalised input.
# ---------------------------------------------------------------------------

def phase_mezzanine(args: argparse.Namespace) -> int:
    work = _prepare_work_dir()

    # Idempotency / resume: if mezzanine.mp4 is already in S3 (a prior run or a
    # retry), reuse it — skip the source download + stream-copy entirely.
    # FORCE_REENCODE overrides.
    out_uri = args.s3_out.rstrip("/") + "/mezzanine.mp4"
    if not _env_flag("FORCE_REENCODE") and _s3_exists(out_uri):
        print("[phase mezzanine] reusing mezzanine.mp4 — already in S3, "
              "skipping download + stream-copy", flush=True)
        emit_stage("mezzanine", "done", 100.0)
        return 0

    # Download source. `--s3-in` is the full object URI; pull the
    # basename for the local filename.
    src_uri = args.s3_in
    _, src_key = _parse(src_uri)
    src_basename = src_key.rsplit("/", 1)[-1]
    local_in = work / src_basename
    # One continuous mezzanine bar: download 0-45%, stream-copy 45-55%,
    # upload 55-100% (the download + upload of the ~full-size file dominate).
    print(f"[phase mezzanine] downloading {src_uri}", flush=True)
    _download(src_uri, local_in, stage=("mezzanine", 0.0, 45.0))

    out_path = work / "mezzanine.mp4"
    info = probe(local_in)

    create_mezzanine(
        MezzanineSpec(
            input_path=local_in,
            output_path=out_path,
            time_limit_s=None,
        ),
        stage_key="mezzanine",
        duration_s=info.duration_s,
        pct_lo=45.0, pct_hi=55.0, terminal=False,
    )

    out_uri = args.s3_out.rstrip("/") + "/mezzanine.mp4"
    print(f"[phase mezzanine] uploading {out_uri}", flush=True)
    _upload_with_done(out_path, out_uri, stage=("mezzanine", 55.0, 100.0))
    emit_stage("mezzanine", "done", 100.0)
    return 0


# ---------------------------------------------------------------------------
# variant — one (codec, tier) encode.
# ---------------------------------------------------------------------------


class _StepTimer:
    """Times the in-container sub-steps of a phase and emits a machine-readable
    marker plus a human line. Combined with the Batch job's createdAt/startedAt/
    stoppedAt (queue + instance bringup + image pull, from `cloud.timing`), this
    gives the full where-does-the-time-go breakdown for a chunk."""

    def __init__(self, phase: str):
        self.phase = phase
        self.marks: list[tuple[str, float]] = []
        self._t0 = time.monotonic()
        self._last = self._t0

    def mark(self, name: str) -> None:
        now = time.monotonic()
        self.marks.append((name, now - self._last))
        self._last = now

    def emit(self, key: str, **extra) -> None:
        total = time.monotonic() - self._t0
        kv = " ".join(f"{n}_s={d:.2f}" for n, d in self.marks)
        # extra carries non-duration measurements (e.g. cpu_s = ffmpeg
        # CPU-seconds) that still ride the same marker so cloud.cpu_report
        # can divide them by reserved-vCPU x encode wall-time per tier.
        extra_kv = "".join(f" {k}={v}" for k, v in extra.items())
        # Machine-readable (parsed by cloud.timing / the app), then human.
        print(f"[[ENCODER-TIMING phase={self.phase} key={key} {kv}{extra_kv} "
              f"total_s={total:.2f}]]", flush=True)
        human = ", ".join(f"{n}={d:.1f}s" for n, d in self.marks)
        print(f"[timing] {self.phase} {key}: {human}, total={total:.1f}s",
              flush=True)


def phase_variant(args: argparse.Namespace) -> int:
    work = _prepare_work_dir()
    timer = _StepTimer("variant")

    # A negative chunk index is the whole-variant sentinel (single-chunk runs:
    # the SFN skips the chunk fan-out + concat and encodes the whole variant in
    # one job). Normalize to None so all the chunk-index logic below treats it
    # as a whole-clip encode writing {codec}_{label}.mp4 directly.
    if args.chunk_index is not None and args.chunk_index < 0:
        args.chunk_index = None

    # Idempotency / resume: the output name is fully determined by codec, tier
    # and the chunk index (a CLI arg), so we can check S3 BEFORE downloading
    # the mezzanine. If this exact variant is already there — a prior run, or a
    # spot-reclaim retry that got far enough to upload — reuse it and skip both
    # the fetch and the encode. S3 only exposes complete objects, so existence
    # == a valid finished output. FORCE_REENCODE overrides.
    ci_suffix = "" if args.chunk_index is None else f"_chunk{args.chunk_index:03d}"
    out_name = f"{args.codec}_{args.label}{ci_suffix}.mp4"
    out_uri = args.s3_out.rstrip("/") + f"/{out_name}"
    ci = "" if args.chunk_index is None else f":chunk{args.chunk_index}"
    if not _env_flag("FORCE_REENCODE") and _s3_exists(out_uri):
        print(f"[phase variant] reusing {out_name} — already in S3, "
              f"skipping fetch + encode", flush=True)
        timer.emit(f"encode:{args.codec}:{args.label}{ci}", cpu_s="0.00", reused="1")
        return 0

    mezz_uri = args.s3_mezz.rstrip("/") + "/mezzanine.mp4"
    mezz_local = work / "mezzanine.mp4"
    print(f"[phase variant] downloading {mezz_uri}", flush=True)
    if not _download_if_complete(mezz_uri, mezz_local):
        print("error: mezzanine.done missing or size mismatch", file=sys.stderr)
        return 1
    timer.mark("fetch")

    rung = _rung_from_args(args)
    info = probe(mezz_local)
    timer.mark("probe")

    # Encode contexts for a single variant are simpler than the full
    # pipeline — no padding (already applied in mezzanine if enabled),
    # no multi-tier loop.
    ctx = EncodeContext(
        mezzanine_path=mezz_local,
        output_dir=work,
        fps=info.fps,
        gop_duration_s=_GOP_DURATION_S,
        content_duration_s=info.duration_s,
        padding_duration_s=0.0,
        # Ladder-level VBV shaping, injected by the SFN (buildSFNInput). Same
        # for every variant of a run; defaults match ladder.py when unset.
        maxrate_percent=int(_env_num("MAXRATE_PERCENT", DEFAULT_MAXRATE_PERCENT)),
        bufsize_multiplier=_env_num("BUFSIZE_MULT", BUFSIZE_MULTIPLIER),
        # Two-pass is HEVC-only and set per-variant: the Step Function
        # injects TWO_PASS via containerOverrides only on HEVC variant jobs
        # (see buildSFNInput — H264 variants get TWO_PASS=false), and the
        # --two-pass flag covers direct invocation of this phase. This
        # worker encodes a single codec, so the env already carries the
        # codec-correct decision; feeding it hevc_two_pass is a no-op for a
        # non-HEVC job (encode_variants gates on codec == "hevc").
        hevc_two_pass=args.two_pass or _env_flag("TWO_PASS"),
    )

    # Chunked mode: encode only chunk `--chunk-index` of the variant. The
    # chunk plan is derived from the mezzanine's own duration, so it matches
    # whatever the concat phase computes from the same mezzanine. Whole-clip
    # mode (no --chunk-index) is unchanged.
    chunk = None
    if args.chunk_index is not None:
        chunks = plan_chunks(info.duration_s, _chunk_duration_s(),
                             _SEGMENT_DURATION_S)
        if args.chunk_index >= len(chunks):
            print(f"error: chunk-index {args.chunk_index} out of range "
                  f"(clip has {len(chunks)} chunk(s))", file=sys.stderr)
            return 1
        chunk = chunks[args.chunk_index]
        print(f"[phase variant] chunk {chunk.index}/{len(chunks)}: "
              f"[{chunk.start_s:.0f}s, {chunk.end_s:.0f}s)", flush=True)

    # CPU-seconds actually consumed by the ffmpeg child(ren) during the
    # encode (both passes, if two-pass). Divided by (reserved vCPU x encode
    # wall-time) per tier, this is the real utilization — i.e. how much of
    # the vCPU we pay for is crunching video vs sitting idle-reserved.
    ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    _enc_t0 = time.monotonic()
    out_path = encode_variant(ctx, args.codec, rung, chunk=chunk)
    encode_wall_s = time.monotonic() - _enc_t0
    ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_s = (ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime)
    # Peak resident memory of the ffmpeg child(ren) — ru_maxrss is the max RSS
    # of the largest child, in KiB on Linux. This is the real number for sizing
    # the job's MEMORY request: if it approaches the cgroup limit we're at OOM
    # risk (an over-the-limit encode is SIGKILLed and fails the whole run).
    peak_mib = ru1.ru_maxrss / 1024.0
    timer.mark("encode")

    limit_mib = _cgroup_mem_limit_mib()
    if limit_mib:
        print(f"[mem] {args.codec} {args.label}{'' if chunk is None else f' chunk{chunk.index}'}: "
              f"peak RSS {peak_mib:.0f} MiB / {limit_mib:.0f} MiB limit "
              f"({peak_mib / limit_mib * 100:.0f}% of limit)", flush=True)
    else:
        print(f"[mem] {args.codec} {args.label}: peak RSS {peak_mib:.0f} MiB", flush=True)

    # out_path.name is {codec}_{tier}.mp4 whole-clip, or
    # {codec}_{tier}_chunkNNN.mp4 for a chunk — upload under the same name.
    out_uri = args.s3_out.rstrip("/") + f"/{out_path.name}"
    print(f"[phase variant] uploading {out_uri}", flush=True)
    _upload_with_done(out_path, out_uri)
    timer.mark("upload")

    ci = "" if chunk is None else f":chunk{chunk.index}"
    timer.emit(f"encode:{args.codec}:{args.label}{ci}",
               cpu_s=f"{cpu_s:.2f}", mem_mib=f"{peak_mib:.0f}")

    # Feed the control plane's learned-speed model (drives the dynamic chunk
    # selector): content-seconds encoded vs encode wall-seconds for this
    # (codec, height, pass). The Go server's Manager.learnSpeed consumes it.
    content_s = (chunk.end_s - chunk.start_s) if chunk is not None else info.duration_s
    two_pass = 1 if (args.codec == "hevc" and ctx.hevc_two_pass) else 0
    if encode_wall_s > 0 and content_s > 0:
        print(f"[[ENCODER-SPEED codec={args.codec} height={rung.height} "
              f"two_pass={two_pass} content_s={content_s:.1f} "
              f"encode_s={encode_wall_s:.1f}]]", flush=True)
    return 0


# ---------------------------------------------------------------------------
# audio — extract / transcode the mezzanine's audio track.
# ---------------------------------------------------------------------------

def phase_audio(args: argparse.Namespace) -> int:
    work = _prepare_work_dir()

    # Idempotency / resume: reuse an existing audio.mp4 from a prior run or a
    # retry — skip the mezzanine download + audio extract. (A source with no
    # audio uploads nothing, so a missing audio.mp4 just runs normally and
    # re-confirms that.) FORCE_REENCODE overrides.
    out_uri = args.s3_out.rstrip("/") + "/audio.mp4"
    if not _env_flag("FORCE_REENCODE") and _s3_exists(out_uri):
        print("[phase audio] reusing audio.mp4 — already in S3, skipping "
              "extract", flush=True)
        emit_stage("audio", "done", 100.0)
        return 0

    # One continuous audio bar: mezzanine download 0-70% (the bulk), transcode
    # 70-95%, audio upload 95-100%.
    mezz_uri = args.s3_mezz.rstrip("/") + "/mezzanine.mp4"
    mezz_local = work / "mezzanine.mp4"
    print(f"[phase audio] downloading {mezz_uri}", flush=True)
    if not _download_if_complete(mezz_uri, mezz_local,
                                 stage=("audio", 0.0, 70.0)):
        print("error: mezzanine.done missing or size mismatch", file=sys.stderr)
        return 1

    info = probe(mezz_local)
    if not info.has_audio:
        # Still upload a zero-byte marker so downstream package phase
        # knows there was no audio. The package phase checks for the
        # file's presence, not its contents.
        print("[phase audio] source has no audio — skipping", flush=True)
        return 0

    out_path = work / "audio.mp4"
    create_audio(
        AudioSpec(
            mezzanine_path=mezz_local,
            output_path=out_path,
            padding_s=0.0,
        ),
        stage_key="audio",
        duration_s=info.duration_s,
        pct_lo=70.0, pct_hi=95.0, terminal=False,
    )

    out_uri = args.s3_out.rstrip("/") + "/audio.mp4"
    _upload_with_done(out_path, out_uri, stage=("audio", 95.0, 100.0))
    emit_stage("audio", "done", 100.0)
    return 0


# ---------------------------------------------------------------------------
# package — Shaka Packager for one codec. Downloads every variant
# MP4 for the codec + audio.mp4 if present.
# ---------------------------------------------------------------------------

def phase_package(args: argparse.Namespace) -> int:
    work = _prepare_work_dir()

    # Discover which rungs were actually encoded by LISTING S3 for
    # {codec}_{label}.mp4 objects (ladder-agnostic — works for any ladder,
    # including apple's ordinal labels like 1080p_1). Per-chunk files
    # ({codec}_{label}_chunkNNN.mp4) are excluded; concat already joined them.
    bucket, base_key = _parse(args.s3_variants.rstrip("/"))
    prefix = f"{base_key}/{args.codec}_"
    labels: list[str] = []
    paginator = _s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if not name.endswith(".mp4"):
                continue
            label = name[len(args.codec) + 1:-4]  # strip "{codec}_" and ".mp4"
            if not label or _CHUNK_RE.search(label):
                continue
            labels.append(label)
    labels = sorted(set(labels))

    labels_present: list[str] = []
    for label in labels:
        uri = args.s3_variants.rstrip("/") + f"/{args.codec}_{label}.mp4"
        dst = work / f"{args.codec}_{label}.mp4"
        if _download_if_complete(uri, dst):
            labels_present.append(label)

    if not labels_present:
        print(f"error: no {args.codec} variants found under {args.s3_variants}",
              file=sys.stderr)
        return 1

    audio_uri = args.s3_audio.rstrip("/") + "/audio.mp4"
    audio_local = work / "audio.mp4"
    has_audio = False
    try:
        if _download_if_complete(audio_uri, audio_local):
            has_audio = True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
            raise

    stem = f"output_{args.codec}"  # flat naming; the Step Function input's
                                   # caller can rename post-download
    pkg_dir = work / stem
    pkg_dir.mkdir(parents=True, exist_ok=True)

    emit_stage(f"package:{args.codec}", "running", 0.0)
    package(PackageSpec(
        tmp_dir=work,
        output_dir=pkg_dir,
        codec=args.codec,
        labels=tuple(labels_present),
        segment_duration_s=_SEGMENT_DURATION_S,
        partial_duration_s=_PARTIAL_DURATION_S,
        include_audio=has_audio,
    ))
    emit_stage(f"package:{args.codec}", "done", 100.0)

    _upload_dir(pkg_dir, args.s3_out.rstrip("/") + f"/{stem}")
    return 0


# ---------------------------------------------------------------------------
# hls — LL-HLS manifest generation for one codec's packaged output.
# ---------------------------------------------------------------------------

def phase_hls(args: argparse.Namespace) -> int:
    work = _prepare_work_dir()
    stem = f"output_{args.codec}"
    pkg_dir = work / stem
    pkg_dir.mkdir(parents=True, exist_ok=True)

    # Pull the packaged dir back down. The package phase uploaded
    # every file under <s3_package>/<stem>/…; mirror locally.
    _download_dir(args.s3_package.rstrip("/") + f"/{stem}", pkg_dir)

    emit_stage(f"hls:{args.codec}", "running", 0.0)
    generate_fmp4_hls(pkg_dir)
    emit_stage(f"hls:{args.codec}", "done", 100.0)

    _upload_dir(pkg_dir, args.s3_out.rstrip("/") + f"/{stem}")
    return 0


# ---------------------------------------------------------------------------
# byteranges — fMP4 fragment byterange sidecars for EXT-X-PART.
# ---------------------------------------------------------------------------

def phase_byteranges(args: argparse.Namespace) -> int:
    work = _prepare_work_dir()
    stem = f"output_{args.codec}"
    pkg_dir = work / stem
    pkg_dir.mkdir(parents=True, exist_ok=True)

    _download_dir(args.s3_package.rstrip("/") + f"/{stem}", pkg_dir)

    emit_stage(f"fragments:{args.codec}", "running", 0.0)
    generate_byteranges_sidecars(pkg_dir)
    # Self-contained DASH: expand fragment byte-ranges into manifest_fragmented.mpd
    write_fragmented_mpd(pkg_dir)
    emit_stage(f"fragments:{args.codec}", "done", 100.0)

    _upload_dir(pkg_dir, args.s3_out.rstrip("/") + f"/{stem}")
    return 0


# ---------------------------------------------------------------------------
# package-all — combined package + byteranges + fMP4 HLS for one codec in a
# single job. Downloads the variants + audio ONCE, then packages, writes the
# byterange sidecars, and generates the LL-HLS playlists all from the local
# package dir (no re-download between steps), and uploads once. Replaces the
# old package -> hls -> byteranges chain of three separate Batch jobs — which
# each cold-started, re-downloaded the whole ladder, and (a latent bug) ran
# HLS before the byteranges it embeds. Correct order is package -> byteranges
# -> hls (hls_from_dash reads the .byteranges sidecars).
# ---------------------------------------------------------------------------

def phase_package_all(args: argparse.Namespace) -> int:
    work = _prepare_work_dir()

    # Discover this codec's variants. Whole-variant runs upload
    # {codec}_{label}.mp4 directly; chunked runs upload
    # {codec}_{label}_chunkNNN.mp4 and are joined HERE — the concat used to be a
    # separate Batch job per variant, which cost a cold-start + a redundant S3
    # round-trip (upload the joined mp4, then re-download it here). Folding it in
    # downloads the chunks once, stream-copies them together, and packages. By
    # the time this phase runs the SFN has barriered on every chunk job
    # succeeding, so a chunk set present in S3 is complete and (max index + 1)
    # is the authoritative chunk count.
    bucket, base_key = _parse(args.s3_variants.rstrip("/"))
    prefix = f"{base_key}/{args.codec}_"
    whole_labels: set[str] = set()
    chunk_last: dict[str, int] = {}  # base label -> highest chunk index in S3
    paginator = _s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            if not name.endswith(".mp4"):
                continue
            label = name[len(args.codec) + 1:-4]  # strip "{codec}_" and ".mp4"
            if not label:
                continue
            m = _CHUNK_RE.search(label)
            if m:
                base = label[:m.start()]
                idx = int(m.group()[len("_chunk"):])
                chunk_last[base] = max(chunk_last.get(base, -1), idx)
            else:
                whole_labels.add(label)

    labels_present: list[str] = []

    # The package stage goes "running" the moment the job starts and its bar
    # advances as each variant is fetched (0->90%), so the long download + concat
    # isn't a dark gap before Shaka runs; package() finishes the remaining 90->100.
    chunked_bases = [b for b in sorted(chunk_last) if b not in whole_labels]
    total_dl = len(whole_labels) + len(chunked_bases)
    done_dl = 0

    def _dl_progress() -> None:
        pct = (done_dl / total_dl * 90.0) if total_dl else 0.0
        emit_stage(f"package:{args.codec}", "running", pct)

    _dl_progress()

    # Whole variants: download the joined mp4 directly.
    for label in sorted(whole_labels):
        uri = args.s3_variants.rstrip("/") + f"/{args.codec}_{label}.mp4"
        if _download_if_complete(uri, work / f"{args.codec}_{label}.mp4"):
            labels_present.append(label)
        done_dl += 1
        _dl_progress()

    # Chunked variants: pull every chunk, concat locally (stream copy), then
    # drop the chunk files so they don't inflate disk during packaging.
    for base in chunked_bases:
        n = chunk_last[base] + 1
        for i in range(n):
            name = f"{args.codec}_{base}_chunk{i:03d}.mp4"
            uri = args.s3_variants.rstrip("/") + f"/{name}"
            if not _download_if_complete(uri, work / name):
                print(f"error: chunk {name} missing/incomplete under "
                      f"{args.s3_variants}", file=sys.stderr)
                return 1
        concat_chunks(work, args.codec, base, n)
        print(f"[phase package-all] joined {n} chunk(s) -> "
              f"{args.codec}_{base}.mp4", flush=True)
        for i in range(n):
            name = f"{args.codec}_{base}_chunk{i:03d}.mp4"
            (work / name).unlink(missing_ok=True)
            (work / f"{name}.done").unlink(missing_ok=True)
        labels_present.append(base)
        done_dl += 1
        _dl_progress()

    labels_present = sorted(set(labels_present))
    if not labels_present:
        print(f"error: no {args.codec} variants found under {args.s3_variants}",
              file=sys.stderr)
        return 1

    audio_local = work / "audio.mp4"
    has_audio = False
    try:
        if _download_if_complete(args.s3_audio.rstrip("/") + "/audio.mp4", audio_local):
            has_audio = True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
            raise

    stem = f"output_{args.codec}"
    pkg_dir = work / stem
    pkg_dir.mkdir(parents=True, exist_ok=True)

    emit_stage(f"package:{args.codec}", "running", 90.0)
    package(PackageSpec(
        tmp_dir=work, output_dir=pkg_dir, codec=args.codec,
        labels=tuple(labels_present), segment_duration_s=_SEGMENT_DURATION_S,
        partial_duration_s=_PARTIAL_DURATION_S, include_audio=has_audio,
    ))
    emit_stage(f"package:{args.codec}", "done", 100.0)

    # Byteranges BEFORE HLS — the playlists embed the fragment byte ranges.
    emit_stage(f"fragments:{args.codec}", "running", 0.0)
    generate_byteranges_sidecars(pkg_dir)
    # Self-contained DASH: expand fragment byte-ranges into manifest_fragmented.mpd
    write_fragmented_mpd(pkg_dir)
    emit_stage(f"fragments:{args.codec}", "done", 100.0)

    emit_stage(f"hls:{args.codec}", "running", 0.0)
    generate_fmp4_hls(pkg_dir)
    emit_stage(f"hls:{args.codec}", "done", 100.0)

    _upload_dir(pkg_dir, args.s3_out.rstrip("/") + f"/{stem}")
    return 0


# ---------------------------------------------------------------------------
# Bulk dir transfer helpers — used by phases that produce or consume
# a whole directory tree (package, hls, byteranges).
# ---------------------------------------------------------------------------

def _upload_dir(local: Path, s3_prefix: str) -> None:
    bucket, base_key = _parse(s3_prefix)
    s3 = _s3()
    for f in local.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(local).as_posix()
        key = f"{base_key}/{rel}"
        s3.upload_file(str(f), bucket, key)


def _download_dir(s3_prefix: str, local: Path) -> None:
    bucket, base_key = _parse(s3_prefix)
    s3 = _s3()
    paginator = s3.get_paginator("list_objects_v2")
    local.mkdir(parents=True, exist_ok=True)
    for page in paginator.paginate(Bucket=bucket, Prefix=base_key + "/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            rel = key[len(base_key) + 1:]
            dst = local / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(bucket, key, str(dst))


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="encoder.cli_local phase",
        description="Per-phase S3-in/S3-out invocation used by Batch jobs.",
    )
    sub = p.add_subparsers(dest="phase", required=True)

    m = sub.add_parser("mezzanine")
    m.add_argument("--s3-in", required=True, dest="s3_in")
    m.add_argument("--s3-out", required=True, dest="s3_out")
    m.set_defaults(fn=phase_mezzanine)

    v = sub.add_parser("variant")
    v.add_argument("--codec", required=True, choices=("h264", "hevc", "av1"))
    # The concrete rung, resolved by the Go control plane from the ladder
    # store (so the worker needs no ladder knowledge — custom ladders work).
    v.add_argument("--label", required=True,
                   help="rung identity, e.g. 1080p or 1080p_1 (apple dup)")
    v.add_argument("--width", required=True, type=int)
    v.add_argument("--height", required=True, type=int)
    v.add_argument("--bitrate", required=True, type=int, help="target kbps")
    v.add_argument("--preset", default="medium")
    v.add_argument("--s3-mezz", required=True, dest="s3_mezz")
    v.add_argument("--s3-out", required=True, dest="s3_out")
    v.add_argument("--two-pass", action="store_true", dest="two_pass",
                   help="two-pass software encode (also honors TWO_PASS env)")
    v.add_argument("--chunk-index", type=int, default=None, dest="chunk_index",
                   help="encode only this 0-based chunk of the variant "
                        "(Batch array index); omit for a whole-clip encode")
    v.set_defaults(fn=phase_variant)

    a = sub.add_parser("audio")
    a.add_argument("--s3-mezz", required=True, dest="s3_mezz")
    a.add_argument("--s3-out", required=True, dest="s3_out")
    a.set_defaults(fn=phase_audio)

    pk = sub.add_parser("package")
    pk.add_argument("--codec", required=True, choices=("h264", "hevc", "av1"))
    pk.add_argument("--s3-variants", required=True, dest="s3_variants")
    pk.add_argument("--s3-audio", required=True, dest="s3_audio")
    pk.add_argument("--s3-out", required=True, dest="s3_out")
    pk.set_defaults(fn=phase_package)

    # Combined package + byteranges + fMP4 HLS in one job (replaces the
    # package -> hls -> byteranges chain).
    pa = sub.add_parser("package-all")
    pa.add_argument("--codec", required=True, choices=("h264", "hevc", "av1"))
    pa.add_argument("--s3-variants", required=True, dest="s3_variants")
    pa.add_argument("--s3-audio", required=True, dest="s3_audio")
    pa.add_argument("--s3-out", required=True, dest="s3_out")
    pa.set_defaults(fn=phase_package_all)

    h = sub.add_parser("hls")
    h.add_argument("--codec", required=True, choices=("h264", "hevc", "av1"))
    h.add_argument("--s3-package", required=True, dest="s3_package")
    h.add_argument("--s3-out", required=True, dest="s3_out")
    h.set_defaults(fn=phase_hls)

    b = sub.add_parser("byteranges")
    b.add_argument("--codec", required=True, choices=("h264", "hevc", "av1"))
    b.add_argument("--s3-package", required=True, dest="s3_package")
    b.add_argument("--s3-out", required=True, dest="s3_out")
    b.set_defaults(fn=phase_byteranges)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    emit_boot_ami()  # report which AMI this Batch worker booted from
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
