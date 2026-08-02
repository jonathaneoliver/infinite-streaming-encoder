"""Phase sub-command dispatcher for Batch jobs.

Each Batch job definition in infra/terraform/modules/jobs/main.tf
invokes this module with a specific phase name + the S3 URIs of its
inputs and outputs:

  python -m infinite_streaming_encoder.cli_local phase mezzanine  --s3-in URI --s3-out URI
  python -m infinite_streaming_encoder.cli_local phase variant    --codec C --tier T --s3-mezz URI --s3-out URI
  python -m infinite_streaming_encoder.cli_local phase audio                  --s3-mezz URI --s3-out URI
  python -m infinite_streaming_encoder.cli_local phase package    --codec C --s3-variants URI --s3-audio URI --s3-out URI
  python -m infinite_streaming_encoder.cli_local phase hls        --codec C --s3-package URI --s3-out URI
  python -m infinite_streaming_encoder.cli_local phase byteranges --codec C --s3-package URI --s3-out URI

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
import subprocess
import sys
import time
from pathlib import Path

from infinite_streaming_encoder.audio import AudioSpec, create_audio
from infinite_streaming_encoder.chunking import Chunk, DEFAULT_CHUNK_DURATION_S
from infinite_streaming_encoder.encode_variants import (
    EncodeContext, concat_chunks, encode_variant,
)
from infinite_streaming_encoder.ffprobe import probe
from infinite_streaming_encoder.hls import (
    TsHlsSpec, generate_byteranges_sidecars, generate_fmp4_hls,
)
from infinite_streaming_encoder.manifests import write_fragmented_mpd
from infinite_streaming_encoder.ladder import (
    BUFSIZE_MULTIPLIER, DEFAULT_MAXRATE_PERCENT, Rung, burnin_for_height,
    label_res_name,
)
from infinite_streaming_encoder.mezzanine import MezzanineSpec, create_mezzanine
from infinite_streaming_encoder.packager import PackageSpec, package
from infinite_streaming_encoder.padding import multi_duration_lcm, plan_padding
from infinite_streaming_encoder.progress import (
    emit_boot_ami, emit_stage, prime_fleet_cpu)
from infinite_streaming_encoder.telemetry import emit, flush as flush_telemetry

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover — boto3 is in requirements.txt for the image
    boto3 = None  # type: ignore
    ClientError = Exception  # type: ignore


# Local scratch dir inside the Batch container. Batch containers get
# their own ephemeral filesystem; no need for per-phase isolation.
_WORK_DIR = Path(os.environ.get("ENCODER_WORK_DIR", "/tmp/work"))

# Profile timing (segment / LL-HLS partial / GOP), read from env so the ladder's
# values — injected by the Step Functions containerOverrides (SEGMENT_DURATION /
# PARTIAL_DURATION / GOP_DURATION) — drive the cloud encode too. Previously
# hardcoded, so cloud ignored the job's/ladder's timing. Falls back to the live
# defaults. PARTIAL_DURATION=0 turns LL-HLS parts off (VOD).
def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name, "")
        return float(v) if v != "" else default
    except ValueError:
        return default


_SEGMENT_DURATION_S = _env_float("SEGMENT_DURATION", 6.0)
_PARTIAL_DURATION_S = _env_float("PARTIAL_DURATION", 0.2)
_GOP_DURATION_S = _env_float("GOP_DURATION", 1.0)
# How far the mezzanine's probed duration may drift from the duration the
# orchestrator planned chunk boundaries against. One frame at 24fps (~41.7ms) is
# the smallest difference that can move a boundary onto a different frame; the
# mezzanine is a pure stream copy, so a drift at this scale means the plan and
# the file genuinely disagree.
#
# The tolerance has to be this tight because of one specific coupling: the
# planned boundaries and the PROBED duration meet in build_ffmpeg_cmd, which
# decides "is this the final chunk" as `chunk.end_s < content_duration_s`. An
# interior chunk is capped to an exact frame count; the final one runs unbounded
# to EOF. If the probe reads even slightly LONGER than the plan, the real final
# chunk fails that test, gets treated as interior, and is truncated to the frame
# grid — silently dropping the tail of the variant.
_PLAN_DURATION_TOLERANCE_S = 1.0 / 24.0


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


def _cache_keep() -> int:
    """How many cached mezzanines to retain (MEZZ_CACHE_KEEP, default 2).

    Two is the smallest number that still buys the cross-job hit: the running
    job's mezzanine plus the previous one, so a back-to-back re-encode of the
    same source lands warm. It was 6, which is a count-based cap on an object
    whose size is the SOURCE's — the mezzanine is a stream copy, so it tracks the
    input. 6 x 474 MB is 2.8 GB and fine; 6 x 10 GB (a 2-hour clip) is 61 GB and
    fills the 30 GiB Batch root out from under the encode that filled it."""
    try:
        return max(1, int(os.environ.get("MEZZ_CACHE_KEEP", "") or 2))
    except ValueError:
        return 2


def _evict_cache_entry(cache_dir: Path, entry: Path) -> int:
    """Delete one cached artifact + its sidecars. Returns bytes reclaimed."""
    freed = 0
    for f in (entry, entry.with_suffix(".mp4.done"),
              cache_dir / (entry.name.rsplit(".", 1)[0] + ".lock")):
        try:
            freed += f.stat().st_size
        except OSError:
            pass
        try:
            f.unlink()
        except OSError:
            pass
    return freed


def _prune_mezz_cache(cache_dir: Path, keep: int | None = None) -> None:
    """Keep only the `keep` most-recent cached mezzanines — they're large. Recent
    ones (any active job's) survive, so this won't yank a symlink target from
    under a running encode in practice. Covers both the mezzanines and #109's
    per-box `vmafref-*` reference files, pruned independently."""
    if keep is None:
        keep = _cache_keep()
    for pat in ("mezz-*.mp4", "vmafref-*.mp4"):
        items = sorted(cache_dir.glob(pat),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        for old in items[keep:]:
            _evict_cache_entry(cache_dir, old)


def _ensure_cache_room(cache_dir: Path, need_bytes: int, what: str) -> bool:
    """Make room for a `need_bytes` download by evicting oldest-first, and report
    whether it fits. Runs BEFORE the download, which is the whole point: the old
    prune ran after, so it could tidy up but never prevent the write that filled
    the disk — and a full disk fails the chunk, not just the cache.

    Keeps a 20% margin because the cache shares the volume with every concurrent
    chunk's work dir, and they are writing while we are.

    False => don't cache this one. The caller downloads privately instead, which
    is exactly the pre-cache behaviour: a clip too big to cache still encodes."""
    margin = int(need_bytes * 1.2)
    for _ in range(64):  # bounded: eviction is monotonic, this is a stuck-loop guard
        try:
            free = shutil.disk_usage(cache_dir).free
        except OSError:
            return True  # can't measure => don't block the encode on a guess
        if free >= margin:
            return True
        items = sorted((p for pat in ("mezz-*.mp4", "vmafref-*.mp4")
                        for p in cache_dir.glob(pat)),
                       key=lambda p: p.stat().st_mtime)
        if not items:
            print(f"[phase variant] {what} too large for the cache volume "
                  f"({need_bytes / 1048576:.0f} MB needed, {free / 1048576:.0f} MB "
                  f"free) — downloading privately", flush=True)
            return False
        _evict_cache_entry(cache_dir, items[0])
    return False


def _object_size(uri: str) -> int:
    """ContentLength of an S3 object, or 0 if it can't be read. 0 means "skip the
    free-space check" rather than "empty" — a HEAD failure is not a reason to
    refuse to encode, and the download itself will report the real error."""
    try:
        bucket, key = _parse(uri)
        return int(_s3().head_object(Bucket=bucket, Key=key)["ContentLength"])
    except Exception:  # noqa: BLE001 — sizing is best-effort
        return 0


def _lock_with_timeout(lockf, what: str) -> bool:
    """Take the cache lock, giving up after MEZZ_CACHE_LOCK_TIMEOUT_S (600s).

    A plain blocking flock waits forever. That is fine while the leader is alive
    — a waiter blocked on the lock costs exactly what a waiter downloading its
    own copy costs, and it transfers 1/Nth the bytes — but a leader killed
    mid-download (spot reclaim, OOM) would hang every other chunk on the box for
    the rest of the job with no upper bound.

    600s is chosen to be far outside any legitimate download: at the measured
    263 MB/s it corresponds to ~157 GB, so expiry means "dead", not "slow"."""
    import fcntl
    try:
        deadline = float(os.environ.get("MEZZ_CACHE_LOCK_TIMEOUT_S", "") or 600)
    except ValueError:
        deadline = 600.0
    end = time.monotonic() + deadline
    while True:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= end:
                print(f"[phase variant] {what} cache lock held >{deadline:.0f}s "
                      f"— assuming the downloader died, fetching privately",
                      flush=True)
                return False
            time.sleep(0.5)


def _fetch_mezz_cached(mezz_uri: str, mezz_local: Path,
                       what: str = "mezzanine") -> bool:
    """Fetch a large per-job artifact (the mezzanine, or #109's pre-scaled VMAF
    reference), cached PER WORKER so N chunks on one box download it once instead
    of once per chunk. Keyed by the URI (unique per job, so it hits across that
    job's chunks) and symlinked into the per-activity work dir (zero-copy). A file
    lock makes concurrent chunks on the same box share one download. MEZZ_CACHE_DIR
    unset → plain per-chunk download (the AWS Batch path, where each chunk is its
    own ephemeral container anyway). `what` is the log label only."""
    cache_dir = os.environ.get("MEZZ_CACHE_DIR")
    if not cache_dir:
        return _download_if_complete(mezz_uri, mezz_local)
    import fcntl
    import hashlib
    os.makedirs(cache_dir, exist_ok=True)
    key = hashlib.sha256(mezz_uri.encode()).hexdigest()[:16]
    cached = Path(cache_dir) / f"mezz-{key}.mp4"
    cached_done = cached.with_suffix(".mp4.done")
    with open(Path(cache_dir) / f"mezz-{key}.lock", "w") as lockf:
        if not _lock_with_timeout(lockf, what):
            # The leader is stuck or dead. Waiting on flock has no upper bound,
            # so every chunk on this box would hang behind it. Fall back to a
            # private download — the pre-cache behaviour, and safe even if the
            # leader is merely slow, because we never touch its output path.
            return _download_if_complete(mezz_uri, mezz_local)
        if cached.is_file() and cached_done.is_file():
            print(f"[phase variant] {what} served from worker cache", flush=True)
        else:
            need = _object_size(mezz_uri)
            if need and not _ensure_cache_room(Path(cache_dir), need, what):
                return _download_if_complete(mezz_uri, mezz_local)
            print(f"[phase variant] caching {what} (first chunk on this box)", flush=True)
            if not _download_if_complete(mezz_uri, cached):
                return False
            _prune_mezz_cache(Path(cache_dir))
    try:
        if mezz_local.is_symlink() or mezz_local.exists():
            mezz_local.unlink()
        mezz_local.symlink_to(cached)
    except OSError:
        shutil.copy(cached, mezz_local)
    return True


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


def _env_flag_default_on(name: str) -> bool:
    """A default-ON env flag: enabled unless explicitly set falsy (0/false/no).
    Unset → True. Used for BURNIN (the text overlay ships on by default)."""
    return os.environ.get(name, "").strip().lower() not in ("0", "false", "no")


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


def _vmaf_estimate_label(args: argparse.Namespace) -> str:
    """Format the design-time VMAF estimate for the burn-in overlay, from the
    --est-vmaf flag (local dist) or EST_VMAF env (cloud SFN). '≥' marks a rung
    ABOVE the measured curve (a clamped nearest-endpoint, not an interpolation);
    '~' an interpolation. Returns "" when no estimate was supplied."""
    v = getattr(args, "est_vmaf", None)
    if v is None:
        ev = os.environ.get("EST_VMAF", "")
        try:
            v = float(ev) if ev else None
        except ValueError:
            v = None
    if not v or v <= 0:
        return ""
    clamped = getattr(args, "est_vmaf_clamped", False) or _env_flag("EST_VMAF_CLAMPED")
    return f"VMAF{'≥' if clamped else '~'}{round(v)}"


# ---------------------------------------------------------------------------
# mezzanine — stream-copy the source clip into a fragmented MP4 so
# subsequent encode/audio phases share one normalised input.
# ---------------------------------------------------------------------------

# #109 pre-scaled VMAF reference — a NEAR-LOSSLESS H.264 downscale of the
# mezzanine, built PER BOX from the box's already-cached mezzanine so the (large)
# reference never crosses the network. Gated (see _should_prescale): only pays
# off with >= a few renditions AND when the source is genuinely slower to
# decode+downscale than a fast H.264 ref. Off / gated out / build failed -> the
# audit uses the STANDARD mezzanine.
_VMAF_REF_CRF = int(os.environ.get("VMAF_PRESCALE_CRF", "8") or "8")
# Rough per-codec software-decode cost, H.264 = 1.0 (only the ratio matters).
_DECODE_FACTOR = {"h264": 1.0, "avc": 1.0, "hevc": 1.6, "h265": 1.6,
                  "av1": 2.5, "vp9": 2.0, "vp8": 1.2, "mpeg2video": 0.6,
                  "prores": 0.5, "mjpeg": 0.4}
_REF_BPS_AT_1080 = 30_000_000  # ~30 Mbps near-lossless 1080p, for the size gate


def _build_prescaled_ref(mezz_path: Path, ref_path: Path, cw: int, ch: int,
                         fps: str, crf: int, keyint: int) -> None:
    """Downscale the mezzanine to (cw x ch) as NEAR-LOSSLESS H.264 (#109), with the
    SAME filter chain measure_vmaf applies to the reference (fps -> bicubic scale
    -> yuv420p -> setsar). Near-lossless (crf~8) shifts VMAF <~0.4 vs lossless but
    is far smaller and fast to decode. `keyint` aligns keyframes to chunk
    boundaries so per-chunk -ss seeks land clean (minimal pre-roll). Video-only;
    CFR timestamps preserved so window seeks stay aligned."""
    vf = f"fps={fps},scale={cw}:{ch}:flags=bicubic,format=yuv420p,setsar=1"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
         "-i", str(mezz_path), "-map", "0:v:0", "-vf", vf,
         "-c:v", "libx264", "-crf", str(crf), "-preset", "ultrafast",
         "-g", str(max(1, keyint)), "-keyint_min", str(max(1, keyint)),
         "-an", str(ref_path)],
        check=True)


def _source_slower_than_ref(info, cw: int, ch: int) -> bool:
    """True when decoding+downscaling the native mezzanine costs more than decoding
    a fast H.264 ref at (cw x ch) — the necessary condition for the pre-scale to
    save time. Proxy: source_pixels x codec_factor vs common_pixels (H.264 = 1.0),
    +15% margin. A 4K AV1 source clears it easily; an already-1080p H.264 source
    does not (nothing to gain)."""
    src_px = max(1, info.width * info.height)
    cmn_px = max(1, cw * ch)
    factor = _DECODE_FACTOR.get((getattr(info, "video_codec", None) or "").lower(), 1.0)
    return src_px * factor > cmn_px * 1.15


def _est_ref_bytes(duration_s: float, ch: int) -> int:
    """Rough near-lossless ref size for the size gate (scales with duration and
    pixels, bitrate ~ (ch/1080)^2 of the 1080p rate)."""
    return int(max(0.0, duration_s) * _REF_BPS_AT_1080 * (ch / 1080.0) ** 2 / 8.0)


def _should_prescale(info, cw: int, ch: int, num_variants: int) -> bool:
    """Gate for #109 (all must hold): the feature is ON (VMAF_PRESCALE); there are
    enough renditions to amortize the one-time build (>= VMAF_PRESCALE_MIN_VARIANTS,
    default 2 — break-even ~2.3 rungs); the source is genuinely slower to
    decode+scale than a fast H.264 ref; and the estimated ref fits locally
    (VMAF_PRESCALE_MAX_BYTES, default 4 GB)."""
    if not _env_flag("VMAF_PRESCALE"):
        return False
    if num_variants < int(os.environ.get("VMAF_PRESCALE_MIN_VARIANTS", "2") or "2"):
        return False
    if not _source_slower_than_ref(info, cw, ch):
        return False
    max_bytes = int(os.environ.get("VMAF_PRESCALE_MAX_BYTES", "") or (4 * 1024**3))
    return _est_ref_bytes(info.duration_s, ch) <= max_bytes


def _get_or_build_prescaled_ref(mezz_local: Path, mezz_uri: str, info,
                                cw: int, ch: int, keyint: int) -> Path | None:
    """Get-or-build the near-lossless pre-scaled reference LOCALLY on this box from
    its already-cached mezzanine (#109). Built once per box per (source, res, crf),
    shared by every chunk + redo via MEZZ_CACHE_DIR + a file lock so concurrent
    chunks share one build. The big ref never crosses the network. Returns the ref
    path, or None => 'use the native mezzanine' (no cache dir / build failed)."""
    cache_dir = os.environ.get("MEZZ_CACHE_DIR")
    if not cache_dir:
        return None  # AWS-Batch path: ephemeral per-chunk container, no reuse
    import fcntl
    import hashlib
    os.makedirs(cache_dir, exist_ok=True)
    key = hashlib.sha256(
        f"{mezz_uri}|{cw}x{ch}|crf{_VMAF_REF_CRF}".encode()).hexdigest()[:16]
    ref = Path(cache_dir) / f"vmafref-{key}.mp4"
    done = ref.with_suffix(".mp4.done")
    with open(Path(cache_dir) / f"vmafref-{key}.lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        if ref.is_file() and done.is_file():
            emit("[[ENCODER-VMAFREF status=cached]]")
        else:
            emit(f"[[ENCODER-VMAFREF status=building dims={cw}x{ch} "
                 f"crf={_VMAF_REF_CRF}]]")
            try:
                tmp = ref.with_suffix(".building.mp4")
                _build_prescaled_ref(mezz_local, tmp, cw, ch, str(info.fps),
                                     _VMAF_REF_CRF, keyint)
                tmp.replace(ref)
                done.write_text(str(ref.stat().st_size))
                _prune_mezz_cache(Path(cache_dir))
                emit(f"[[ENCODER-VMAFREF status=built dims={cw}x{ch}]]")
            except Exception as e:  # noqa: BLE001 — fall back to native mezz
                emit(f"[[ENCODER-VMAFREF status=failed "
                     f"err={type(e).__name__}:{str(e)[:120]}]]")
                return None
    return ref


def phase_mezzanine(args: argparse.Namespace) -> int:
    work = _prepare_work_dir()

    # Idempotency / resume / cross-job cache: if a COMPLETE mezzanine is already
    # in S3 (a prior run, a retry, or another job on the same source when
    # --s3-out is the shared mezz-cache/ prefix), reuse it — skip the source
    # download + stream-copy entirely. Gate on the .done sidecar, not the .mp4:
    # a half-uploaded mezzanine has the .mp4 but no .done, and reusing it would
    # feed chunks a truncated file. FORCE_REENCODE overrides.
    out_uri = args.s3_out.rstrip("/") + "/mezzanine.mp4"
    if not _env_flag("FORCE_REENCODE") and _s3_exists(out_uri + ".done"):
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
            # Normalize any VFR source to EXACT CFR here (lossless), so the
            # per-chunk encodes pass frames through 1:1 and the VMAF audit
            # can't drift on a jittery reference (see mezzanine.py docstring).
            fps_num=info.fps.numerator,
            fps_den=info.fps.denominator,
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
        emit(f"[[ENCODER-TIMING phase={self.phase} key={key} {kv}{extra_kv} "
             f"total_s={total:.2f}]]")
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
    print(f"[phase variant] fetching {mezz_uri}", flush=True)
    if not _fetch_mezz_cached(mezz_uri, mezz_local):
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
        # Per-codec pass count for THIS variant's codec. TWO_PASS is already
        # per-variant (the upstream resolved it from the profile's passes), so
        # it carries the codec-correct 1/2 — map it under this codec. Supersedes
        # hevc_two_pass and lets AV1/H264 two-pass, not just HEVC.
        passes={args.codec: 2 if (args.two_pass or _env_flag("TWO_PASS")) else 1},
        # Per-codec profile extra_args: the --extra-args flag (local dist path)
        # or the EXTRA_ARGS container env (cloud SFN). This worker encodes a
        # single codec, so map the value under its own codec for
        # build_ffmpeg_cmd's ctx.extra_args[codec] lookup.
        extra_args={args.codec: (getattr(args, "extra_args", "") or os.environ.get("EXTRA_ARGS", ""))},
        # Text overlay, on by default. Disabled by EITHER the --no-burnin flag
        # (local-dist path) OR a falsy BURNIN env (cloud SFN containerOverrides).
        burnin=getattr(args, "burnin", True) and _env_flag_default_on("BURNIN"),
        # Design-time VMAF estimate the Go control plane looked up per rung from
        # the quality curves, passed via --est-vmaf (local dist) or EST_VMAF env
        # (cloud SFN). Burned into the overlay as one row; "" omits it.
        vmaf_label=_vmaf_estimate_label(args),
    )

    # Chunked mode: encode only chunk `--chunk-index` of the variant. The
    # chunk plan is derived from the mezzanine's own duration, so it matches
    # whatever the concat phase computes from the same mezzanine. Whole-clip
    # mode (no --chunk-index) is unchanged.
    chunk = None
    if args.chunk_index is not None:
        # The ORCHESTRATOR owns the chunk plan and hands us our span. We do not
        # re-derive it.
        #
        # Both paths used to plan independently here, from this worker's own
        # probe of the mezzanine, with the orchestrator passing only a count (or,
        # on local-dist, not even that — three processes each ran plan_chunks and
        # were kept in step by a COALESCE_RUNT_TAIL env flag so they'd fold a
        # short tail identically). Agreement by convention: it held only while
        # every process saw the same duration, and a disagreement surfaced as
        # "chunk-index out of range" or, worse, as silently shifted split points.
        if args.chunk_start is None or args.chunk_span is None:
            print("error: --chunk-index requires --chunk-start and --chunk-span "
                  "(the orchestrator passes the chunk plan; this worker does not "
                  "derive it). A pre-plan orchestrator is talking to a post-plan "
                  "worker — redeploy both.", file=sys.stderr)
            return 1
        chunk = Chunk(index=args.chunk_index, start_s=args.chunk_start,
                      duration_s=args.chunk_span)
        # Validate rather than derive: the cloud control plane plans from the
        # SOURCE probe, before the mezzanine job has run, so its boundaries
        # assume the stream copy preserved the duration exactly. Fail loudly if
        # it didn't — the alternative is every chunk boundary shifting by the
        # drift and the concatenated variant silently gaining or losing frames.
        if args.content_duration is not None:
            drift = abs(info.duration_s - args.content_duration)
            if drift > _PLAN_DURATION_TOLERANCE_S:
                print(f"error: chunk plan was built for a "
                      f"{args.content_duration:.6f}s clip but this mezzanine is "
                      f"{info.duration_s:.6f}s ({drift:.6f}s drift, tolerance "
                      f"{_PLAN_DURATION_TOLERANCE_S}s). Every chunk boundary "
                      f"would be wrong.", file=sys.stderr)
                return 1
        # No total to report: this worker is handed one span and never builds
        # the full plan, so it does not know how many chunks there are.
        print(f"[phase variant] chunk {chunk.index}: "
              f"[{chunk.start_s:.3f}s, {chunk.end_s:.3f}s)", flush=True)

    # CPU-seconds actually consumed by the ffmpeg child(ren) during the
    # encode (both passes, if two-pass). Divided by (reserved vCPU x encode
    # wall-time) per tier, this is the real utilization — i.e. how much of
    # the vCPU we pay for is crunching video vs sitting idle-reserved.
    ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
    _enc_t0 = time.monotonic()
    # ENCODE_THREADS pins ffmpeg's thread count independent of the node: under a
    # scheduler (Nomad) the CPU reservation is for bin-packing, not a hard core
    # cap, so we can't rely on cgroup detection to size the encode. When unset
    # (0), fall back to the cgroup quota (the AWS Batch path, where vCPU == cap).
    _threads_env = int(os.environ.get("ENCODE_THREADS", "0") or "0")
    out_path = encode_variant(ctx, args.codec, rung, chunk=chunk,
                              threads=_threads_env or None)
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

    # VMAF audit (#24, opt-in via measure_vmaf). Score THIS rendition against the
    # mezzanine at the SOURCE resolution (common-res upscale + source-driven
    # model). Runs per-chunk so it fans out across the fleet exactly like the
    # encode; the control plane aggregates the ENCODER-VMAF markers into a
    # per-rung score. Best-effort — a VMAF failure never fails the encode.
    if getattr(args, "measure_vmaf", False) or _env_flag("MEASURE_VMAF"):
        try:
            from infinite_streaming_encoder.vmaf_audit import (
                common_dimensions, measure_vmaf, pick_model, vmaf_marker,
            )
            # Compare at the source res CAPPED to MAX_VMAF_HEIGHT (default
            # 1080p) — a 4K comparison OOM-kills on 8-10 GB Docker VMs and runs
            # ~2x slower; the model follows the capped height (#24).
            cmn_w, cmn_h = common_dimensions(info.width, info.height)
            # #109: use a NEAR-LOSSLESS pre-scaled reference built ONCE PER BOX
            # from this box's local mezzanine — decode a small fast H.264 file
            # instead of re-downscaling the native (4K/AV1) mezzanine on every
            # chunk. Gated (feature on + enough renditions + source genuinely
            # slower to decode+scale + fits locally). In its ABSENCE (off / gated
            # out / build failed) the audit uses the STANDARD mezzanine — the ref
            # is a decode-cost optimization only, never a correctness dependency.
            vmaf_ref = ctx.mezzanine_path
            _nvar = int(os.environ.get("NUM_VARIANTS", "0") or "0")
            if _should_prescale(info, cmn_w, cmn_h, _nvar):
                _keyint = max(1, int(round(_chunk_duration_s() * float(info.fps))))
                _built = _get_or_build_prescaled_ref(
                    ctx.mezzanine_path, args.s3_mezz, info, cmn_w, cmn_h, _keyint)
                if _built is not None:
                    vmaf_ref = _built
            # This chunk's frame-exact length (same ceil(t*fps) math the encode
            # uses, #90) so the audit can clamp both streams to it — otherwise the
            # seeked reference window's ~1-frame seam drift injects a spurious
            # 0-VMAF frame that pins min/harmonic to noise (#108). Whole-clip
            # (chunk is None) needs no clamp.
            n_frames = None
            if chunk is not None:
                from fractions import Fraction

                def _frames_before(t: float) -> int:
                    x = Fraction(t) * info.fps
                    return max(0, -(-x.numerator // x.denominator))  # ceil(t*fps)
                n_frames = _frames_before(chunk.end_s) - _frames_before(chunk.start_s)
            r = measure_vmaf(
                out_path, vmaf_ref, cmn_w, cmn_h,
                pick_model(cmn_h),
                ref_start_s=(chunk.start_s if chunk is not None else 0.0),
                ref_duration_s=(chunk.duration_s if chunk is not None else None),
                n_subsample=5, n_threads=(_threads_env or 0),
                # Force both streams onto the source's frame grid so a cadence
                # slip can't desync the comparison (learned from the offline
                # ladder audit — VMAF craters on a 1-frame slip in high motion).
                fps=str(info.fps),
                n_frames=n_frames,
            )
            emit(vmaf_marker(args.codec, args.label, rung.height,
                             chunk.index if chunk is not None else -1, r))
        except Exception as e:  # noqa: BLE001 — audit must never fail the encode
            # Prefix with [[ENCODER so the temporal worker relays it (it only
            # forwards [[ENCODER… lines) — otherwise a VMAF failure is invisible.
            import traceback
            emit(f"[[ENCODER-VMAF-ERROR codec={args.codec} label={args.label} "
                 f"chunk={chunk.index if chunk is not None else -1}: "
                 f"{type(e).__name__}: {str(e)[:220]}]]")
            print("[vmaf] " + traceback.format_exc().replace("\n", " | "),
                  flush=True)
        timer.mark("vmaf")

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
    # selector, cost, and ETA): content-seconds encoded vs encode wall-seconds,
    # keyed by every dimension that moves encode time. machine = this worker's
    # label (mac/ubuntu/macmini for local-dist, unset on cloud batch → its
    # workers are all Graviton, so "graviton"); preset + fps because encode time
    # scales with both. The Go server's Manager.learnSpeed consumes it.
    content_s = (chunk.end_s - chunk.start_s) if chunk is not None else info.duration_s
    two_pass = 1 if (args.codec == "hevc" and ctx.hevc_two_pass) else 0
    machine = os.environ.get("WORKER_LABEL") or "graviton"
    fps_i = max(1, round(float(info.fps)))
    if encode_wall_s > 0 and content_s > 0:
        emit(f"[[ENCODER-SPEED machine={machine} codec={args.codec} "
             f"height={rung.height} two_pass={two_pass} preset={args.preset} "
             f"fps={fps_i} content_s={content_s:.1f} "
             f"encode_s={encode_wall_s:.1f}]]")
    # ENCODER-SPEED and ENCODER-TIMING are the LAST things this phase says, and
    # both are records that cannot be recovered without re-encoding. Hand them to
    # the sink before returning rather than relying on the interpreter shutting
    # down cleanly — on spot capacity it often does not.
    flush_telemetry()
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
    # Timed like the variant phase. Without this the package job recorded no
    # marks at all, so "how much of package-all is fetching versus joining
    # versus actually packaging" could not be answered from a finished run —
    # only estimated from the shape of a progress bar. It is also why package
    # rows show "—" for cpu/worker in the phase rollup.
    _pkg_timer = _StepTimer("package-all")
    _pkg_ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
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

    _pkg_timer.mark("fetch_join")
    emit_stage(f"package:{args.codec}", "running", 90.0)
    package(PackageSpec(
        tmp_dir=work, output_dir=pkg_dir, codec=args.codec,
        labels=tuple(labels_present), segment_duration_s=_SEGMENT_DURATION_S,
        partial_duration_s=_PARTIAL_DURATION_S, include_audio=has_audio,
    ))
    _pkg_timer.mark("shaka")
    emit_stage(f"package:{args.codec}", "done", 100.0)

    # Byteranges BEFORE HLS — the playlists embed the fragment byte ranges.
    emit_stage(f"fragments:{args.codec}", "running", 0.0)
    generate_byteranges_sidecars(pkg_dir)
    # Self-contained DASH: expand fragment byte-ranges into manifest_fragmented.mpd
    write_fragmented_mpd(pkg_dir)
    _pkg_timer.mark("fragments")
    emit_stage(f"fragments:{args.codec}", "done", 100.0)

    emit_stage(f"hls:{args.codec}", "running", 0.0)
    generate_fmp4_hls(pkg_dir)
    _pkg_timer.mark("hls")
    emit_stage(f"hls:{args.codec}", "done", 100.0)

    _upload_dir(pkg_dir, args.s3_out.rstrip("/") + f"/{stem}")
    _pkg_timer.mark("upload")
    _ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    _cpu = ((_ru1.ru_utime - _pkg_ru0.ru_utime)
            + (_ru1.ru_stime - _pkg_ru0.ru_stime))
    _pkg_timer.emit(f"package:{args.codec}", cpu_s=f"{_cpu:.2f}")
    flush_telemetry()
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
        prog="infinite_streaming_encoder.cli_local phase",
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
    v.add_argument("--est-vmaf", type=float, default=None, dest="est_vmaf",
                   help="design-time VMAF estimate for this rung (from the quality "
                        "curves) to burn into the overlay; also honors EST_VMAF env")
    v.add_argument("--est-vmaf-clamped", action="store_true", dest="est_vmaf_clamped",
                   help="the estimate is a clamped endpoint (rung above the measured "
                        "range) → shown as VMAF≥N; also honors EST_VMAF_CLAMPED env")
    v.add_argument("--preset", default="medium")
    v.add_argument("--s3-mezz", required=True, dest="s3_mezz")
    v.add_argument("--s3-out", required=True, dest="s3_out")
    v.add_argument("--two-pass", action="store_true", dest="two_pass",
                   help="two-pass software encode (also honors TWO_PASS env)")
    v.add_argument("--extra-args", default="", dest="extra_args",
                   help="raw per-codec ffmpeg args from the ladder profile, "
                        "appended after rate control (also honors EXTRA_ARGS "
                        "env); shlex-split to argv, never shell-eval'd")
    v.add_argument("--measure-vmaf", action="store_true", dest="measure_vmaf",
                   help="after encoding, measure per-rendition VMAF vs the "
                        "mezzanine at source res (also honors MEASURE_VMAF env); "
                        "emits an [[ENCODER-VMAF …]] marker. Slow — off by default")
    v.add_argument("--chunk-index", type=int, default=None, dest="chunk_index",
                   help="encode only this 0-based chunk of the variant "
                        "(Batch array index); omit for a whole-clip encode")
    v.add_argument("--chunk-start", type=float, default=None, dest="chunk_start",
                   help="absolute offset (s) of this chunk, from the "
                        "orchestrator's plan; required with --chunk-index")
    v.add_argument("--chunk-span", type=float, default=None, dest="chunk_span",
                   help="duration (s) of this chunk, from the orchestrator's "
                        "plan; required with --chunk-index")
    v.add_argument("--content-duration", type=float, default=None,
                   dest="content_duration",
                   help="clip duration the chunk plan was built against; the "
                        "worker fails if its own probe disagrees, rather than "
                        "encoding a plan meant for a different-length file")
    v.add_argument("--no-burnin", action="store_false", dest="burnin",
                   help="disable the burnt-in text overlay (timecode/rate/codec/"
                        "watermark labels); on by default. Also honors BURNIN env "
                        "(0/false/no disables)")
    v.set_defaults(fn=phase_variant, burnin=True)

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
    # Take the CPU baseline now so the FIRST progress heartbeat can already carry
    # a fleet sample. Without it the first call has nothing to difference against
    # and emits nothing — which on a sub-second chunk means no CPU is reported at
    # all for that job.
    prime_fleet_cpu()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
