"""Submit an encode from the command line, through the control plane.

    python3 -m infinite_streaming_encoder.encoder_cli clip.mp4 --target cloud \\
        --codec both --max-res 1080p --wait

The other cli_* modules in this package are WORKER entry points — the Go server
runs them inside containers with `--input` / `--output-dir` / `--job-prefix`.
Driving one of those by hand skips everything the control plane does: the
skip-what-already-exists codec narrowing, staging in $TMP_DIR so partial output
never lands in OUTPUT_DIR, archive-on-force, reattach-after-restart, and the
cost markers. This module is the opposite — a thin HTTP client that submits the
same request the browser does, so a scripted encode behaves like a clicked one.

## Why this exists

`make smoke` hand-rolled a curl to POST /api/encode plus a poll loop, and then
`make oobe` hand-rolled the same block again with subtly different failure
handling. Two callers each writing their own is the argument for one.

## What it deliberately does NOT do

Validate semantics. The server already rejects an inverted min/max band, a
resolution band that selects no rungs for the chosen codec, an unknown target,
and a source name that is not in SOURCE_DIR — with better messages than a client
could write, because it can see the ladder and the directory. A second copy of
those rules here would be one more thing to drift (see the three spot rates in
#217). This checks SHAPE only: is that a known flag, is the enum spelled right.

Resolutions are the exception that proves it: they are parsed, not enumerated
(`954p` and `1800p` are real rungs in the Apple-uniq ladders), so --max-res
takes any `<N>p` rather than a fixed choices list that would reject a valid tier.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_SERVER = os.environ.get("ENCODER_SERVER", "http://localhost:8080")

# Terminal job states, from encode.Job.Status. "gone" is this module's own: a
# job that vanished from /api/jobs is not a success, and treating a missing job
# as "probably fine" is how a broken run reports PASS.
_TERMINAL_OK = ("done",)
_TERMINAL_BAD = ("failed", "move-failed", "cancelled", "gone")

_RES = re.compile(r"^\d+p$")


def _res(value: str) -> str:
    """A resolution tier. Shape only — which tiers exist depends on the ladder."""
    if value == "" or _RES.match(value):
        return value
    raise argparse.ArgumentTypeError(
        f"{value!r} is not a resolution tier (expected e.g. 720p, 1080p, 2160p)")


def _chunk(value: str) -> str:
    """"dynamic" | "whole" | a seconds count. The multiple-of-segment rule is
    the server's to enforce; this only rejects a typo like "12s"."""
    if value in ("", "dynamic", "whole") or value.isdigit():
        return value
    raise argparse.ArgumentTypeError(
        f"{value!r} must be 'dynamic', 'whole', or a number of seconds")


# --- flag <-> JobConfig -----------------------------------------------------
#
# Every entry maps ONE argparse dest to ONE JobConfig json tag. The pairing is
# explicit rather than derived from the dest name so that
# scripts/test_encoder_cli.py can reflect over internal/encode/job.go and fail
# when a field is added to JobConfig without reaching the command line. That
# test is the real defence here: 23 fields hand-copied WILL drift, and the
# failure is silent — the flag simply does not exist while the browser keeps
# working.
#
# str fields: omitted from the body when empty, so the server's own default
# applies rather than an empty string overriding it.
_STR_FIELDS = {
    "codec": "codec",
    "ladder": "ladder",
    "max_res": "max_res",
    "min_res": "min_res",
    "target": "target",
    "time": "time",
    "segment_duration": "segment_duration",
    "partial_duration": "partial_duration",
    "chunk_duration": "chunk_duration",
    "gop_duration": "gop_duration",
    "hls_format": "hls_format",
    "padding": "padding",
    "cpu_arch": "cpu_arch",
}

# Plain booleans: false IS the default, so sending it changes nothing.
_BOOL_FIELDS = {
    "keep_mezzanine": "keep_mezzanine",
    "force_reencode": "force_reencode",
    "promote": "promote",
    "hevc_single_pass": "hevc_single_pass",
    "measure_vmaf": "measure_vmaf",
    "vmaf_prescale": "vmaf_prescale",
}

# TRI-STATE. These are pointers in JobConfig, where nil means "use the server
# default" and only an explicit value overrides it — SKIP_OUTPUT_MEDIA for
# skip_media_download, on-by-default for burnin, USE_SPOT for use_spot. So they
# must be OMITTED when unset, not sent as false: argparse's store_true would
# turn "I didn't say" into "no", silently disabling burn-in on every scripted
# encode. BooleanOptionalAction with default=None gives --x / --no-x / unset.
_TRISTATE_FIELDS = {
    "burnin": "burnin",
    "skip_media_download": "skip_media_download",
    "use_spot": "use_spot",
}

