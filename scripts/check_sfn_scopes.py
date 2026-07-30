#!/usr/bin/env python3
"""Static check: every `$.field` a Step Functions Map state references must be
present in that Map's ItemSelector.

Step Functions resolves `"Value.$": "$.est_vmaf"` against the CURRENT state's
input, and inside a Map that input is whatever the ItemSelector built — not the
enclosing scope. Reference a field the selector does not project and the
execution dies at runtime with:

    States.Runtime: The JSONPath '$.est_vmaf' specified for the field 'Value.$'
    could not be found in the input '{...}'

Nothing catches that earlier: the template is valid JSON, `tofu validate` passes,
and the apply succeeds. It surfaces only when a real encode reaches the state.
Which is exactly how #157 shipped broken — the workflow has TWO nested Maps
(variants, then chunks within a variant), the field was added to the outer
selector and both Environment blocks, and the inner chunk selector was missed.
Whole-variant encodes would have worked; every chunked encode failed.

Run over infra/terraform/modules/workflow/definition.json.tpl. Terraform
interpolations (${...}) are stubbed before parsing, since they are not JSON.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TEMPLATE = Path("infra/terraform/modules/workflow/definition.json.tpl")

# A whole-string JSONPath into the current scope: "$.field". Deliberately NOT
# matching "$$.Map.Item.Value" (context object, always available) or longer paths
# like "$.a.b" — a nested lookup means the field is a struct and projecting the
# root is enough.
_SCOPE_REF = re.compile(r"\$\.([A-Za-z_][A-Za-z0-9_]*)$")


def _refs(node: object, out: set[str]) -> set[str]:
    """Every `$.field` that resolves against THIS state's scope, beneath node.

    Stops at a nested Map's body. A Map rebinds the scope for everything inside
    its ItemProcessor, so `$.chunk_start` in an inner body is a reference to the
    INNER item, not to the enclosing one — counting it here would demand that the
    outer ItemSelector project fields that only ever exist in the inner scope.
    The nested Map is still checked, on its own, by _walk.

    A nested Map's ItemsPath and ItemSelector are the exception: those are
    evaluated BEFORE the rebind, i.e. in this scope, so they are still walked.
    """
    if isinstance(node, dict):
        if node.get("Type") == "Map":
            # Evaluated in the enclosing scope — keep walking these two.
            for key in ("ItemsPath", "ItemSelector"):
                if key in node:
                    _refs(node[key], out)
            # Everything else about the nested Map belongs to the inner scope.
            return out
        for value in node.values():
            if isinstance(value, str):
                m = _SCOPE_REF.fullmatch(value)
                if m:
                    out.add(m.group(1))
            else:
                _refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _refs(value, out)
    return out


def _selector_fields(state: dict) -> set[str]:
    """The field names a Map's ItemSelector projects into its item scope."""
    sel = state.get("ItemSelector") or {}
    return {k[:-2] if k.endswith(".$") else k for k in sel}


def _walk(states: dict | None, path: str) -> list[tuple[str, list[str]]]:
    """Report (map_path, missing_fields) for every Map whose body references a
    field its own ItemSelector does not project.

    Descends through EVERY state container, not just Map bodies: this workflow
    nests its Maps inside a Parallel, and an earlier version of this check that
    only recursed into Map bodies found zero Maps and passed vacuously.
    """
    problems: list[tuple[str, list[str]]] = []
    for name, state in (states or {}).items():
        if not isinstance(state, dict):
            continue
        here = f"{path}/{name}"

        if state.get("Type") == "Map":
            projected = _selector_fields(state)
            processor = state.get("ItemProcessor") or state.get("Iterator") or {}
            # Only the Map's BODY resolves against the item scope. ItemsPath and
            # ItemSelector resolve against the ENCLOSING scope, so they are
            # excluded — including them would flag every correct selector.
            missing = sorted(_refs(processor, set()) - projected)
            if missing:
                problems.append((here, missing))

        # Recurse into any nested states, whatever the parent type.
        for container in ("ItemProcessor", "Iterator"):
            sub = state.get(container) or {}
            problems += _walk(sub.get("States"), here)
        for i, branch in enumerate(state.get("Branches") or []):
            problems += _walk((branch or {}).get("States"), f"{here}[{i}]")
    return problems


