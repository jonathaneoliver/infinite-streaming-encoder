#!/usr/bin/env python3
"""The local-dist mezzanine lock spans PROCESSES, which is the whole point.

#291: two jobs on one source both miss the cache, both run ffmpeg on the host,
and both upload a source-sized file to the same prefix. Nothing is corrupted —
the stream copy is deterministic and PutObject is atomic — but one local encode
and ~2.3 GB of upload are wasted per submission, which is the round trip #266
removed.

The Go fix is an in-process mutex and is right for cloud-batch, where one server
builds every mezzanine. It cannot reach local-dist: each job runs in its own
orchestrator CONTAINER, so two jobs on one source are two processes. Hence flock
on a file in the shared $TMP_DIR.

So this test uses real subprocesses. An in-process lock passes a threaded test
and fails here, which is exactly the distinction worth pinning.
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" {detail}"))
    if not cond:
        FAILURES.append(name)


WORKER = textwrap.dedent("""
    import os, sys, time
    sys.path.insert(0, "__ROOT__")
    from infinite_streaming_encoder.cli_local_dist import _mezz_build_lock
    key, marker, hold = sys.argv[1], sys.argv[2], float(sys.argv[3])
    with _mezz_build_lock(key):
        # Append on entry and exit; interleaved pairs mean the lock did not hold.
        with open(marker, "a") as f:
            f.write(f"IN {os.getpid()}\\n"); f.flush()
        time.sleep(hold)
        with open(marker, "a") as f:
            f.write(f"OUT {os.getpid()}\\n"); f.flush()
""")


def run_workers(tmp, key, n, hold=0.4):
    marker = Path(tmp) / "order.txt"
    env = dict(os.environ, TMP_DIR=str(tmp))
    src = WORKER.replace("__ROOT__", str(ROOT))
    procs = [subprocess.Popen([sys.executable, "-c", src, key, str(marker), str(hold)],
                              env=env) for _ in range(n)]
    for p in procs:
        p.wait(timeout=60)
    return [l.split()[0] for l in marker.read_text().splitlines()] if marker.exists() else []


print("mezzanine build lock, across processes")

# --- the property: never two holders of one key ---------------------------
with tempfile.TemporaryDirectory() as tmp:
    seq = run_workers(tmp, "mezz-cache/abc123", n=3)
    # Serialised runs read IN,OUT,IN,OUT,IN,OUT. Any IN,IN is an overlap.
    overlapped = any(a == "IN" and b == "IN" for a, b in zip(seq, seq[1:]))
    check("three processes on one key never overlap", not overlapped,
          f"(sequence: {','.join(seq)})")
    check("all three ran", seq.count("IN") == 3, f"(got {seq.count('IN')})")

# --- distinct keys must NOT serialise -------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    env = dict(os.environ, TMP_DIR=str(tmp))
    src = WORKER.replace("__ROOT__", str(ROOT))
    marker = Path(tmp) / "order.txt"
    t0 = time.monotonic()
    procs = [subprocess.Popen([sys.executable, "-c", src, f"mezz-cache/k{i}",
                               str(marker), "0.4"], env=env) for i in range(3)]
    for p in procs:
        p.wait(timeout=60)
    elapsed = time.monotonic() - t0
    # Serialised would be >=1.2s; concurrent well under. Generous bound for CI.
    check("three distinct keys run concurrently", elapsed < 1.0,
          f"(took {elapsed:.2f}s; serialised would be >=1.2s)")

# --- a key with slashes must not escape the lock dir ----------------------
with tempfile.TemporaryDirectory() as tmp:
    run_workers(tmp, "mezz-cache/deep/nested/key", n=1, hold=0.05)
    stray = list(Path(tmp).rglob("*.lock"))
    check("the lock file stays flat in TMP_DIR",
          len(stray) == 1 and stray[0].parent == Path(tmp),
          f"(found {[str(s) for s in stray]})")

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
    sys.exit(1)
print("all checks passed")
