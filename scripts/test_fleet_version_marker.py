"""Pins the ENCODER-FLEET / ENCODER-HOST `version` field across the language
boundary (#248).

Python writes these markers (cli_local_dist), Go parses them (internal/encode/
job.go). That is the same shape of contract as the MinIO staging key and the
telemetry queue name, and it fails the same way: both sides keep working, no
error is raised, and the field simply never arrives.

So this does NOT hardcode a copy of the Go pattern — it READS the pattern out of
job.go and runs the real Python builders against it. A copy would drift exactly
like the thing being guarded. If job.go's declaration is reformatted past
recognition this fails loudly, which is the correct outcome: it means somebody
should re-check the pairing by hand.

Run directly or via `make check`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from infinite_streaming_encoder.cli_local_dist import (  # noqa: E402
    fleet_marker, host_marker,
)

JOB_GO = ROOT / "internal" / "encode" / "job.go"

failures: list[str] = []


def go_pattern(var: str) -> re.Pattern:
    """Pull one `xxxMarkerRe = regexp.MustCompile(`...`)` pattern out of job.go.

    Go's RE2 and Python's re agree on everything these patterns use (named
    groups, optional groups, character classes), so the string transfers as-is.
    """
    src = JOB_GO.read_text()
    m = re.search(var + r"\s*=\s*regexp\.MustCompile\(`([^`]+)`\)", src)
    if not m:
        raise SystemExit(
            f"FAIL: could not find {var} in {JOB_GO}. If it was renamed or "
            f"reformatted, re-check the Python/Go marker pairing by hand."
        )
    return re.compile(m.group(1))


FLEET_RE = go_pattern("fleetMarkerRe")
HOST_RE = go_pattern("hostMarkerRe")


def check(label: str, line: str, rx: re.Pattern, **want) -> None:
    m = rx.match(line)
    if not m:
        failures.append(f"{label}: Go pattern does not match Python output\n    {line}")
        return
    for group, expected in want.items():
        got = m.group(group)
        if got != expected:
            failures.append(
                f"{label}: group {group!r} = {got!r}, want {expected!r}\n    {line}")


# --- ENCODER-FLEET ----------------------------------------------------------
check("fleet with version",
      fleet_marker("ubuntu", 7.5, 8, "84df69e", ["enc-1", "enc-2"]),
      FLEET_RE, machine="ubuntu", version="84df69e", chunks="enc-1|enc-2")

# The pre-version form: emitted by the cloud path and by farm workers too old to
# report one. It must keep parsing, or the chunk plot loses its per-machine
# colouring on every target at once.
check("fleet without version",
      fleet_marker("mac", 0, 8, "", []),
      FLEET_RE, machine="mac", version=None, chunks="")

# The ordering trap that motivated putting version FIRST: chunks is `[^\]]*`, so
# a trailing field would be swallowed into the chunk list instead of parsed.
_line = fleet_marker("mac", 1, 8, "abc123", ["enc-1"])
if "version" in (FLEET_RE.match(_line).group("chunks") or ""):
    failures.append(f"fleet: version leaked into the chunk list\n    {_line}")

# --- ENCODER-HOST -----------------------------------------------------------
check("host with version",
      host_marker("encode:h264:396p:chunk1", "ubuntu", "84df69e"),
      HOST_RE, key="encode:h264:396p:chunk1", instance="ubuntu", version="84df69e")

check("host without version",
      host_marker("encode:h264:396p:chunk1", "ubuntu"),
      HOST_RE, key="encode:h264:396p:chunk1", instance="ubuntu", version=None)

# A stage key carries colons and dots; an instance can be an EC2 id or a label.
check("host with a realistic cloud key",
      host_marker("encode:hevc:2160p:chunk12", "i-0abc123def456", "84df69e"),
      HOST_RE, instance="i-0abc123def456", version="84df69e")


# --- emission timing --------------------------------------------------------
# The above pins the marker FORMAT. What the first cut got wrong was WHEN the
# marker is emitted, which no format test can catch: a real run tagged 2 of 14
# chunks because a chunk is normally first seen (via last_worker_identity)
# before its worker's first heartbeat lands, so the first emission carried no
# version — and deduping on machine alone then suppressed every retry.
from infinite_streaming_encoder.cli_local_dist import (  # noqa: E402
    should_emit_host,
)

seen: dict = {}

# Poll 1: the chunk is visible, but no heartbeat has arrived, so the box's
# build is not known yet. Emitting is still right — the chunk plot needs the
# colour immediately — it just cannot carry a version.
if not should_emit_host("enc-1", "ubuntu", "", seen):
    failures.append("timing: first sighting did not emit at all")

# Poll 2: nothing new. Must NOT spam.
if should_emit_host("enc-1", "ubuntu", "", seen):
    failures.append("timing: re-emitted with no new information")

# Poll 3: a heartbeat has landed and the box has said what it is running. THIS
# is the emission the original code never made.
if not should_emit_host("enc-1", "ubuntu", "3676141", seen):
    failures.append("timing: learning the build did not re-emit — "
                    "this is the 2-of-14 bug")

# Poll 4: same box, same build. Quiet again.
if should_emit_host("enc-1", "ubuntu", "3676141", seen):
    failures.append("timing: re-emitted after the version was already known")

# Failover to another box re-tags, even at the same version.
if not should_emit_host("enc-1", "mac", "3676141", seen):
    failures.append("timing: failover to another machine did not re-tag")

# An activity with no identity yet is not emittable at all.
if should_emit_host("enc-2", "", "3676141", seen):
    failures.append("timing: emitted for an activity with no machine")

if failures:
    print("test_fleet_version_marker: FAILED")
    for f in failures:
        print("  " + f)
    raise SystemExit(1)
print("test_fleet_version_marker: ok (format + emission timing)")