# Filled by the server from the selected ladder's output_tag, not typed by a
# user (see JobConfig.OutputTag). Named here so the drift test can tell
# "deliberately absent" from "forgotten".
_NOT_USER_FIELDS = ("files", "output_tag")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="encoder_cli",
        description="Submit an encode to a running encoder server.",
        epilog="Sources are named as they appear in the SERVER's SOURCE_DIR, "
               "not as paths on this machine — use --list-sources to see them.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    p.add_argument("files", nargs="*", metavar="FILE",
                   help="a path to a local file (uploaded first), or the name "
                        "of one already in the server's SOURCE_DIR. Each becomes "
                        "its own job, so they run concurrently.")
    p.add_argument("--server", default=DEFAULT_SERVER,
                   help="encoder server base URL (env: ENCODER_SERVER)")
    p.add_argument("--no-upload", action="store_true",
                   help="never upload: treat every FILE as a name already on "
                        "the server, even if a local file shares that name")
    p.add_argument("--download", metavar="DIR",
                   help="after a successful encode, copy the produced output "
                        "directories into DIR. Implies --wait. Files already "
                        "present at the right size are skipped, so an "
                        "interrupted download resumes.")

    g = p.add_argument_group("what to encode")
    g.add_argument("--codec", choices=["h264", "hevc", "av1", "both", "all"],
                   default="", help="codec selection (server default: both)")
    g.add_argument("--ladder", default="",
                   help="bitrate ladder by name (see --list-ladders)")
    g.add_argument("--max-res", type=_res, default="", metavar="TIER",
                   help="ceiling tier, e.g. 1080p")
    g.add_argument("--min-res", type=_res, default="", metavar="TIER",
                   help="floor tier — with --max-res, selects a band")
    g.add_argument("--time", default="", metavar="SECONDS",
                   help="encode only the first N seconds of each file")

    g = p.add_argument_group("where to run it")
    g.add_argument("--target", choices=["local", "cloud"], default="",
                   help="local fleet or AWS Batch (server default: local)")
    g.add_argument("--cpu-arch", choices=["graviton", "intel", "amd"], default="",
                   help="cloud only; ignored for local")
    g.add_argument("--spot", dest="use_spot", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="cloud purchasing mode (unset: server default USE_SPOT)")
    g.add_argument("--chunk-duration", type=_chunk, default="", metavar="SPEC",
                   help="'dynamic', 'whole', or seconds")

    g = p.add_argument_group("packaging")
    g.add_argument("--segment-duration", default="", metavar="SECONDS")
    g.add_argument("--partial-duration", default="", metavar="MS",
                   help="LL-HLS part target, in milliseconds")
    g.add_argument("--gop-duration", default="", metavar="SECONDS")
    g.add_argument("--hls-format", choices=["fmp4", "ts", "both"], default="")
    g.add_argument("--padding", choices=["", "black", "pink"], default="",
                   help="pad to a segment boundary with this colour")

    g = p.add_argument_group("options")
    g.add_argument("--burnin", action=argparse.BooleanOptionalAction, default=None,
                   help="diagnostic text overlay (unset: on). Biases VMAF.")
    g.add_argument("--skip-media-download", action=argparse.BooleanOptionalAction,
                   default=None,
                   help="cloud only: leave segments in S3, fetch later "
                        "(unset: server default SKIP_OUTPUT_MEDIA)")
    g.add_argument("--force-reencode", action="store_true",
                   help="re-encode even where output exists; the old output is "
                        "archived, not deleted")
    g.add_argument("--keep-mezzanine", action="store_true")
    g.add_argument("--promote", action="store_true",
                   help="rsync each output to PROMOTE_DESTS on success")
    g.add_argument("--hevc-single-pass", action="store_true",
                   help="HEVC single-pass (~2x faster, for bitrate comparison)")
    g.add_argument("--measure-vmaf", action="store_true",
                   help="per-rendition VMAF audit after encoding (slow)")
    g.add_argument("--vmaf-prescale", action="store_true",
                   help="build a pre-scaled VMAF reference per worker box")

    g = p.add_argument_group("mode")
    g.add_argument("--dry-run", action="store_true",
                   help="print the JSON body that would be POSTed, and exit")
    g.add_argument("--estimate", action="store_true",
                   help="price this configuration instead of running it")
    g.add_argument("--wait", action="store_true",
                   help="poll until every job reaches a terminal state; exit "
                        "non-zero if any did not finish 'done'")
    g.add_argument("--quiet", action="store_true",
                   help="with --wait, print only terminal transitions")
    g.add_argument("--poll-interval", type=float, default=5.0, metavar="S")
    g.add_argument("--timeout", type=float, default=3600.0, metavar="S",
                   help="give up waiting after this long (the encode keeps going)")
    g.add_argument("--list-sources", action="store_true",
                   help="list the server's source files, then exit")
    g.add_argument("--list-ladders", action="store_true",
                   help="list the server's ladder names, then exit")
    g.add_argument("--json", action="store_true",
                   help="machine-readable output on stdout")
    return p


