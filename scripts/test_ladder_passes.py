#!/usr/bin/env python3
"""The two-pass default is written down TWICE. This asserts the copies agree.

`LadderDef.passesFor` (internal/encode/ladder_store.go) and `ladder_passes`
(ladder.py) are mirrors of one rule. Which one decides depends on the target:

  - cloud-batch  -> Go resolves the ladder and bakes the pass count into the
                    Batch job's environment
  - local-dist   -> cli_local_dist resolves the ladder IN PYTHON, so Go's value
                    never reaches the encode

So changing one alone silently changes behaviour on one target and not the
other. That is not hypothetical: h264 was moved from one pass to two on the Go
side only, and four full-length local encodes ran single-pass while their output
tags said "2p". Nothing errored. The delivered bitrates just stayed at the
single-pass level, which looks exactly like "two-pass did not help much" — the
wrong conclusion, reached from real data.

Read from source rather than by importing Go: this has to fail when someone
edits one file, which is precisely when no build step is guaranteed to run.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GO = ROOT / "internal/encode/ladder_store.go"
PY = ROOT / "scripts/infinite_streaming_encoder/ladder.py"

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" {detail}"))
    if not cond:
        FAILURES.append(name)


print("two-pass default, Go vs Python")

# Go: the fallback inside passesFor, after the explicit-pin branch.
go_src = GO.read_text()
m = re.search(r"func \(d LadderDef\) passesFor\(codec string\) int \{(.*?)\n\}",
              go_src, re.S)
check("passesFor found in ladder_store.go", m is not None)
go_default = None
if m:
    body = m.group(1)
    # The trailing `return N` — the value used when the ladder pins nothing.
    rets = re.findall(r"return (\d+)", body)
    check("passesFor has a fallback return", bool(rets), f"(body: {body.strip()[:80]})")
    if rets:
        go_default = int(rets[-1])
        # A per-codec special case would mean the two files can disagree per
        # codec, which this test could not see. Fail loudly if one reappears.
        check("passesFor has no per-codec branch",
              'codec ==' not in body,
              "(a per-codec default needs this test extended per codec)")

py_src = PY.read_text()
pm = re.search(r"^_DEFAULT_PASSES = (\d+)", py_src, re.M)
check("_DEFAULT_PASSES found in ladder.py", pm is not None)
py_default = int(pm.group(1)) if pm else None

check(f"they agree (go={go_default}, py={py_default})",
      go_default is not None and go_default == py_default,
      "-- change BOTH or local-dist and cloud silently diverge")

# An explicit ladder pin must still win on the Python side; that path is what
# made the incomplete change survivable and is worth keeping honest.
sys.path.insert(0, str(ROOT / "scripts"))
from infinite_streaming_encoder.ladder import ladder_passes  # noqa: E402

check("an explicit pin overrides the default",
      ladder_passes({"passes": {"h264": 1}}, "h264") == 1)
check("an unset codec falls back to the default",
      ladder_passes({"passes": {"hevc": 1}}, "h264") == py_default)
check("no passes map at all falls back to the default",
      ladder_passes({}, "h264") == py_default)
check("a zero/garbage pin is ignored rather than honoured",
      ladder_passes({"passes": {"h264": 0}}, "h264") == py_default)

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
    sys.exit(1)
print("all checks passed")
