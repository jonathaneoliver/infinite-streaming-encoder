#!/usr/bin/env python3
"""How many cores did a finished run actually use? (#183)

Answers "is the declared thread budget being honoured" from DURABLE RECORDS —
an output's run.json plus its job log — rather than by re-running an encode.
That distinction is the point: #190's lag instrument was never committed, so
its measurement could not be repeated and the question it answered had to be
reconstructed from an issue comment months later.

The two numbers that matter:

  threads per encode = cpu_s / encode_s      what the encoder REALLY used
  mean cores busy    = sum(cpu_s) / phase_wall   what the box carried

Compare the second against the fleet's physical-core budget. `_default_slots`
sizes local concurrency as physical/2 assuming ENCODE_THREADS=2 per encode, so
a mean-cores-busy far above the perf-core count means the work spilled onto SMT
regardless of what the setting says.

Usage:
    python3 scripts/analyze_thread_load.py <run.json> <job.log>

Both are fetchable from a running server:
    curl -s localhost:8080/api/outputs/<name>/run  -o run.json
    curl -s localhost:8080/logs/<job_id>.log       -o job.log
"""
import json
import re
import sys
from datetime import datetime

# cpu_s is getrusage over the whole ffmpeg process, so it counts every thread
# the encoder spawned — which is exactly the quantity a self-sizing pool hides.
TIMING = re.compile(
    r"ENCODER-TIMING phase=variant key=(\S+).*?encode_s=([0-9.]+).*?cpu_s=([0-9.]+)")


def _epoch(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def analyze(run_path, log_path):
    run = json.load(open(run_path))

    # Encode-phase wall, from the stage spans. NOTE these spans include time an
    # activity spent QUEUED, so they are usable for the phase envelope but must
    # not be read as per-chunk execution time — that is what encode_s is for.
    bounds = []
    for s in run.get("stages") or []:
        if s.get("key", "").startswith("encode:") and s.get("started_at") and s.get("ended_at"):
            bounds.append((_epoch(s["started_at"]), _epoch(s["ended_at"])))
    if not bounds:
        sys.exit("no completed encode stages in this run record")
    phase_wall = max(b for _, b in bounds) - min(a for a, _ in bounds)

    timings = {}
    for line in open(log_path):
        m = TIMING.search(line)
        if m:
            timings[m.group(1)] = (float(m.group(2)), float(m.group(3)))
    if not timings:
        sys.exit("no ENCODER-TIMING lines with cpu_s — log predates #196?")

    sum_enc = sum(e for e, _ in timings.values())
    sum_cpu = sum(c for _, c in timings.values())
    threads = sorted(c / e for e, c in timings.values() if e > 0)

    def pct(p):
        return threads[min(len(threads) - 1, int(p * len(threads)))]

    print(f"run        : {run.get('job_id')}  {run.get('target')} / {run.get('codec')}")
    print(f"chunks     : {len(timings)}")
    print(f"phase wall : {phase_wall:.0f}s   (encode stages, first start -> last end)")
    print()
    print(f"threads per encode : p10 {pct(0.1):.2f}   median {pct(0.5):.2f}   "
          f"p90 {pct(0.9):.2f}")
    print(f"mean concurrent    : {sum_enc / phase_wall:.2f} encodes")
    print(f"MEAN CORES BUSY    : {sum_cpu / phase_wall:.2f}")
    print()
    print("Compare MEAN CORES BUSY against the fleet's PHYSICAL core budget")
    print("(encode.defaultLocalPerfCores documents 16 for the full farm).")
    print("Above it, the run was on SMT/E-cores whatever ENCODE_THREADS says.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    analyze(sys.argv[1], sys.argv[2])
