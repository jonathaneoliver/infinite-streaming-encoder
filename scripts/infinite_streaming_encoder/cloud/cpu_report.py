"""Per-tier CPU-utilization report for a cloud-batch execution.

Every encode chunk job reports the ffmpeg CPU-seconds it consumed (cpu_s)
next to its encode wall-time (encode_s) in the [[ENCODER-TIMING]] marker.
Because jobs are bin-packed onto shared instances, instance-level CloudWatch
CPU can't isolate a tier — but this per-job number can. Grouped by tier and
compared against the vCPU each tier reserves, it answers: how much of the
vCPU you pay for is actually crunching video?

  effective_cores = cpu_s / encode_s          (avg cores busy during encode)
  utilization     = effective_cores / reserved_vcpu

A tier well under 100% is over-reserved — it's holding vCPU (hard-reserved by
Batch, so unusable by other jobs) that ffmpeg never uses. Drop that tier's
allocation in variantResources (internal/encode/job.go) to pack more per box.

  python3 -m infinite_streaming_encoder.cloud.cpu_report --execution-arn <arn> [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict

from botocore.exceptions import ClientError

from infinite_streaming_encoder.cloud.aws import region
from infinite_streaming_encoder.cloud.timing import (
    _container_timing,
    _exec_name,
    _jobs_for_execution,
)

# Mirrors variantResources() in internal/encode/job.go — the vCPU each tier's
# chunk job reserves. Keep in sync if that table changes.
_TIER_VCPU = {
    "360p": 2, "540p": 2,
    "720p": 4, "1080p": 4,
    "1440p": 4, "2160p": 4,
}
# Batch job name: var-<codec>-<tier>-c<chunk>-<exec>
_JOBNAME_RE = re.compile(r"^var-([^-]+)-([^-]+)-c(\d+)-")


def collect(execution_arn: str) -> dict:
    jobs = _jobs_for_execution(_exec_name(execution_arn))
    by_tier: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for j in jobs:
        m = _JOBNAME_RE.match(j.get("name", ""))
        if not m:
            continue
        tier = m.group(2)
        t = _container_timing(j.get("log_stream"))
        cpu, enc = t.get("cpu"), t.get("encode")
        if cpu is None or not enc:
            continue
        by_tier[tier].append((cpu, enc))

    rows = []
    for tier, samples in sorted(by_tier.items(),
                                key=lambda kv: _TIER_VCPU.get(kv[0], 99)):
        n = len(samples)
        avg_cpu = sum(c for c, _ in samples) / n
        avg_enc = sum(e for _, e in samples) / n
        vcpu = _TIER_VCPU.get(tier)
        eff = avg_cpu / avg_enc if avg_enc else 0.0
        util = (eff / vcpu) if vcpu else None
        rows.append({
            "tier": tier,
            "chunks": n,
            "reserved_vcpu": vcpu,
            "avg_encode_s": round(avg_enc, 1),
            "avg_cpu_s": round(avg_cpu, 1),
            "effective_cores": round(eff, 2),
            "utilization_pct": round(util * 100) if util is not None else None,
        })
    return {"region": region(), "tiers": rows}


def _print_human(d: dict) -> None:
    rows = d["tiers"]
    if not rows:
        print("no per-tier cpu data found — was this run on an image with "
              "cpu_s instrumentation? (encode jobs must emit cpu_s)")
        return
    print(f"Per-tier encode CPU utilization ({d['region']})\n")
    hdr = (f"{'tier':<7} {'chunks':>6} {'vCPU':>5} {'encode_s':>9} "
           f"{'cpu_s':>8} {'eff.cores':>10} {'util':>6}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        util = f"{r['utilization_pct']}%" if r['utilization_pct'] is not None else "-"
        print(f"{r['tier']:<7} {r['chunks']:>6} {r['reserved_vcpu'] or '-':>5} "
              f"{r['avg_encode_s']:>9} {r['avg_cpu_s']:>8} "
              f"{r['effective_cores']:>10} {util:>6}")
    print("\neff.cores = cpu_s / encode_s (avg cores busy during the encode)")
    print("util      = eff.cores / reserved vCPU")
    low = [r for r in rows if r["utilization_pct"] is not None and r["utilization_pct"] < 60]
    if low:
        tiers = ", ".join(f"{r['tier']} (~{r['utilization_pct']}%)" for r in low)
        print(f"\nOver-reserved (<60%): {tiers}")
        print("  -> these hold vCPU ffmpeg doesn't use. Lower their allocation "
              "in variantResources() to pack more chunks per instance.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="infinite_streaming_encoder.cloud.cpu_report")
    p.add_argument("--execution-arn", required=True, dest="execution_arn")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    try:
        data = collect(args.execution_arn)
    except ClientError as e:
        print(json.dumps({"error": str(e)}) if args.json else f"error: {e}",
              file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        _print_human(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
