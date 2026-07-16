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
import shutil
import sys
import time
from pathlib import Path

from encoder.audio import AudioSpec, create_audio
from encoder.chunking import DEFAULT_CHUNK_DURATION_S, chunk_count, plan_chunks
from encoder.encode_variants import EncodeContext, concat_chunks, encode_variant
from encoder.ffprobe import probe
from encoder.hls import (
    TsHlsSpec, generate_byteranges_sidecars, generate_fmp4_hls,
)
from encoder.ladder import LADDER, Tier, select_tiers
from encoder.mezzanine import MezzanineSpec, create_mezzanine
from encoder.packager import PackageSpec, package
from encoder.padding import multi_duration_lcm, plan_padding
from encoder.progress import emit_stage

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


def _download(uri: str, dst: Path) -> None:
    bucket, key = _parse(uri)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _s3().download_file(bucket, key, str(dst))


def _upload(src: Path, uri: str) -> None:
    bucket, key = _parse(uri)
    _s3().upload_file(str(src), bucket, key)


def _write_done(path: Path) -> None:
    """Write <path>.done containing the file's size; atomic rename."""
    size = path.stat().st_size
    marker = path.with_suffix(path.suffix + ".done")
    tmp = marker.with_suffix(marker.suffix + ".tmp")
    tmp.write_text(f"{size}\n")
    tmp.rename(marker)


def _upload_with_done(local: Path, s3_uri: str) -> None:
    """Upload a file + its .done sidecar so consumers can verify."""
    _write_done(local)
    _upload(local, s3_uri)
    _upload(local.with_suffix(local.suffix + ".done"), s3_uri + ".done")


def _download_if_complete(s3_uri: str, dst: Path) -> bool:
    """Download `s3_uri` + its .done sidecar into dst. Returns True iff
    both landed and the .done recorded size matches the file size.

    The remote phase that produced `dst` calls `_upload_with_done`, so
    the .done-matches-size invariant is a correctness check — if it
    fails, the object was overwritten mid-upload or partial and the
    downstream phase should abort rather than continue.
    """
    _download(s3_uri, dst)
    marker_s3 = s3_uri + ".done"
    marker_local = dst.with_suffix(dst.suffix + ".done")
    try:
        _download(marker_s3, marker_local)
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


def _tier_by_name(name: str) -> Tier:
    for t in LADDER:
        if t.name == name:
            return t
    raise ValueError(f"unknown tier: {name}")


# ---------------------------------------------------------------------------
# mezzanine — stream-copy the source clip into a fragmented MP4 so
# subsequent encode/audio phases share one normalised input.
# ---------------------------------------------------------------------------

def phase_mezzanine(args: argparse.Namespace) -> int:
    work = _prepare_work_dir()

    # Download source. `--s3-in` is the full object URI; pull the
    # basename for the local filename.
    src_uri = args.s3_in
    _, src_key = _parse(src_uri)
    src_basename = src_key.rsplit("/", 1)[-1]
    local_in = work / src_basename
    print(f"[phase mezzanine] downloading {src_uri}", flush=True)
    _download(src_uri, local_in)

    out_path = work / "mezzanine.mp4"
    info = probe(local_in)

    emit_stage("mezzanine", "running", 0.0)
    create_mezzanine(
        MezzanineSpec(
            input_path=local_in,
            output_path=out_path,
            time_limit_s=None,
        ),
        stage_key="mezzanine",
        duration_s=info.duration_s,
    )
    emit_stage("mezzanine", "done", 100.0)

    out_uri = args.s3_out.rstrip("/") + "/mezzanine.mp4"
    print(f"[phase mezzanine] uploading {out_uri}", flush=True)
    _upload_with_done(out_path, out_uri)
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

    def emit(self, key: str) -> None:
        total = time.monotonic() - self._t0
        kv = " ".join(f"{n}_s={d:.2f}" for n, d in self.marks)
        # Machine-readable (parsed by cloud.timing / the app), then human.
        print(f"[[ENCODER-TIMING phase={self.phase} key={key} {kv} "
              f"total_s={total:.2f}]]", flush=True)
        human = ", ".join(f"{n}={d:.1f}s" for n, d in self.marks)
        print(f"[timing] {self.phase} {key}: {human}, total={total:.1f}s",
              flush=True)