# Go structs whose JSON tags must satisfy the template's `$$.Map.Item.Value.<f>`
# lookups, and the Map (by state name) that iterates each.
_ITEM_STRUCTS = {
    "Variants": ("internal/encode/ladder.go", "sfnVariant"),
    "EncodeChunks": ("internal/encode/chunkplan.go", "chunkSpan"),
}
_GO_TAG = re.compile(r'json:"([A-Za-z0-9_]+)')


def _go_json_keys(path: Path, struct: str) -> set[str] | None:
    """JSON tag names declared on a Go struct, or None if it can't be read."""
    try:
        src = path.read_text()
    except OSError:
        return None
    m = re.search(rf"type {re.escape(struct)} struct \{{(.*?)\n\}}", src, re.S)
    return set(_GO_TAG.findall(m.group(1))) if m else None


def _item_refs(node: object, out: set[str]) -> set[str]:
    """Every `$$.Map.Item.Value.<field>` beneath node, not crossing a nested Map
    (whose item is a different type — see _refs)."""
    if isinstance(node, dict):
        if node.get("Type") == "Map":
            return out
        for value in node.values():
            if isinstance(value, str):
                m = re.fullmatch(r"\$\$\.Map\.Item\.Value\.([A-Za-z0-9_]+)", value)
                if m:
                    out.add(m.group(1))
            else:
                _item_refs(value, out)
    elif isinstance(node, list):
        for value in node:
            _item_refs(value, out)
    return out


def _check_item_shape(doc: dict) -> list[str]:
    """Every field the template reads off a Map ITEM must exist on the Go struct
    that produces that item.

    The scope check above proves a field is projected into scope; it cannot
    prove the control plane ever emits it. Rename a Go json tag (or add a
    template lookup for a field that was never marshalled) and the execution
    dies with the same States.Runtime failure #157 caused — at runtime, on every
    cloud encode, because nothing earlier looks at both sides at once.
    """
    problems: list[str] = []
    found: dict[str, dict] = {}

    def collect(states: dict | None) -> None:
        for name, st in (states or {}).items():
            if not isinstance(st, dict):
                continue
            if st.get("Type") == "Map" and name in _ITEM_STRUCTS:
                found[name] = st
            for container in ("ItemProcessor", "Iterator"):
                collect((st.get(container) or {}).get("States"))
            for branch in st.get("Branches") or []:
                collect((branch or {}).get("States"))

    collect(doc.get("States"))
    for state, (path, struct) in _ITEM_STRUCTS.items():
        if state not in found:
            problems.append(f"  {state}: Map not found — did the state get renamed? "
                            f"(this check silently covers nothing until fixed)")
            continue
        keys = _go_json_keys(Path(path), struct)
        if keys is None:
            problems.append(f"  {state}: could not read {struct} in {path}")
            continue
        # The Map's own ItemSelector/ItemsPath read the ENCLOSING scope.
        body = {k: v for k, v in found[state].items()
                if k not in ("ItemSelector", "ItemsPath")}
        missing = sorted(_item_refs(body, set()) | _item_refs(
            found[state].get("ItemSelector") or {}, set()) - keys)
        missing = [f for f in missing if f not in keys]
        if missing:
            problems.append(f"  {state}: reads {', '.join(missing)} off each item, "
                            f"but {struct} ({path}) does not marshal it")
    return problems