def build_body(args) -> dict:
    """The exact JSON the browser posts. Empty strings and unset tri-states are
    OMITTED rather than sent, so the server's defaults still apply."""
    body: dict = {"files": list(args.files)}
    for dest, field in _STR_FIELDS.items():
        v = getattr(args, dest, "")
        if v != "":
            body[field] = v
    for dest, field in _BOOL_FIELDS.items():
        if getattr(args, dest, False):
            body[field] = True
    for dest, field in _TRISTATE_FIELDS.items():
        v = getattr(args, dest, None)
        if v is not None:
            body[field] = bool(v)
    return body


# --- HTTP -------------------------------------------------------------------

def _request(url: str, body: dict | None = None, method: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method or ("POST" if data else "GET"),
        headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def _fail(msg: str) -> int:
    print(f"!!! {msg}", file=sys.stderr, flush=True)
    return 1


def _call(url: str, body: dict | None = None) -> tuple[object, str | None]:
    """Returns (parsed, error). The server's 400 body is the useful part — it
    says WHICH resolution band selects no rungs — so it is surfaced verbatim
    rather than reduced to a status code."""
    try:
        return _request(url, body), None
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode(errors="replace").strip()
        return None, f"server said {e.code}: {detail or e.reason}"
    except urllib.error.URLError as e:
        return None, (f"could not reach {url} ({e.reason}). Is the server "
                      "running? Set --server or ENCODER_SERVER.")
    except (TimeoutError, ValueError) as e:
        return None, f"{url}: {e}"


# --- upload -----------------------------------------------------------------

def _upload(server: str, path: str, quiet: bool) -> tuple[str | None, str | None]:
    """POST one local file to /api/sources/upload; returns (server name, error).

    Streams from disk in chunks rather than reading the file into a bytes
    object: sources are multi-GB, and the server takes the same care on its
    side (MultipartReader, never ParseMultipartForm). Building the body as one
    string here would undo that.
    """
    import mimetypes
    import uuid
    name = os.path.basename(path)
    size = os.path.getsize(path)
    boundary = "----encoder-cli-" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    head = (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    def body():
        yield head
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                yield chunk
        yield tail

    req = urllib.request.Request(
        f"{server}/api/sources/upload", data=body(), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 # Without an explicit length urllib would use chunked transfer
                 # encoding, which Go's multipart reader handles but which makes
                 # the upload unresumable and hides its size from any proxy.
                 "Content-Length": str(len(head) + size + len(tail))})
    if not quiet:
        print(f"    uploading {name} ({_human(size)})...", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=3600) as r:
            saved = (json.loads(r.read() or b"{}") or {}).get("saved") or []
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode(errors="replace").strip()
        return None, f"upload {name}: server said {e.code}: {detail or e.reason}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"upload {name}: {e}"
    if not saved:
        return None, f"upload {name}: server saved nothing"
    return saved[0], None


def _resolve_inputs(server: str, files: list[str], args) -> tuple[list[str], str | None]:
    """Turn the positional arguments into names the server knows.

    A FILE that exists on this machine is uploaded and replaced by its
    basename; anything else is passed through as an existing source name. That
    ambiguity is resolved by the filesystem rather than by a flag because both
    forms are natural — `encoder_cli ./clip.mp4` from a laptop and
    `encoder_cli clip.mp4` on the master box — and --no-upload forces the
    second reading when a local file happens to share a name with a server one.
    """
    out = []
    for f in files:
        if not args.no_upload and os.path.isfile(f):
            name, err = _upload(server, f, args.quiet)
            if err:
                return [], err
            out.append(name)
        else:
            # A path-looking argument that does not exist locally is almost
            # certainly a typo, not a source name — say so here rather than
            # letting the server reject "../foo.mp4" for a different reason.
            if os.sep in f and not args.no_upload:
                return [], (f"{f}: no such local file (a name with a path "
                            "separator is treated as a local path)")
            out.append(f)
    return out, None


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# --- wait -------------------------------------------------------------------

def _describe(job: dict) -> str:
    """One line of live state: percent, plus whichever of stages / progress is
    actually saying something.

    `progress` is the worker's latest line and `stages` is the structured view,
    and they are NOT both worth showing. On local-dist, progress stops at the
    orchestrator's "[dist] starting workflow …" while the stage list keeps
    moving, so including both repeated the same 60 stale characters on every
    line of a real smoke run — text that looks current and is not. Stages win
    when there are any; progress is the fallback for a target or a phase that
    reports no stages (submission, the cloud upload, a plain local encode).
    """
    bits = []
    pct = job.get("overall_progress") or 0
    if pct:
        bits.append(f"{pct:.0f}%")
    running = [s.get("label") or s.get("key")
               for s in (job.get("stages") or []) if s.get("status") == "running"]
    if running:
        bits.append(", ".join(running[:3]))
    else:
        line = (job.get("progress") or "").strip()
        if line and line not in bits:
            bits.append(line)
    return " · ".join(bits) or job.get("status", "")


def _wait(server: str, ids: list[str], args) -> tuple[int, dict[str, list[str]]]:
    """Poll until every job is terminal, narrating as it goes.

    A job that DISAPPEARS counts as bad — silence is not success. That is
    exactly how `make oobe` could reach its PASS branch for a run that never
    happened.

    Returns (exit code, {job id: produced output dir names}).
    """
    pending, deadline = set(ids), time.time() + args.timeout
    bad: dict[str, str] = {}
    outputs: dict[str, list[str]] = {}
    last: dict[str, str] = {}
    while pending and time.time() < deadline:
        jobs, err = _call(f"{server}/api/jobs")
        if err:
            return _fail(err), outputs
        by_id = {j["id"]: j for j in (jobs or [])}
        for jid in sorted(pending):
            job = by_id.get(jid) or {}
            st = job.get("status", "gone")
            if st in _TERMINAL_OK:
                pending.discard(jid)
                outputs[jid] = job.get("outputs") or []
                if not args.json:
                    got = f" -> {', '.join(outputs[jid])}" if outputs[jid] else ""
                    print(f"    {jid} {st}{got}", flush=True)
            elif st in _TERMINAL_BAD:
                pending.discard(jid)
                bad[jid] = f"{st}: {job.get('error')}" if job.get("error") else st
                if not args.json:
                    print(f"    {jid} {bad[jid]}", flush=True)
            elif not args.quiet and not args.json:
                # Only on CHANGE. A 13-minute encode polled every 5s would
                # otherwise print 156 identical lines.
                desc = _describe(job)
                if desc and desc != last.get(jid):
                    last[jid] = desc
                    print(f"    {jid} {desc}", flush=True)
        if pending:
            time.sleep(args.poll_interval)
    if pending:
        return _fail(f"timed out after {args.timeout:.0f}s with "
                     f"{len(pending)} job(s) still running: {', '.join(sorted(pending))}"
                     " — the encode itself keeps going"), outputs
    if bad:
        return _fail("; ".join(f"{k} {v}" for k, v in sorted(bad.items()))), outputs
    return 0, outputs


# --- download ---------------------------------------------------------------

def _download(server: str, names: list[str], dest: str, quiet: bool) -> int:
    """Copy finished output directories to a local directory over HTTP.

    Not rsync — but rsync's useful property, which is skipping what is already
    there. Size is taken from the listing, so the skip costs no extra request,
    and an interrupted download resumes instead of re-paying. Same idiom as
    cli_batch's S3 fetch.

    HTTP rather than ssh+rsync deliberately: it works identically whether the
    server is this machine or another one, and needs no key, no account, and no
    knowledge of the server's filesystem layout.
    """
    from pathlib import Path
    total_got = total_skipped = 0
    for name in names:
        listing, err = _call(f"{server}/api/outputs/{urllib.parse.quote(name)}/files")
        if err:
            return _fail(f"{name}: {err}")
        files = (listing or {}).get("files") or []
        if not files:
            print(f"    {name}: nothing to download", flush=True)
            continue
        root = Path(dest) / name
        got = skipped = 0
        for f in files:
            rel, size = f["path"], f.get("size", 0)
            dst = root / rel
            if dst.exists() and dst.stat().st_size == size:
                skipped += 1
                total_skipped += size
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            url = (f"{server}/content/{urllib.parse.quote(name)}/"
                   + urllib.parse.quote(rel))
            # Straight to a temp file then rename, so an interrupted transfer
            # cannot leave a short file that the size check would later accept
            # as complete.
            tmp = dst.with_name(dst.name + ".part")
            try:
                with urllib.request.urlopen(url, timeout=300) as r, \
                        open(tmp, "wb") as out:
                    while chunk := r.read(1 << 20):
                        out.write(chunk)
                tmp.replace(dst)
            except (urllib.error.URLError, OSError) as e:
                tmp.unlink(missing_ok=True)
                return _fail(f"{name}/{rel}: {e}")
            got += 1
            total_got += size
            if not quiet and got % 25 == 0:
                print(f"    {name}: {got}/{len(files)} files...", flush=True)
        print(f"    {name}: {got} file(s) -> {root}"
              + (f", {skipped} already present" if skipped else ""), flush=True)
    if not quiet:
        print(f"    downloaded {_human(total_got)}"
              + (f", skipped {_human(total_skipped)} already on disk"
                 if total_skipped else ""), flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server = args.server.rstrip("/")

    if args.list_sources:
        got, err = _call(f"{server}/api/sources")
        if err:
            return _fail(err)
        names = [s["name"] for s in (got or [])]
        print(json.dumps(names) if args.json else "\n".join(names))
        return 0
    if args.list_ladders:
        got, err = _call(f"{server}/api/ladders")
        if err:
            return _fail(err)
        names = sorted((got or {}).keys())
        print(json.dumps(names) if args.json else "\n".join(names))
        return 0

    if not args.files:
        return _fail("no source files given (see --list-sources)")

    # --download needs a finished job to fetch from, so it cannot mean anything
    # without waiting. Implying it beats rejecting the combination.
    if args.download:
        args.wait = True

    if args.dry_run:
        # Before any upload: a dry run must not have side effects, and pushing a
        # multi-GB source is the largest side effect this tool has.
        print(json.dumps(build_body(args), indent=2))
        return 0

    names, err = _resolve_inputs(server, args.files, args)
    if err:
        return _fail(err)
    args.files = names
    body = build_body(args)

    if args.estimate:
        est, err = _call(f"{server}/api/encode/estimate", body)
        if err:
            return _fail(err)
        print(json.dumps(est, indent=2) if args.json else _format_estimate(est))
        return 0

    jobs, err = _call(f"{server}/api/encode", body)
    if err:
        return _fail(err)
    # One job per file — the server splits the request, so there is never
    # exactly one id to wait on unless exactly one file was named.
    ids = [j["id"] for j in (jobs or [])]
    if not ids:
        return _fail("server accepted the request but created no jobs")
    if args.json:
        print(json.dumps({"jobs": ids}))
    else:
        print(f"submitted {len(ids)} job(s): {', '.join(ids)}", flush=True)
    if not args.wait:
        return 0

    rc, outputs = _wait(server, ids, args)
    if rc != 0 or not args.download:
        return rc
    produced = [n for ns in outputs.values() for n in ns]
    if not produced:
        # Done, but nothing moved — resolveCodec skipped every codec because the
        # output already existed. Not a failure, and not something to download.
        print("    nothing produced (already encoded — use --force-reencode "
              "to replace)", flush=True)
        return 0
    return _download(server, produced, args.download, args.quiet)


def _format_estimate(est) -> str:
    if not isinstance(est, dict):
        return str(est)
    if est.get("local"):
        return "local target — nothing is billed ($0.00)"
    rows = [("spot", "spot_usd"), ("on-demand", "ondemand_usd"),
            ("egress", "egress_usd"), ("TOTAL", "total_usd")]
    out = [f"  {label:<12}${est.get(key, 0):.4f}" for label, key in rows
           if key in est]
    if est.get("duration_s"):
        out.insert(0, f"  {'duration':<12}{est['duration_s']:.0f}s")
    return "\n".join(out) or json.dumps(est)


if __name__ == "__main__":
    sys.exit(main())