def phase_variant(args: argparse.Namespace) -> int:
    work = _prepare_work_dir()
    timer = _StepTimer("variant")

    mezz_uri = args.s3_mezz.rstrip("/") + "/mezzanine.mp4"
    mezz_local = work / "mezzanine.mp4"
    print(f"[phase variant] downloading {mezz_uri}", flush=True)
    if not _download_if_complete(mezz_uri, mezz_local):
        print("error: mezzanine.done missing or size mismatch", file=sys.stderr)
        return 1
    timer.mark("fetch")

    tier = _tier_by_name(args.tier)
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
        # Two-pass is enabled per-execution: the Step Function injects
        # TWO_PASS via containerOverrides (see buildSFNInput), and the
        # --two-pass flag covers direct/local invocation of this phase.
        two_pass=args.two_pass or _env_flag("TWO_PASS"),
    )

    # Chunked mode: encode only chunk `--chunk-index` of the variant. The
    # chunk plan is derived from the mezzanine's own duration, so it matches
    # whatever the concat phase computes from the same mezzanine. Whole-clip
    # mode (no --chunk-index) is unchanged.
    chunk = None
    if args.chunk_index is not None:
        chunks = plan_chunks(info.duration_s, DEFAULT_CHUNK_DURATION_S,
                             _SEGMENT_DURATION_S)
        if args.chunk_index >= len(chunks):
            print(f"error: chunk-index {args.chunk_index} out of range "
                  f"(clip has {len(chunks)} chunk(s))", file=sys.stderr)
            return 1
        chunk = chunks[args.chunk_index]
        print(f"[phase variant] chunk {chunk.index}/{len(chunks)}: "
              f"[{chunk.start_s:.0f}s, {chunk.end_s:.0f}s)", flush=True)

    out_path = encode_variant(ctx, args.codec, tier, chunk=chunk)
    timer.mark("encode")

    # out_path.name is {codec}_{tier}.mp4 whole-clip, or
    # {codec}_{tier}_chunkNNN.mp4 for a chunk — upload under the same name.
    out_uri = args.s3_out.rstrip("/") + f"/{out_path.name}"
    print(f"[phase variant] uploading {out_uri}", flush=True)
    _upload_with_done(out_path, out_uri)
    timer.mark("upload")

    ci = "" if chunk is None else f":chunk{chunk.index}"
    timer.emit(f"encode:{args.codec}:{args.tier}{ci}")
    return 0


# ---------------------------------------------------------------------------
# concat-variant — join a variant's chunk encodes into the whole variant.
# Runs after the chunk array job for one (codec, tier) completes.
# ---------------------------------------------------------------------------

def phase_concat_variant(args: argparse.Namespace) -> int:
    work = _prepare_work_dir()
    tier = _tier_by_name(args.tier)

    # Derive the chunk count from the mezzanine duration — the same source
    # the variant phase used to plan chunks, so the two never disagree.
    mezz_uri = args.s3_mezz.rstrip("/") + "/mezzanine.mp4"
    mezz_local = work / "mezzanine.mp4"
    if not _download_if_complete(mezz_uri, mezz_local):
        print("error: mezzanine.done missing or size mismatch", file=sys.stderr)
        return 1
    n_chunks = chunk_count(probe(mezz_local).duration_s, DEFAULT_CHUNK_DURATION_S)

    # Pull every chunk file down (verifying its .done sidecar).
    for i in range(n_chunks):
        name = f"{args.codec}_{args.tier}_chunk{i:03d}.mp4"
        uri = args.s3_chunks.rstrip("/") + f"/{name}"
        if not _download_if_complete(uri, work / name):
            print(f"error: chunk {name} missing or incomplete at {uri}",
                  file=sys.stderr)
            return 1

    out_path = concat_chunks(work, args.codec, tier, n_chunks)

    out_uri = args.s3_out.rstrip("/") + f"/{args.codec}_{args.tier}.mp4"
    print(f"[phase concat-variant] joined {n_chunks} chunk(s) → {out_uri}",
          flush=True)
    _upload_with_done(out_path, out_uri)
    return 0


