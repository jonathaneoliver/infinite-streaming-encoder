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
                   help="source file name(s) in the server's SOURCE_DIR. Each "
                        "becomes its own job, so they run concurrently.")
    p.add_argument("--server", default=DEFAULT_SERVER,
                   help="encoder server base URL (env: ENCODER_SERVER)")

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


def _wait(server: str, ids: list[str], args) -> int:
    """Poll until every job is terminal. A job that DISAPPEARS counts as bad —
    silence is not success."""
    pending, deadline = set(ids), time.time() + args.timeout
    bad: dict[str, str] = {}
    while pending and time.time() < deadline:
        jobs, err = _call(f"{server}/api/jobs")
        if err:
            return _fail(err)
        by_id = {j["id"]: j for j in (jobs or [])}
        for jid in sorted(pending):
            st = (by_id.get(jid) or {}).get("status", "gone")
            if st in _TERMINAL_OK:
                pending.discard(jid)
                if not args.json:
                    print(f"    {jid} {st}", flush=True)
            elif st in _TERMINAL_BAD:
                pending.discard(jid)
                bad[jid] = st
                if not args.json:
                    print(f"    {jid} {st}", flush=True)
        if pending:
            time.sleep(args.poll_interval)
    if pending:
        return _fail(f"timed out after {args.timeout:.0f}s with "
                     f"{len(pending)} job(s) still running: {', '.join(sorted(pending))}"
                     " — the encode itself keeps going")
    if bad:
        return _fail("; ".join(f"{k} {v}" for k, v in sorted(bad.items())))
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

    body = build_body(args)
    if args.dry_run:
        print(json.dumps(body, indent=2))
        return 0

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
    return _wait(server, ids, args) if args.wait else 0


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