JOBS_TF = Path("infra/terraform/modules/jobs/main.tf")
_REF = re.compile(r'"Ref::([A-Za-z0-9_]+)"')
# Job definition resource name -> the ${...} the state machine interpolates for it.
_JOB_DEFS = {
    "variant": "variant_def",
    "mezzanine": "mezzanine_def",
    "audio": "audio_def",
    "package_all": "package_all_def",
}


def _jobdef_refs() -> dict[str, set[str]]:
    """Ref:: parameter names each job definition's command requires."""
    try:
        src = JOBS_TF.read_text()
    except OSError:
        return {}
    out: dict[str, set[str]] = {}
    for res, tf_var in _JOB_DEFS.items():
        m = re.search(rf'resource "aws_batch_job_definition" "{res}".*?\n\}}\n',
                      src, re.S)
        if m:
            out[tf_var] = set(_REF.findall(m.group(0)))
    return out


def _check_jobdef_params(raw: str) -> list[str]:
    """Every Ref:: a job definition's command uses must be supplied by EVERY
    state that submits to it.

    Batch substitutes Ref:: against the submitting call's `parameters` map, so a
    job definition shared by two states needs BOTH to supply every reference.
    Adding one to the command and to only one caller fails at submit time with
    "Unable to substitute value. No parameter found for reference X" — which is
    what shipping --chunk-start without updating EncodeWhole did: the chunked
    path worked and every whole-variant encode died.

    Parsed off the raw template (not the JSON-stubbed copy) because the
    JobDefinition value is a Terraform interpolation naming which job def it is.
    """
    required = _jobdef_refs()
    if not required:
        return [f"  could not read job definitions from {JOBS_TF}"]

    problems: list[str] = []
    # Each submitJob state: the ${job_def} it targets + the parameter names it
    # supplies. Matched textually so the ${...} survives.
    for block in re.finditer(
            r'"Resource":\s*"arn:aws:states:::batch:submitJob\.sync".*?'
            r'"JobDefinition":\s*"\$\{(\w+)\}".*?'
            r'"Parameters":\s*\{(.*?)\n(\s*)\},',
            raw, re.S):
        tf_var, params_body = block.group(1), block.group(2)
        supplied = {k.rstrip(".$") for k in re.findall(r'"([A-Za-z0-9_]+)(?:\.\$)?"\s*:', params_body)}
        missing = sorted(required.get(tf_var, set()) - supplied)
        if missing:
            problems.append(f"  a state submitting to ${{{tf_var}}} does not supply: "
                            f"{', '.join(missing)}")
    return problems


def main() -> int:
    if not TEMPLATE.exists():
        print(f"{TEMPLATE}: not found (run from the repo root)", file=sys.stderr)
        return 1
    raw = TEMPLATE.read_text()
    # ${job_queue_arn} etc. are Terraform interpolations, not JSON. Stub them to
    # a literal so the document parses; their values are irrelevant here.
    doc = json.loads(re.sub(r"\$\{[^}]*\}", "TF", raw))

    jd = _check_jobdef_params(raw)
    if jd:
        print(f"{TEMPLATE}: Batch Ref:: parameters a submitting state does not supply:",
              file=sys.stderr)
        for line in jd:
            print(line, file=sys.stderr)
        print("  Batch resolves Ref:: against the SUBMITTING call's parameters, so "
              "every state sharing a job definition must supply all of them.",
              file=sys.stderr)
        return 1

    shape = _check_item_shape(doc)
    if shape:
        print(f"{TEMPLATE}: Map item fields the Go control plane does not emit:",
              file=sys.stderr)
        for line in shape:
            print(line, file=sys.stderr)
        return 1

    problems = _walk(doc.get("States"), "")
    if not problems:
        return 0
    print(f"{TEMPLATE}: Map body references fields its ItemSelector does not project:",
          file=sys.stderr)
    for map_path, missing in problems:
        print(f"  {map_path}: {', '.join(missing)}", file=sys.stderr)
    print("  Add them to that Map's ItemSelector, or the execution fails at "
          "runtime with States.Runtime.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