# ---------------------------------------------------------------------------
# audio — extract / transcode the mezzanine's audio track.
# ---------------------------------------------------------------------------

def phase_audio(args: argparse.Namespace) -> int:
    work = _prepare_work_dir()

    mezz_uri = args.s3_mezz.rstrip("/") + "/mezzanine.mp4"
    mezz_local = work / "mezzanine.mp4"
    print(f"[phase audio] downloading {mezz_uri}", flush=True)
    if not _download_if_complete(mezz_uri, mezz_local):
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
    emit_stage("audio", "running", 0.0)
    create_audio(
        AudioSpec(
            mezzanine_path=mezz_local,
            output_path=out_path,
            padding_s=0.0,
        ),
        stage_key="audio",
        duration_s=info.duration_s,
    )
    emit_stage("audio", "done", 100.0)

    out_uri = args.s3_out.rstrip("/") + "/audio.mp4"
    _upload_with_done(out_path, out_uri)
    return 0


# ---------------------------------------------------------------------------
# package — Shaka Packager for one codec. Downloads every variant
# MP4 for the codec + audio.mp4 if present.
# ---------------------------------------------------------------------------

def phase_package(args: argparse.Namespace) -> int:
    work = _prepare_work_dir()

    # Probe the first variant we can download so we can feed select_tiers
    # a real source width. For now we always expect all 6 tiers; if the
    # source was smaller we'd need extra signalling from the Step Function.
    tiers_present: list[Tier] = []
    for tier in LADDER:
        uri = args.s3_variants.rstrip("/") + f"/{args.codec}_{tier.name}.mp4"
        dst = work / f"{args.codec}_{tier.name}.mp4"
        try:
            if _download_if_complete(uri, dst):
                tiers_present.append(tier)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                continue
            raise

    if not tiers_present:
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
        tiers=tuple(tiers_present),
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
    emit_stage(f"fragments:{args.codec}", "done", 100.0)

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
    v.add_argument("--tier", required=True,
                   choices=("360p", "540p", "720p", "1080p", "1440p", "2160p"))
    v.add_argument("--s3-mezz", required=True, dest="s3_mezz")
    v.add_argument("--s3-out", required=True, dest="s3_out")
    v.add_argument("--two-pass", action="store_true", dest="two_pass",
                   help="two-pass software encode (also honors TWO_PASS env)")
    v.add_argument("--chunk-index", type=int, default=None, dest="chunk_index",
                   help="encode only this 0-based chunk of the variant "
                        "(Batch array index); omit for a whole-clip encode")
    v.set_defaults(fn=phase_variant)

    cv = sub.add_parser("concat-variant")
    cv.add_argument("--codec", required=True, choices=("h264", "hevc", "av1"))
    cv.add_argument("--tier", required=True,
                    choices=("360p", "540p", "720p", "1080p", "1440p", "2160p"))
    cv.add_argument("--s3-mezz", required=True, dest="s3_mezz",
                    help="prefix holding mezzanine.mp4 (used to derive chunk count)")
    cv.add_argument("--s3-chunks", required=True, dest="s3_chunks",
                    help="prefix holding the {codec}_{tier}_chunkNNN.mp4 files")
    cv.add_argument("--s3-out", required=True, dest="s3_out")
    cv.set_defaults(fn=phase_concat_variant)

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
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
