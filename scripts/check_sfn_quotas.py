#!/usr/bin/env python3
"""Check the hardcoded Step Functions limits against what AWS actually publishes.

`internal/encode/chunkbudget.go` hardcodes two AWS quotas:

    sfnHistoryLimit    = 25000    events per execution
    sfnInputLimitBytes = 262144   bytes of execution input

The whole chunk-budget mechanism is built on them: #316 rations the dynamic
chunk target against the first, and #312's guard refuses a fixed chunk duration
against both. If AWS moves either number, nothing in the repo notices.

Both are in the Service Quotas API under stable quota codes, so this asks:

    L-CE44C76B  Execution history size
    L-8FEC45E4  Input or result data size in task state or execution

## Which direction matters

Being wrong is asymmetric, and neither direction is loud.

  * AWS RAISES a limit (the plausible one) and we keep rationing against the old
    value — chunk counts held below what the platform allows, costing
    parallelism on every long run, forever, with nothing to indicate it.
  * AWS LOWERS one (essentially unheard of for a hard quota) and we submit runs
    that fail mid-execution.

## Degrades OPEN, unlike require-valid-sfn

A skip here means "could not ask", and the honest response is to carry on: the
constants are conservative and a run planned against them is valid whatever the
real quota is. That is the opposite of `check_sfn_definition.py --require`,
which refuses to let a deploy proceed unattested — there, proceeding blind costs
a half-applied stack.

Exit codes: 0 match (or skipped), 1 mismatch.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

REGION_DEFAULT = "us-west-2"
SOURCE = Path(__file__).resolve().parent.parent / "internal" / "encode" / "chunkbudget.go"

# quota code -> (human name, Go constant name)
QUOTAS = {
    "L-CE44C76B": ("Execution history size", "sfnHistoryLimit"),
    "L-8FEC45E4": ("Input or result data size", "sfnInputLimitBytes"),
}


def go_constants():
    """Read the constants out of the Go source rather than restating them here.

    A second copy in this file would be one more thing to keep in step, and the
    failure would be this check passing while the planner used a different
    number.
    """
    src = SOURCE.read_text()
    out = {}
    for const in ("sfnHistoryLimit", "sfnInputLimitBytes"):
        m = re.search(rf"^\s*{const}\s*=\s*(\d+)", src, re.M)
        if m:
            out[const] = int(m.group(1))
    return out


def aws_quota(code, region):
    """Live value for one quota code, or None if we could not ask."""
    try:
        p = subprocess.run(
            ["aws", "service-quotas", "get-service-quota",
             "--service-code", "states", "--quota-code", code,
             "--region", region, "--output", "json"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0 or not p.stdout.strip():
        return None
    try:
        return int(json.loads(p.stdout)["Quota"]["Value"])
    except (ValueError, KeyError, TypeError):
        return None


def main():
    region = REGION_DEFAULT
    for i, a in enumerate(sys.argv):
        if a == "--region" and i + 1 < len(sys.argv):
            region = sys.argv[i + 1]

    consts = go_constants()
    if not consts:
        print(f"could not read constants from {SOURCE} — skipped")
        return 0

    checked, mismatched = 0, []
    for code, (name, const) in QUOTAS.items():
        want = consts.get(const)
        if want is None:
            continue
        got = aws_quota(code, region)
        if got is None:
            continue  # no credentials, no network, or the code moved
        checked += 1
        if got != want:
            mismatched.append(
                f"{name} ({code}): AWS says {got:,}, {const} says {want:,}")

    if checked == 0:
        # No credentials is the normal case in CI. Say skipped, never passed.
        print("skipped (no AWS credentials, or Service Quotas unreachable)")
        return 0
    if mismatched:
        print(f"AWS quotas no longer match {SOURCE.name}:")
        for m in mismatched:
            print(f"  {m}")
        print("  The chunk budget (#316) and the fixed-duration guard (#312) are")
        print("  both derived from these. A raised limit means we are rationing")
        print("  chunk counts below what the platform allows.")
        return 1
    print(f"ok ({checked} quota(s) match)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
