#!/usr/bin/env python3
"""Remove learned encode-speed entries that describe a configuration which no
longer runs.

Written for #314, where every two-pass h264 encode filed its measurement under
the 1-pass key for a release. Those entries are not merely stale — they are a
blend of genuine 1-pass samples and mislabelled 2-pass ones, at an EWMA weight
(w=0.3, half-life ~2 samples) that leaves the stored value describing whichever
regime ran most recently. There is no way to unmix them and no reason to: the
2-pass keys they should have gone to are empty, so nothing is lost by starting
those from the seed model, and leaving the 1-pass keys in place means the next
ladder that pins `passes: {"h264": 1}` inherits a number measured from two-pass
encodes.

Dry-run by default. Nothing is written without --apply.

    python3 scripts/purge_speed_keys.py --codec h264 --pass 1
    python3 scripts/purge_speed_keys.py --codec h264 --pass 1 --apply

THE SERVER MUST BE STOPPED. EncodeSpeedStore is loaded into memory once at
startup and rewritten whole on every sample, so a purge applied underneath a
running server is overwritten by the next chunk that finishes — silently, and
looking exactly like the purge did not work. This refuses to --apply while the
server container is up unless you pass --force.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Run from the HOST, so these are host paths. STATE_DIR wins once the store has
# been moved out of $TMP_DIR (#331); the literal is the container's default view
# and the last resort when neither var is set.
DEFAULT_STORE = os.path.join(
    os.environ.get("STATE_DIR") or os.environ.get("TMP_DIR") or "/media/tmp",
    "encode_speeds.json")


def server_running(name: str) -> bool | None:
    """True/False if we could ask docker, None if we could not.

    Exact name match, not `--filter name=`: that filter is a SUBSTRING match, so
    the default `infinite-streaming-encoder` also matches
    `infinite-streaming-encoder-temporal-1` and friends. Every one of those is up
    whenever the farm is, so the filter form would report the server as running
    forever and this guard would just be a --force prompt.
    """
    try:
        out = subprocess.run(["docker", "ps", "--format", "{{.Names}}"],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return name in out.stdout.split()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", default=DEFAULT_STORE,
                    help=f"path to encode_speeds.json (default {DEFAULT_STORE})")
    ap.add_argument("--codec", required=True, help="codec to purge, e.g. h264")
    ap.add_argument("--pass", dest="passes", type=int, required=True, choices=(1, 2),
                    help="pass count to purge")
    ap.add_argument("--machine", default="",
                    help="limit to one machine (graviton/mac/ubuntu/macmini); "
                         "default every machine")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without this, report only")
    ap.add_argument("--force", action="store_true",
                    help="--apply even though the server looks like it is running")
    ap.add_argument("--container", default="infinite-streaming-encoder",
                    help="server container name to check for (CONTAINER_NAME in "
                         "docker-compose.yml; default infinite-streaming-encoder)")
    args = ap.parse_args()

    path = Path(args.store)
    if not path.is_file():
        print(f"no store at {path}", file=sys.stderr)
        return 1
    data = json.loads(path.read_text())
    speeds = data.get("speeds") or {}
    samples = data.get("samples") or {}

    # Key shape: {machine}:{codec}:{height}:{pass}:{preset}:{fps} (speedKey in
    # internal/encode/speed.go). Match on the fields rather than a substring —
    # ":1:" alone would also hit a preset or an fps of 1.
    doomed = []
    for k in sorted(set(speeds) | set(samples)):
        parts = k.split(":")
        if len(parts) != 6:
            continue
        machine, codec, _height, npass, _preset, _fps = parts
        if codec != args.codec or npass != str(args.passes):
            continue
        if args.machine and machine != args.machine:
            continue
        doomed.append(k)

    if not doomed:
        print(f"nothing matches codec={args.codec} pass={args.passes}"
              + (f" machine={args.machine}" if args.machine else ""))
        return 0

    total = sum(samples.get(k, 0) for k in doomed)
    print(f"{path}")
    print(f"{len(doomed)} keys, {total:,} samples matching codec={args.codec} "
          f"pass={args.passes}" + (f" machine={args.machine}" if args.machine else ""))
    for k in doomed:
        print(f"  {k:<44} {speeds.get(k, 0):>8.3f}  n={samples.get(k, 0)}")

    # What survives, so the report answers "is there anything left" without a
    # second command — the whole worry with a purge is taking more than intended.
    kept = len(set(speeds) | set(samples)) - len(doomed)
    print(f"\n{kept} keys remain")

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    running = server_running(args.container)
    if running and not args.force:
        print("\nREFUSING: the encoder server is running.\n"
              "  It holds this store in memory and rewrites it whole on every\n"
              "  finished chunk, so the purge would be undone by the next one.\n"
              "  Stop it (make stop), purge, then start it (make run).\n"
              "  --force overrides.", file=sys.stderr)
        return 1
    if running is None:
        print("\nNOTE: could not ask docker whether the server is running.\n"
              "  If it is, restart it after this so it reloads the store.")

    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    for k in doomed:
        speeds.pop(k, None)
        samples.pop(k, None)
    data["speeds"], data["samples"] = speeds, samples
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    print(f"\nremoved {len(doomed)} keys ({total:,} samples); backup at {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
