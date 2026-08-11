#!/usr/bin/env python3
"""Static check: the state machine definition is one Step Functions will accept.

`check_sfn_scopes.py` validates the ASL against ITSELF and against the job
definitions — that a `$.field` a Map body reads is projected by its ItemSelector,
and that a `Ref::` a job definition expects is supplied by the submitting state.
Both are real and both caught real bugs. Neither asks the only question AWS will
ask at deploy time: **is this document valid ASL?**

#283 was a `Comment` key inside three `ContainerOverrides.Environment` entries.
Step Functions validates a `batch:submitJob` task's Parameters against the Batch
API shape, where an environment entry is `{Name, Value}` and nothing else, so it
rejected the definition:

    ERROR (SCHEMA_VALIDATION_FAILED): The field "Comment" is not supported by
    Step Functions. Did you mean 'Name'?

The template was valid JSON, `tofu validate` passed, `make check` passed, and the
first thing to find out was `make deploy`.

## Why that is worse than a failed deploy

Terraform applies job definitions BEFORE the state machine. The apply got far
enough to bump all seven job definitions `:55 -> :56` — which **deregisters
`:55`** — and then failed on the state machine, leaving it pinned to revisions
that no longer exist. Cloud encoding was broken, nothing rolled it back, and
every retry re-broke it the same way (a revision bump re-renders the ASL, so the
state machine always updates).

So this check is a **pre-deploy gate**, not just a lint. `make deploy` and
`make infra-plan` require it to pass, which is what turns this class of failure
from "half-applied and broken" into "nothing changed".

## How

`aws stepfunctions validate-state-machine-definition` is exactly this question,
asked of the service that will answer it at deploy time. It creates nothing and
costs nothing.

Two things about it are traps:

- **It exits 0 when the definition is INVALID.** The API call succeeded; the
  verdict is in the payload's `result` field. A check written the obvious way
  (`if aws … ; then ok; fi`) passes everything forever — a confident green for a
  thing that was never checked, which is the failure mode this repo keeps
  hitting. Gate on `result`, never on the exit code.
- **It exits 0 on an auth failure too**, printing to stderr and nothing to
  stdout. That is why "no usable `result` in the output" is treated as
  UNAVAILABLE (skip, or refuse under --require) rather than as a pass.

AWS's own docs say not to depend on the exact wording, order or count of the
diagnostics, so only `result` is load-bearing here; the diagnostics are printed
verbatim for the human, with their `location`, so the failure is actionable
without a deploy.

## Rendering

The template is not valid JSON until the `${...}` interpolations are
substituted, so this does the substitution rather than guessing around it. The
placeholder is a fake ARN and its shape is NOT load-bearing: verified against the
real API that a rendering with the literal string "TF" in every interpolated
position also returns OK, so the schema does not resolve or parse these values.
The ARN shape is insurance for a future ASL feature that might.

The variable names come from main.tf's own `templatefile(...)` block, so a
template referencing a variable Terraform does not supply is caught here too —
that is a `tofu` error at plan time, but it is the same class and free to catch.

## Exit codes

0 = valid, 1 = invalid (or the template could not be rendered), 2 = skipped.

Skipped means "could not ask": no `aws` CLI, no credentials, no network. That is
how tofu/ruff/staticcheck already degrade in `make check`, so the pre-push hook
stays fast and offline. Unlike those, **CI is NOT the authority here** — CI has
no AWS credentials, so this check can only ever run on a machine that has them,
which is the machine that deploys. That is the whole reason `--require` exists:
on the deploy path a skip is a refusal, because deploying without being able to
validate is precisely the situation that costs a half-applied stack.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

TEMPLATE = Path("infra/terraform/modules/workflow/definition.json.tpl")
MAIN_TF = Path("infra/terraform/modules/workflow/main.tf")

# The state machine is STANDARD (main.tf); EXPRESS caps at 5 minutes and our
# worst-case encode is far longer. The type changes which ASL features validate,
# so it must match what is deployed.
SM_TYPE = "STANDARD"

# One placeholder for every interpolation. Deliberately unmistakable: nothing
# should ever try to resolve it, and if a diagnostic ever quotes it back the
# reader should immediately see it is not a real ARN.
PLACEHOLDER = "arn:aws:placeholder:us-west-2:000000000000:not-a-real-arn/rendered-by-check-sfn-definition"

OK, INVALID, SKIPPED = 0, 1, 2


def referenced_vars(raw: str) -> set[str]:
    """Every `${name}` the template interpolates."""
    return set(re.findall(r"\$\{(\w+)\}", raw))


def supplied_vars(main_tf: str) -> set[str]:
    """Every name main.tf's `templatefile(...)` block passes to the template.

    Parsed from the `locals { definition = templatefile(...) }` block rather than
    hardcoded, so adding a variable in one file and forgetting the other is
    caught rather than encoded.
    """
    m = re.search(r"templatefile\(\s*[^,]+,\s*\{(.*?)\n\s*\}\s*\)", main_tf, re.S)
    if not m:
        return set()
    return set(re.findall(r"^\s*(\w+)\s*=", m.group(1), re.M))


def render(raw: str) -> str:
    """Substitute every interpolation with the placeholder."""
    return re.sub(r"\$\{\w+\}", PLACEHOLDER, raw)


def validate(definition: str) -> tuple[int, str]:
    """Ask Step Functions whether it would accept this definition.

    Returns (exit code, human-readable detail). SKIPPED covers every "could not
    ask" case — missing CLI, no credentials, no network — which is why this
    reads the payload rather than the process's exit status: the CLI exits 0
    for an auth error exactly as it does for a successful validation.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write(definition)
        path = fh.name
    try:
        proc = subprocess.run(
            ["aws", "stepfunctions", "validate-state-machine-definition",
             "--type", SM_TYPE, "--severity", "WARNING",
             "--definition", f"file://{path}", "--output", "json"],
            capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return SKIPPED, "skipped (aws CLI not installed)"
    except subprocess.TimeoutExpired:
        return SKIPPED, "skipped (aws stepfunctions validate timed out)"
    finally:
        Path(path).unlink(missing_ok=True)

    try:
        body = json.loads(proc.stdout)
        result = body["result"]
    except (json.JSONDecodeError, KeyError, TypeError):
        # No verdict in the payload: an auth, region or network failure, which
        # the CLI reports on stderr while still exiting 0. Emphatically NOT a
        # pass — that is the trap this whole check exists to avoid.
        why = (proc.stderr or proc.stdout or "").strip().splitlines()
        return SKIPPED, "skipped (cannot reach Step Functions: " + (
            why[-1] if why else "no output from the aws CLI") + ")"

    if result == "OK":
        # Diagnostics can be present and still be OK (warnings). Surface them —
        # they are the early notice for a definition that is drifting toward
        # invalid — without failing on them.
        warnings = body.get("diagnostics") or []
        detail = "valid"
        if warnings:
            detail += f" ({len(warnings)} warning(s))\n" + _format(warnings)
        return OK, detail

    return INVALID, ("Step Functions would REJECT this definition:\n"
                     + _format(body.get("diagnostics") or [])
                     + "\n  A deploy with this definition applies the job definitions FIRST"
                       "\n  (deregistering the live revisions) and only then fails on the"
                       "\n  state machine — leaving cloud encoding broken.")


def _format(diagnostics: list) -> str:
    lines = []
    for d in diagnostics:
        loc = d.get("location")
        lines.append(f"  {d.get('severity', '?')} ({d.get('code', '?')}) "
                     f"{d.get('message', '')}" + (f"  at {loc}" if loc else ""))
    return "\n".join(lines) or "  (the API returned FAIL with no diagnostics)"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--require", action="store_true",
                    help="treat 'could not ask' as a failure (the deploy path: "
                         "deploying without being able to validate is the case "
                         "this check exists to prevent)")
    args = ap.parse_args(argv)

    if not TEMPLATE.exists() or not MAIN_TF.exists():
        print(f"{TEMPLATE}: not found (run from the repo root)", file=sys.stderr)
        return INVALID

    raw = TEMPLATE.read_text()
    missing = sorted(referenced_vars(raw) - supplied_vars(MAIN_TF.read_text()))
    if missing:
        print(f"{TEMPLATE}: interpolates variables {MAIN_TF} does not supply: "
              f"{', '.join(missing)}\n"
              f"  templatefile() fails at plan time on an unsupplied variable.",
              file=sys.stderr)
        return INVALID

    rendered = render(raw)
    try:
        json.loads(rendered)
    except json.JSONDecodeError as e:
        # Either the template is malformed, or an interpolation sits in a
        # non-string position (`"n": ${count}`) where a quoted placeholder
        # cannot go. Say both, because the second is not obvious.
        print(f"{TEMPLATE}: not valid JSON once rendered: {e}\n"
              f"  If an interpolation is used unquoted (a number or a bare "
              f"object), this renderer cannot substitute it — give it a typed "
              f"placeholder here.", file=sys.stderr)
        return INVALID

    code, detail = validate(rendered)
    print(detail, file=sys.stderr if code == INVALID else sys.stdout)
    if code == SKIPPED and args.require:
        print("  --require: refusing to continue without a validated definition."
              "\n  A rejected ASL fails the apply AFTER Terraform has already"
              "\n  deregistered the live job definitions.", file=sys.stderr)
        return INVALID
    return code


if __name__ == "__main__":
    sys.exit(main())
