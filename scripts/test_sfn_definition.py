#!/usr/bin/env python3
"""The ASL schema gate (#284) — and above all, that it FAILS when it should.

A validator that silently passes everything is worse than none: it converts "we
have no gate" into "we have a gate and it says we are fine", which is the exact
failure mode #284 exists to close. So most of what is pinned here is the reject
direction, and the two ways this particular check could quietly stop rejecting:

  * `aws stepfunctions validate-state-machine-definition` exits **0** when the
    definition is invalid — the verdict is in the payload, not the exit status.
  * It exits 0 on an auth failure too, printing nothing to stdout.

Read either as success and the gate is decorative. Both are simulated here with
a fake `aws` on PATH, so they are tested with no credentials and no network.

The end-to-end checks — the real template validates, the pre-#283 template does
not — need AWS and skip without it. They create nothing and cost nothing.
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

os.chdir(REPO)
import check_sfn_definition as chk  # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f" {detail}"))
    if not cond:
        FAILURES.append(name)


def fake_aws(stdout: str, stderr: str = "", exit_code: int = 0) -> str:
    """A directory holding an `aws` that prints what we tell it and exits 0.

    Exit 0 is the point: the real CLI does that for an INVALID definition and
    for an auth failure alike.
    """
    d = tempfile.mkdtemp()
    p = Path(d) / "aws"
    p.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        cat <<'STDOUT_EOF'
        {stdout}
        STDOUT_EOF
        cat >&2 <<'STDERR_EOF'
        {stderr}
        STDERR_EOF
        exit {exit_code}
        """))
    p.chmod(0o755)
    return d


def with_fake_aws(dirpath, fn):
    old = os.environ["PATH"]
    os.environ["PATH"] = dirpath + os.pathsep + old
    try:
        return fn()
    finally:
        os.environ["PATH"] = old


print("ASL schema gate")

# --- rendering ------------------------------------------------------------
raw = chk.TEMPLATE.read_text()
rendered = chk.render(raw)
check("the rendered template has no interpolations left", "${" not in rendered)
try:
    doc = json.loads(rendered)
    check("the rendered template is valid JSON", True)
except json.JSONDecodeError as e:
    doc = None
    check("the rendered template is valid JSON", False, f"({e})")

# main.tf and the template must agree on the variable set. templatefile() fails
# at plan time on an unsupplied one, so catching it here is free.
supplied = chk.supplied_vars(chk.MAIN_TF.read_text())
referenced = chk.referenced_vars(raw)
check("main.tf's templatefile() block is parseable", bool(supplied),
      "(the locals block regex found nothing — main.tf's shape changed)")
check("every interpolation the template uses is supplied by main.tf",
      not (referenced - supplied), f"(missing: {sorted(referenced - supplied)})")

# --- the two ways this gate could go quietly blind -------------------------
FAIL_BODY = json.dumps({
    "result": "FAIL",
    "diagnostics": [{"severity": "ERROR", "code": "SCHEMA_VALIDATION_FAILED",
                     "message": 'The field "Comment" is not supported by Step Functions.',
                     "location": "/States/Mezzanine/Parameters"}],
})
code, detail = with_fake_aws(fake_aws(FAIL_BODY), lambda: chk.validate("{}"))
check("result=FAIL is a failure even though the CLI exits 0", code == chk.INVALID,
      f"(got exit code {code} — the gate is reading the exit status, not the verdict)")
check("the rejection names the offending field", "Comment" in detail, f"({detail!r})")
check("the rejection names its location", "/States/Mezzanine/Parameters" in detail,
      f"({detail!r})")

AUTH_ERR = "aws: [ERROR]: An error occurred (UnrecognizedClientException) ..."
code, detail = with_fake_aws(fake_aws("", stderr=AUTH_ERR), lambda: chk.validate("{}"))
check("an auth failure is SKIPPED, not OK", code == chk.SKIPPED,
      f"(got exit code {code} — no credentials would read as a valid definition)")
check("the skip says why", "UnrecognizedClientException" in detail, f"({detail!r})")

OK_BODY = json.dumps({"result": "OK", "diagnostics": []})
code, _ = with_fake_aws(fake_aws(OK_BODY), lambda: chk.validate("{}"))
check("result=OK is a pass", code == chk.OK, f"(got exit code {code})")

# A warning must not fail the check — result is the only load-bearing field, and
# AWS explicitly reserves the right to add diagnostics.
WARN_BODY = json.dumps({"result": "OK", "diagnostics": [
    {"severity": "WARNING", "code": "NO_DOLLAR", "message": "something advisory"}]})
code, detail = with_fake_aws(fake_aws(WARN_BODY), lambda: chk.validate("{}"))
check("a WARNING diagnostic does not fail the check", code == chk.OK, f"(got {code})")
check("the warning is still shown", "something advisory" in detail, f"({detail!r})")

# --- --require turns a skip into a refusal --------------------------------
def _main(argv):
    """main() with its own reporting muffled — we are asserting on its code."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        return chk.main(argv)


code = with_fake_aws(fake_aws("", stderr=AUTH_ERR), lambda: _main([]))
check("without --require, an unavailable API skips", code == chk.SKIPPED,
      f"(got {code})")
code = with_fake_aws(fake_aws("", stderr=AUTH_ERR), lambda: _main(["--require"]))
check("with --require, an unavailable API is a refusal", code == chk.INVALID,
      f"(got {code} — the deploy path would proceed unvalidated)")

# --- a missing `aws` is a skip, not a crash -------------------------------
empty = tempfile.mkdtemp()
old_path = os.environ["PATH"]
os.environ["PATH"] = empty
try:
    code, detail = chk.validate("{}")
finally:
    os.environ["PATH"] = old_path
check("a missing aws CLI skips cleanly", code == chk.SKIPPED, f"(got {code})")
check("the skip names the missing CLI", "aws" in detail, f"({detail!r})")

# --- the real API: the acceptance criterion -------------------------------
# `make check` fails on the pre-#283 template and passes on the fixed one.
have_aws = shutil.which("aws") is not None
creds = have_aws and subprocess.run(
    ["aws", "sts", "get-caller-identity"], capture_output=True).returncode == 0

if not creds:
    print("  skip  end-to-end validation (no aws CLI or no credentials)")
elif doc is None:
    print("  skip  end-to-end validation (the template did not render)")
else:
    code, detail = chk.validate(rendered)
    check("the CURRENT template validates against the real API", code == chk.OK,
          f"({detail})")

    # Re-inject #283 verbatim: a `Comment` key inside a Batch Environment entry.
    # Step Functions validates those against the Batch API shape, where an entry
    # is {Name, Value} and nothing else.
    broken = copy.deepcopy(doc)
    env = broken["States"]["Mezzanine"]["Parameters"]["ContainerOverrides"]["Environment"]
    env[0]["Comment"] = "explaining why this variable exists"
    code, detail = chk.validate(json.dumps(broken))
    check("the PRE-#283 template is rejected by the real API", code == chk.INVALID,
          "(the gate would not have caught the bug it was built for)")
    check("the real rejection is actionable", "Comment" in detail, f"({detail})")

print()
if FAILURES:
    print(f"FAILED: {len(FAILURES)} check(s): {', '.join(FAILURES)}")
    sys.exit(1)
print("all checks passed")
